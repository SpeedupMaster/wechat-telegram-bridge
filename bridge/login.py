"""Setup through the bot: Telegram account login (pairing code + QR or phone code) and WeChat QR login."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import random
import re
import secrets
import time
import uuid

import httpx
from telethon import Button, errors, utils

from .wechat import NoAccountYet, WeChatError

log = logging.getLogger("login")

LOGIN_DONE_STATES = {"succeeded", "failed", "expired", "rejected", "cancelled", "needs_login"}
MAX_PAIRING_FAILURES = 10


def qr_png(data: str) -> io.BytesIO:
    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=3)
    buf.seek(0)
    buf.name = "qr.png"
    return buf


def _describe_code_delivery(sent) -> str:
    kind = type(getattr(sent, "type", None)).__name__
    return {
        "SentCodeTypeApp": "Telegram 客户端内（系统通知消息）",
        "SentCodeTypeSms": "手机短信",
        "SentCodeTypeCall": "语音电话",
        "SentCodeTypeFragmentSms": "Fragment 短信",
        "SentCodeTypeFirebaseSms": "手机短信",
    }.get(kind, "你的 Telegram（或手机短信）")


def _fmt_zh_seconds(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        m, rem = divmod(s, 60)
        return f"{m} 分 {rem} 秒" if rem else f"{m} 分钟"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h} 小时 {m} 分钟" if m else f"{h} 小时"


def _is_op_in_progress(err: BaseException) -> bool:
    return isinstance(err, WeChatError) and err.status == 409 and err.code == "operation_in_progress"


def _is_wx_restoring(info: dict) -> bool:
    conn = str(info.get("connection") or "").lower()
    state = str(info.get("state") or "").lower()
    return conn in ("connecting", "reconnecting") or state in ("starting", "connecting", "reconnecting")


def _format_login_failure(state: str, res: dict, *, silent: bool = False) -> tuple[str, str]:
    """Return (friendly_summary, diagnostic_details) for a terminal login operation.

    Failed login results include `service` (rejected sign-in step) and `serviceCode`
    (WeChat numeric rejection code) when WeChat refused the step.
    """
    code = res.get("code")
    reason = code or res.get("reason") or state
    friendly = {
        "cancelled": "已取消",
        "expired": "登录请求已过期" if silent else "二维码或确认已过期",
        "rejected": "已在手机上取消登录",
        "timeout": "登录超时",
        "needs_login": "需要重新验证登录",
        "failed": "登录失败",
    }.get(str(reason), str(reason))
    service = res.get("service")
    service_code = res.get("serviceCode")
    details = ""
    if service is not None or service_code is not None:
        parts = [str(p) for p in (code or reason, service, service_code) if p is not None and str(p) != ""]
        details = " · ".join(parts)
    return friendly, details


class LoginMixin:
    """Mixed into Bridge. Expects self.user, self.bot, self.wx, self.store, self.cfg."""

    def _init_login(self) -> None:
        self.tg_ready = asyncio.Event()
        self.pairing_code: str | None = None
        self.pairing_failures = 0
        self.login_owner: int | None = None  # Telegram user id that entered the pairing code
        self.login_step: str | None = None  # None / "method" / "phone" / "code" / "password" / "qr"
        self.login_phone = ""
        self.login_phone_code_hash: str | None = None
        self.login_delivery = "你的 Telegram（或手机短信）"
        self.login_code_buf = ""
        self.login_code_msg_id: int | None = None
        self.login_msgs: list[int] = []  # QR messages to clean up
        self.qr_task: asyncio.Task | None = None
        self.wx_ready = False
        self.wx_login_lock = asyncio.Lock()
        self.wx_login_requested = asyncio.Event()
        self.wx_state = "unknown"
        self.owner_id: int | None = None
        self.login_poll_interval = 2.0
        self.wx_startup_poll_interval = 2.0
        self.wx_startup_wait_timeout = 60.0
        self.wx_login_busy_timeout = 30.0
        self.wx_idle_auth_check_interval = 15.0
        self._wx_login_phase = ""
        self._wx_force_qr_restart = False
        self._wx_force_qr_next = False
        self._wx_login_interrupt = asyncio.Event()
        self._wx_login_task: asyncio.Task | None = None
        self._wx_auto_relogin_task: asyncio.Task | None = None
        self._wx_auto_relogin_cancel = asyncio.Event()
        self._wx_auto_relogin_phase = ""  # "" / "waiting" / "running"
        self._wx_auto_relogin_attempted = False
        self._wx_auto_relogin_due_at: float | None = None

    # ======================================================================
    # Telegram account login, driven from the bot's private chat
    # ======================================================================
    def start_pairing(self) -> None:
        self.pairing_code = secrets.token_hex(4)
        banner = "=" * 64
        why = ("The bridge has no owner yet (topics mode)." if getattr(self, "topics", False)
               else "Telegram account is not logged in.")
        log.warning("\n%s\n  %s\n  Open a private chat with @%s and send:\n\n"
                    "      /login %s\n\n  PAIRING CODE: %s\n%s",
                    banner, why, self.bot_me.username, self.pairing_code, self.pairing_code, banner)

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
                await event.reply("配对码不对。请在服务器日志中查看（docker compose logs telegram-bridge）。")
                return
            self.login_owner = sender
            if getattr(self, "topics", False):
                await self._pair_topics_owner(event)
                return
            await self._ask_method(event)
            return
        if sender != self.login_owner:
            if text == "/start":
                await event.reply("这个桥还没有完成配对。请先发送 /login 配对码（在服务器日志里）。")
            return
        step = self.login_step
        if step == "phone":
            msg = getattr(event, "message", None)
            contact = getattr(msg, "contact", None) or getattr(event, "contact", None)
            if contact and getattr(contact, "phone_number", None):
                phone = str(contact.phone_number).strip()
                if not phone.startswith("+"):
                    phone = "+" + phone
                await self._delete_quietly(event)
                await self._send_tg_code(event.chat_id, phone)
                return
            if text == "📷 改用扫码登录":
                await self.bot.send_message(event.chat_id, "🔄 已切换为扫码登录：", buttons=Button.clear())
                await self._start_tg_qr(event.chat_id)
                return
            if text == "⬅️ 返回选择方式":
                await self.bot.send_message(event.chat_id, "已返回登录方式选择：", buttons=Button.clear())
                await self._ask_method(event)
                return
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
        data = (event.data or b"").decode("utf-8", errors="ignore")
        if data == "tg:method":
            was_phone = self.login_step == "phone"
            self._cancel_qr_task()
            self.login_code_buf = ""
            await event.answer()
            if was_phone:
                await self.bot.send_message(event.chat_id, "已返回登录方式选择：", buttons=Button.clear())
            await self._ask_method_chat(event.chat_id, edit_event=None if was_phone else event)
            return
        if data == "tg:qr":
            was_phone = self.login_step == "phone"
            await event.answer("正在生成二维码…")
            if was_phone:
                await self.bot.send_message(event.chat_id, "🔄 已切换为扫码登录：", buttons=Button.clear())
            await self._start_tg_qr(event.chat_id)
            return
        if data == "tg:phone":
            self._cancel_qr_task()
            self.login_code_buf = ""
            await event.answer()
            await self._ask_phone(event.chat_id)
            return
        if data == "tg:resend":
            if not self.login_phone:
                await event.answer("请先输入手机号", alert=True)
                await self._ask_phone(event.chat_id)
                return
            await event.answer("正在重新发送验证码…")
            await self._send_tg_code(event.chat_id, self.login_phone)
            return
        if data.startswith("tg:k:"):
            await self._on_code_key(event, data[5:])
            return
        await event.answer()

    def _cancel_qr_task(self) -> None:
        if self.qr_task and not self.qr_task.done():
            self.qr_task.cancel()

    def _spawn_login_task(self, coro) -> asyncio.Task:
        spawn = getattr(self, "_spawn_bg", None)
        return spawn(coro) if callable(spawn) else asyncio.create_task(coro)

    async def _start_tg_qr(self, chat_id: int) -> None:
        self._cancel_qr_task()
        self.login_step = "qr"
        self.qr_task = self._spawn_login_task(self._login_qr(chat_id))

    @staticmethod
    def _method_text() -> str:
        return (
            "🔗 <b>配对成功</b>\n\n"
            "接下来登录<b>你的 Telegram 账号</b>（用于自动创建微信群聊与同步状态），请选择方式：\n\n"
            "• <b>💬 验证码登录（单台手机推荐）</b>：支持一键使用当前手机号，并在机器人键盘上点按输入验证码，"
            "不会发到聊天记录里，避免被 Telegram 判定泄露作废。\n"
            "• <b>📷 扫码登录</b>：需在另一台设备（电脑版或网页版）打开本对话，再用手机 Telegram「设置 → 设备 → 关联桌面设备」扫码。"
        )

    @staticmethod
    def _method_buttons() -> list[list]:
        return [[Button.inline("💬 验证码登录（推荐）", b"tg:phone"), Button.inline("📷 扫码登录", b"tg:qr")]]

    async def _ask_method(self, event) -> None:
        self.login_step = "method"
        await event.reply(self._method_text(), parse_mode="html", buttons=self._method_buttons())

    async def _ask_method_chat(self, chat_id: int, *, edit_event=None) -> None:
        self.login_step = "method"
        if edit_event is not None and hasattr(edit_event, "edit"):
            try:
                await edit_event.edit(self._method_text(), parse_mode="html", buttons=self._method_buttons())
                return
            except Exception:
                pass
        await self.bot.send_message(chat_id, self._method_text(), parse_mode="html", buttons=self._method_buttons())

    async def _ask_phone(self, chat_id: int) -> None:
        self.login_step = "phone"
        await self.bot.send_message(
            chat_id,
            "📱 <b>第 1 步：输入手机号</b>\n\n"
            "• 如果是当前正在使用的 Telegram 账号，可直接点底部「<b>📱 一键使用当前手机号</b>」。\n"
            "• 也可以手动输入带国家码的手机号（例如 <code>+8613800000000</code>）。",
            parse_mode="html",
            buttons=[
                [Button.request_phone("📱 一键使用当前手机号", resize=True, single_use=True)],
                [Button.text("📷 改用扫码登录", resize=True, single_use=True),
                 Button.text("⬅️ 返回选择方式", resize=True, single_use=True)],
            ],
        )

    def _code_prompt_text(self, notice: str = "") -> str:
        slots = " ".join(list(self.login_code_buf) + ["_"] * max(0, 5 - len(self.login_code_buf)))
        head = f"{notice}\n\n" if notice else ""
        return (
            f"{head}"
            "📩 <b>第 2 步：输入验证码</b>\n"
            f"已发送至：{html.escape(self.login_delivery)}（<code>{html.escape(self.login_phone)}</code>）\n\n"
            "请点按下方<b>数字键盘</b>输入（不发聊天消息，防泄露作废）：\n"
            f"当前输入：<code>{slots}</code>\n\n"
            "💡 也可以直接回复用空格隔开的数字（如 <code>1 2 3 4 5</code>）。"
        )

    def _code_keypad_buttons(self) -> list[list]:
        submit = (
            Button.inline("✅ 确认登录", b"tg:k:submit")
            if len(self.login_code_buf) >= 4
            else Button.inline("⏳ 请输入验证码", b"tg:k:noop")
        )
        return [
            [Button.inline("1", b"tg:k:1"), Button.inline("2", b"tg:k:2"), Button.inline("3", b"tg:k:3")],
            [Button.inline("4", b"tg:k:4"), Button.inline("5", b"tg:k:5"), Button.inline("6", b"tg:k:6")],
            [Button.inline("7", b"tg:k:7"), Button.inline("8", b"tg:k:8"), Button.inline("9", b"tg:k:9")],
            [Button.inline("⌫ 退格", b"tg:k:back"), Button.inline("0", b"tg:k:0"), Button.inline("🗑 清空", b"tg:k:clear")],
            [submit],
            [Button.inline("🔄 重发", b"tg:resend"), Button.inline("📱 换号", b"tg:phone"),
             Button.inline("📷 扫码", b"tg:qr")],
        ]

    async def _send_code_keypad(self, chat_id: int, notice: str = "") -> None:
        msg = await self.bot.send_message(
            chat_id, self._code_prompt_text(notice), parse_mode="html", buttons=self._code_keypad_buttons())
        self.login_code_msg_id = getattr(msg, "id", None)

    async def _refresh_code_keypad(self, event, notice: str = "") -> None:
        text = self._code_prompt_text(notice)
        buttons = self._code_keypad_buttons()
        if hasattr(event, "edit"):
            try:
                await event.edit(text, parse_mode="html", buttons=buttons)
                return
            except errors.MessageNotModifiedError:
                return
            except Exception:
                pass
        if self.login_code_msg_id:
            try:
                await self.bot.edit_message(event.chat_id, self.login_code_msg_id, text,
                                            parse_mode="html", buttons=buttons)
                return
            except Exception:
                pass
        await self._send_code_keypad(event.chat_id, notice)

    async def _on_code_key(self, event, key: str) -> None:
        if self.login_step != "code":
            await event.answer("当前不在验证码输入阶段，请重新操作", alert=True)
            return
        if key == "noop":
            await event.answer("请先在键盘上输入至少 4 位验证码")
            return
        if key == "submit":
            if not 4 <= len(self.login_code_buf) <= 8:
                await event.answer("请先输入 4-8 位验证码", alert=True)
                return
            await event.answer("正在验证登录…")
            code = self.login_code_buf
            self.login_code_buf = ""
            await self._submit_tg_code(event.chat_id, code, keypad_event=event)
            return
        if key == "back":
            if not self.login_code_buf:
                await event.answer("当前没有已输入的数字")
                return
            self.login_code_buf = self.login_code_buf[:-1]
        elif key == "clear":
            if not self.login_code_buf:
                await event.answer("输入框已是空的")
                return
            self.login_code_buf = ""
        elif len(key) == 1 and key.isdigit():
            if len(self.login_code_buf) >= 8:
                await event.answer("验证码最多 8 位")
                return
            self.login_code_buf += key
        else:
            await event.answer()
            return
        await event.answer()
        await self._refresh_code_keypad(event)

    async def _login_qr(self, chat_id: int) -> None:
        try:
            qr = await self.user.qr_login()
            qr_btn = [[Button.inline("💬 改用验证码登录", b"tg:phone")]]
            for _ in range(6):
                await self._clear_login_msgs(chat_id)
                msg = await self.bot.send_file(
                    chat_id, qr_png(qr.url),
                    caption="📱 请用手机 Telegram「设置 → 设备 → 关联桌面设备」扫描此二维码（过期会自动刷新）。",
                    buttons=qr_btn)
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
            self.login_step = "method"
            await self.bot.send_message(
                chat_id, "⏱ 二维码多次过期，请选择其他方式重试：", buttons=self._method_buttons())
        except errors.SessionPasswordNeededError:
            await self._clear_login_msgs(chat_id)
            self.login_step = "password"
            await self.bot.send_message(
                chat_id,
                "🔐 <b>已扫码确认，请输入两步验证密码</b>\n我读取后会立即删除你的消息。",
                parse_mode="html",
                buttons=[[Button.inline("⬅️ 返回选择方式", b"tg:method")]])
        except asyncio.CancelledError:
            await self._clear_login_msgs(chat_id)
            raise
        except Exception as e:
            log.exception("QR login failed")
            self.login_step = "method"
            await self.bot.send_message(
                chat_id, f"⚠️ 扫码登录出错：{html.escape(str(e))}",
                parse_mode="html", buttons=self._method_buttons())

    async def _login_phone(self, event, text: str) -> None:
        phone = re.sub(r"[^\d+]", "", text)
        if not re.fullmatch(r"\+?\d{6,16}", phone):
            await event.reply(
                "手机号格式不对，请带上国家码，例如 <code>+8613800000000</code>",
                parse_mode="html")
            return
        await self._send_tg_code(event.chat_id, phone)

    async def _send_tg_code(self, chat_id: int, phone: str) -> None:
        await self.bot.send_message(chat_id, "⏳ 正在发送验证码…", buttons=Button.clear())
        try:
            sent = await self.user.send_code_request(phone)
        except errors.FloodWaitError as e:
            await self.bot.send_message(
                chat_id, f"⏳ 请求太频繁，Telegram 要求等待 {e.seconds} 秒后再试。",
                buttons=[[Button.inline("🔄 重试手机号", b"tg:phone"), Button.inline("📷 改用扫码登录", b"tg:qr")]])
            return
        except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError) as e:
            await self.bot.send_message(
                chat_id, f"⚠️ 这个手机号无法登录（{type(e).__name__}），请检查后重试。",
                buttons=[[Button.inline("📱 重新输入手机号", b"tg:phone"), Button.inline("📷 改用扫码登录", b"tg:qr")]])
            return
        self.login_phone = phone
        self.login_phone_code_hash = getattr(sent, "phone_code_hash", None)
        self.login_delivery = _describe_code_delivery(sent)
        self.login_code_buf = ""
        self.login_step = "code"
        await self._send_code_keypad(chat_id)

    async def _login_code(self, event, text: str) -> None:
        code = re.sub(r"\D", "", text)
        await self._delete_quietly(event)
        if not 4 <= len(code) <= 8:
            await self._send_code_keypad(event.chat_id, notice="⚠️ 请输入 4-8 位数字验证码：")
            return
        await self._submit_tg_code(event.chat_id, code)

    async def _clear_code_keypad(self, chat_id: int, status_text: str, keypad_event=None) -> None:
        if keypad_event is not None and hasattr(keypad_event, "edit"):
            try:
                await keypad_event.edit(status_text, buttons=None)
                self.login_code_msg_id = None
                return
            except Exception:
                pass
        if self.login_code_msg_id:
            try:
                await self.bot.edit_message(chat_id, self.login_code_msg_id, status_text, buttons=None)
            except Exception:
                pass
            self.login_code_msg_id = None

    async def _submit_tg_code(self, chat_id: int, code: str, keypad_event=None) -> None:
        sign_kw = {"phone": self.login_phone, "code": code}
        if self.login_phone_code_hash:
            sign_kw["phone_code_hash"] = self.login_phone_code_hash
        try:
            await self.user.sign_in(**sign_kw)
        except errors.SessionPasswordNeededError:
            self.login_step = "password"
            self.login_code_buf = ""
            await self._clear_code_keypad(chat_id, "✅ 验证码校验通过。", keypad_event=keypad_event)
            await self.bot.send_message(
                chat_id,
                "🔐 <b>第 3 步：输入两步验证密码</b>\n请直接发送密码（我读取后会立即删除该消息）。",
                parse_mode="html",
                buttons=[[Button.inline("⬅️ 返回选择方式", b"tg:method")]])
            return
        except errors.PhoneCodeInvalidError:
            self.login_code_buf = ""
            notice = "⚠️ 验证码不正确，请重新输入："
            if keypad_event is not None:
                await self._refresh_code_keypad(keypad_event, notice=notice)
            else:
                await self._send_code_keypad(chat_id, notice=notice)
            return
        except (errors.PhoneCodeExpiredError, errors.SendCodeUnavailableError):
            self.login_step = "phone"
            self.login_code_buf = ""
            msg = "⏱ 验证码已失效，请重新获取或更换手机号。"
            btns = [[Button.inline("🔄 重发验证码", b"tg:resend"), Button.inline("📱 换手机号", b"tg:phone")],
                    [Button.inline("📷 改用扫码登录", b"tg:qr")]]
            if keypad_event is not None and hasattr(keypad_event, "edit"):
                try:
                    await keypad_event.edit(msg, buttons=btns)
                    return
                except Exception:
                    pass
            await self.bot.send_message(chat_id, msg, buttons=btns)
            return
        await self._clear_code_keypad(chat_id, "✅ 验证码校验通过，正在完成登录…", keypad_event=keypad_event)
        await self._tg_login_done(chat_id)

    async def _login_password(self, event, text: str) -> None:
        await self._delete_quietly(event)
        try:
            await self.user.sign_in(password=text)
        except errors.PasswordHashInvalidError:
            await event.respond(
                "⚠️ 两步验证密码不正确，请重新发送密码：",
                buttons=[[Button.inline("⬅️ 返回选择方式", b"tg:method")]])
            return
        await self._tg_login_done(event.chat_id)

    async def _tg_login_done(self, chat_id: int) -> None:
        me = await self.user.get_me()
        self.login_step = None
        self.pairing_code = None
        self.login_code_buf = ""
        self.login_code_msg_id = None
        name = utils.get_display_name(me)
        log.info("Telegram account logged in through the bot: %s", name)
        text = f"✅ <b>Telegram 已登录</b>：{html.escape(name)}\n\n接下来会自动发起微信登录（支持手机确认或扫码）。"
        if me.id != self.login_owner:
            text += ("\n\n⚠️ 注意：登录的不是当前对话账号。之后我只响应登录账号的命令，"
                     "微信群聊与通知也都会在该账号中生成。")
        await self.bot.send_message(chat_id, text, parse_mode="html", buttons=Button.clear())
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

    def _wx_login_busy_hint(self) -> str:
        if self._wx_login_phase == "auto_relogin":
            return "⏳ 正在自动尝试重新登录微信，请稍候…"
        if self._wx_login_phase == "awaiting_scan":
            return "⏳ 正在等待扫码，请扫描上方发送的二维码。"
        if self._wx_login_phase == "awaiting_approval":
            return "⏳ 正在等待手机微信确认，请在手机上点「登录」。"
        return "⏳ 正在发起微信登录，请稍候…"

    def _wx_state_text(self) -> str:
        if self.wx_state == "signed_out":
            if self._wx_auto_relogin_phase == "waiting":
                if self._wx_auto_relogin_due_at is not None:
                    rem = max(0.0, self._wx_auto_relogin_due_at - time.monotonic())
                    if rem >= 1:
                        return f"已退出登录（约 {_fmt_zh_seconds(rem)}后自动尝试登录）"
                return "已退出登录（即将自动尝试登录）"
            if self._wx_auto_relogin_phase == "running":
                return "已退出登录（正在自动尝试登录…）"
        return {
            "online": "在线",
            "disconnected": "连接断开，重连中",
            "signed_out": "已退出登录（发 /wxlogin）",
            "unknown": "未登录（发 /wxlogin）",
        }.get(self.wx_state, self.wx_state)

    def _wx_auto_relogin_status_line(self) -> str:
        if not self.cfg.bridge.auto_relogin:
            return ""
        cd = self._auto_relogin_cooldown_remaining()
        if cd >= 1:
            return f"\n掉线自动登录：已开启（冷却剩余约 {_fmt_zh_seconds(cd)}）"
        return "\n掉线自动登录：已开启"

    def _mark_wx_online(self) -> None:
        self.wx_state = "online"
        self._wx_auto_relogin_attempted = False

    def _last_auto_relogin_at(self) -> float:
        store = getattr(self, "store", None)
        raw = store.get("auto_relogin_last_at") if store is not None else None
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def _record_auto_relogin_attempt(self) -> None:
        store = getattr(self, "store", None)
        if store is not None:
            store.set("auto_relogin_last_at", str(time.time()))

    def _auto_relogin_cooldown_remaining(self) -> float:
        cooldown = self.cfg.bridge.auto_relogin_cooldown_seconds
        if cooldown <= 0:
            return 0.0
        last_at = self._last_auto_relogin_at()
        if last_at <= 0:
            return 0.0
        elapsed = time.time() - last_at
        if elapsed < 0 or elapsed >= cooldown:
            return 0.0
        return cooldown - elapsed

    def _pick_auto_relogin_delay(self) -> float:
        b = self.cfg.bridge
        if b.auto_relogin_delay_max <= b.auto_relogin_delay_min:
            return b.auto_relogin_delay_min
        return random.uniform(b.auto_relogin_delay_min, b.auto_relogin_delay_max)

    def _cancel_auto_relogin(self) -> None:
        self._wx_auto_relogin_cancel.set()
        self._wx_login_interrupt.set()
        self._wx_auto_relogin_due_at = None

    async def _cancel_wx_op_quietly(self, op: dict | None) -> None:
        if not isinstance(op, dict):
            return
        op_id = op.get("id")
        if not op_id or op.get("state") in LOGIN_DONE_STATES:
            return
        try:
            await self.wx.cancel_operation(op_id)
        except (httpx.HTTPError, WeChatError):
            pass

    @staticmethod
    def _wx_login_buttons(*, include_qr: bool = False) -> list[list]:
        row = [Button.inline("🔑 重新登录微信", b"wxlogin:auto")]
        if include_qr:
            row.append(Button.inline("📷 扫码登录", b"wxlogin:qr"))
        return [row]

    @staticmethod
    def _wx_auto_relogin_buttons() -> list[list]:
        return [[
            Button.inline("🔑 立即手动登录", b"wxlogin:auto"),
            Button.inline("⏹ 取消自动登录", b"wxlogin:cancel_auto"),
        ]]

    async def _on_wx_login_callback(self, event, mode: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        if mode == "cancel_auto":
            if self._wx_auto_relogin_phase in ("waiting", "running"):
                self._cancel_auto_relogin()
                await event.answer("已取消本次自动登录")
                msg = "⏹ 已取消本次自动登录。需要时可点击下方按钮或发送 /wxlogin 手动登录。"
                btns = self._wx_login_buttons(include_qr=True)
                if hasattr(event, "edit"):
                    try:
                        await event.edit(msg, buttons=btns)
                        return
                    except Exception:
                        pass
                await self.notify(msg, buttons=btns)
                return
            await event.answer("当前没有进行中的自动登录")
            return
        if mode == "qr":
            if self._wx_auto_relogin_phase == "waiting":
                self._cancel_auto_relogin()
            elif self._wx_auto_relogin_phase == "running":
                self._cancel_auto_relogin()
                await event.answer("正在取消自动登录并切换为扫码登录…")
                self._wx_login_task = self._spawn_login_task(self.request_wechat_login(force_qr=True))
                return
            if self.wx_login_lock.locked():
                if self._wx_login_phase == "awaiting_scan":
                    await event.answer("已在扫码登录中，请扫描上方二维码")
                    return
                self._wx_force_qr_restart = True
                self._wx_login_interrupt.set()
                await event.answer("正在切换为扫码登录…")
                return
            await event.answer("正在获取登录二维码…")
            self._wx_login_task = self._spawn_login_task(self.request_wechat_login(force_qr=True))
            return
        if self._wx_auto_relogin_phase == "waiting":
            self._cancel_auto_relogin()
        if self.wx_login_lock.locked():
            await event.answer(self._wx_login_busy_hint())
            return
        await event.answer("正在发起微信登录…")
        self._wx_login_task = self._spawn_login_task(self.request_wechat_login(force_qr=False))

    async def _finish_authenticated(self, info: dict) -> None:
        if info.get("connection") == "disconnected":
            try:
                await self.wx.connect()
            except WeChatError as e:
                if not _is_op_in_progress(e):
                    raise
        self.wx_ready = True
        self._mark_wx_online()

    async def _wait_wx_busy(self, deadline: float) -> dict | None:
        """Poll account_info() quietly while agent-wechat is restoring/reconnecting on startup."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                info = await self.wx.account_info()
            except (httpx.HTTPError, WeChatError) as e:
                log.debug("account_info during startup wait failed: %s", e)
                info = None
            if info is not None and (info.get("authenticated") or not _is_wx_restoring(info)):
                return info
            remaining = deadline - loop.time()
            if remaining <= 0:
                return info
            await asyncio.sleep(min(self.wx_startup_poll_interval, remaining))

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
        loop = asyncio.get_running_loop()
        busy_deadline: float | None = None
        while True:
            force_qr = False
            try:
                info = await self.wx.account_info()
                if not info.get("authenticated") and _is_wx_restoring(info):
                    if busy_deadline is None:
                        busy_deadline = loop.time() + self.wx_startup_wait_timeout
                        log.info("agent-wechat is restoring/connecting (%s); waiting quietly",
                                 info.get("connection") or info.get("state"))
                    waited = await self._wait_wx_busy(busy_deadline)
                    if waited is not None:
                        info = waited
                if info.get("authenticated"):
                    await self._finish_authenticated(info)
                    return
                force_qr = self._wx_force_qr_next
                self._wx_force_qr_next = False
                if await self.wechat_login(
                    force_qr=force_qr,
                    start_notice="🔐 微信尚未登录，正在发起登录（请在手机确认或扫码）…",
                ):
                    self.wx_ready = True
                    self._mark_wx_online()
                    return
                busy_deadline = None
            except WeChatError as e:
                if _is_op_in_progress(e):
                    self._wx_force_qr_next = force_qr
                    now = loop.time()
                    if busy_deadline is None:
                        busy_deadline = now + self.wx_startup_wait_timeout
                        log.info("agent-wechat already has an operation in progress; waiting up to %.0fs to settle",
                                 self.wx_startup_wait_timeout)
                    remaining = busy_deadline - now
                    if remaining > 0:
                        await asyncio.sleep(min(self.wx_startup_poll_interval, remaining))
                        continue
                    busy_deadline = None
                    log.warning("WeChat login step still blocked by operation_in_progress after %.0fs",
                                self.wx_startup_wait_timeout)
                    await self.notify(
                        "⚠️ agent-wechat 当前一直有正在进行的登录/重连操作，暂时无法发起新登录。"
                        "请稍等片刻后点击下方按钮重试，也可以发送 /wxlogin 重试。",
                        buttons=self._wx_login_buttons(include_qr=True))
                else:
                    busy_deadline = None
                    log.warning("WeChat login step failed: %s", e)
                    await self.notify(
                        f"⚠️ 微信登录出错：{e}。点击下方按钮重新登录，也可以发送 /wxlogin 重试（/wxlogin qr 改用扫码）。",
                        buttons=self._wx_login_buttons(include_qr=True))
            except httpx.HTTPError as e:
                busy_deadline = None
                log.warning("WeChat login step failed: %s", e)
                await self.notify(
                    f"⚠️ 微信登录出错：{e}。点击下方按钮重新登录，也可以发送 /wxlogin 重试（/wxlogin qr 改用扫码）。",
                    buttons=self._wx_login_buttons(include_qr=True))
            self.wx_login_requested.clear()
            while True:
                if self.wx_idle_auth_check_interval > 0:
                    try:
                        await asyncio.wait_for(
                            self.wx_login_requested.wait(), timeout=self.wx_idle_auth_check_interval)
                        break
                    except asyncio.TimeoutError:
                        try:
                            info = await self.wx.account_info()
                        except (httpx.HTTPError, WeChatError):
                            continue
                        if info.get("authenticated"):
                            await self._finish_authenticated(info)
                            await self.notify("✅ 微信已恢复在线。")
                            return
                else:
                    await self.wx_login_requested.wait()
                    break

    async def _start_login_when_free(
        self, force_qr: bool, wait_timeout: float
    ) -> tuple[dict | None, bool, bool]:
        """Start a WeChat login op, waiting out transient 409 operation_in_progress when `wait_timeout > 0`.

        Returns (operation, already_signed_in, effective_force_qr).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_timeout if wait_timeout > 0 else 0.0
        notified_busy = False
        effective_qr = force_qr
        while True:
            try:
                op = await self.wx.start_login(f"bridge-login-{uuid.uuid4()}", force_qr=effective_qr)
                return op, False, effective_qr
            except WeChatError as e:
                if not _is_op_in_progress(e) or wait_timeout <= 0:
                    raise
                try:
                    info = await self.wx.account_info()
                except (httpx.HTTPError, WeChatError):
                    info = {}
                if info.get("authenticated"):
                    await self._finish_authenticated(info)
                    return None, True, effective_qr
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise
                if not notified_busy:
                    notified_busy = True
                    log.info("WeChat login hit 409 operation_in_progress; waiting up to %.0fs for account to become free",
                             wait_timeout)
                    await self.notify("⏳ agent-wechat 正在处理其他登录/同步任务，请稍候，空闲后会自动继续…")
                sleep_for = min(self.wx_startup_poll_interval, remaining)
                if sleep_for > 0:
                    try:
                        await asyncio.wait_for(self._wx_login_interrupt.wait(), timeout=sleep_for)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
                self._wx_login_interrupt.clear()
                if self._wx_force_qr_restart:
                    self._wx_force_qr_restart = False
                    effective_qr = True

    async def wechat_login(self, force_qr: bool = False, *, start_notice: str = "",
                           wait_busy_timeout: float = 0.0) -> bool:
        if self.wx_login_lock.locked():
            await self.notify(self._wx_login_busy_hint())
            return False
        async with self.wx_login_lock:
            current_force_qr = force_qr
            self._wx_force_qr_restart = False
            self._wx_login_interrupt.clear()
            notice_sent = False
            just_switched_qr = False
            while True:
                loop = asyncio.get_running_loop()
                self._wx_login_phase = "starting"
                busy_timeout = (
                    max(wait_busy_timeout, min(10.0, self.wx_login_busy_timeout))
                    if just_switched_qr else wait_busy_timeout
                )
                just_switched_qr = False
                op, already_signed_in, current_force_qr = await self._start_login_when_free(
                    current_force_qr, busy_timeout
                )
                if already_signed_in:
                    await self.notify("✅ 微信已恢复在线。")
                    return True
                assert op is not None
                if start_notice and not notice_sent and op.get("state") != "succeeded":
                    notice_sent = True
                    await self.notify(start_notice)
                last_gen, qr_msg, approval_since, hinted = None, None, None, False
                switched_to_qr = False
                deadline = loop.time() + 660
                try:
                    while loop.time() < deadline:
                        state, res = op.get("state"), op.get("result") or {}
                        if state == "succeeded":
                            info = await self.wx.account_info()
                            await self._finish_authenticated(info)
                            await self.notify("✅ 微信已登录！之后收到的消息会自动建群并转发到这里。")
                            return True
                        if state in LOGIN_DONE_STATES:
                            if self._wx_force_qr_restart:
                                self._wx_force_qr_restart = False
                                current_force_qr = True
                                switched_to_qr = True
                                break
                            friendly, details = _format_login_failure(state, res, silent=False)
                            log.warning("WeChat login did not complete (state=%s, details=%s)",
                                        state, details or friendly)
                            lines = [f"⚠️ 微信登录未完成（{friendly}）。"]
                            if details:
                                lines.append(f"详情：{details}")
                            if not current_force_qr and last_gen is None and state != "cancelled":
                                lines.append("复用登录未成功，建议点击「📷 扫码登录」（同账号扫码，保留聊天记录），"
                                             "或点击「🔑 重新登录微信」重试。")
                            else:
                                lines.append("点击下方按钮重新登录或改用扫码登录，也可以发送 /wxlogin 重试。")
                            await self.notify("\n".join(lines), buttons=self._wx_login_buttons(include_qr=True))
                            return False
                        gen = res.get("qrGeneration")
                        if state == "awaiting_scan":
                            self._wx_login_phase = "awaiting_scan"
                            if res.get("qrAvailable") and gen != last_gen:
                                try:
                                    png = await self.wx.login_qr(op["id"], gen)
                                except WeChatError:
                                    png = None
                                if png:
                                    if qr_msg:
                                        await self._delete_owner_msg(qr_msg)
                                    f = io.BytesIO(png)
                                    f.name = "wechat-qr.png"
                                    qr_msg = await self.notify(
                                        "📱 请用手机微信「扫一扫」扫描此二维码登录（过期会自动刷新）。", file=f)
                                    last_gen = gen
                        if state == "awaiting_approval":
                            self._wx_login_phase = "awaiting_approval"
                            if approval_since is None:
                                approval_since = loop.time()
                                if last_gen is not None:
                                    await self.notify("✅ 已扫码，请在手机微信上点「登录」确认。")
                                else:
                                    await self.notify(
                                        "📱 已向手机微信发送登录确认请求，请在手机微信上点「登录」确认。",
                                        buttons=[[Button.inline("📷 改用扫码登录", b"wxlogin:qr")]])
                            elif loop.time() - approval_since > 90 and not hinted:
                                hinted = True
                                await self.notify(
                                    "⏳ 手机确认后一直没反应？\n部分设备确认登录需要手机直连服务器的 TCP "
                                    "<code>8050-8100</code> 端口，可在 VPS 安全组/防火墙临时放行后再试，"
                                    "登录成功后再关闭。",
                                    parse_mode="html")
                        if self.login_poll_interval > 0:
                            try:
                                await asyncio.wait_for(
                                    self._wx_login_interrupt.wait(), timeout=self.login_poll_interval)
                            except asyncio.TimeoutError:
                                pass
                        else:
                            await asyncio.sleep(0)
                        self._wx_login_interrupt.clear()
                        if self._wx_force_qr_restart:
                            self._wx_force_qr_restart = False
                            await self._cancel_wx_op_quietly(op)
                            await self.notify("🔄 正在切换为扫码登录…")
                            current_force_qr = True
                            switched_to_qr = True
                            break
                        op = await self.wx.operation(op["id"])
                    if switched_to_qr:
                        just_switched_qr = True
                        continue
                    await self.notify(
                        "⏱ 微信登录超时，手机上一直没有确认。点击下方按钮重新登录或改用扫码登录，也可以发送 /wxlogin 重试。",
                        buttons=self._wx_login_buttons(include_qr=True))
                    await self._cancel_wx_op_quietly(op)
                    return False
                finally:
                    self._wx_login_phase = ""
                    if qr_msg:
                        await self._delete_owner_msg(qr_msg)

    async def _delete_owner_msg(self, msg) -> None:
        try:
            await self.bot.delete_messages(self.owner_id, [msg.id])
        except Exception:
            pass

    async def request_wechat_login(self, force_qr: bool = False) -> None:
        """/wxlogin or login button: retry initial login or re-authenticate after WeChat signs out."""
        if self._wx_auto_relogin_phase == "waiting":
            self._cancel_auto_relogin()
        elif self._wx_auto_relogin_phase == "running" and force_qr:
            self._cancel_auto_relogin()
        if force_qr and self._wx_auto_relogin_cancel.is_set():
            task = self._wx_auto_relogin_task
            if task and not task.done():
                await asyncio.gather(task, return_exceptions=True)
        if self.wx_login_lock.locked():
            if force_qr and self._wx_login_phase != "awaiting_scan":
                self._wx_force_qr_restart = True
                self._wx_login_interrupt.set()
                await self.notify("🔄 正在切换为扫码登录…")
            else:
                await self.notify(self._wx_login_busy_hint())
            return
        if not self.wx_ready:
            self._wx_force_qr_next = force_qr
            self.wx_login_requested.set()
            return
        try:
            if await self.wechat_login(force_qr=force_qr, wait_busy_timeout=self.wx_login_busy_timeout):
                self._mark_wx_online()
        except (httpx.HTTPError, WeChatError) as e:
            if _is_op_in_progress(e):
                try:
                    info = await self.wx.account_info()
                except (httpx.HTTPError, WeChatError):
                    info = {}
                if info.get("authenticated"):
                    await self._finish_authenticated(info)
                    await self.notify("✅ 微信已恢复在线。")
                    return
                await self.notify(
                    "⏳ agent-wechat 当前已有正在进行的登录/重连操作，请稍等几秒后再点击重试。",
                    buttons=self._wx_login_buttons(include_qr=True))
                return
            await self.notify(
                f"⚠️ 微信登录出错：{e}。点击下方按钮重新登录，也可以发送 /wxlogin 重试（/wxlogin qr 改用扫码）。",
                buttons=self._wx_login_buttons(include_qr=True))

    async def _on_wx_signed_out(self) -> None:
        """Handle transition into signed_out: optionally schedule a single delayed auto-relogin."""
        if self.wx_login_lock.locked():
            return
        if not self.cfg.bridge.auto_relogin:
            await self.notify(
                "⚠️ 微信已退出登录（可能在手机上移除了这台设备），点击下方按钮或发送 /wxlogin 重新登录。",
                buttons=self._wx_login_buttons(include_qr=True))
            return
        if self._wx_auto_relogin_attempted:
            await self.notify(
                "⚠️ 微信已退出登录。本次掉线已尝试过自动登录，请点击下方按钮或发送 /wxlogin 手动登录。",
                buttons=self._wx_login_buttons(include_qr=True))
            return
        rem = self._auto_relogin_cooldown_remaining()
        if rem > 0:
            self._wx_auto_relogin_attempted = True
            cooldown = self.cfg.bridge.auto_relogin_cooldown_seconds
            log.info("Skipping auto-relogin due to cooldown (%.0fs remaining)", rem)
            await self.notify(
                f"⚠️ 微信已退出登录。距离上次自动登录不足 {_fmt_zh_seconds(cooldown)}"
                f"（冷却剩余约 {_fmt_zh_seconds(rem)}），为避免触发风控，本次不会自动登录。"
                "请点击下方按钮或发送 /wxlogin 手动登录。",
                buttons=self._wx_login_buttons(include_qr=True))
            return
        self._wx_auto_relogin_attempted = True
        delay = self._pick_auto_relogin_delay()
        self._wx_auto_relogin_cancel.clear()
        self._wx_auto_relogin_phase = "waiting"
        self._wx_auto_relogin_due_at = time.monotonic() + delay
        when = f"约 {_fmt_zh_seconds(delay)}后" if delay >= 1 else "稍后"
        log.info("WeChat signed out; scheduling one-shot auto-relogin in %.1fs", delay)
        await self.notify(
            f"⚠️ 微信已退出登录，将在{when}自动尝试重新登录一次"
            "（需手机微信已开启「自动登录该设备」；失败则不再自动重试）。\n"
            "你也可以点击下方按钮立即手动登录，或取消本次自动登录。",
            buttons=self._wx_auto_relogin_buttons())
        if self._wx_auto_relogin_cancel.is_set():
            self._wx_auto_relogin_phase = ""
            self._wx_auto_relogin_due_at = None
            return
        self._wx_auto_relogin_task = asyncio.create_task(self._run_auto_relogin(delay))

    async def _run_auto_relogin(self, delay: float) -> None:
        """Wait a human-like randomized delay, re-verify signed-out state, then try auto-login once."""
        try:
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wx_auto_relogin_cancel.wait(), timeout=delay)
                    log.info("Auto-relogin cancelled before starting")
                    return
                except asyncio.TimeoutError:
                    pass
            if self._wx_auto_relogin_cancel.is_set() or self.wx_state == "online":
                return
            if self.wx_login_lock.locked():
                log.info("Skipping auto-relogin: a WeChat login is already in progress")
                return
            try:
                info = await self.wx.account_info()
            except (httpx.HTTPError, WeChatError) as e:
                log.warning("Auto-relogin pre-check failed: %s", e)
                await self.notify(
                    f"⚠️ 微信自动登录失败（无法检查账号状态：{e}），不再自动重试。"
                    "请点击下方按钮或发送 /wxlogin 手动登录。",
                    buttons=self._wx_login_buttons(include_qr=True))
                return
            if self._wx_auto_relogin_cancel.is_set():
                return
            if info.get("authenticated"):
                if info.get("connection") == "disconnected":
                    try:
                        await self.wx.connect()
                    except (httpx.HTTPError, WeChatError):
                        pass
                self._mark_wx_online()
                await self.notify("✅ 微信已恢复在线，无需自动重新登录。")
                return
            if _is_wx_restoring(info):
                log.info("Skipping auto-relogin: agent-wechat is already reconnecting/restoring")
                self.wx_state = "disconnected"
                self._wx_auto_relogin_attempted = False
                return
            if self.wx_login_lock.locked():
                log.info("Skipping auto-relogin: a WeChat login started during pre-check")
                return
            async with self.wx_login_lock:
                if self._wx_auto_relogin_cancel.is_set() or self.wx_state == "online":
                    return
                self._wx_auto_relogin_phase = "running"
                self._wx_auto_relogin_due_at = None
                self._record_auto_relogin_attempt()
                ok, reason = await self._attempt_silent_wechat_login()
            if ok:
                await self.notify("✅ 微信掉线后已自动重新登录并恢复在线。")
            elif reason != "user_cancelled":
                await self.notify(
                    f"⚠️ 微信自动登录失败（{reason}），不再自动重试。"
                    "请点击下方按钮或发送 /wxlogin 手动登录。",
                    buttons=self._wx_login_buttons(include_qr=True))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Unexpected error during auto-relogin")
            await self.notify(
                f"⚠️ 微信自动登录出错（{e}），不再自动重试。"
                "请点击下方按钮或发送 /wxlogin 手动登录。",
                buttons=self._wx_login_buttons(include_qr=True))
        finally:
            self._wx_auto_relogin_phase = ""
            self._wx_auto_relogin_due_at = None

    async def _wait_silent_auth_settle(self, deadline: float) -> tuple[bool, str]:
        """Wait for an already-running daemon operation (409 operation_in_progress) to authenticate."""
        loop = asyncio.get_running_loop()
        while True:
            if self._wx_auto_relogin_cancel.is_set():
                return False, "user_cancelled"
            try:
                info = await self.wx.account_info()
            except (httpx.HTTPError, WeChatError):
                info = None
            if info and info.get("authenticated"):
                connected = True
                if info.get("connection") == "disconnected":
                    try:
                        await self.wx.connect()
                    except (httpx.HTTPError, WeChatError) as e:
                        if not _is_op_in_progress(e):
                            log.warning("Post-settle connect check failed: %s", e)
                            connected = False
                if connected:
                    self._mark_wx_online()
                else:
                    self.wx_state = "disconnected"
                    self._wx_auto_relogin_attempted = False
                return True, ""
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, "守护进程当前有正在进行的登录/重连操作，请稍后手动重试"
            if self.login_poll_interval > 0:
                try:
                    await asyncio.wait_for(
                        self._wx_login_interrupt.wait(),
                        timeout=min(self.login_poll_interval, remaining),
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)
            self._wx_login_interrupt.clear()

    async def _attempt_silent_wechat_login(self) -> tuple[bool, str]:
        """Single non-interactive login attempt using strategy='auto'.

        Succeeds when mobile WeChat has '自动登录该设备' enabled. Cancels the daemon operation
        immediately on QR fallback or timeout so it never loops or refreshes QR codes unattended.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.bridge.auto_relogin_timeout_seconds
        self._wx_login_phase = "auto_relogin"
        self._wx_login_interrupt.clear()
        op: dict | None = None
        saw_approval = False
        try:
            try:
                op = await self.wx.start_login(f"bridge-login-{uuid.uuid4()}", force_qr=False)
            except WeChatError as e:
                if _is_op_in_progress(e):
                    log.info("Auto-relogin saw operation_in_progress; waiting for in-flight auth to settle")
                    return await self._wait_silent_auth_settle(deadline)
                raise
            while True:
                if self._wx_auto_relogin_cancel.is_set():
                    await self._cancel_wx_op_quietly(op)
                    return False, "user_cancelled"
                state, res = op.get("state"), op.get("result") or {}
                if state == "succeeded":
                    op = None
                    connected = True
                    try:
                        info = await self.wx.account_info()
                        if info.get("connection") == "disconnected":
                            await self.wx.connect()
                    except (httpx.HTTPError, WeChatError) as e:
                        if not _is_op_in_progress(e):
                            log.warning("Auto-relogin succeeded but post-login connect check failed: %s", e)
                            connected = False
                    if connected:
                        self._mark_wx_online()
                    else:
                        self.wx_state = "disconnected"
                        self._wx_auto_relogin_attempted = False
                    return True, ""
                if state == "awaiting_scan":
                    await self._cancel_wx_op_quietly(op)
                    return False, "当前需要扫码登录，无法自动完成"
                if state in LOGIN_DONE_STATES:
                    friendly, details = _format_login_failure(state, res, silent=True)
                    log.warning("WeChat auto-relogin did not complete (state=%s, details=%s)",
                                state, details or friendly)
                    if details:
                        reason = res.get("code") or res.get("reason") or state
                        return False, details if friendly == str(reason) else f"{friendly}（{details}）"
                    return False, friendly
                if state == "awaiting_approval":
                    saw_approval = True
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self._cancel_wx_op_quietly(op)
                    if saw_approval:
                        return False, "等待自动登录超时，可能需要手机端手动确认（请确认手机微信已开启「自动登录该设备」）"
                    return False, "自动登录超时"
                if self.login_poll_interval > 0:
                    try:
                        await asyncio.wait_for(
                            self._wx_login_interrupt.wait(),
                            timeout=min(self.login_poll_interval, remaining),
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
                self._wx_login_interrupt.clear()
                if self._wx_auto_relogin_cancel.is_set():
                    await self._cancel_wx_op_quietly(op)
                    return False, "user_cancelled"
                op = await self.wx.operation(op["id"])
        except asyncio.CancelledError:
            await self._cancel_wx_op_quietly(op)
            raise
        except (httpx.HTTPError, WeChatError) as e:
            log.warning("WeChat auto-relogin failed: %s", e)
            await self._cancel_wx_op_quietly(op)
            return False, str(e)
        finally:
            self._wx_login_phase = ""

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
                if _is_wx_restoring(info):
                    if self.wx_state == "online":
                        self.wx_state = "disconnected"
                        await self.notify("⚠️ 微信正在重连或恢复登录态，请稍候…")
                    continue
                if self.wx_state != "signed_out":
                    # Guard against a brief unauthenticated window right after agent-wechat restarts.
                    confirm_wait = min(5.0, interval / 2.0) if interval >= 1.0 else 0.0
                    if confirm_wait > 0:
                        await asyncio.sleep(confirm_wait)
                        try:
                            info2 = await self.wx.account_info()
                        except (httpx.HTTPError, WeChatError):
                            continue
                        if info2.get("authenticated") or _is_wx_restoring(info2):
                            continue
                    self.wx_state = "signed_out"
                    await self._on_wx_signed_out()
            elif conn == "disconnected":
                if self._wx_auto_relogin_phase == "waiting":
                    self._cancel_auto_relogin()
                if not self.wx_login_lock.locked():
                    try:
                        await self.wx.connect()
                    except WeChatError:
                        pass
                if self.wx_state == "online":
                    self.wx_state = "disconnected"
                    await self.notify("⚠️ 微信连接断开，正在尝试重连…")
            elif conn == "online" and self.wx_state != "online":
                if self._wx_auto_relogin_phase == "waiting":
                    self._cancel_auto_relogin()
                if not self.wx_login_lock.locked():
                    self._mark_wx_online()
                    await self.notify("✅ 微信已恢复在线。")
