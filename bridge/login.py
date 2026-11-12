"""Setup through the bot: Telegram account login (pairing code + QR or phone code) and WeChat QR login."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import time
import uuid

import httpx
import segno
from telethon import Button, errors, utils

from .wechat import NoAccountYet, WeChatError

log = logging.getLogger("login")

LOGIN_DONE_STATES = {"succeeded", "failed", "expired", "rejected", "cancelled", "needs_login"}
MAX_PAIRING_FAILURES = 10


def qr_png(data: str) -> io.BytesIO:
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=3)
    buf.seek(0)
    buf.name = "qr.png"
    return buf


class LoginMixin:
    """Mixed into Bridge. Expects self.user, self.bot, self.wx, self.store, self.cfg."""

    def _init_login(self) -> None:
        self.tg_ready = asyncio.Event()
        self.pairing_code: str | None = None
        self.pairing_failures = 0
        self.login_owner: int | None = None  # Telegram user id that entered the pairing code
        self.login_step: str | None = None  # None / "method" / "phone" / "code" / "password" / "qr"
        self.login_phone = ""
        self.login_msgs: list[int] = []  # QR messages to clean up
        self.qr_task: asyncio.Task | None = None
        self.wx_ready = False
        self.wx_login_lock = asyncio.Lock()
        self.wx_login_requested = asyncio.Event()
        self.wx_state = "unknown"
        self.owner_id: int | None = None
        self.login_poll_interval = 2.0

    # ======================================================================
    # Telegram account login, driven from the bot's private chat
    # ======================================================================
    def start_pairing(self) -> None:
        self.pairing_code = secrets.token_hex(4)
        banner = "=" * 64
        log.warning("\n%s\n  Telegram account is not logged in.\n  Open a private chat with @%s and send:\n\n"
                    "      /login %s\n\n  PAIRING CODE: %s\n%s",
                    banner, self.bot_me.username, self.pairing_code, self.pairing_code, banner)

    async def on_login_message(self, event) -> None:
        """Bot private messages while the Telegram account is not logged in yet."""
        text = (event.raw_text or "").strip()
        sender = event.sender_id
        if text.startswith("/login"):
            code = text.partition(" ")[2].strip()
            if self.pairing_failures >= MAX_PAIRING_FAILURES:
                return
            if not code or not secrets.compare_digest(code, self.pairing_code or ""):
                self.pairing_failures += 1
                log.warning("Wrong pairing code from Telegram user %s (%d/%d)", sender, self.pairing_failures,
                            MAX_PAIRING_FAILURES)
                await event.reply("配对码不对。配对码在服务器日志里（docker compose logs telegram-bridge）。")
                return
            self.login_owner = sender
            await self._ask_method(event)
            return
        if sender != self.login_owner:
            if text == "/start":
                await event.reply("这个桥还没有完成设置。请发送 /login 配对码（在服务器日志里）。")
            return
        step = self.login_step
        if step == "phone":
            await self._login_phone(event, text)
        elif step == "code":
            await self._login_code(event, text)
        elif step == "password":
            await self._login_password(event, text)
        else:
            await self._ask_method(event)

    async def on_login_callback(self, event) -> None:
        if event.sender_id != self.login_owner or self.tg_ready.is_set():
            await event.answer()
            return
        data = event.data or b""
        await event.answer()
        if data == b"tg:qr":
            if self.qr_task and not self.qr_task.done():
                self.qr_task.cancel()
            self.login_step = "qr"
            self.qr_task = asyncio.create_task(self._login_qr(event.chat_id))
        elif data == b"tg:phone":
            if self.qr_task and not self.qr_task.done():
                self.qr_task.cancel()
            self.login_step = "phone"
            await self.bot.send_message(event.chat_id, "请发送要登录的 Telegram 账号手机号，带国家码，例如 <code>+8613800000000</code>",
                                        parse_mode="html")

    async def _ask_method(self, event) -> None:
        self.login_step = "method"
        await event.reply(
            "配对成功。接下来登录<b>你自己的 Telegram 账号</b>（程序用它来自动建群），请选择方式：\n\n"
            "• <b>扫码登录</b>：我会发一张二维码，用手机 Telegram 的「设置 → 设备 → 关联桌面设备」扫码。"
            "二维码需要显示在另一台设备上（比如电脑版或网页版 Telegram 打开这个对话）。\n"
            "• <b>验证码登录</b>：发手机号，再把收到的验证码<b>用空格隔开</b>发给我。",
            parse_mode="html", buttons=[[Button.inline("扫码登录", b"tg:qr"), Button.inline("验证码登录", b"tg:phone")]])

    async def _login_qr(self, chat_id: int) -> None:
        try:
            qr = await self.user.qr_login()
            for attempt in range(6):
                await self._clear_login_msgs(chat_id)
                msg = await self.bot.send_file(
                    chat_id, qr_png(qr.url),
                    caption="用手机 Telegram 扫码：设置 → 设备 → 关联桌面设备。二维码过期后会自动换新的。")
                self.login_msgs.append(msg.id)
                timeout = max(5.0, qr.expires.timestamp() - time.time())
                try:
                    await qr.wait(timeout=timeout)
                    await self._clear_login_msgs(chat_id)
                    await self._tg_login_done(chat_id)
                    return
                except asyncio.TimeoutError:
                    await qr.recreate()
            await self._clear_login_msgs(chat_id)
            await self.bot.send_message(chat_id, "二维码多次过期。发送任意消息重新选择登录方式。")
            self.login_step = "method"
        except errors.SessionPasswordNeededError:
            await self._clear_login_msgs(chat_id)
            self.login_step = "password"
            await self.bot.send_message(chat_id, "这个账号开了两步验证，请发送密码（我读到后会立即删除这条消息）。")
        except asyncio.CancelledError:
            await self._clear_login_msgs(chat_id)
            raise
        except Exception as e:
            log.exception("QR login failed")
            await self.bot.send_message(chat_id, f"扫码登录出错：{e}\n发送任意消息重新选择登录方式。")
            self.login_step = "method"

    async def _login_phone(self, event, text: str) -> None:
        phone = re.sub(r"[^\d+]", "", text)
        if not re.fullmatch(r"\+?\d{6,16}", phone):
            await event.reply("手机号格式不对，请带国家码，例如 +8613800000000")
            return
        try:
            await self.user.send_code_request(phone)
        except errors.FloodWaitError as e:
            await event.reply(f"请求太频繁，Telegram 要求等待 {e.seconds} 秒后再试。")
            return
        except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError) as e:
            await event.reply(f"这个手机号不能登录：{type(e).__name__}")
            return
        self.login_phone = phone
        self.login_step = "code"
        await event.reply("验证码已发到你的 Telegram（或短信）。\n\n"
                          "⚠️ 请把验证码<b>用空格隔开</b>发给我，例如 <code>1 2 3 4 5</code>。"
                          "如果原样发送，Telegram 会认为验证码泄露并作废这次登录。", parse_mode="html")

    async def _login_code(self, event, text: str) -> None:
        code = re.sub(r"\D", "", text)
        await self._delete_quietly(event)
        if not 4 <= len(code) <= 8:
            await event.respond("请发送验证码数字，用空格隔开，例如 1 2 3 4 5")
            return
        try:
            await self.user.sign_in(phone=self.login_phone, code=code)
        except errors.SessionPasswordNeededError:
            self.login_step = "password"
            await event.respond("这个账号开了两步验证，请发送密码（我读到后会立即删除这条消息）。")
            return
        except errors.PhoneCodeInvalidError:
            await event.respond("验证码不对，请重新发送（用空格隔开）。")
            return
        except (errors.PhoneCodeExpiredError, errors.SendCodeUnavailableError):
            self.login_step = "phone"
            await event.respond("验证码已失效（可能是因为原样发送过）。请重新发送手机号获取新的验证码。")
            return
        await self._tg_login_done(event.chat_id)

    async def _login_password(self, event, text: str) -> None:
        await self._delete_quietly(event)
        try:
            await self.user.sign_in(password=text)
        except errors.PasswordHashInvalidError:
            await event.respond("两步验证密码不对，请重新发送。")
            return
        await self._tg_login_done(event.chat_id)

    async def _tg_login_done(self, chat_id: int) -> None:
        me = await self.user.get_me()
        self.login_step = None
        self.pairing_code = None
        name = utils.get_display_name(me)
        log.info("Telegram account logged in through the bot: %s", name)
        text = f"✅ Telegram 已登录：{name}"
        if me.id != self.login_owner:
            text += ("\n\n注意：登录的不是正在跟我对话的这个账号。之后我只听登录账号的命令，"
                     "微信群和通知也都在那个账号里。")
        await self.bot.send_message(chat_id, text)
        self.tg_ready.set()

    async def _clear_login_msgs(self, chat_id: int) -> None:
        if self.login_msgs:
            try:
                await self.bot.delete_messages(chat_id, self.login_msgs)
            except Exception:
                pass
            self.login_msgs = []

    async def _delete_quietly(self, event) -> None:
        try:
            await event.delete()
        except Exception:
            log.info("Could not delete a login message from the chat; delete it manually")

    # ======================================================================
    # WeChat login, QR codes relayed by the bot
    # ======================================================================
    async def notify(self, text: str, file=None, **kw):
        if not self.owner_id:
            log.info("notify: %s", text)
            return None
        try:
            if file is not None:
                return await self.bot.send_file(self.owner_id, file, caption=text, **kw)
            return await self.bot.send_message(self.owner_id, text, **kw)
        except Exception as e:
            log.warning("Could not message you on Telegram (%s): %s", e, text)
            return None

    async def ensure_wechat(self) -> None:
        delay = 2.0
        while True:
            try:
                try:
                    await self.wx.resolve_account()
                except NoAccountYet:
                    await self.wx.create_account("me")
                    log.info("Created agent-wechat account %s", self.wx.account)
                log.info("agent-wechat account: %s", self.wx.account)
                break
            except (httpx.HTTPError, WeChatError) as e:
                log.warning("agent-wechat not reachable yet (%s); retrying in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        while True:
            try:
                info = await self.wx.account_info()
                if info.get("authenticated"):
                    if info.get("connection") == "disconnected":
                        await self.wx.connect()
                    self.wx_ready, self.wx_state = True, "online"
                    return
                await self.notify("需要登录微信，正在获取二维码…")
                if await self.wechat_login():
                    self.wx_ready, self.wx_state = True, "online"
                    return
            except (httpx.HTTPError, WeChatError) as e:
                log.warning("WeChat login step failed: %s", e)
                await self.notify(f"微信登录出错：{e}。发送 /wxlogin 重试。")
            self.wx_login_requested.clear()
            await self.wx_login_requested.wait()

    async def wechat_login(self, force_qr: bool = False) -> bool:
        if self.wx_login_lock.locked():
            await self.notify("正在登录中，请扫描上面的二维码。")
            return False
        async with self.wx_login_lock:
            loop = asyncio.get_running_loop()
            op = await self.wx.start_login(f"bridge-login-{uuid.uuid4()}", force_qr=force_qr)
            last_gen, qr_msg, approval_since, hinted = None, None, None, False
            deadline = loop.time() + 660
            try:
                while loop.time() < deadline:
                    state, res = op.get("state"), op.get("result") or {}
                    if state == "succeeded":
                        info = await self.wx.account_info()
                        if info.get("connection") == "disconnected":
                            await self.wx.connect()
                        await self.notify("✅ 微信已登录。之后收到的微信消息会自动建群转发过来。")
                        return True
                    if state in LOGIN_DONE_STATES:
                        reason = res.get("code") or res.get("reason") or state
                        await self.notify(f"微信登录没有完成（{reason}）。发送 /wxlogin 重试。")
                        return False
                    gen = res.get("qrGeneration")
                    if state == "awaiting_scan" and res.get("qrAvailable") and gen != last_gen:
                        try:
                            png = await self.wx.login_qr(op["id"], gen)
                        except WeChatError:
                            png = None
                        if png:
                            if qr_msg:
                                await self._delete_owner_msg(qr_msg)
                            f = io.BytesIO(png)
                            f.name = "wechat-qr.png"
                            qr_msg = await self.notify("📱 用手机微信「扫一扫」扫这个二维码，然后在手机上确认登录。"
                                                       "二维码过期会自动换新的。", file=f)
                            last_gen = gen
                    if state == "awaiting_approval":
                        if approval_since is None:
                            approval_since = loop.time()
                            await self.notify("已扫码，请在手机微信上点「登录」确认。")
                        elif loop.time() - approval_since > 90 and not hinted:
                            hinted = True
                            await self.notify(
                                "手机确认后一直没反应？有些登录确认需要手机直连服务器的 TCP 8050-8100 端口。"
                                "可以在 VPS 防火墙/安全组里临时放行这些端口后再试，登录成功后再关掉。")
                    await asyncio.sleep(self.login_poll_interval)
                    op = await self.wx.operation(op["id"])
                await self.notify("微信登录超时。发送 /wxlogin 重试。")
                try:
                    await self.wx.cancel_operation(op["id"])
                except WeChatError:
                    pass
                return False
            finally:
                if qr_msg:
                    await self._delete_owner_msg(qr_msg)

    async def _delete_owner_msg(self, msg) -> None:
        try:
            await self.bot.delete_messages(self.owner_id, [msg.id])
        except Exception:
            pass

    async def request_wechat_login(self) -> None:
        """/wxlogin: retry the initial login, or log in again after WeChat signed this device out."""
        if self.wx_login_lock.locked():
            await self.notify("正在登录中，请扫描上面的二维码。")
            return
        if not self.wx_ready:
            self.wx_login_requested.set()
            return
        try:
            if await self.wechat_login():
                self.wx_state = "online"
        except (httpx.HTTPError, WeChatError) as e:
            await self.notify(f"微信登录出错：{e}")

    async def wechat_monitor(self, interval: float = 60.0) -> None:
        """Tell the owner when WeChat goes offline or gets signed out, and when it is back."""
        failures = 0
        while True:
            await asyncio.sleep(interval)
            try:
                info = await self.wx.account_info()
                failures = 0
            except (httpx.HTTPError, WeChatError) as e:
                failures += 1
                if failures == 3:
                    await self.notify(f"⚠️ 连不上 agent-wechat 守护进程（{e}）。请检查 agent-wechat 容器。")
                continue
            auth, conn = info.get("authenticated"), info.get("connection")
            if not auth:
                if self.wx_state != "signed_out":
                    self.wx_state = "signed_out"
                    await self.notify("⚠️ 微信已退出登录（可能在手机上被移除了这台设备）。发送 /wxlogin 重新扫码登录。")
            elif conn == "disconnected":
                try:
                    await self.wx.connect()
                except WeChatError:
                    pass
                if self.wx_state == "online":
                    self.wx_state = "disconnected"
                    await self.notify("⚠️ 微信连接断开，正在尝试重连…")
            elif conn == "online" and self.wx_state != "online":
                self.wx_state = "online"
                await self.notify("✅ 微信已恢复在线。")
