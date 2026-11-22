"""WeChat friend requests and contact settings, shown as Telegram cards with buttons."""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import secrets
import time

from telethon import Button

from .wechat import OP_OK, WeChatError, is_group_id

log = logging.getLogger("contacts")

PRIVACY_FIELDS = ("chatsOnly", "hideMyPosts", "hideTheirPosts")
# Moments toggles (privacy field, button label); WeChat hides them while the friend is chats-only
MOMENT_FLAGS = (("hideMyPosts", "不让 TA 看我"), ("hideTheirPosts", "不看 TA"))
CHATS_ONLY = {"chatsOnly": True, "hideMyPosts": False, "hideTheirPosts": False}
NAME_SYNC_ROUNDS = 15  # /syncnames: a round pulls at most 10 pages or runs 2 minutes, then resumes

CONTACT_ERRORS = {
    "stale_contact_request": "这条请求刚刚有了变化，请重新查看",
    "contact_request_not_actionable": "这条请求已经无法通过（可能已过期，或你们已经是好友）",
    "stale_contact_privacy": "权限刚在别处改过，已刷新，请重试",
    "contact_privacy_unresolved": "暂时读不到和 TA 的权限设置",
    "contact_privacy_pending": "上一次权限修改还在处理中，请稍后再试",
    "contact_privacy_partial": "只有部分权限改成功了，请刷新后确认",
    "contact_flags_unresolved": "暂时无法确认和 TA 的好友关系",
    "forbidden": "daemon 令牌没有 admin 权限，不能修改（请给桥接用的令牌加上 admin 范围）",
    "insufficient_scope": "daemon 令牌没有 admin 权限，不能修改（请给桥接用的令牌加上 admin 范围）",
    "ordinary_contact_required": "这类联系人不支持设置朋友圈权限",
    "profile_identity_unavailable": "微信账号资料暂时不可读，请稍后再试",
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

RELATIONS = {"friend": "✅ 微信好友", "non_contact": "⚠️ 不是微信好友", "unknown": "❔ 好友关系未知"}


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

    async def on_wx_profile_changed(self, wx_id: str | None, *, asked: bool = False) -> bool:
        """A contact's remark/nickname or a group's name changed in WeChat: follow it in Telegram.
        asked: the owner made this change here (/remark), so follow it even right after a /syncnames.
        True when a Telegram title was changed."""
        if not wx_id:
            return False
        self._forget_names(wx_id)
        if not self.cfg.bridge.auto_rename:
            return False
        if not asked and not is_group_id(wx_id) and (
                self._names_syncing or time.monotonic() < self._names_quiet_until):
            return False  # a /syncnames reports every contact as updated: renaming them all hits flood limits
        dest = self._dest_for_thread(wx_id)
        if dest is None or await self._outdated_title(wx_id) is None:
            return False
        title, status = await self.sync_title(dest[0], wx_id)
        if status and status != "unchanged":
            log.info("Could not follow the WeChat name of %s: %s", wx_id, status)
            return False
        log.info("Renamed the Telegram %s of %s to %r", "topic" if dest[1] is not None else "group", wx_id, title)
        return status == ""

    async def _outdated_title(self, wx_id: str) -> str | None:
        """The title the bridged chat of wx_id should have, when it differs from the current one."""
        try:
            thread = await self.wx.thread(wx_id)
        except WeChatError:
            if is_group_id(wx_id):
                return None
            try:
                contact = await self.wx.contact(wx_id)
            except WeChatError:
                return None
            thread = {"id": wx_id, "kind": "direct", "displayName": contact.get("displayName")}
        thread = await self._named_thread(thread)
        if not thread.get("displayName"):
            return None
        title = self._title(thread)
        return None if title == self._link_title(wx_id) else title

    # --- remarks: /remark in a chat, /syncnames in private ----------------------------
    async def cmd_remark(self, chat_id: int, reply_to: int | None, wx_id: str, arg: str) -> None:
        async def say(text: str) -> None:
            await self.bot.send_message(chat_id, text, parse_mode="html", reply_to=reply_to, link_preview=False)

        if is_group_id(wx_id):
            return await say("这是微信群聊，<code>/remark</code> 只能改好友的备注。")
        if not self.wx_ready:
            return await say("🔐 微信尚未登录，登录后再改备注。")
        arg = arg.strip()
        try:
            contact = await self.wx.contact(wx_id)
        except WeChatError as e:
            return await say(f"⚠️ 读取联系人资料失败：{_esc(friendly(e.code))}")
        current = str(contact.get("remark") or "")
        if not arg:
            now = f"当前备注：<b>{_esc(current)}</b>" if current else "当前没有备注"
            return await say(f"{now}\n\n发送 <code>/remark 新备注</code> 修改，<code>/remark -</code> 清除。")
        remark = None if arg in ("-", "－") else arg
        if (remark or "") == current:
            return await say("备注没有变化。")
        key = f"ct-remark-{secrets.token_hex(8)}"
        for attempt in range(2):
            version = str(contact.get("privacyVersion") or "")
            if not version:
                why = contact.get("privacyUnavailable")
                return await say("⚠️ 暂时不能改这个联系人的备注：" + _esc(friendly(why) if why else "读不到资料版本"))
            try:
                op = await self.wx.set_contact_remark(wx_id, remark, version, key)
                break
            except WeChatError as e:
                if e.code != "stale_contact_privacy" or attempt:
                    return await say(f"⚠️ 修改备注失败：{_esc(friendly(e.code))}")
                # Changed elsewhere since we read it: read the new version and try once more
                key = f"ct-remark-{secrets.token_hex(8)}"
                try:
                    contact = await self.wx.contact(wx_id)
                except WeChatError as e2:
                    return await say(f"⚠️ 修改备注失败：{_esc(friendly(e2.code))}")
        if op.get("state") not in OP_OK:
            return await say(f"⚠️ 修改备注失败：{_esc(op_error(op))}")
        self._forget_names(wx_id)
        noun = self._thread_noun(wx_id)
        done = f"已把备注改为 <b>{_esc(remark)}</b>" if remark else "已清除备注"
        follow = f"，{noun}标题会跟着更新" if self.cfg.bridge.auto_rename else ""
        await say(f"✅ {done}。几秒后同步到手机微信{follow}。")
        # The daemon also reports the change as contacts.updated; following it here as well covers a missed event
        await self._await_remark(wx_id, remark)
        await self.on_wx_profile_changed(wx_id, asked=True)

    async def _await_remark(self, wx_id: str, remark: str | None, wait: float = 10.0) -> None:
        """Wait (briefly) until the daemon reports the new remark as saved."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            try:
                c = await self.wx.contact(wx_id)
                if not c.get("remarkPending") and (c.get("remark") or None) == remark:
                    return
            except WeChatError:
                pass
            if loop.time() >= deadline:
                return
            await asyncio.sleep(self._remark_poll)

    async def cmd_sync_names(self, event) -> None:
        """Pull the whole contact catalog from WeChat again (remarks missing from old profiles come back).
        Only the contact data is refreshed: Telegram titles are left alone (renaming many chats at once
        runs into Telegram's flood limits); the chats whose title is now outdated are listed for /rename."""
        if self._names_syncing:
            await event.reply("⏳ 已经在同步了，请稍候。")
            return
        self._names_syncing = True
        try:
            await event.reply("⏳ 正在从微信重新同步联系人资料（含备注），好友多时需要几分钟…")
            result: dict = {}
            for _ in range(NAME_SYNC_ROUNDS):
                try:
                    op = await self.wx.sync_contacts()
                except WeChatError as e:
                    await event.reply(f"⚠️ 同步失败：{_esc(friendly(e.code))}", parse_mode="html")
                    return
                if op.get("state") not in OP_OK:
                    await event.reply(f"⚠️ 同步没有完成：{_esc(op_error(op))}", parse_mode="html")
                    return
                result = op.get("result") if isinstance(op.get("result"), dict) else {}
                if result.get("complete") or not result.get("resumeAvailable"):
                    break  # otherwise: a round stops after 10 pages or 2 minutes; the next one resumes
            self._contact_name_cache.clear()
            self.names.clear()
            outdated: list[tuple[str, str]] = []
            for wx_id, _, _ in self._link_rows():
                if not is_group_id(wx_id) and (title := await self._outdated_title(wx_id)):
                    outdated.append((self._link_title(wx_id) or wx_id, title))
            has_standalone = self.topics and any(
                not self._is_forum_chat(c) for _, c, _, _ in self.store.list_topic_groups()
            )
            noun = "群/话题" if has_standalone else ("话题" if self.topics else "群")
            lines = ["✅ 联系人资料已从微信重新同步。" if result.get("complete") or not result.get("resumeAvailable")
                     else "⚠️ 同步了一部分，还没完成，稍后再发一次 <code>/syncnames</code> 会接着同步。"]
            failed = result.get("failedItems")
            failed = len(failed) if isinstance(failed, (list, dict)) else failed if isinstance(failed, int) else 0
            if failed:
                lines.append(f"有 {failed} 个联系人这次没拉到，可以稍后再试。")
            if not outdated:
                lines.append(f"{noun}标题都已是最新。")
            else:
                lines += ["", f"为避免触发 Telegram 频率限制，没有自动改{noun}标题。"
                              f"以下 {len(outdated)} 个{noun}的标题与微信不一致，需要时在对应{noun}里发 <code>/rename</code>："]
                lines += [f"• {_esc(old)} → <b>{_esc(new)}</b>" for old, new in outdated[:30]]
                if len(outdated) > 30:
                    lines.append(f"……还有 {len(outdated) - 30} 个")
            await event.reply("\n".join(lines), parse_mode="html")
        finally:
            self._names_syncing = False
            # the daemon's contacts.updated events for this sync may still be on their way
            self._names_quiet_until = time.monotonic() + 120

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
        noun = self._thread_noun(wx_id)
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
        if is_group_id(wx_id):
            return "👥 这是微信群聊，没有好友资料和朋友圈权限可设置。", None
        contact = await self.wx.contact(wx_id)
        name = contact.get("displayName") or contact.get("remark") or contact.get("nickname") or wx_id
        relation = contact.get("relation")
        lines = [f"👤 <b>{_esc(name)}</b>"]
        if relation:
            lines.append(RELATIONS.get(relation, _esc(relation)))
        lines.append("")
        alias = contact.get("alias")
        lines.append(f"微信号：<code>{_esc(alias or wx_id)}</code>")
        if alias and alias != wx_id:
            lines.append(f"wxid：<code>{_esc(wx_id)}</code>")
        if (nick := contact.get("nickname")) and nick != name:
            lines.append(f"昵称：{_esc(nick)}")
        if remark := contact.get("remark"):
            lines.append(f"备注：{_esc(remark)}" + ("（正在与微信同步…）" if contact.get("remarkPending") else ""))
        elif contact.get("remarkPending"):
            lines.append("备注：正在与微信同步…")
        privacy: dict | None = None
        version = ""
        why = ""
        if relation != "non_contact":
            privacy = contact.get("privacy") or None
            version = str(contact.get("privacyVersion") or "")
            if not privacy and contact.get("privacyUnavailable"):
                why = friendly(contact["privacyUnavailable"])
        buttons: list = []
        if privacy:
            current = {k: bool(privacy.get(k)) for k in PRIVACY_FIELDS}

            def toggle(field: str, value: bool):
                return self._make_ui_token({"action": "ct_privacy", "wx_id": wx_id, "field": field,
                                            "value": value, "privacy": current, "version": version})

            def choice(label: str, chats_only: bool):
                if current["chatsOnly"] == chats_only:  # already chosen: pressing it changes nothing
                    return Button.inline(f"🔘 {label}", self._make_ui_token({"action": "ct_noop"}))
                return Button.inline(f"⚪ {label}", toggle("chatsOnly", chats_only))

            lines += ["", "<b>🔐 朋友权限</b>"]
            buttons.append([choice("聊天、朋友圈等", False), choice("仅聊天", True)])
            if current["chatsOnly"]:
                lines.append("仅聊天（互相看不到朋友圈）")
            else:
                mine, theirs = current["hideMyPosts"], current["hideTheirPosts"]
                lines.append("聊天、朋友圈等 · 朋友圈" + (
                    "互不可见" if mine and theirs else "不让 TA 看我" if mine else "不看 TA" if theirs
                    else "互相可见"))
                buttons.append([Button.inline(f"{'✅' if current[f] else '⬜'} {label}", toggle(f, not current[f]))
                                for f, label in MOMENT_FLAGS])
        elif why:
            lines += ["", f"<i>朋友圈权限暂时读不到：{_esc(why)}</i>"]
        if note:
            lines += ["", note]
        last = [Button.inline("🔄 刷新", self._make_ui_token({"action": "ct_refresh", "wx_id": wx_id, "live": True}))]
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
            "ct_noop": self._ui_noop,
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
            try:
                thread = await self.wx.thread(peer)
            except WeChatError:
                thread = {"id": peer, "kind": "direct", "displayName": name}
            noun = self._thread_noun(peer, thread)
            try:
                chat = await self.ensure_chat(thread)
            except Exception:
                log.exception("Could not open a Telegram %s for %s", noun, peer)
                chat = None
            if chat is None:
                text += f"\n⚠️ 无法建立 Telegram {noun}，请查看我刚发的提示。"
            else:
                text += f"\n💬 已建立 Telegram {noun}：<b>{_esc(self._link_title(peer) or self._title(thread))}</b>"
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
        wx_id, note = p["wx_id"], ""
        if not p.get("live"):  # "back" from another screen: just redraw
            await event.answer()
        else:
            # Ask WeChat itself, so a remark set on the phone (or missing from an old profile) shows up
            await event.answer("正在向微信刷新…")
            try:
                op = await self.wx.refresh_contact(wx_id)
                if op.get("state") not in OP_OK:
                    note = f"<i>向微信刷新没有完成：{_esc(op_error(op))}</i>"
            except WeChatError as e:
                note = f"<i>向微信刷新失败：{_esc(friendly(e.code))}</i>"
            self._forget_names(wx_id)
        text, buttons = await self.contact_card(wx_id, note=note)
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_noop(self, event, p: dict) -> None:
        await event.answer("当前已经是这个设置")

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
        noun = self._thread_noun(p["wx_id"])
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
        noun = self._thread_noun(p["wx_id"])
        await self._edit_ui_msg(event, f"🗑 已删除微信好友 <b>{_esc(name)}</b>。聊天记录和当前{noun}的绑定都保留。")
