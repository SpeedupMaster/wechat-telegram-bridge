"""Tests for pairing, Telegram auth, WeChat QR/device login, and auto-relogin."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from telethon import errors, types

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from tests.fakes import ACC, FakeBot, FakeUser, LoginDaemon, _CallbackEvent


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="t"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(), Path(self.tmp.name) / "config.toml")
        self.b = Bridge(cfg, user_client=None, bot_client=FakeBot())
        self.daemon = LoginDaemon()
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler))
        self.b.wx._poll_initial_delay = 0.0
        self.b.bot.deleted = []

        async def delete_messages(chat, ids):
            self.b.bot.deleted.extend(ids)
        self.b.bot.delete_messages = delete_messages
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.login_poll_interval = 0

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def test_wechat_account_created_and_qr_relayed(self):
        self.b.owner_id = 42
        await self.b.ensure_wechat()
        self.assertEqual(self.daemon.accounts, [{"id": ACC, "alias": "me"}])
        self.assertTrue(self.b.wx_ready and self.daemon.connected)
        self.assertEqual(self.daemon.qr_fetches, ["1", "2"])
        photos = [s for s in self.b.bot.sent if s.file is not None]
        self.assertEqual(len(photos), 2)
        self.assertTrue(all(s.chat == 42 for s in self.b.bot.sent))
        self.assertEqual(photos[0].file.read(), b"\x89PNG-qr")
        self.assertEqual(sorted(self.b.bot.deleted), sorted(p.id for p in photos))  # old and final QR removed
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertTrue(any("手机微信上点" in t for t in texts))
        self.assertIn("微信已登录", texts[-1])

    async def test_startup_without_telegram_login_waits_for_pairing(self):
        from telethon import errors

        class Client:
            def __init__(self, authorized):
                self.authorized = authorized
                self.handlers = []

            async def start(self, **kw):
                pass

            async def connect(self):
                pass

            async def get_me(self):
                return SimpleNamespace(username="mybot", id=1)

            async def is_user_authorized(self):
                return self.authorized

            def add_event_handler(self, *a):
                self.handlers.append(a)

            async def run_until_disconnected(self):
                if not self.authorized:  # what Telegram does for a session that is not logged in
                    raise errors.AuthKeyUnregisteredError(request=None)
                await asyncio.Event().wait()

        self.b.bot, self.b.user = Client(True), Client(False)
        task = asyncio.create_task(self.b.run())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), task.done() and task.exception())
        self.assertIsNotNone(self.b.pairing_code)
        task.cancel()

    async def test_pairing_code_gate(self):
        replies = []

        def ev(sender, text):
            async def reply(t, **kw):
                replies.append((sender, t, kw))
            return SimpleNamespace(raw_text=text, sender_id=sender, chat_id=sender, reply=reply, respond=reply)

        self.b.start_pairing()
        code = self.b.pairing_code
        await self.b.on_bot_private(ev(7, "/login wrong"))
        self.assertIsNone(self.b.login_owner)
        self.assertIn("配对码不对", replies[-1][1])
        await self.b.on_bot_private(ev(7, f"/login {code}"))
        self.assertEqual(self.b.login_owner, 7)
        self.assertIn("buttons", replies[-1][2])
        n = len(replies)
        await self.b.on_bot_private(ev(8, "+8613800000000"))  # a stranger cannot drive the login
        self.assertEqual(len(replies), n)
        self.assertEqual(self.b.login_step, "method")

    async def test_phone_push_approval_copy_and_switch_to_qr(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        await self.b.wx.resolve_account()

        # Phone push confirmation skips awaiting_scan completely -> must NOT say "已扫码"
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "awaiting_approval", "result": {}},
            ],
            "qr": [
                {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        self.b.login_poll_interval = 0.05

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        login_task = asyncio.create_task(self.b.request_wechat_login())
        # Wait until the awaiting_approval phone push message is sent
        for _ in range(50):
            if any("登录确认请求" in (s.text or "") for s in self.b.bot.sent):
                break
            await asyncio.sleep(0.01)

        push_msg = next(s for s in self.b.bot.sent if s.text and "登录确认请求" in s.text)
        self.assertNotIn("已扫码", push_msg.text)
        switch_btn = push_msg.kwargs["buttons"][0][0]
        self.assertIn("扫码登录", switch_btn.text)

        # Clicking "改用扫码登录" switches immediately to strategy="qr" and completes
        cb = _CallbackEvent(42, btn_data(switch_btn))
        await self.b.on_bot_callback(cb)
        await login_task

        self.assertEqual(self.daemon.login_strategies, ["auto", "qr"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.b.wx_state, "online")
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertTrue(any("✅ 已扫码" in t for t in texts))
        self.assertIn("微信已登录", texts[-1])

    async def test_signed_out_notification_has_login_button(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        for _ in range(50):
            if self.b.wx_state == "signed_out" and self.b.bot.sent:
                break
            await asyncio.sleep(0.01)
        mon.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await mon

        alert = self.b.bot.sent[-1]
        self.assertIn("微信已退出登录", alert.text)
        login_btn = alert.kwargs["buttons"][0][0]
        qr_btn = alert.kwargs["buttons"][0][1]
        self.assertIn("重新登录", login_btn.text)
        self.assertEqual(btn_data(login_btn), b"wxlogin:auto")
        self.assertEqual(btn_data(qr_btn), b"wxlogin:qr")

        # Stranger clicking the login button is rejected
        unauth = _CallbackEvent(111, btn_data(login_btn))
        await self.b.on_bot_callback(unauth)
        self.assertEqual(unauth.answers, [("只有桥接账号本人可以操作", True)])
        self.assertEqual(self.daemon.login_strategies, [])

        # Owner clicking the login button launches WeChat login and restores online state
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        auth_cb = _CallbackEvent(42, btn_data(login_btn))
        await self.b.on_bot_callback(auth_cb)
        self.assertIsNotNone(self.b._wx_login_task)
        await self.b._wx_login_task
        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.b.wx_state, "online")
        self.assertTrue(self.daemon.authenticated)

    async def test_auto_relogin_succeeds_silently_after_random_delay(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.02
        self.b.cfg.bridge.auto_relogin_delay_max = 0.03
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 2.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 3600.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)  # let monitor tick again to ensure no duplicate online alerts
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertTrue(self.daemon.authenticated)
        self.assertTrue(self.daemon.connected)
        self.assertEqual(self.b.wx_state, "online")
        self.assertFalse(self.b._wx_auto_relogin_attempted)
        self.assertIsNotNone(self.b.store.get("auto_relogin_last_at"))
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertTrue(any("自动尝试重新登录一次" in t for t in texts))
        # Must NOT send the noisy phone confirmation prompt when silent auto-login succeeds
        self.assertFalse(any("登录确认请求" in t for t in texts))
        self.assertEqual(sum("自动重新登录" in t for t in texts), 1)
        self.assertEqual(sum("恢复在线" in t for t in texts), 1)

    async def test_auto_relogin_only_tries_once_and_cancels_on_qr_fallback(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.01
        self.b.cfg.bridge.auto_relogin_delay_max = 0.01
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 2.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # multiple subsequent monitor ticks must NOT retry
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.daemon.qr_fetches, [])
        self.assertEqual(self.b.wx_state, "signed_out")
        self.assertTrue(self.b._wx_auto_relogin_attempted)
        last = self.b.bot.sent[-1]
        self.assertIn("微信自动登录失败", last.text)
        self.assertIn("扫码登录", last.text)
        self.assertIn("不再自动重试", last.text)
        self.assertEqual(len(last.kwargs["buttons"][0]), 2)

    async def test_auto_relogin_times_out_waiting_for_approval_and_reminds_manual(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 0.04
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "awaiting_approval", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.04)
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.b.wx_state, "signed_out")
        last = self.b.bot.sent[-1]
        self.assertIn("自动登录该设备", last.text)
        self.assertIn("手动登录", last.text)

    async def test_auto_relogin_respects_cooldown_on_repeat_signout(self):
        import time as _time
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 3600.0
        self.b.store.set("auto_relogin_last_at", str(_time.time() - 60))
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(50):
                if self.b.wx_state == "signed_out" and self.b.bot.sent:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, [])
        self.assertIsNone(self.b._wx_auto_relogin_task)
        last = self.b.bot.sent[-1]
        self.assertIn("冷却", last.text)
        self.assertIn("不会自动登录", last.text)

    async def test_auto_relogin_cancel_button_and_manual_override(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 5.0
        self.b.cfg.bridge.auto_relogin_delay_max = 5.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 3600.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(50):
                if self.b._wx_auto_relogin_task is not None:
                    break
                await asyncio.sleep(0.01)
            # /status should report waiting auto-relogin while pending
            replies = []
            async def reply(text, **kw):
                replies.append((text, kw))
            await self.b.on_bot_command(SimpleNamespace(raw_text="/status", reply=reply))
            self.assertIn("自动尝试登录", replies[-1][0])
            self.assertIn("掉线自动登录：已开启", replies[-1][0])

            alert = self.b.bot.sent[-1]
            cancel_btn = alert.kwargs["buttons"][0][1]
            self.assertEqual(btn_data(cancel_btn), b"wxlogin:cancel_auto")
            cb = _CallbackEvent(42, btn_data(cancel_btn))
            await self.b.on_bot_callback(cb)
            await self.b._wx_auto_relogin_task
            self.assertEqual(cb.answers, [("已取消本次自动登录", False)])
            self.assertTrue(cb.edits and "已取消本次自动登录" in cb.edits[-1][0])
            self.assertEqual(self.daemon.login_strategies, [])
            # Cancelling before calling WeChat should not burn the cooldown
            self.assertIsNone(self.b.store.get("auto_relogin_last_at"))
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        # Now test clicking "立即手动登录" while waiting: cancels timer & runs manual login once
        self.b._mark_wx_online()
        self.b.bot.sent.clear()
        self.b._wx_auto_relogin_task = None
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        self.b.wx_state = "signed_out"
        await self.b._on_wx_signed_out()
        self.assertIsNotNone(self.b._wx_auto_relogin_task)
        wait_task = self.b._wx_auto_relogin_task
        manual_btn = self.b.bot.sent[-1].kwargs["buttons"][0][0]
        self.assertEqual(btn_data(manual_btn), b"wxlogin:auto")
        cb2 = _CallbackEvent(42, btn_data(manual_btn))
        await self.b.on_bot_callback(cb2)
        await wait_task
        await self.b._wx_login_task
        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.b.wx_state, "online")
        self.assertIsNone(self.b.store.get("auto_relogin_last_at"))

    async def test_auto_relogin_aborts_if_account_recovers_during_delay(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.03
        self.b.cfg.bridge.auto_relogin_delay_max = 0.03
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        await self.b._on_wx_signed_out()
        self.daemon.authenticated = True
        self.daemon.connected = True
        await self.b._wx_auto_relogin_task

        self.assertEqual(self.daemon.login_strategies, [])
        self.assertEqual(self.b.wx_state, "online")
        self.assertIn("无需自动重新登录", self.b.bot.sent[-1].text)

    async def test_auto_relogin_qr_override_while_running(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 5.0
        self.b.login_poll_interval = 0.05
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "awaiting_approval", "result": {}},
            ],
            "qr": [
                {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
                {"state": "succeeded", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        await self.b._on_wx_signed_out()
        for _ in range(50):
            if self.b._wx_auto_relogin_phase == "running" and self.daemon.login_strategies == ["auto"]:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.b._wx_auto_relogin_phase, "running")

        cb = _CallbackEvent(42, b"wxlogin:qr")
        await self.b.on_bot_callback(cb)
        await self.b._wx_auto_relogin_task
        await self.b._wx_login_task

        self.assertEqual(self.daemon.login_strategies, ["auto", "qr"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.b.wx_state, "online")
        self.assertFalse(any("自动登录失败" in (s.text or "") for s in self.b.bot.sent))

    async def test_auto_relogin_failure_state_does_not_retry_and_reminds_manual(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "failed", "result": {"code": "device_auth_expired"}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.b.wx_state, "signed_out")
        last = self.b.bot.sent[-1]
        self.assertIn("微信自动登录失败", last.text)
        self.assertIn("device_auth_expired", last.text)
        self.assertIn("不再自动重试", last.text)
        self.assertIn("/wxlogin", last.text)

    async def test_ensure_wechat_waits_quietly_on_409_operation_in_progress_until_authenticated(self):
        self.b.owner_id = 42
        self.b.wx_startup_poll_interval = 0.01
        self.b.wx_startup_wait_timeout = 1.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.account_infos = [
            {"authenticated": False, "connection": "disconnected"},
            {"authenticated": True, "connection": "online"},
        ]
        self.daemon.login_errors = [(409, "operation_in_progress")]

        await self.b.ensure_wechat()

        self.assertTrue(self.b.wx_ready)
        self.assertEqual(self.b.wx_state, "online")
        self.assertEqual(self.daemon.login_strategies, [])
        self.assertEqual(self.b.bot.sent, [], "must not spam login errors while daemon is still restoring")

    async def test_ensure_wechat_waits_quietly_while_account_is_reconnecting(self):
        self.b.owner_id = 42
        self.b.wx_startup_poll_interval = 0.01
        self.b.wx_startup_wait_timeout = 1.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.account_infos = [
            {"authenticated": False, "connection": "reconnecting"},
            {"authenticated": False, "connection": "reconnecting"},
            {"authenticated": True, "connection": "online"},
        ]

        await self.b.ensure_wechat()

        self.assertTrue(self.b.wx_ready)
        self.assertEqual(self.b.wx_state, "online")
        self.assertEqual(self.daemon.login_strategies, [])
        self.assertEqual(self.b.bot.sent, [])

    async def test_ensure_wechat_retries_login_after_409_clears_when_still_unauthenticated(self):
        self.b.owner_id = 42
        self.b.wx_startup_poll_interval = 0.01
        self.b.wx_startup_wait_timeout = 1.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.login_errors = [(409, "operation_in_progress")]
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }

        await self.b.ensure_wechat()

        self.assertTrue(self.b.wx_ready)
        self.assertEqual(self.b.wx_state, "online")
        self.assertEqual(self.daemon.login_strategies, ["auto"])
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertFalse(any("409" in t or "登录出错" in t for t in texts))
        self.assertIn("微信已登录", texts[-1])

    async def test_auto_relogin_waits_on_409_operation_in_progress_and_recovers(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 1.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.account_infos = [
            {"authenticated": False, "connection": "disconnected"},  # pre-check
            {"authenticated": False, "connection": "reconnecting"},  # settle poll 1
            {"authenticated": True, "connection": "online"},         # settle poll 2
        ]
        self.daemon.login_errors = [(409, "operation_in_progress")]
        await self.b.wx.resolve_account()

        await self.b._on_wx_signed_out()
        await self.b._wx_auto_relogin_task

        self.assertEqual(self.b.wx_state, "online")
        self.assertIn("自动重新登录并恢复在线", self.b.bot.sent[-1].text)
        self.assertFalse(any("409" in (s.text or "") for s in self.b.bot.sent))

    async def test_monitor_ignores_transient_reconnecting_state_when_unauthenticated(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.account_infos = [
            {"authenticated": False, "connection": "reconnecting"},
            {"authenticated": True, "connection": "online"},
        ]
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(50):
                if self.b.wx_state == "online" and any("恢复在线" in (s.text or "") for s in self.b.bot.sent):
                    break
                await asyncio.sleep(0.01)
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertIsNone(self.b._wx_auto_relogin_task)
        self.assertEqual(self.daemon.login_strategies, [])
        self.assertFalse(any("已退出登录" in (s.text or "") for s in self.b.bot.sent))

    async def test_login_failure_shows_service_and_service_code_and_qr_hint(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "failed", "result": {"code": "login_failed", "service": "saved_device_login", "serviceCode": -100}},
            ],
        }
        await self.b.wx.resolve_account()

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        await self.b.request_wechat_login(force_qr=False)

        msg = self.b.bot.sent[-1]
        self.assertIn("微信登录未完成", msg.text)
        self.assertIn("login_failed · saved_device_login · -100", msg.text)
        self.assertIn("扫码登录", msg.text)
        btns = [btn_data(btn) for btn in msg.kwargs["buttons"][0]]
        self.assertEqual(btns, [b"wxlogin:auto", b"wxlogin:qr"])

    async def test_auto_relogin_failure_includes_service_and_service_code(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "failed", "result": {"code": "login_failed", "service": "saved_device_login", "serviceCode": -100}},
            ],
        }
        await self.b.wx.resolve_account()

        await self.b._on_wx_signed_out()
        await self.b._wx_auto_relogin_task

        last = self.b.bot.sent[-1]
        self.assertIn("微信自动登录失败（login_failed · saved_device_login · -100）", last.text)
        self.assertEqual(len(last.kwargs["buttons"][0]), 2)

    async def test_manual_wxlogin_waits_out_409_and_recovers_when_authenticated(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.wx_startup_poll_interval = 0.01
        self.b.wx_login_busy_timeout = 1.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.account_infos = [
            {"authenticated": False, "connection": "reconnecting"},
            {"authenticated": True, "connection": "online"},
        ]
        self.daemon.login_errors = [(409, "operation_in_progress"), (409, "operation_in_progress")]
        await self.b.wx.resolve_account()

        await self.b.request_wechat_login(force_qr=False)

        self.assertEqual(self.b.wx_state, "online")
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertTrue(any("稍候" in t for t in texts))
        self.assertIn("恢复在线", texts[-1])
        self.assertFalse(any("409" in t for t in texts))

    async def test_manual_wxlogin_retries_after_409_clears_and_tolerates_connect_409(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.wx_startup_poll_interval = 0.01
        self.b.wx_login_busy_timeout = 1.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.login_errors = [(409, "operation_in_progress")]
        self.daemon.connect_errors = [(409, "operation_in_progress")]
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        await self.b.request_wechat_login(force_qr=False)

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.b.wx_state, "online")
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertIn("微信已登录", texts[-1])
        self.assertFalse(any("登录出错" in t for t in texts))

    async def test_switch_to_qr_waits_out_transient_409_after_cancel(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.login_poll_interval = 0.02
        self.b.wx_startup_poll_interval = 0.01
        self.b.wx_login_busy_timeout = 1.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "awaiting_approval", "result": {}},
            ],
            "qr": [
                {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
                {"state": "succeeded", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        task = asyncio.create_task(self.b.request_wechat_login(force_qr=False))
        for _ in range(50):
            if any("登录确认请求" in (s.text or "") for s in self.b.bot.sent):
                break
            await asyncio.sleep(0.01)
        # Simulate daemon taking a moment to release the lock of the cancelled auto op
        self.daemon.login_errors = [(409, "operation_in_progress")]
        await self.b.on_bot_callback(_CallbackEvent(42, b"wxlogin:qr"))
        await task

        self.assertEqual(self.daemon.login_strategies, ["auto", "qr"])
        self.assertEqual(self.b.wx_state, "online")
        self.assertIn("微信已登录", self.b.bot.sent[-1].text)

    async def test_telegram_code_login_keypad_one_tap_phone_and_2fa(self):
        from telethon import errors, types

        deleted_events = []

        class AuthUser(FakeUser):
            def __init__(self):
                super().__init__()
                self.code_requests: list[str] = []
                self.sign_ins: list[dict] = []
                self.send_types = [
                    SimpleNamespace(phone_code_hash="h1", type=types.auth.SentCodeTypeApp(length=5)),
                    SimpleNamespace(phone_code_hash="h2", type=types.auth.SentCodeTypeSms(length=5)),
                ]

            async def send_code_request(self, phone):
                self.code_requests.append(phone)
                return self.send_types[len(self.code_requests) - 1]

            async def sign_in(self, **kw):
                self.sign_ins.append(kw)
                if "password" in kw:
                    if kw["password"] != "goodpass":
                        raise errors.PasswordHashInvalidError(request=None)
                    return
                if kw["code"] == "99999":
                    raise errors.PhoneCodeInvalidError(request=None)
                if kw["code"] == "12345":
                    raise errors.SessionPasswordNeededError(request=None)

            async def get_me(self):
                return SimpleNamespace(id=7, first_name="Owner", last_name=None, username="owner")

        self.b.user = AuthUser()
        self.b.start_pairing()
        code = self.b.pairing_code

        replies = []

        def make_ev(sender, text="", *, contact=None):
            async def reply(msg, **kw):
                replies.append((sender, msg, kw))
                return SimpleNamespace(text=msg, kwargs=kw)

            async def delete():
                deleted_events.append((sender, text, contact is not None))

            return SimpleNamespace(
                sender_id=sender, chat_id=sender, raw_text=text,
                message=SimpleNamespace(contact=contact),
                reply=reply, respond=reply, delete=delete,
            )

        # Pair -> method chooser
        await self.b.on_bot_private(make_ev(7, f"/login {code}"))
        self.assertEqual(self.b.login_step, "method")

        # Stranger callback ignored
        stranger_cb = _CallbackEvent(99, b"tg:phone")
        await self.b.on_bot_callback(stranger_cb)
        self.assertEqual(self.b.login_step, "method")
        self.assertEqual(stranger_cb.answers, [(None, False)])

        # Owner picks verification code login -> one-tap phone ReplyKeyboard
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:phone"))
        self.assertEqual(self.b.login_step, "phone")
        phone_prompt = self.b.bot.sent[-1]
        self.assertIn("一键使用当前手机号", phone_prompt.text)
        self.assertIsInstance(phone_prompt.kwargs["buttons"][0][0].button.type, types.ButtonTypeRequestPhone)

        # Invalid manual phone rejected
        await self.b.on_bot_private(make_ev(7, "abc"))
        self.assertIn("手机号格式不对", replies[-1][1])

        # One-tap contact sharing deletes contact message and displays inline keypad
        contact = SimpleNamespace(phone_number="8613800138000")
        await self.b.on_bot_private(make_ev(7, "", contact=contact))
        self.assertIn((7, "", True), deleted_events)
        self.assertEqual(self.b.user.code_requests, ["+8613800138000"])
        self.assertEqual(self.b.login_step, "code")
        keypad_msg = self.b.bot.sent[-1]
        self.assertIn("Telegram 客户端内", keypad_msg.text)
        self.assertIn("<code>_ _ _ _ _</code>", keypad_msg.text)
        self.assertEqual(keypad_msg.kwargs["buttons"][4][0].type.data, b"tg:k:noop")

        # Tapping noop before 4 digits or back on empty buffer
        noop_cb = _CallbackEvent(7, b"tg:k:noop")
        await self.b.on_bot_callback(noop_cb)
        self.assertIn("至少 4 位", noop_cb.answers[0][0])
        back_empty_cb = _CallbackEvent(7, b"tg:k:back")
        await self.b.on_bot_callback(back_empty_cb)
        self.assertIn("没有已输入的数字", back_empty_cb.answers[0][0])

        # Tapping digits + backspace updates keypad in place and unlocks confirm
        for key in ("1", "2", "3", "back", "9", "4", "5"):
            cb = _CallbackEvent(7, f"tg:k:{key}".encode())
            await self.b.on_bot_callback(cb)
        last_edit_text, last_edit_kw = cb.edits[-1]
        self.assertIn("<code>1 2 9 4 5</code>", last_edit_text)
        self.assertEqual(last_edit_kw["buttons"][4][0].type.data, b"tg:k:submit")

        # Clear empties buffer
        clear_cb = _CallbackEvent(7, b"tg:k:clear")
        await self.b.on_bot_callback(clear_cb)
        self.assertIn("<code>_ _ _ _ _</code>", clear_cb.edits[-1][0])

        # Wrong code via keypad resets buffer with error notice
        for key in ("9", "9", "9", "9", "9", "submit"):
            err_cb = _CallbackEvent(7, f"tg:k:{key}".encode())
            await self.b.on_bot_callback(err_cb)
        self.assertIn("验证码不正确", err_cb.edits[-1][0])
        self.assertIn("<code>_ _ _ _ _</code>", err_cb.edits[-1][0])

        # Resend code uses new delivery channel label (SMS)
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:resend"))
        self.assertEqual(self.b.user.code_requests, ["+8613800138000", "+8613800138000"])
        self.assertIn("手机短信", self.b.bot.sent[-1].text)

        # Valid code triggering 2FA clears keypad and enters password step
        for key in ("1", "2", "3", "4", "5", "submit"):
            ok_cb = _CallbackEvent(7, f"tg:k:{key}".encode())
            await self.b.on_bot_callback(ok_cb)
        self.assertEqual(self.b.user.sign_ins[-1], {"phone": "+8613800138000", "code": "12345", "phone_code_hash": "h2"})
        self.assertIsNone(ok_cb.edits[-1][1]["buttons"])
        self.assertEqual(self.b.login_step, "password")
        self.assertIn("两步验证密码", self.b.bot.sent[-1].text)

        # Wrong password -> deleted and prompts retry; right password -> completes login
        await self.b.on_bot_private(make_ev(7, "badpass"))
        self.assertIn((7, "badpass", False), deleted_events)
        self.assertIn("密码不正确", replies[-1][1])
        await self.b.on_bot_private(make_ev(7, "goodpass"))
        self.assertIn((7, "goodpass", False), deleted_events)
        self.assertTrue(self.b.tg_ready.is_set())
        self.assertIsNone(self.b.login_step)
        self.assertIn("Telegram 已登录", self.b.bot.sent[-1].text)
        self.assertIsInstance(self.b.bot.sent[-1].kwargs["buttons"], types.ReplyKeyboardHide)

    async def test_telegram_login_method_switching_and_text_code_fallback(self):
        from datetime import datetime, timedelta, timezone
        from telethon import types

        qr_wait_started = asyncio.Event()
        qr_cancelled = False

        class FakeQR:
            url = "tg://login?token=abc"
            expires = datetime.now(timezone.utc) + timedelta(minutes=2)

            async def wait(self, timeout=None):
                nonlocal qr_cancelled
                qr_wait_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    qr_cancelled = True
                    raise

            async def recreate(self):
                pass

        class AuthUser(FakeUser):
            def __init__(self):
                super().__init__()
                self.sign_ins: list[dict] = []

            async def qr_login(self):
                return FakeQR()

            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="h9", type=types.auth.SentCodeTypeApp(length=5))

            async def sign_in(self, **kw):
                self.sign_ins.append(kw)

            async def get_me(self):
                return SimpleNamespace(id=7, first_name="Owner", last_name=None, username="owner")

        self.b.user = AuthUser()
        self.b.start_pairing()
        deleted = []

        def make_ev(sender, text):
            async def reply(msg, **kw):
                return SimpleNamespace(text=msg, kwargs=kw)

            async def delete():
                deleted.append(text)

            return SimpleNamespace(
                sender_id=sender, chat_id=sender, raw_text=text,
                message=SimpleNamespace(contact=None), reply=reply, respond=reply, delete=delete,
            )

        await self.b.on_bot_private(make_ev(7, f"/login {self.b.pairing_code}"))
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:phone"))
        self.assertEqual(self.b.login_step, "phone")

        # Switch from phone ReplyKeyboard button to QR login
        await self.b.on_bot_private(make_ev(7, "📷 改用扫码登录"))
        await asyncio.wait_for(qr_wait_started.wait(), timeout=1.0)
        self.assertEqual(self.b.login_step, "qr")
        qr_msg = next(s for s in self.b.bot.sent if s.file is not None)
        self.assertEqual(qr_msg.kwargs["buttons"][0][0].type.data, b"tg:phone")

        # Switch back from QR inline button to verification code login -> cancels QR task & clears QR msg
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:phone"))
        await asyncio.sleep(0)
        self.assertTrue(qr_cancelled)
        self.assertIn(qr_msg.id, self.b.bot.deleted)
        self.assertEqual(self.b.login_step, "phone")

        # Manual phone + spaced text verification code fallback still works and clears keypad buttons
        await self.b.on_bot_private(make_ev(7, "+8613900001111"))
        self.assertEqual(self.b.login_step, "code")
        keypad_id = self.b.login_code_msg_id
        await self.b.on_bot_private(make_ev(7, " 1 2 3 4 5 "))
        self.assertIn(" 1 2 3 4 5 ", deleted)
        self.assertEqual(self.b.user.sign_ins[-1], {"phone": "+8613900001111", "code": "12345", "phone_code_hash": "h9"})
        self.assertTrue(any(cid == 7 and mid == keypad_id and kw.get("buttons") is None
                            for cid, mid, _, kw in self.b.bot.edited))
        self.assertTrue(self.b.tg_ready.is_set())
