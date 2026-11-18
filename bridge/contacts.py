"""WeChat friend requests and contact settings (agent-wechat >= 2.7), shown as Telegram cards with buttons."""
from __future__ import annotations

import hashlib
import html
import logging
import secrets
import time

from telethon import Button

from .wechat import OP_OK, WeChatError

log = logging.getLogger("contacts")

# (privacy field, button label, card label)
PRIVACY_FLAGS = (
    ("chatsOnly", "仅聊天", "🔒 仅聊天"),
    ("hideMyPosts", "不让 TA 看我的朋友圈", "🙈 不让 TA 看我的朋友圈"),
    ("hideTheirPosts", "不看 TA 的朋友圈", "🚫 不看 TA 的朋友圈"),
)
CHATS_ONLY = {"chatsOnly": True, "hideMyPosts": False, "hideTheirPosts": False}

CONTACT_ERRORS = {
    "stale_contact_request": "这条请求刚刚有了变化，请重新查看",
    "contact_request_not_actionable": "这条请求已经无法通过（可能已过期，或你们已经是好友）",
    "stale_contact_privacy": "权限刚在别处改过，已刷新，请重试",
    "contact_privacy_unresolved": "暂时读不到和 TA 的权限设置",
    "contact_privacy_pending": "上一次权限修改还在处理中，请稍后再试",
    "contact_privacy_partial": "只有部分权限改成功了，请刷新后确认",
    "contact_flags_unresolved": "暂时无法确认和 TA 的好友关系",
    "stale_recipient_reference": "搜索结果已过期，请重新发送 /add",
    "invalid_recipient_reference": "搜索结果已过期，请重新发送 /add",
    "contact_discovery_rejected": "微信没有找到这个用户（或对方不允许通过微信号搜到自己）",
    "invalid_contact_input": "输入格式不对（太长或含有特殊字符）",
    "contact_operation_interrupted": "操作被中断，请在手机微信上确认结果",
    "needs_review": "结果未知，请在手机微信上确认，不要重复操作",
    "not_found": "找不到这条记录",
    "profile_unavailable": "微信资料暂时不可读，请稍后再试",
    "auth_required": "微信未登录",
    "idempotency_key_expired": "操作已过期，请重新操作",
    "operation_expired": "操作已过期，请重新操作",
}

RELATIONS = {"friend": "✅ 好友", "non_contact": "⚠️ 不是好友", "unknown": "❔ 未知"}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def friendly(code: str | None) -> str:
    code = str(code or "unknown")
    return CONTACT_ERRORS.get(code, code)


def op_error(op: dict) -> str:
    """Why a finished contact operation did not succeed, in words."""
    state = op.get("state") or "unknown"
    if state == "needs_review":
        return CONTACT_ERRORS["needs_review"]
    result = op.get("result") if isinstance(op.get("result"), dict) else {}
    err = op.get("error") if isinstance(op.get("error"), dict) else {}
    return friendly(result.get("code") or op.get("reason") or err.get("code") or state)


def ago(ts) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    secs = max(0, int(time.time()) - ts)
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    if secs < 86400:
        return f"{secs // 3600} 小时前"
    if secs < 30 * 86400:
        return f"{secs // 86400} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def one_line(text, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + "…" if len(text) > limit else text


