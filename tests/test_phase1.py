"""Unit tests for Phase 1 hardening: UTF-16 HTML splitting, caption overflow, multi-chunk message.updated,
concurrent event_loop with high-water cursor, Store pruning/transactions, streaming wav_to_ogg & Media.sha256,
CallWeb Mini App auth/handoff/CSP, cards domains, and BridgeConfig validation.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
import httpx
from telethon import errors, types

from bridge import article as A
from bridge import cards, core, stickers, tghtml
from bridge.callweb import make_handoff_ticket, verify_handoff_ticket
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.store import Store
from bridge.wechat import APP, APP_QUOTE, IMAGE, LOCATION, STICKER, TEXT, Media
from tests.fakes import FakeBot, FakeDaemon, FakeForumBot, FakeUser, _FakeCallsSocket, _init_data


class HtmlSplitTests(unittest.TestCase):
    def test_fast_path_returns_original_html_verbatim(self):
        raw = '<b>Hello &amp; welcome</b> <blockquote expandable>quote</blockquote> <tg-time unix="1700000000" format="t">12:00</tg-time>'
        self.assertEqual(tghtml.split_html(raw, limit=4000), [raw])
        self.assertEqual(tghtml.split_html("", limit=4000), [""])

    def test_splits_utf16_safely_across_entities_tags_and_emojis(self):
        # Mix non-BMP emojis (2 UTF-16 units each), ZWJ family cluster, HTML entities, bold, link, expandable blockquote, tg-time.
        family = "👨‍👩‍👧‍👦"  # 11 UTF-16 units
        flag = "🇨🇳"  # 4 UTF-16 units
        body = (
            "<b>" + ("A &amp; B " * 12) + "</b>\n"
            + '<a href="https://example.com/?a=1&amp;b=2">' + (family * 8) + "</a>\n"
            + "<blockquote expandable>" + (flag * 20) + " " + ("<i>斜体文字</i>" * 10) + "</blockquote>\n"
            + '<pre><code class="language-py">x = 1 &lt; 2\n' + ("y = 3\n" * 15) + "</code></pre>\n"
            + tghtml.tg_time(1700000000, "2023-11-15 00:00", "dT")
        )
        limit = 80
        chunks = tghtml.split_html(body, limit=limit)
        self.assertGreater(len(chunks), 3)

        orig_plain, _ = tghtml.HTML.parse(body)
        reconstructed = []
        for ch in chunks:
            plain, ents = tghtml.HTML.parse(ch)
            self.assertLessEqual(tghtml.utf16_len(plain), limit, f"chunk exceeded limit: {ch!r}")
            # Must never contain lone surrogates or broken HTML entities
            plain.encode("utf-8")
            reconstructed.append(plain)
            # Expandable blockquote and tg-time attributes survive round-trip where present
            if "<blockquote expandable>" in ch:
                self.assertTrue(any(isinstance(e, types.MessageEntityBlockquote) and e.collapsed for e in ents))
            if "<tg-time " in ch:
                self.assertTrue(any(isinstance(e, types.MessageEntityFormattedDate) for e in ents))

        # No family ZWJ cluster or flag was split across chunk boundaries
        for ch in chunks:
            plain, _ = tghtml.HTML.parse(ch)
            self.assertNotIn("\u200d", plain.replace(family, ""))
        self.assertEqual(re.sub(r"\s+", "", "".join(reconstructed)), re.sub(r"\s+", "", orig_plain))

    def test_prefers_newline_boundary_and_supports_rest_limit(self):
        lines = [f"line-{i:02d}-" + ("x" * 20) for i in range(10)]
        html_text = "\n".join(lines)
        chunks = tghtml.split_html(html_text, limit=70, rest_limit=160)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertLessEqual(tghtml.utf16_len(tghtml.HTML.parse(chunks[0])[0]), 70)
        for ch in chunks[1:]:
            self.assertLessEqual(tghtml.utf16_len(tghtml.HTML.parse(ch)[0]), 160)
        # First chunk should end cleanly at a line boundary rather than mid-word
        self.assertTrue(chunks[0].endswith("\n"))

    def test_skips_whitespace_only_intermediate_segments(self):
        html_text = ("A" * 40) + "\n" + (" " * 55) + "\n" + ("B" * 40)
        chunks = tghtml.split_html(html_text, limit=50)
        self.assertGreaterEqual(len(chunks), 2)
        for ch in chunks:
            plain, _ = tghtml.HTML.parse(ch)
            self.assertTrue(plain.strip(), f"whitespace-only chunk produced: {ch!r}")


class StoreHardeningTests(unittest.TestCase):
    def test_close_is_idempotent_and_tg_msgs_for_wx_orders_chunks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            st = Store(Path(tmp) / "s.db")
            try:
                st.map_message(-1001, "bot", 30, "wx_a", "m1", "s1")
                st.map_message(-1001, "bot", 10, "wx_a", "m1", "s1")
                st.map_message(-1001, "bot", 20, "wx_a", "m1", "s1")
                self.assertEqual(st.tg_msgs_for_wx("wx_a", "m1", None), (-1001, [10, 20, 30]))
                self.assertEqual(st.tg_for_wx("wx_a", "m1", None), (-1001, 10))
            finally:
                st.close()
                st.close()  # idempotent

    def test_prune_trims_unbounded_tables_by_rowid(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            st = Store(Path(tmp) / "s.db", max_sent=3, max_msgmap=4, max_articles=2, prune_interval=1000)
            try:
                for i in range(6):
                    st.add_sent("t", f"srv{i}")
                for i in range(7):
                    st.map_message(-10, "bot", i + 1, "t", f"m{i}", f"s{i}")
                aids = [st.article_id(f"https://example.com/{i}") for i in range(5)]

                removed = st.prune()
                self.assertEqual(removed, {"sent": 3, "msgmap": 3, "articles": 3})
                # Second prune is an O(log N) fast-path no-op without opening a write transaction
                self.assertEqual(st.prune(), {"sent": 0, "msgmap": 0, "articles": 0})

                self.assertFalse(st.is_sent("t", "srv0"))
                self.assertTrue(st.is_sent("t", "srv5"))
                self.assertIsNone(st.tg_for_wx("t", "m0", None))
                self.assertIsNotNone(st.tg_for_wx("t", "m6", None))
                self.assertIsNone(st.article_url(aids[0]))
                self.assertEqual(st.article_url(aids[-1]), "https://example.com/4")
            finally:
                st.close()

    def test_auto_prune_triggers_after_interval(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            st = Store(Path(tmp) / "s.db", max_sent=2, max_msgmap=2, max_articles=2, prune_interval=5)
            try:
                for i in range(5):
                    st.add_sent("t", f"s{i}")
                cnt = st.db.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
                self.assertEqual(cnt, 2)
            finally:
                st.close()

    def test_explicit_transaction_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            st = Store(Path(tmp) / "s.db")
            try:
                st.link("wx_1", -100, "Original")
                with self.assertRaises(RuntimeError):
                    with st._tx():
                        st.db.execute("DELETE FROM threads WHERE wx_thread=?", ("wx_1",))
                        raise RuntimeError("boom")
                self.assertEqual(st.chat_for_thread("wx_1"), -100)
            finally:
                st.close()


class MediaAndExecutorTests(unittest.IsolatedAsyncioTestCase):
    def test_media_sha256_streams_file_without_read(self):
        payload = os.urandom(200_000)  # > 3 chunks of 64 KiB
        expected = hashlib.sha256(payload).hexdigest()
        mem_media = Media(payload, "image/gif", "a.gif")
        self.assertEqual(mem_media.sha256(), expected)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            p = os.path.join(tmp, "big.gif")
            with open(p, "wb") as f:
                f.write(payload)
            file_media = Media(b"", "image/gif", "big.gif", path=p)
            with mock.patch.object(file_media, "read", side_effect=AssertionError("should not call read()")):
                self.assertEqual(file_media.sha256(), expected)
            file_media.path = None  # let TemporaryDirectory clean up

    async def test_wav_to_ogg_uses_path_and_enforces_timeout(self):
        captured_args = []

        class FakeProc:
            returncode = 0

            def __init__(self, *, slow: bool = False):
                self.slow = slow
                self.killed = False

            async def communicate(self, input=None):
                self.input = input
                if self.slow:
                    await asyncio.sleep(5.0)
                return b"OggS-data", b""

            def kill(self):
                self.killed = True

            async def wait(self):
                return -9

        proc_ok = FakeProc()

        async def fake_exec(*args, **kwargs):
            captured_args.append((args, kwargs))
            return proc_ok

        with mock.patch("bridge.stickers.find_ffmpeg", return_value="ffmpeg"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            out = await core.wav_to_ogg(path="/tmp/voice.wav", timeout=5.0)
        self.assertEqual(out, b"OggS-data")
        self.assertIn("/tmp/voice.wav", captured_args[0][0])
        self.assertNotIn("pipe:0", captured_args[0][0])
        self.assertIsNone(proc_ok.input)

        proc_slow = FakeProc(slow=True)

        async def fake_slow(*args, **kwargs):
            return proc_slow

        with mock.patch("bridge.stickers.find_ffmpeg", return_value="ffmpeg"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=fake_slow):
            out2 = await core.wav_to_ogg(b"RIFF...", timeout=0.02)
        self.assertIsNone(out2)
        self.assertTrue(proc_slow.killed)

    async def test_media_executor_is_isolated_with_two_workers(self):
        ex = stickers.get_media_executor()
        self.assertEqual(ex._max_workers, 2)
        self.assertEqual(ex._thread_name_prefix, "bridge-media")
        name = await stickers.run_in_media_executor(lambda: __import__("threading").current_thread().name)
        self.assertTrue(name.startswith("bridge-media"))


class BridgePhase1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(
            WeChatConfig(token="secret-token"),
            TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
            BridgeConfig(create_interval_seconds=0, sync_avatar=False),
            Path(self.tmp.name) / "config.toml",
        )
        self.daemon = FakeDaemon()
        self.b = core.Bridge(cfg, user_client=FakeUser(), bot_client=FakeBot())
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.wx._poll_initial_delay = 0.0
        self.b.wx._avatar_poll_interval = 0.0
        self.b._bot_pair_wait = 0.0
        self.b.bot_input = object()
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        await self.b.wx.resolve_account()

    async def asyncTearDown(self):
        await self.b.close()
        self.tmp.cleanup()

    async def test_media_caption_overflow_continues_as_follow_up_messages_and_maps_all(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -501, "Bob")
        long_desc = "🌟" * 600  # 1200 UTF-16 code units (> 1000 caption limit)
        msg = {
            "id": "m_cap", "threadId": "wxid_bob", "serverId": "s_cap",
            "type": APP, "appMessageType": 51,
            "link": {"title": "超长标题", "description": long_desc, "sourceName": "频道"},
            "direction": "incoming",
        }
        media = Media(b"\x89PNG\r\n\x1a\nfake", "image/png", "cover.png")
        sent = await self.b._send_tg(-501, {"id": "wxid_bob", "kind": "direct"}, "", "", msg, media, None, False)
        self.assertGreaterEqual(len(sent), 2)
        first = sent[0]
        self.assertIsNotNone(first.file)
        cap_plain, _ = tghtml.HTML.parse(first.kwargs["caption"])
        self.assertLessEqual(tghtml.utf16_len(cap_plain), core.TG_CAPTION_LIMIT)
        # Follow-up message replies to the media message and preserves remaining text
        follow = sent[1]
        self.assertIsNone(follow.file)
        self.assertEqual(follow.kwargs["reply_to"], first.id)
        follow_plain, _ = tghtml.HTML.parse(follow.text)
        self.assertIn("🌟", follow_plain)

    async def test_message_updated_preserves_quote_and_edits_multichunk_idempotently(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -501, "Bob")
        # Initial short message (single chunk)
        self.daemon.add_message(
            "m_upd", "wxid_bob", "初始正文", type_=APP, appMessageType=APP_QUOTE, server="s_upd",
            quote={"senderName": "Alice", "embeddedText": "被引用的话"},
        )
        await self.b.handle_event({"cursor": "1", "kind": "message.committed",
                                   "data": {"messageId": "m_upd", "threadId": "wxid_bob", "origin": "live"}})
        self.assertEqual(len(self.b.bot.sent), 1)
        first_id = self.b.bot.sent[0].id
        self.assertIn("<blockquote>Alice：被引用的话</blockquote>", self.b.bot.sent[0].text)

        # Update 1: body expands across 2 chunks
        long_body = ("第一段内容\n" * 450) + ("第二段内容\n" * 450)
        self.daemon.messages["m_upd"]["body"] = long_body
        await self.b.on_wx_message_updated({"messageId": "m_upd", "threadId": "wxid_bob"})
        self.assertEqual(len(self.b.bot.sent), 2, "only 1 overflow chunk should be added")
        self.assertIn("<blockquote>Alice：被引用的话</blockquote>", self.b.bot.sent[0].text)
        mapped = self.b.store.tg_msgs_for_wx("wxid_bob", "m_upd", "s_upd")
        self.assertEqual(mapped, (-501, [first_id, self.b.bot.sent[1].id]))

        # Update 2: identical content (raises MessageNotModifiedError) must NOT append duplicate overflow
        orig_edit = self.b.bot.edit_message

        async def edit_with_not_modified(chat, msg_id, text, **kw):
            cur = next(m for m in self.b.bot.sent if m.chat == chat and m.id == msg_id)
            if cur.text == text:
                raise errors.MessageNotModifiedError(request=None)
            return await orig_edit(chat, msg_id, text, **kw)

        self.b.bot.edit_message = edit_with_not_modified
        await self.b.on_wx_message_updated({"messageId": "m_upd", "threadId": "wxid_bob"})
        self.assertEqual(len(self.b.bot.sent), 2, "duplicate update must not append overflow messages")

        # Update 3: second chunk changes -> edits chunk 2 in place without sending new messages
        self.daemon.messages["m_upd"]["body"] = long_body + "末尾追加"
        await self.b.on_wx_message_updated({"messageId": "m_upd", "threadId": "wxid_bob"})
        self.assertEqual(len(self.b.bot.sent), 2)
        self.assertIn("末尾追加", self.b.bot.sent[1].text)

    async def test_event_loop_cross_session_concurrency_per_session_order_and_hwm_cursor(self):
        self.b.store.set("cursor", "0")
        order: list[str] = []
        a1_started = asyncio.Event()
        a1_release = asyncio.Event()
        b1_done = asyncio.Event()
        a2_done = asyncio.Event()

        async def fake_handle(ev: dict):
            mid = ev["data"]["messageId"]
            order.append(f"start:{mid}")
            if mid == "a1":
                a1_started.set()
                await a1_release.wait()
            order.append(f"end:{mid}")
            if mid == "b1":
                b1_done.set()
            if mid == "a2":
                a2_done.set()

        self.b.handle_event = fake_handle

        async def fake_stream(after: str):
            if after == "0":
                yield {"cursor": "10", "kind": "message.committed", "data": {"messageId": "a1", "threadId": "tA"}}
                yield {"cursor": "11", "kind": "message.committed", "data": {"messageId": "b1", "threadId": "tB"}}
                yield {"cursor": "12", "kind": "message.committed", "data": {"messageId": "a2", "threadId": "tA"}}
                await a2_done.wait()
            await asyncio.sleep(60)

        self.b.wx.stream = fake_stream
        acks: list[str] = []

        async def fake_ack(c: str):
            acks.append(c)

        self.b._ack = fake_ack

        task = asyncio.create_task(self.b.event_loop())
        try:
            await asyncio.wait_for(a1_started.wait(), timeout=2.0)
            await asyncio.wait_for(b1_done.wait(), timeout=2.0)
            # tB finished while tA(a1) is still blocked -> cursor must NOT skip ahead to 11!
            self.assertEqual(self.b.store.get("cursor"), "0")
            # a2 (same session tA) must not have started yet!
            self.assertNotIn("start:a2", order)

            a1_release.set()
            await asyncio.wait_for(a2_done.wait(), timeout=2.0)
            await asyncio.sleep(0.02)

            # High-water mark now advances cleanly to 12
            self.assertEqual(self.b.store.get("cursor"), "12")
            # Strict FIFO within tA: a1 ended before a2 started
            self.assertLess(order.index("end:a1"), order.index("start:a2"))
            # Concurrency across sessions: b1 ended before a1 ended
            self.assertLess(order.index("end:b1"), order.index("end:a1"))
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_venue_and_sticker_topic_gone_triggers_recreate(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            cfg = Config(
                WeChatConfig(token="secret-token"),
                TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
                BridgeConfig(mode="topics", forum_chat=-1009000000001, create_interval_seconds=0, sync_avatar=False),
                Path(tmp.name) / "config.toml",
            )
            bot = FakeForumBot()
            b = core.Bridge(cfg, user_client=None, bot_client=bot)
            await b.wx.http.aclose()
            b.wx.http = httpx.AsyncClient(
                transport=httpx.MockTransport(self.daemon.handler),
                headers={"Authorization": "Bearer secret-token"},
            )
            await b.wx.resolve_account()
            b.bot_me = SimpleNamespace(username="mybot", id=1)
            b.store.link_topic("wxid_bob", -1009000000001, 55, "Bob")
            try:
                orig_send_file = bot.send_file
                calls = 0

                async def fail_topic_once(chat, file, **kw):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise errors.RPCError(request=None, message="TOPIC_DELETED", code=400)
                    return await orig_send_file(chat, file, **kw)

                bot.send_file = fail_topic_once
                loc_msg = {
                    "id": "m_loc", "threadId": "wxid_bob", "type": LOCATION, "direction": "incoming",
                    "location": {"latitude": 31.23, "longitude": 121.47, "name": "外滩", "address": "上海"},
                }
                sent = await b._send_tg(-1009000000001, {"id": "wxid_bob", "kind": "direct", "displayName": "Bob"},
                                        "", "", loc_msg, None, None, False)
                self.assertEqual(len(sent), 1)
                self.assertNotEqual(b.store.topic_for_thread("wxid_bob")[1], 55)
            finally:
                await b.close()
        finally:
            tmp.cleanup()


class CallWebSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(
            WeChatConfig(token="secret-token"),
            TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
            BridgeConfig(
                create_interval_seconds=0, sync_avatar=False,
                call_web_url="http://127.0.0.1/wxcall",
                call_web_listen="127.0.0.1:0",
                call_web_app="callapp",
            ),
            Path(self.tmp.name) / "config.toml",
        )
        self.daemon = FakeDaemon()
        self.b = core.Bridge(cfg, user_client=FakeUser(), bot_client=FakeBot())
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.tg_ready.set()
        self.b.owner_id = 999
        await self.b.wx.resolve_account()
        self.b._calls_sock = _FakeCallsSocket()
        await self.b.call_web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.b.call_web.bound_port}/wxcall"
        await self.b.handle_call_event({
            "type": "hello", "protocolVersion": 3, "clientId": "ctl-1", "conferenceInvitations": [],
            "capabilities": {"directAudio": True},
            "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
        })

    async def asyncTearDown(self):
        await self.http.close()
        await self.b.close()
        self.tmp.cleanup()

    async def test_miniapp_mode_requires_init_data_or_valid_handoff_ticket(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "sec-1", "revision": 1, "kind": "direct", "callType": "voice", "callerId": "wxid_alice"}})
        entry = self.b._active_calls["sec-1"]
        token = entry["web_token"]

        # 1) Plain browser without initData or handoff ticket -> rejected in Mini App mode
        ws_anon = await self.http.ws_connect(self.base + "/ws")
        await ws_anon.send_json({"type": "auth", "token": token, "initData": ""})
        msg = await ws_anon.receive_json(timeout=2)
        self.assertEqual(msg["type"], "fatal")
        await ws_anon.close()

        # 2) Tampered handoff ticket -> rejected
        ws_bad = await self.http.ws_connect(self.base + "/ws")
        await ws_bad.send_json({"type": "auth", "token": token, "handoff": "9999999999.deadbeef"})
        msg = await ws_bad.receive_json(timeout=2)
        self.assertEqual(msg["type"], "fatal")
        await ws_bad.close()

        # 3) Owner Mini App initData -> accepted and receives signed handoff ticket
        ws_owner = await self.http.ws_connect(self.base + "/ws")
        await ws_owner.send_json({"type": "auth", "token": token, "initData": _init_data("123:abc", 999)})
        st = await ws_owner.receive_json(timeout=2)
        self.assertEqual(st["type"], "state")
        self.assertEqual(st["phase"], "ringing")
        handoff = st.get("handoff")
        self.assertTrue(handoff)
        self.assertTrue(verify_handoff_ticket(handoff, token, "123:abc"))
        self.assertFalse(verify_handoff_ticket(handoff, "other-token", "123:abc"))
        await ws_owner.close()

        # 4) External browser carrying valid handoff ticket -> accepted and gets refreshed ticket for reconnects
        ws_browser = await self.http.ws_connect(self.base + "/ws")
        await ws_browser.send_json({"type": "auth", "token": token, "handoff": handoff})
        st2 = await ws_browser.receive_json(timeout=2)
        self.assertEqual(st2["type"], "state")
        self.assertEqual(st2["phase"], "ringing")
        self.assertTrue(verify_handoff_ticket(st2.get("handoff", ""), token, "123:abc"))
        await ws_browser.close()

        # 5) Expired handoff ticket -> rejected
        expired = make_handoff_ticket(token, "123:abc", ttl=10, now=time.time() - 60)
        self.assertFalse(verify_handoff_ticket(expired, token, "123:abc"))

    async def test_call_page_sets_nonce_csp_matching_both_inline_scripts(self):
        async with self.http.get(self.base + "/call/sometoken") as r:
            self.assertEqual(r.status, 200)
            csp = r.headers.get("Content-Security-Policy", "")
            body = await r.text()
        m = re.search(r"script-src 'nonce-([^']+)' blob:", csp)
        self.assertIsNotNone(m, csp)
        nonce = m.group(1)
        self.assertIn("connect-src 'self' ws: wss:", csp)
        self.assertIn("media-src 'self' blob: mediastream:", csp)
        self.assertEqual(body.count(f'<script nonce="{nonce}">'), 2)
        self.assertNotIn("__NONCE__", body)


class CardsAndConfigPhase1Tests(unittest.TestCase):
    def test_cards_recognizes_xhslink_cn_rednote_and_bili2233(self):
        xhs1 = cards.extract_text_link("标题 https://xhslink.cn/o/abc123 复制本条信息，打开小红书App查看！")
        self.assertIsNotNone(xhs1)
        self.assertEqual(xhs1["sourceName"], "小红书")

        xhs2 = cards.extract_text_link("标题 https://www.rednote.com/explore/0123456789abcdef01234567 复制后打开小红书查看")
        self.assertIsNotNone(xhs2)
        self.assertEqual(xhs2["sourceName"], "小红书")

        bili = cards.extract_text_link("【测试视频-哔哩哔哩】 https://bili2233.cn/AbCd123")
        self.assertIsNotNone(bili)
        self.assertEqual(bili["sourceName"], "哔哩哔哩")

        self.assertEqual(cards.identify_link_source({"url": "https://xhslink.cn/o/abc"}).brand, "xhs")
        self.assertEqual(cards.identify_link_source({"url": "https://www.rednote.com/explore/123"}).brand, "xhs")
        self.assertEqual(cards.identify_link_source({"url": "https://bili2233.cn/xyz"}).brand, "bili")

    def test_bridge_config_strict_validation(self):
        # Valid edge cases: port 0 and create_interval_seconds=0 remain allowed
        c = BridgeConfig(call_web_listen="127.0.0.1:0", create_interval_seconds=0, max_media_mb=1, skip_threads=["a"])
        self.assertEqual((c.max_media_mb, c.create_interval_seconds), (1, 0.0))

        for bad_kw in (
            {"max_media_mb": 0},
            {"max_media_mb": -10},
            {"max_media_mb": True},
            {"max_media_mb": 1.5},
            {"create_interval_seconds": -1},
            {"create_interval_seconds": float("inf")},
            {"create_interval_seconds": float("nan")},
            {"create_interval_seconds": False},
            {"call_web_listen": "127.0.0.1:65536"},
            {"bridge_groups": "yes"},
            {"sync_avatar": 1},
            {"call_notifications": "false"},
            {"skip_threads": "weixin"},
            {"skip_threads": [123]},
            {"forum_chat": True},
        ):
            with self.assertRaises(ValueError, msg=f"expected ValueError for {bad_kw!r}"):
                BridgeConfig(**bad_kw)  # type: ignore[arg-type]

    def test_article_fetcher_httpx_fallback_sets_trust_env_false(self):
        created_kw = []

        class FakeAsyncClient:
            def __init__(self, **kw):
                created_kw.append(kw)

            async def aclose(self):
                pass

        with mock.patch.object(A, "_CurlAsyncSession", None), \
             mock.patch.object(A.httpx, "AsyncClient", FakeAsyncClient):
            fetcher = A.ArticleFetcher(proxy="http://127.0.0.1:8080")
            fetcher._ensure_session()
            asyncio.run(fetcher.close())
        self.assertEqual(len(created_kw), 1)
        self.assertIs(created_kw[0].get("trust_env"), False)


if __name__ == "__main__":
    unittest.main()