def _digest(*parts) -> str:
    """Stable idempotency-key part: the same press on the same request version is one operation."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


class ContactsMixin:
    """Friend-request notifications, /requests, /add and the /contact card. Mixed into Bridge."""

    # --- events ---------------------------------------------------------------
    async def on_friend_request_event(self, data: dict) -> None:
        # Our own accept/delete/send also logs an event (with operationId); the card already shows it.
        if not self.cfg.bridge.friend_requests or data.get("operationId"):
            return
        rid = data.get("requestId")
        if not rid:
            return
        try:
            req = await self.wx.contact_request(str(rid))
        except WeChatError as e:
            log.info("Could not read friend request %s: %s", rid, e.code)
            return
        if req.get("direction") != "incoming" or not req.get("actionable") or req.get("state") == "contact":
            return
        if data.get("source") == "contact_sync" and not req.get("attention"):
            return  # an old request found while syncing contacts, already handled on the phone
        key = f"friend_request:{rid}"
        version = str(req.get("version") or "")
        if self.store.get(key) == version:
            return
        text, buttons = self._request_card(req, new=True)
        if await self.notify(text, parse_mode="html", buttons=buttons, link_preview=False) is not None:
            self.store.set(key, version)

    async def on_wx_profile_changed(self, wx_id: str | None) -> None:
        """A contact's remark/nickname or a group's name changed in WeChat: follow it in Telegram."""
        if not wx_id or not self.cfg.bridge.auto_rename:
            return
        dest = self._dest_for_thread(wx_id)
        if dest is None:
            return
        try:
            thread = await self.wx.thread(wx_id)
        except WeChatError:
            if wx_id.endswith("@chatroom"):
                return
            try:
                contact = await self.wx.contact(wx_id)
            except WeChatError:
                return
            thread = {"id": wx_id, "kind": "direct", "displayName": contact.get("displayName")}
        if not thread.get("displayName"):
            return
        if self._title(thread) == self._link_title(wx_id):
            return
        title, status = await self.sync_title(dest[0], wx_id)
        if status and status != "unchanged":
            log.info("Could not follow the WeChat name of %s: %s", wx_id, status)
        else:
            log.info("Renamed the Telegram %s of %s to %r", "topic" if self.topics else "group", wx_id, title)

    # --- friend request cards ---------------------------------------------------
    def _request_card(self, req: dict, *, new: bool = False, note: str = "") -> tuple[str, list | None]:
        name = req.get("displayName") or req.get("peerId") or "?"
        peer = req.get("peerId") or ""
        incoming = req.get("direction") == "incoming"
        state = req.get("state")
        if state == "contact":
            head = "🤝 <b>已经是微信好友</b>"
        elif incoming:
            head = "👋 <b>新的微信好友请求</b>" if new else "👋 <b>微信好友请求</b>"
        else:
            head = "📨 <b>已发出的好友请求</b>"
        lines = [head, "", f"<b>{_esc(name)}</b>"]
        if peer and peer != name:
            lines.append(f"微信号：<code>{_esc(peer)}</code>")
        msg = one_line(req.get("message"))
        if msg:
            lines.append(f"💬 「{_esc(msg)}」")
        if when := ago(req.get("updatedAt")):
            lines.append(f"🕘 {when}")
        older = [h for h in (req.get("history") or [])
                 if isinstance(h, dict) and one_line(h.get("message")) and one_line(h.get("message")) != msg]
        older = sorted(older, key=lambda h: h.get("createdAt") or 0)[-3:]
        if older:
            lines += ["", "<b>更早的验证消息</b>"]
            for h in older:
                who = "TA" if h.get("direction") == "incoming" else "我"
                when = ago(h.get("createdAt"))
                lines.append(f"• {who}：{_esc(one_line(h.get('message'), 80))}" + (f"（{when}）" if when else ""))
        if note:
            lines += ["", note]
        buttons = None
        if incoming and req.get("actionable") and state != "contact":
            base = {"rid": req.get("id"), "name": name, "peer": peer}

            def tok(action: str, **kw) -> bytes:
                return self._make_ui_token({"action": action, **base, **kw})

            buttons = [
                [Button.inline("✅ 通过", tok("fr_accept", mode="plain")),
                 Button.inline("💬 通过并聊天", tok("fr_accept", mode="chat"))],
                [Button.inline("🔒 通过（仅聊天）", tok("fr_accept", mode="chats_only")),
                 Button.inline("🗑 删除记录", tok("fr_delete"))],
            ]
        return "\n".join(lines), buttons

    def _open_chat_button(self, wx_id: str, name: str) -> list:
        noun = "话题" if self.topics else "群"
        label = "💬 打开聊天" if self._dest_for_thread(wx_id) is not None else f"💬 发起聊天（建{noun}）"
        return [Button.inline(label, self._make_ui_token(
            {"action": "open_wx", "wx_id": wx_id, "name": name, "kind": "direct"}))]

    async def cmd_requests(self, event) -> None:
        page = await self.wx.contact_requests(direction="incoming", actionable=True, limit=10)
        items = page.get("items") or []
        total = max(int(page.get("pendingIncomingCount") or 0), len(items))
        if len(items) == 1:
            text, buttons = self._request_card(items[0])
            await event.reply(text, parse_mode="html", buttons=buttons, link_preview=False)
            return
        if not items:
            lines = ["✅ 没有待处理的微信好友请求。"]
            try:
                sent = (await self.wx.contact_requests(direction="outgoing", limit=10)).get("items") or []
            except WeChatError:
                sent = []
            sent = [r for r in sent if r.get("state") == "request_sent"][:5]
            if sent:
                lines += ["", "<b>等待对方通过</b>"]
                for r in sent:
                    when = ago(r.get("updatedAt"))
                    lines.append(f"• {_esc(r.get('displayName') or r.get('peerId'))}" + (f" · {when}" if when else ""))
            lines += ["", "💡 发送 <code>/add 微信号 验证消息</code> 添加好友。"]
            await event.reply("\n".join(lines), parse_mode="html")
            return
        lines = [f"👋 <b>待处理的微信好友请求（{total}）</b>", ""]
        buttons = []
        for i, r in enumerate(items, 1):
            name = r.get("displayName") or r.get("peerId") or "?"
            msg = one_line(r.get("message"), 40)
            when = ago(r.get("updatedAt"))
            lines.append(f"{i}. <b>{_esc(name)}</b>" + (f" — 「{_esc(msg)}」" if msg else "")
                         + (f" · {when}" if when else ""))
            buttons.append([Button.inline(f"👤 {i}. {name[:24]}", self._make_ui_token(
                {"action": "fr_view", "rid": r.get("id"), "name": name}))])
        if total > len(items):
            lines.append(f"\n（只显示最近 {len(items)} 条）")
        lines.append("\n点下方名字查看详情并通过。")
        await event.reply("\n".join(lines), parse_mode="html", buttons=buttons)

    # --- adding friends -----------------------------------------------------------
    async def cmd_add(self, event, arg: str) -> None:
        query, _, greeting = arg.strip().partition(" ")
        greeting = greeting.strip()
        if not query:
            await event.reply(
                "➕ <b>添加微信好友</b>\n\n用法：<code>/add 微信号 [验证消息]</code>\n"
                "例如 <code>/add wxid_abc123 你好，我是小王</code>\n\n"
                "需要对方准确的微信号；我会先搜索并预览，确认后再发送。",
                parse_mode="html")
            return
        try:
            res = await self.wx.discover_contact(query)
        except WeChatError as e:
            await event.reply(f"⚠️ 搜索 <code>{_esc(query)}</code> 失败：{_esc(friendly(e.code))}", parse_mode="html")
            return
        text, buttons = self._discovery_card(query, greeting, res)
        await event.reply(text, parse_mode="html", buttons=buttons, link_preview=False)

    def _discovery_card(self, query: str, greeting: str, res: dict) -> tuple[str, list | None]:
        req = res.get("request") if isinstance(res.get("request"), dict) else None
        if contact_id := res.get("contactId"):
            return (f"🤝 <code>{_esc(query)}</code> 已经是你的微信好友了。",
                    [self._open_chat_button(contact_id, (req or {}).get("displayName") or query)])
        if req and req.get("direction") == "incoming" and req.get("actionable"):
            return self._request_card(req, note="💡 对方已经向你发过好友请求，直接通过即可。")
        lines = ["➕ <b>添加微信好友</b>", "", f"微信号：<code>{_esc(query)}</code>"]
        if req and req.get("state") == "request_sent":
            when = ago(req.get("updatedAt"))
            lines.append(f"📨 {when + '已' if when else '之前已'}发过请求，对方还没有通过")
        ref = res.get("recipientRef")
        if not ref:
            lines += ["", "⚠️ 找到了这个用户，但微信没有提供添加凭证，暂时无法从这里添加"
                          "（对方可能关闭了「通过微信号添加我」）。"]
            return "\n".join(lines), None
        lines.append(f"验证消息：「{_esc(one_line(greeting, 300))}」" if greeting else "验证消息：（无）")
        lines.append("")
        if not greeting:
            lines.append("💡 可以在微信号后面附上验证消息，例如 <code>/add 微信号 你好，我是…</code>")
        lines.append("⏳ 请在 10 分钟内确认发送。")
        base = {"ref": ref, "query": query, "greeting": greeting}
        buttons = [
            [Button.inline("📨 发送好友请求", self._make_ui_token({"action": "add_send", "mode": "plain", **base})),
             Button.inline("🔒 发送（仅聊天）", self._make_ui_token({"action": "add_send", "mode": "chats_only", **base}))],
            [Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))],
        ]
        return "\n".join(lines), buttons

    # --- contact card (/contact in a bridged chat) -----------------------------------
    async def contact_card(self, wx_id: str, *, note: str = "") -> tuple[str, list | None]:
        if wx_id.endswith("@chatroom"):
            return "👥 这是微信群聊，没有好友资料和朋友圈权限可设置。", None
        contact = await self.wx.contact(wx_id)
        name = contact.get("displayName") or contact.get("remark") or contact.get("nickname") or wx_id
        lines = [f"👤 <b>{_esc(name)}</b>", ""]
        alias = contact.get("alias")
        lines.append(f"微信号：<code>{_esc(alias or wx_id)}</code>")
        if alias and alias != wx_id:
            lines.append(f"ID：<code>{_esc(wx_id)}</code>")
        if (nick := contact.get("nickname")) and nick != name:
            lines.append(f"昵称：{_esc(nick)}")
        if remark := contact.get("remark"):
            lines.append(f"备注：{_esc(remark)}")
        relation = contact.get("relation")
        if relation:
            lines.append(f"关系：{RELATIONS.get(relation, _esc(relation))}")
        privacy: dict | None = None
        version = ""
        why = ""
        if relation != "non_contact":
            try:
                p = await self.wx.contact_privacy(wx_id)
                privacy, version = p.get("privacy") or None, str(p.get("version") or "")
            except WeChatError as e:
                why = friendly(e.code)
        buttons: list = []
        if privacy:
            lines += ["", "<b>朋友圈与聊天权限</b>（点下方按钮切换）"]
            for field, label, card_label in PRIVACY_FLAGS:
                on = bool(privacy.get(field))
                lines.append(f"{card_label}：{'开' if on else '关'}")
                buttons.append([Button.inline(f"{'✅' if on else '⬜'} {label}", self._make_ui_token(
                    {"action": "ct_privacy", "wx_id": wx_id, "field": field, "value": not on,
                     "privacy": {k: bool(privacy.get(k)) for k, _, _ in PRIVACY_FLAGS}, "version": version}))])
        elif why:
            lines += ["", f"<i>朋友圈权限暂时读不到：{_esc(why)}</i>"]
        if note:
            lines += ["", note]
        last = [Button.inline("🔄 刷新", self._make_ui_token({"action": "ct_refresh", "wx_id": wx_id}))]
        if relation == "friend":
            last.append(Button.inline("🗑 删除好友", self._make_ui_token(
                {"action": "ct_remove_ask", "wx_id": wx_id, "name": name})))
        buttons.append(last)
        return "\n".join(lines), buttons

    # --- button presses ---------------------------------------------------------------
    async def on_contact_ui(self, event, payload: dict) -> None:
        if not self.wx_ready:
            await event.answer("微信还没有登录，请先 /wxlogin", alert=True)
            return
        action = payload.get("action")
        handler = {
            "fr_view": self._ui_request_view,
            "fr_accept": self._ui_request_accept,
            "fr_delete": self._ui_request_delete,
            "add_send": self._ui_add_send,
            "ct_refresh": self._ui_contact_refresh,
            "ct_privacy": self._ui_contact_privacy,
            "ct_remove_ask": self._ui_contact_remove_ask,
            "ct_remove": self._ui_contact_remove,
        }.get(action)
        if handler is None:
            await event.answer()
            return
        try:
            await handler(event, payload)
        except WeChatError as e:
            await self._edit_ui_msg(event, f"⚠️ 操作失败：{_esc(friendly(e.code))}")
        except Exception as e:
            log.exception("Contact action %s failed", action)
            await self._edit_ui_msg(event, f"⚠️ 操作失败：{_esc(type(e).__name__)}")

    async def _fresh_request(self, rid: str) -> dict | None:
        try:
            return await self.wx.contact_request(rid)
        except WeChatError as e:
            if e.status in (404, 410):
                return None
            raise

    async def _ui_request_view(self, event, p: dict) -> None:
        await event.answer()
        req = await self._fresh_request(p["rid"])
        if req is None:
            await self._edit_ui_msg(event, f"这条好友请求（{_esc(p.get('name'))}）已经不在了。")
            return
        text, buttons = self._request_card(req)
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_request_accept(self, event, p: dict) -> None:
        name, peer, mode = p.get("name") or "?", p.get("peer") or "", p.get("mode")
        await event.answer("正在通过…")
        req = await self._fresh_request(p["rid"])
        if req is None or not req.get("actionable") or req.get("state") == "contact":
            if req is not None and req.get("state") == "contact":
                await self._edit_ui_msg(event, f"🤝 <b>{_esc(name)}</b> 已经是你的微信好友了。",
                                        [self._open_chat_button(req.get("peerId") or peer, name)])
            else:
                await self._edit_ui_msg(event, f"⚠️ {_esc(CONTACT_ERRORS['contact_request_not_actionable'])}")
            return
        version = str(req.get("version") or "")
        await self._edit_ui_msg(event, f"⏳ 正在通过 <b>{_esc(name)}</b> 的好友请求…")
        privacy = CHATS_ONLY if mode == "chats_only" else None
        key = f"fr-accept-{_digest(p['rid'], version)}-{mode}"
        try:
            op = await self.wx.accept_contact_request(p["rid"], version, key, privacy)
        except WeChatError as e:
            await self._request_failed(event, p["rid"], friendly(e.code))
            return
        if op.get("state") not in OP_OK:
            await self._request_failed(event, p["rid"], op_error(op))
            return
        peer = req.get("peerId") or peer
        text = f"✅ 已通过 <b>{_esc(name)}</b> 的好友请求" + ("（仅聊天）" if mode == "chats_only" else "")
        if mode == "chat":
            noun = "话题" if self.topics else "群"
            try:
                thread = await self.wx.thread(peer)
            except WeChatError:
                thread = {"id": peer, "kind": "direct", "displayName": name}
            try:
                chat = await self.ensure_chat(thread)
            except Exception:
                log.exception("Could not open a Telegram %s for %s", noun, peer)
                chat = None
            if chat is None:
                text += f"\n⚠️ 无法建立 Telegram {noun}，请查看我刚发的提示。"
            else:
                text += f"\n💬 已建立 Telegram {noun}：<b>{_esc(self._title(thread))}</b>"
            await self._edit_ui_msg(event, text)
            return
        await self._edit_ui_msg(event, text, [self._open_chat_button(peer, name)])

    async def _request_failed(self, event, rid: str, reason: str) -> None:
        try:
            req = await self._fresh_request(rid)
        except WeChatError:
            req = None
        if req is None:
            await self._edit_ui_msg(event, f"⚠️ 操作失败：{_esc(reason)}")
            return
        text, buttons = self._request_card(req, note=f"⚠️ 操作失败：{_esc(reason)}")
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_request_delete(self, event, p: dict) -> None:
        name = p.get("name") or "?"
        await event.answer("正在删除…")
        req = await self._fresh_request(p["rid"])
        if req is None:
            await self._edit_ui_msg(event, f"🗑 <b>{_esc(name)}</b> 的好友请求记录已经不在了。")
            return
        version = str(req.get("version") or "")
        key = f"fr-delete-{_digest(p['rid'], version)}"
        op = await self.wx.delete_contact_request(p["rid"], version, key)
        if op.get("state") not in OP_OK:
            await self._request_failed(event, p["rid"], op_error(op))
            return
        await self._edit_ui_msg(
            event, f"🗑 已删除 <b>{_esc(name)}</b> 的好友请求记录。\n只删除了本地记录，不会拒绝对方，对方也不会收到通知。")

    async def _ui_add_send(self, event, p: dict) -> None:
        query, mode = p.get("query") or "", p.get("mode")
        await event.answer("正在发送…")
        await self._edit_ui_msg(event, f"⏳ 正在向 <code>{_esc(query)}</code> 发送好友请求…")
        key = f"fr-send-{_digest(p['ref'])}-{mode}"
        try:
            op = await self.wx.send_contact_request(p["ref"], p.get("greeting") or "", key,
                                                    CHATS_ONLY if mode == "chats_only" else None)
        except WeChatError as e:
            await self._edit_ui_msg(event, f"⚠️ 发送失败：{_esc(friendly(e.code))}")
            return
        if op.get("state") not in OP_OK:
            await self._edit_ui_msg(event, f"⚠️ 发送失败：{_esc(op_error(op))}")
            return
        text = (f"📨 已向 <code>{_esc(query)}</code> 发送好友请求"
                + ("（仅聊天）" if mode == "chats_only" else "") + "。\n"
                "对方通过后，可以用 <code>/open</code> 发起聊天；发送 <code>/requests</code> 查看等待中的请求。")
        await self._edit_ui_msg(event, text)

    async def _ui_contact_refresh(self, event, p: dict) -> None:
        await event.answer("已刷新")
        text, buttons = await self.contact_card(p["wx_id"])
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_contact_privacy(self, event, p: dict) -> None:
        wx_id, field = p["wx_id"], p["field"]
        await event.answer("正在修改…")
        desired = dict(p.get("privacy") or {})
        desired[field] = bool(p.get("value"))
        key = f"ct-privacy-{secrets.token_hex(8)}"
        try:
            op = await self.wx.update_contact_privacy(wx_id, desired, p.get("version") or "", key)
            note = "✅ 已更新" if op.get("state") in OP_OK else f"⚠️ 修改失败：{_esc(op_error(op))}"
        except WeChatError as e:
            note = f"⚠️ 修改失败：{_esc(friendly(e.code))}"
        text, buttons = await self.contact_card(wx_id, note=note)
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_contact_remove_ask(self, event, p: dict) -> None:
        await event.answer()
        noun = "话题" if self.topics else "群"
        name = p.get("name") or p["wx_id"]
        buttons = [[
            Button.inline("⚠️ 确认删除", self._make_ui_token(
                {"action": "ct_remove", "wx_id": p["wx_id"], "name": name, "key": secrets.token_hex(8)})),
            Button.inline("↩️ 返回", self._make_ui_token({"action": "ct_refresh", "wx_id": p["wx_id"]})),
        ]]
        await self._edit_ui_msg(
            event,
            f"确定要删除微信好友 <b>{_esc(name)}</b> 吗？\n\n删除后聊天记录仍会保留，当前{noun}的绑定也不会变；"
            f"之后想再加回来需要重新发送好友请求。",
            buttons)

    async def _ui_contact_remove(self, event, p: dict) -> None:
        name = p.get("name") or p["wx_id"]
        await event.answer("正在删除…")
        op = await self.wx.remove_contact(p["wx_id"], f"ct-remove-{p.get('key') or secrets.token_hex(8)}")
        if op.get("state") not in OP_OK:
            text, buttons = await self.contact_card(p["wx_id"], note=f"⚠️ 删除失败：{_esc(op_error(op))}")
            await self._edit_ui_msg(event, text, buttons)
            return
        noun = "话题" if self.topics else "群"
        await self._edit_ui_msg(event, f"🗑 已删除微信好友 <b>{_esc(name)}</b>。聊天记录和当前{noun}的绑定都保留。")
