"""Telegram bot private commands, forum/group commands, and interactive link/find/open pickers."""
from __future__ import annotations

import logging
import secrets
import time

from telethon import Button, errors, functions, utils

from .formatting import (
    HELP,
    TG_TEXT_LIMIT,
    TOPIC_GROUP_HELP,
    TOPIC_HELP,
    _dead_chat,
    _esc,
    _is_official_account,
    _member_label,
    _msg_topic,
)
from .groups import GROUP_COMMANDS
from .wechat import WeChatError, is_group_id

log = logging.getLogger("bridge")


class CommandsMixin:
    """Mixin on Bridge handling private bot commands, forum setup, /link pickers, and /members."""

    async def on_forum_message(self, event) -> None:
        """Group messages received by the bot from owner (topics mode: forums and standalone groups;
        groups mode: dedicated official-accounts forum and /oaforum)."""
        msg = event.message
        if msg.action or msg.via_bot_id:
            return
        text = (msg.message or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd_name, _, at_bot = cmd.partition("@")
        cmd_name = cmd_name.lower() if cmd_name.startswith("/") else ""
        if at_bot and at_bot.lower() != self.bot_me.username.lower():
            cmd_name = ""
        if cmd_name == "/forum" and self.topics:
            await self._set_forum(event)
            return
        if cmd_name == "/oaforum":
            await self._set_oa_forum(event, arg.strip())
            return
        is_forum = self._is_forum_chat(event.chat_id)
        if not is_forum and not self.topics:
            return
        if self._thread_for_event(event) is not None:
            await self.on_tg_message(event)
            return
        if not cmd_name:
            return
        if not is_forum:
            # Unlinked non-forum group in topics mode: allow on-demand /link into a standalone group.
            if cmd_name == "/link":
                try:
                    chat = await event.get_chat() if hasattr(event, "get_chat") else getattr(event, "chat", None)
                except Exception:
                    chat = None
                if getattr(chat, "forum", False):
                    await self._send_group_notice(
                        event.chat_id, msg.id,
                        "这个群开启了「话题」，但还不是当前话题群。\n"
                        "• 要把它设为话题群，请先发送 <code>/forum</code>（公众号专用群发 <code>/oaforum</code>）；\n"
                        "• 要把单个微信会话绑定到独立群，请在<b>未开启话题</b>的普通群里发送 <code>/link</code>。")
                    return
                await self._handle_group_link(event, arg.strip())
            return
        # The General topic, or a topic not bound to a WeChat chat: only a few commands do anything.
        topic = _msg_topic(msg)
        if cmd_name == "/link":
            if topic is None:
                await self._send_group_notice(
                    event.chat_id, msg.id,
                    "请打开要绑定的话题，在话题里发送 <code>/link</code>；"
                    "想为微信会话新建话题，私聊我发 <code>/open</code>。")
            else:
                await self._handle_group_link(event, arg.strip())
        elif cmd_name == "/help":
            await self._send_group_notice(event.chat_id, msg.id, TOPIC_GROUP_HELP)
        elif cmd_name in ("/wx", "/unlink", "/members", "/at", "/recall", "/rename", "/title", "/avatar",
                          "/contact", "/info", "/call", "/vcall", "/remark", *GROUP_COMMANDS):
            await self._send_group_notice(event.chat_id, msg.id,
                                          "这个话题还没有绑定微信会话，发送 <code>/link</code> 绑定。")

    async def _set_oa_forum(self, event, arg: str = "") -> None:
        chat_id, msg_id = event.chat_id, event.message.id
        fixed = self.cfg.bridge.official_accounts_forum_chat
        if arg.lower() in ("off", "clear", "disable"):
            if fixed:
                await self._send_group_notice(
                    chat_id, msg_id,
                    f"公众号话题群已由 TG_OA_FORUM_CHAT / official_accounts_forum_chat 固定为 <code>{fixed}</code>。"
                    "要取消，请清空该配置后重启。")
                return
            self.oa_forum_chat = None
            self.store.set("oa_forum_chat", "")
            self._forum_warned_at = None
            log.info("Official-accounts forum group cleared")
            await self._send_group_notice(
                chat_id, msg_id,
                "✅ 已取消公众号/服务号专用话题群设置（原有话题绑定已保留）。")
            return
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        if not getattr(chat, "forum", False):
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群还没有开启「话题」：请在群设置中开启话题（Topics），"
                "并把我设为管理员、勾选「管理话题」权限，然后再发 <code>/oaforum</code>。")
            return
        bound = self.store.thread_for_topic_group(chat_id) if self.topics else self.store.thread_for_chat(chat_id)
        if bound is not None:
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群当前已绑定到一个普通微信会话。请新建一个专用的话题群（或先发送 <code>/unlink</code> 解绑），"
                "再发送 <code>/oaforum</code>。")
            return
        if fixed and fixed != chat_id:
            await self._send_group_notice(
                chat_id, msg_id,
                f"公众号话题群已由 TG_OA_FORUM_CHAT / official_accounts_forum_chat 固定为 <code>{fixed}</code>。"
                f"要改用这个群，请改为 <code>{chat_id}</code>（或删掉它）后重启。")
            return
        old = self.oa_forum_chat
        self.oa_forum_chat = chat_id
        self.store.set("oa_forum_chat", str(chat_id))
        self._forum_warned_at = None
        log.info("Official-accounts forum group set to %s (was %s)", chat_id, old)
        text = "✅ 已把当前群设为<b>公众号/服务号专用话题群</b>。每个公众号/服务号会自动在这里生成独立话题。"
        if old and old != chat_id:
            text += f"\n\n（原公众号话题群 <code>{old}</code> 的绑定已保留，改回去即可恢复。）"
        if not self.cfg.bridge.bridge_official_accounts:
            text += ("\n\n⚠️ 当前尚未开启公众号/服务号转发：请在 <code>.env</code> 设置 "
                     "<code>BRIDGE_OFFICIAL_ACCOUNTS=true</code>（或在 <code>config/config.toml</code> 中设 "
                     "<code>bridge_official_accounts = true</code>）后重启生效。")
        await self._send_group_notice(chat_id, msg_id, text)

    async def _set_forum(self, event) -> None:
        chat_id, msg_id = event.chat_id, event.message.id
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        if not getattr(chat, "forum", False):
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群还没有开启「话题」：请在群设置中开启话题（Topics），"
                "并把我设为管理员、勾选「管理话题」权限，然后再发 <code>/forum</code>。")
            return
        if self.store.thread_for_topic_group(chat_id) is not None:
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群当前已绑定到一个普通微信会话。请新建一个专用的话题群（或先发送 <code>/unlink</code> 解绑），"
                "再发送 <code>/forum</code>。")
            return
        fixed = self.cfg.bridge.forum_chat
        if fixed and fixed != chat_id:
            await self._send_group_notice(
                chat_id, msg_id,
                f"话题群已由 TG_FORUM_CHAT 固定为 <code>{fixed}</code>。要改用这个群，"
                f"请把 TG_FORUM_CHAT 改为 <code>{chat_id}</code>（或删掉它）后重启。")
            return
        old = self.forum_chat
        self.forum_chat = chat_id
        self.store.set("forum_chat", str(chat_id))
        self._forum_warned_at = None
        log.info("Forum group set to %s (was %s)", chat_id, old)
        text = "✅ 已把当前群设为桥接话题群。新的微信消息会自动在这里创建话题；私聊我发 <code>/open</code> 可主动新建话题。"
        if old and old != chat_id:
            text += f"\n\n（原话题群 <code>{old}</code> 的绑定已保留，改回去即可恢复。）"
        await self._send_group_notice(chat_id, msg_id, text)

    async def _send_members(self, chat_id: int, reply_to: int, thread_id: str, query: str) -> None:
        if not is_group_id(thread_id):
            await self.bot.send_message(chat_id, "只有在微信群聊里才能查看群成员", reply_to=reply_to)
            return
        try:
            members = await self.wx.group_members(thread_id, search=query or None)
        except WeChatError as e:
            await self.bot.send_message(chat_id, f"获取群成员失败：{e.code}", reply_to=reply_to)
            return
        terms = query.lower().split() if query else []
        if not members:
            await self.bot.send_message(chat_id, "没有匹配的群成员" if query else "没有拿到群成员", reply_to=reply_to)
            return
        lines = []
        for m in members:
            label = _member_label(m)
            disp = (self._known_remark(str(m.get("id") or "")) or m.get("displayName") or "").strip()
            nick = (m.get("nickname") or "").strip()
            alias = (m.get("alias") or "").strip()
            extras: list[str] = []
            if disp and disp != label:
                extras.append(disp)
            if nick and nick not in (label, disp):
                extras.append(nick)
            if alias and alias not in (label, disp, nick) and any(t in alias.lower() for t in terms):
                extras.append(f"微信号 {alias}")
            extra = f" （{_esc(' · '.join(extras))}）" if extras else ""
            lines.append(f"• <code>@{_esc(label)}</code>{extra}")
        header = f"<b>群成员</b>（{len(members)} 人，点按可复制）：\n也可直接发送 <code>@所有人</code>\n\n"
        pages: list[str] = []
        cur = header
        for line in lines:
            if len(cur) + len(line) + 1 > TG_TEXT_LIMIT:
                pages.append(cur.rstrip())
                cur = ""
            cur += line + "\n"
        if cur:
            pages.append(cur.rstrip())
        top = self._topic_for(chat_id, thread_id)
        for i, part in enumerate(pages):
            await self.bot.send_message(chat_id, part, parse_mode="html", reply_to=reply_to if i == 0 else top)

    def _make_ui_token(self, payload: dict) -> bytes:
        if len(self._ui_tokens) >= 400:
            for k in list(self._ui_tokens)[:200]:
                self._ui_tokens.pop(k, None)
        token = secrets.token_hex(6)
        self._ui_tokens[token] = payload
        return f"ui:{token}".encode()

    async def _wx_search_candidates(self, query: str, limit: int = 10) -> list[dict]:
        q = query.strip()
        items: list[dict] = []
        seen: set[str] = set()

        def add(raw: dict, default_kind: str | None = None) -> None:
            tid = str(raw.get("id") or "").strip()
            if not tid or tid in seen:
                return
            seen.add(tid)
            kind = raw.get("kind") or default_kind or ("group" if is_group_id(tid) else "direct")
            name = (raw.get("displayName") or raw.get("remark") or raw.get("nickname") or tid).strip()
            items.append({
                "id": tid, "name": name, "kind": kind, "relation": raw.get("relation"),
                "classification": raw.get("classification"),
            })

        if not q:
            try:
                for t in await self.wx.list_threads(limit=limit):
                    add(t)
            except WeChatError as e:
                log.debug("list_threads failed: %s", e.code)
            return items[:limit]

        if " " not in q:
            try:
                add(await self.wx.thread(q))
            except WeChatError:
                pass
        try:
            # Show friends first, followed by people you share a group with (or who removed you).
            found = await self.wx.search_contacts(q, limit=limit, relation="all")
            for c in sorted(found, key=lambda c: c.get("relation") not in (None, "friend")):
                add(c, "direct")
        except WeChatError as e:
            log.debug("search_contacts failed: %s", e.code)
        try:
            for g in await self.wx.search_groups(q, limit=limit):
                add(g, "group")
        except WeChatError as e:
            log.debug("search_groups failed: %s", e.code)
        if len(items) < limit:
            ql = q.lower()
            try:
                for t in await self.wx.list_threads(limit=30):
                    name = str(t.get("displayName") or "")
                    tid = str(t.get("id") or "")
                    if ql in name.lower() or ql in tid.lower():
                        add(t)
            except WeChatError:
                pass
        return items[:limit]

    async def _list_tg_groups(self, limit: int = 12) -> list[tuple[int, str, str | None]]:
        iter_dialogs = getattr(self.user, "iter_dialogs", None)
        if not callable(iter_dialogs):
            return []
        unlinked: list[tuple[int, str, str | None]] = []
        linked: list[tuple[int, str, str | None]] = []
        try:
            async for d in iter_dialogs(limit=80):
                if not getattr(d, "is_group", False):
                    continue
                cid = d.id
                if self._is_forum_chat(cid):
                    continue
                title = (getattr(d, "title", None) or getattr(d, "name", None) or str(cid)).strip()
                wx_id = self.store.thread_for_chat(cid)
                entry = (cid, title, wx_id)
                (linked if wx_id else unlinked).append(entry)
        except Exception as e:
            log.info("Could not list Telegram dialogs: %s", e)
            return []
        return (unlinked + linked)[:limit]

    def _wx_picker_buttons(self, cands: list[dict], *, action: str, extra: dict | None = None,
                           current: tuple[int, int | None] | None = None) -> list[list]:
        rows: list[list] = []
        for c in cands[:8]:
            icon = "👥" if c["kind"] == "group" else ("📰" if _is_official_account(c) else "👤")
            linked = self._dest_for_thread(c["id"])
            if current is not None and linked == current:
                suffix = "（当前）"
            elif linked is not None:
                suffix = "（已绑）"
            else:
                suffix = ""
            payload = {"action": action, "wx_id": c["id"], "wx_name": c["name"], "kind": c["kind"], **(extra or {})}
            rows.append([Button.inline(f"{icon} {c['name'][:22]}{suffix}", self._make_ui_token(payload))])
        rows.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
        return rows

    def _tg_picker_buttons(self, groups: list[tuple[int, str, str | None]], *, action: str,
                           extra: dict | None = None) -> list[list]:
        rows: list[list] = []
        for cid, title, wx_id in groups[:10]:
            prefix = "🔗" if wx_id else "💬"
            suffix = "（已绑）" if wx_id else ""
            payload = {"action": action, "tg_chat": cid, "tg_title": title, **(extra or {})}
            rows.append([Button.inline(f"{prefix} {title[:22]}{suffix}", self._make_ui_token(payload))])
        rows.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
        return rows

    async def _send_group_bot_message(self, chat_id: int, text: str, *, reply_to: int | None = None,
                                      buttons=None, silent: bool = False):
        try:
            return await self.bot.send_message(
                chat_id, text, parse_mode="html", reply_to=reply_to, buttons=buttons, silent=silent)
        except Exception as e:
            if not _dead_chat(e):
                raise
            if self.topics or self._is_forum_chat(chat_id):  # bot must be admin in forums; do not invite via user
                log.info("Bot cannot send to group %s: %s", chat_id, e)
                return None
        try:
            if chat_id < -1000000000000:
                await self.user(functions.channels.InviteToChannelRequest(
                    channel=await self.user.get_input_entity(chat_id), users=[self.bot_input]))
            else:
                await self.user(functions.messages.AddChatUserRequest(
                    chat_id=utils.resolve_id(chat_id)[0], user_id=self.bot_input, fwd_limit=0))
        except Exception as inv_err:
            log.info("Could not invite bot to group %s: %s", chat_id, inv_err)
            return None
        try:
            return await self.bot.send_message(
                chat_id, text, parse_mode="html", reply_to=None, buttons=buttons, silent=silent)
        except Exception as e2:
            log.info("Bot still could not send to group %s after invite: %s", chat_id, e2)
            return None

    async def _send_group_notice(self, chat_id: int, reply_to: int | None, text: str, buttons=None) -> None:
        sent = await self._send_group_bot_message(chat_id, text, reply_to=reply_to, buttons=buttons)
        if sent is None and not self.topics and not self._is_forum_chat(chat_id) and hasattr(self.user, "send_message"):
            try:
                await self.user.send_message(
                    chat_id, f"⚠️ 请先把 @{self.bot_me.username} 拉进这个群，再发送 /link", reply_to=reply_to)
            except Exception as e:
                log.info("Fallback user notice in %s failed: %s", chat_id, e)

    async def on_tg_unlinked_command(self, event) -> None:
        msg = event.message
        if not msg or msg.action or msg.via_bot_id or self._is_forum_chat(event.chat_id):
            return
        text = (msg.message or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd_name, _, at_bot = cmd.partition("@")
        if at_bot and at_bot.lower() != self.bot_me.username.lower():
            return
        if cmd_name.lower() == "/link":
            await self._handle_group_link(event, arg.strip())

    async def _bot_group_read_warning(self, chat_id: int) -> str:
        """In topics mode standalone groups, warn if the bot cannot read regular messages (privacy mode on & not admin)."""
        if not self.topics or self._is_forum_chat(chat_id):
            return ""
        if getattr(self.bot_me, "bot_chat_history", False):
            return ""
        get_perms = getattr(self.bot, "get_permissions", None)
        if not callable(get_perms):
            return ""
        try:
            perm = await get_perms(chat_id, self.bot_me)
        except Exception:
            return ""
        if getattr(perm, "is_admin", False):
            return ""
        return (f"\n\n⚠️ 请把 @{_esc(self.bot_me.username)} 设为本群<b>管理员</b>"
                "（或在 @BotFather 关闭群隐私模式），否则群里发送的普通消息不会转发到微信。")

    async def _standalone_link_notes(self, chat_id: int, moved_from_topic: bool, *, check_perm: bool = True) -> str:
        notes = ""
        if moved_from_topic:
            notes += "\n（已解除原话题绑定并保留历史；之后在本群发 <code>/unlink</code> 可回到话题群。）"
        if check_perm:
            notes += await self._bot_group_read_warning(chat_id)
        return notes

    async def _handle_group_link(self, event, arg: str) -> None:
        chat_id = event.chat_id
        msg_id = event.message.id
        if not self.wx_ready:
            await self._send_group_notice(chat_id, msg_id, "微信还没有登录，请先在私聊中扫码或发 /wxlogin。")
            return
        chat_obj = getattr(event, "chat", None)
        if chat_obj is None and hasattr(event, "get_chat"):
            try:
                chat_obj = await event.get_chat()
            except Exception:
                chat_obj = None
        tg_title = (getattr(chat_obj, "title", None) or str(chat_id)).strip()
        is_forum = self._is_forum_chat(chat_id)
        topic = _msg_topic(event.message) if is_forum else None
        noun = "话题" if is_forum else "群"
        cands = await self._wx_search_candidates(arg, limit=10)
        if arg and " " not in arg:
            exact = next((c for c in cands if c["id"] == arg), None)
            if exact:
                prev = self._dest_for_thread(exact["id"])
                moved_from_topic = self.topics and not is_forum and prev is not None and prev[1] is not None
                wx_title, _ = await self._complete_link(exact["id"], chat_id, exact["name"], notify_group=False,
                                                        tg_topic=topic)
                notes = await self._standalone_link_notes(chat_id, moved_from_topic) if (self.topics and not is_forum) else ""
                await self._send_group_notice(
                    chat_id, msg_id,
                    f"✅ 已将当前{noun}绑定到微信会话：<b>{_esc(wx_title)}</b>\n<code>{_esc(exact['id'])}</code>{notes}")
                return
        if not cands:
            hint = (f"没有找到匹配「{_esc(arg)}」的微信联系人或群。" if arg
                    else "暂时没有获取到最近的微信会话，请发 <code>/link 关键词</code> 搜索。")
            await self._send_group_notice(chat_id, msg_id, hint)
            return
        buttons = self._wx_picker_buttons(
            cands, action="link_bind",
            extra={"tg_chat": chat_id, "tg_title": tg_title, "in_group": True, "tg_topic": topic},
            current=(chat_id, topic))
        header = f"<b>搜索「{_esc(arg)}」的结果</b>" if arg else f"<b>选择要绑定到此{noun}的微信会话</b>"
        tip = "点下方按钮即可绑定（也可发 <code>/link 关键词</code> 缩小范围）："
        await self._send_group_notice(chat_id, msg_id, f"{header}\n{tip}", buttons=buttons)

    async def _complete_link(self, wx_id: str, tg_chat: int, wx_name: str = "",
                             *, notify_group: bool = False, tg_topic: int | None = None) -> tuple[str, bool]:
        title = wx_name.strip()
        if not title and self.wx_ready:
            try:
                title = (await self.wx.thread(wx_id)).get("displayName") or ""
            except WeChatError:
                if not is_group_id(wx_id):
                    try:
                        title = (await self.wx.contact(wx_id)).get("displayName") or ""
                    except WeChatError:
                        pass
        title = title or wx_id
        if tg_topic and self._is_forum_chat(tg_chat):
            self.store.link_topic(wx_id, tg_chat, tg_topic, title)
        elif self.topics:
            self.store.link_topic_group(wx_id, tg_chat, title)
        else:
            self.store.link(wx_id, tg_chat, title)
        bot_ok = True
        if notify_group:
            sent = await self._send_group_bot_message(
                tg_chat,
                f"🔗 已将此群绑定到微信会话：<b>{_esc(title)}</b>\n<code>{_esc(wx_id)}</code>",
                silent=True)
            bot_ok = sent is not None
        return title, bot_ok

    async def _edit_ui_msg(self, event, text: str, buttons=None) -> None:
        if hasattr(event, "edit"):
            try:
                await event.edit(text, parse_mode="html", buttons=buttons)
                return
            except errors.MessageNotModifiedError:
                return
            except Exception:
                pass
        chat_id = getattr(event, "chat_id", None) or self.owner_id
        if chat_id is not None:
            await self.bot.send_message(chat_id, text, parse_mode="html", buttons=buttons)

    async def _on_link_search_input(self, event, query: str) -> None:
        wait = self._link_wait
        if not wait or wait.get("mode") != "search_wx":
            return
        if not self.wx_ready:
            self._link_wait = None
            await event.reply("微信还没有登录，请先扫描我发的二维码，或发 /wxlogin。")
            return
        cands = await self._wx_search_candidates(query, limit=10)
        if not cands:
            cancel_btn = [[Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))]]
            await event.reply(
                f"没有找到匹配「{_esc(query)}」的微信联系人或群，请换个关键词重试：",
                parse_mode="html", buttons=cancel_btn)
            return
        tg_chat, tg_title = wait["tg_chat"], wait["tg_title"]
        buttons = self._wx_picker_buttons(
            cands, action="link_bind", extra={"tg_chat": tg_chat, "tg_title": tg_title, "in_group": False},
            current=(tg_chat, None))
        await event.reply(
            f"搜索「<b>{_esc(query)}</b>」的结果（将绑定到 <b>{_esc(tg_title)}</b>）：",
            parse_mode="html", buttons=buttons)

    async def _on_ui_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        payload = self._ui_tokens.get(token)
        if not payload:
            await event.answer("按钮已过期，请重新发送命令", alert=True)
            return
        action = payload.get("action")
        if str(action).startswith(("fr_", "add_", "ct_")):
            await self.on_contact_ui(event, payload)
            return
        if str(action).startswith("gp_"):
            await self.on_group_ui(event, payload)
            return
        if action == "cancel":
            self._link_wait = None
            await self._edit_ui_msg(event, "已取消操作。")
            await event.answer("已取消")
            return
        if action == "open_wx":
            if not self.wx_ready:
                await event.answer("微信还没有登录，请先 /wxlogin", alert=True)
                return
            wx_id = payload["wx_id"]
            existed = self._dest_for_thread(wx_id) is not None
            try:
                thread = await self.wx.thread(wx_id)
            except WeChatError:
                thread = {"id": wx_id, "kind": payload.get("kind", "direct"),
                          "displayName": payload.get("name") or wx_id,
                          "classification": payload.get("classification")}
            noun = self._thread_noun(wx_id, thread)
            if await self.ensure_chat(thread) is None:
                await self._edit_ui_msg(event, f"⚠️ 无法建立 Telegram {noun}，请查看我刚发的提示。")
                await event.answer()
                return
            title = self._link_title(wx_id) or self._title(thread)
            msg = (f"ℹ️ <b>{_esc(title)}</b> 已经有对应的 Telegram {noun}了" if existed
                   else f"✅ 已建立 Telegram {noun}：<b>{_esc(title)}</b>")
            await self._edit_ui_msg(event, msg)
            await event.answer(f"已有{noun}" if existed else f"已建{noun}")
            return
        if action == "link_pick_tg":
            if not self.wx_ready:
                await event.answer("微信还没有登录，请先 /wxlogin", alert=True)
                return
            tg_chat, tg_title = payload["tg_chat"], payload["tg_title"]
            self._link_wait = {"mode": "search_wx", "tg_chat": tg_chat, "tg_title": tg_title}
            cands = await self._wx_search_candidates("", limit=8)
            buttons = self._wx_picker_buttons(
                cands, action="link_bind", extra={"tg_chat": tg_chat, "tg_title": tg_title, "in_group": False},
                current=(tg_chat, None))
            prompt = (
                f"已选 Telegram 群：<b>{_esc(tg_title)}</b>\n\n"
                + ("请点选下方最近的微信会话，或<b>直接回复微信名称/关键词</b>搜索："
                   if cands else "请<b>直接回复微信名称/关键词</b>搜索要绑定的会话：")
            )
            await self._edit_ui_msg(event, prompt, buttons=buttons)
            await event.answer()
            return
        if action == "link_pick_wx":
            wx_id, wx_name = payload["wx_id"], payload["wx_name"]
            groups = await self._list_tg_groups(limit=12)
            if not groups:
                await self._edit_ui_msg(
                    event,
                    f"已选微信会话：<b>{_esc(wx_name)}</b>\n\n"
                    f"未检测到可选择的 Telegram 群。请直接到目标 Telegram 群里发送 <code>/link {_esc(wx_id)}</code>。")
                await event.answer()
                return
            buttons = self._tg_picker_buttons(
                groups, action="link_bind", extra={"wx_id": wx_id, "wx_name": wx_name, "in_group": False})
            await self._edit_ui_msg(
                event, f"已选微信会话：<b>{_esc(wx_name)}</b>\n\n请点选要绑定的 Telegram 群：", buttons=buttons)
            await event.answer()
            return
        if action == "link_bind":
            self._link_wait = None
            wx_id = payload["wx_id"]
            wx_name = payload.get("wx_name") or ""
            tg_chat = payload["tg_chat"]
            tg_title = payload.get("tg_title") or str(tg_chat)
            in_group = bool(payload.get("in_group"))
            tg_topic = payload.get("tg_topic")
            if tg_topic is None and self._is_forum_chat(tg_chat):
                await self._edit_ui_msg(event, "⚠️ 这是当前话题群，请在其中的某个话题里发送 <code>/link</code>。")
                await event.answer()
                return
            prev = self._dest_for_thread(wx_id)
            moved_from_topic = self.topics and not tg_topic and prev is not None and prev[1] is not None
            wx_title, bot_ok = await self._complete_link(wx_id, tg_chat, wx_name, notify_group=not in_group,
                                                         tg_topic=tg_topic)
            notes = await self._standalone_link_notes(
                tg_chat, moved_from_topic, check_perm=bot_ok) if (self.topics and not tg_topic) else ""
            if in_group:
                text = (f"✅ 已将当前{'话题' if tg_topic else '群'}绑定到微信会话：<b>{_esc(wx_title)}</b>\n"
                        f"<code>{_esc(wx_id)}</code>{notes}")
            else:
                text = f"✅ 已绑定：<b>{_esc(tg_title)}</b> ↔ <b>{_esc(wx_title)}</b>\n<code>{_esc(wx_id)}</code>{notes}"
                if not bot_ok:
                    text += f"\n\n⚠️ 机器人暂未进入该群，请把 @{_esc(self.bot_me.username)} 拉进「{_esc(tg_title)}」后再使用。"
            await self._edit_ui_msg(event, text)
            await event.answer("绑定成功")
            return
        await event.answer()

    # ======================================================================
    # bot commands (private chat with the bot, from you only)
    # ======================================================================
    async def on_bot_command(self, event) -> None:
        cmd, _, arg = event.raw_text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        noun = "话题" if self.topics else "群"
        try:
            if cmd in ("/start", "/help"):
                await event.reply(TOPIC_HELP if self.topics else HELP, parse_mode="html")
            elif cmd == "/status":
                up = int(time.time() - self.started)
                state = self._wx_state_text()
                mode = ""
                if self.topics:
                    forum = f"<code>{self.forum_chat}</code>" if self.forum_chat else "未设置（在群里发 /forum）"
                    mode = f"\n模式：话题群 {forum}"
                    standalone = sum(1 for _, c, _, _ in self.store.list_topic_groups() if not self._is_forum_chat(c))
                    if standalone:
                        mode += f"（另含 {standalone} 个独立群）"
                if self.oa_forum_chat:
                    oa_note = "" if self.cfg.bridge.bridge_official_accounts else "（未开启 bridge_official_accounts）"
                    mode += f"\n公众号话题群：<code>{self.oa_forum_chat}</code>{oa_note}"
                mode += self._wx_auto_relogin_status_line()
                if self.wx_ready:
                    try:
                        page = await self.wx.contact_requests(direction="incoming", actionable=True, limit=1)
                        if pending := int(page.get("pendingIncomingCount") or 0):
                            mode += f"\n好友请求：{pending} 条待处理（发 /requests 查看）"
                    except (WeChatError, ValueError, TypeError):
                        pass  # not readable right now
                await event.reply(f"运行 {up // 3600}h{up % 3600 // 60}m，已转发 {self.forwarded} 条，"
                                  f"已连接 {len(self._link_rows())} 个会话。\n"
                                  f"微信：{state}，账号 <code>{_esc(self.wx.account or '-')}</code>{mode}",
                                  parse_mode="html")
            elif cmd in ("/chats", "/list"):
                rows = self._link_rows()
                kw = arg.lower()
                if kw:
                    rows = [r for r in rows if kw in r[0].lower() or kw in r[1].lower() or kw in r[2]]
                if not rows:
                    return await event.reply("没有找到匹配的已连接会话" if kw else "还没有任何已连接的会话")
                lines = [
                    f"• {_esc(title or wx_id)} — <code>{_esc(wx_id)}</code> ↔ <code>{_esc(where)}</code>"
                    for wx_id, title, where in rows[:40]
                ]
                suffix = f"\n\n共 {len(rows)} 个会话（仅显示前 40 个）" if len(rows) > 40 else f"\n\n共 {len(rows)} 个会话"
                await event.reply("\n".join(lines) + suffix, parse_mode="html")
            elif cmd == "/link" and self.topics:
                parts = arg.split()
                if len(parts) == 2:
                    try:
                        tg_chat = int(parts[1])
                    except ValueError:
                        tg_chat = None
                    if tg_chat is not None:
                        wx_id = parts[0]
                        if tg_chat >= 0:
                            return await event.reply(
                                "TG 群 ID 应为负数（普通群如 <code>-123456</code>，超级群如 <code>-1001234567890</code>）。",
                                parse_mode="html")
                        if self._is_forum_chat(tg_chat):
                            return await event.reply(
                                f"<code>{tg_chat}</code> 是当前话题群，不能直接绑定整个群；"
                                f"请打开其中的某个话题，在话题里发送 <code>/link {_esc(wx_id)}</code>。",
                                parse_mode="html")
                        get_entity = getattr(self.bot, "get_entity", None)
                        if callable(get_entity):
                            try:
                                ent = await get_entity(tg_chat)
                            except Exception:
                                ent = None
                            if getattr(ent, "forum", False):
                                return await event.reply(
                                    f"<code>{tg_chat}</code> 开启了「话题」，不能绑定为独立普通群；"
                                    "请改用未开启话题的普通群，或先在该群发送 <code>/forum</code>。",
                                    parse_mode="html")
                        prev = self._dest_for_thread(wx_id)
                        moved_from_topic = prev is not None and prev[1] is not None
                        wx_title, bot_ok = await self._complete_link(wx_id, tg_chat, notify_group=True)
                        notes = await self._standalone_link_notes(tg_chat, moved_from_topic, check_perm=bot_ok)
                        text = f"✅ 已绑定：<b>{_esc(wx_title)}</b> (<code>{_esc(wx_id)}</code> ↔ <code>{tg_chat}</code>){notes}"
                        if not bot_ok:
                            text += (f"\n\n⚠️ 机器人暂时无法在该群发消息，请先把 @{_esc(self.bot_me.username)} "
                                     "拉进群并设为管理员。")
                        await event.reply(text, parse_mode="html")
                        return
                await event.reply(
                    "<b>话题模式下的绑定方式</b>\n\n"
                    f"• <b>绑定到独立新群</b>：新建一个<b>未开启话题</b>的 Telegram 群，把 @{_esc(self.bot_me.username)} "
                    "拉进群并设为管理员，在群里发送 <code>/link</code> [关键词]（也可在这里直接发 <code>/link 微信ID TG群ID</code>）。\n"
                    "• <b>绑定到话题群的话题</b>：打开要绑定的话题，在话题里发送 <code>/link</code> [关键词]。\n"
                    "• <b>新建话题</b>：直接发 <code>/open</code> [关键词]。",
                    parse_mode="html")
            elif cmd == "/link":
                parts = arg.split()
                if len(parts) == 2:
                    try:
                        tg_chat = int(parts[1])
                    except ValueError:
                        tg_chat = None
                    if tg_chat is not None:
                        wx_id = parts[0]
                        wx_title, _ = await self._complete_link(wx_id, tg_chat, notify_group=False)
                        await event.reply(
                            f"已绑定：<b>{_esc(wx_title)}</b> (<code>{_esc(wx_id)}</code> ↔ <code>{tg_chat}</code>)",
                            parse_mode="html")
                        return
                if not self.wx_ready:
                    return await event.reply(
                        "🔐 微信尚未登录，请先在手机微信上确认登录或扫码登录，也可以点击下方按钮重新发起登录。",
                        buttons=self._wx_login_buttons())
                if arg:
                    cands = await self._wx_search_candidates(arg, limit=10)
                    if not cands:
                        return await event.reply(
                            f"没有找到匹配「{_esc(arg)}」的微信联系人或群。\n"
                            "💡 也可以直接发送 <code>/link</code> 选择 Telegram 群，或到目标群里直接发送 <code>/link</code>。",
                            parse_mode="html")
                    buttons = self._wx_picker_buttons(cands, action="link_pick_wx")
                    return await event.reply(
                        f"<b>搜索「{_esc(arg)}」的结果</b>\n请先点选要绑定的微信会话：",
                        parse_mode="html", buttons=buttons)
                groups = await self._list_tg_groups(limit=12)
                if not groups:
                    return await event.reply(
                        "没有检测到可选择的 Telegram 群。\n\n"
                        "💡 <b>最简单的绑定方式</b>：直接到你要绑定的 Telegram 群里发送 <code>/link</code>。\n"
                        "也可以先发送 <code>/link 微信名称</code> 搜索微信会话。",
                        parse_mode="html")
                buttons = self._tg_picker_buttons(groups, action="link_pick_tg")
                await event.reply(
                    "<b>绑定 Telegram 群与微信会话</b>\n\n"
                    "💡 <b>最省事的方式</b>：直接在目标 Telegram 群里发 <code>/link</code>（无需查 ID）。\n\n"
                    "也可以点选下方 Telegram 群（未绑定的群排在前面）：",
                    parse_mode="html", buttons=buttons)
            elif cmd == "/wxlogin":
                await self.request_wechat_login(force_qr=(arg.lower() in ("qr", "scan")))
            elif cmd in ("/find", "/open", "/requests", "/add", "/newgroup", "/syncnames") and not self.wx_ready:
                await event.reply(
                    "🔐 微信尚未登录，请先在手机微信上确认登录或扫码登录，也可以点击下方按钮重新发起登录。",
                    buttons=self._wx_login_buttons())
            elif cmd == "/requests":
                await self.cmd_requests(event)
            elif cmd == "/add":
                await self.cmd_add(event, arg)
            elif cmd == "/newgroup":
                await self.cmd_new_group(event, arg)
            elif cmd == "/syncnames":
                await self.cmd_sync_names(event)
            elif cmd == "/find":
                cands = await self._wx_search_candidates(arg, limit=15 if arg else 10)
                if not cands:
                    return await event.reply("没有找到匹配的微信联系人或群" if arg else "暂时没有获取到最近的微信会话")
                lines = []
                for c in cands:
                    icon = "👥" if c["kind"] == "group" else ("📰" if _is_official_account(c) else "👤")
                    item_noun = self._thread_noun(c["id"], c)
                    has_chat = self._dest_for_thread(c["id"]) is not None
                    tag = (f"（已绑{item_noun}）" if (has_chat and self.topics and item_noun == "群")
                           else f"（已建{item_noun}）" if has_chat else "")
                    if c.get("relation") == "non_contact":
                        tag += "（非好友）"
                    lines.append(f"{icon} <b>{_esc(c['name'])}</b>{tag} — <code>{_esc(c['id'])}</code>")
                buttons = []
                for c in cands[:8]:
                    item_noun = self._thread_noun(c["id"], c)
                    has_chat = self._dest_for_thread(c["id"]) is not None
                    lbl = f"{'💬 打开' if has_chat else f'➕ 建{item_noun}'}：{c['name'][:22]}"
                    tok = self._make_ui_token({
                        "action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"],
                        "classification": c.get("classification"),
                    })
                    buttons.append([Button.inline(lbl, tok)])
                header = (f"<b>搜索「{_esc(arg)}」的结果</b>（点按钮可直接建{noun}）：\n\n" if arg
                          else f"<b>最近的微信会话</b>（点按钮可直接建{noun}）：\n\n")
                await event.reply(header + "\n".join(lines), parse_mode="html", buttons=buttons)
            elif cmd == "/open":
                if not arg:
                    cands = await self._wx_search_candidates("", limit=10)
                    if not cands:
                        return await event.reply("暂时没有获取到最近的微信会话，请直接发 <code>/open 微信名称</code> 搜索。",
                                                 parse_mode="html")
                    buttons = [
                        [Button.inline(
                            f"{'💬' if self._dest_for_thread(c['id']) is not None else '➕'} {c['name'][:24]}",
                            self._make_ui_token({
                                "action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"],
                                "classification": c.get("classification"),
                            })
                        )]
                        for c in cands[:8]
                    ]
                    buttons.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
                    return await event.reply("请点选要发起聊天的微信会话（也可直接发 <code>/open 关键词</code>）：",
                                             parse_mode="html", buttons=buttons)
                thread = None
                if " " not in arg:
                    try:
                        thread = await self.wx.thread(arg)
                    except WeChatError:
                        pass
                if thread is None:
                    cands = await self._wx_search_candidates(arg, limit=10)
                    if not cands and " " not in arg and arg.startswith(("wxid_", "gh_")):
                        try:
                            contact = await self.wx.contact(arg)
                            cands = [{"id": arg, "name": contact.get("displayName") or arg, "kind": "direct"}]
                        except WeChatError:
                            pass
                    if not cands:
                        return await event.reply(f"没有找到与「{_esc(arg)}」匹配的微信联系人或群")
                    exact = next((c for c in cands if c["id"] == arg), None)
                    if exact or len(cands) == 1:
                        chosen = exact or cands[0]
                        try:
                            thread = await self.wx.thread(chosen["id"])
                        except WeChatError:
                            thread = {
                                "id": chosen["id"], "kind": chosen["kind"], "displayName": chosen["name"],
                                "classification": chosen.get("classification"),
                            }
                    else:
                        buttons = [
                            [Button.inline(
                                f"{'💬' if self._dest_for_thread(c['id']) is not None else '➕'} {c['name'][:24]}",
                                self._make_ui_token({
                                    "action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"],
                                    "classification": c.get("classification"),
                                })
                            )]
                            for c in cands[:8]
                        ]
                        buttons.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
                        return await event.reply(
                            f"找到多个匹配「<b>{_esc(arg)}</b>」的会话，请点选：",
                            parse_mode="html", buttons=buttons)
                target_noun = self._thread_noun(thread["id"], thread)
                existed = self._dest_for_thread(thread["id"]) is not None
                if await self.ensure_chat(thread) is None:
                    return await event.reply(f"⚠️ 无法建立 Telegram {target_noun}，请查看我刚发的提示。")
                title = self._link_title(thread["id"]) or self._title(thread)
                await event.reply(
                    f"这个会话已经有{target_noun}了：<b>{_esc(title)}</b>" if existed
                    else f"已建{target_noun}：<b>{_esc(title)}</b>",
                    parse_mode="html")
        except WeChatError as e:
            await event.reply(f"出错：{e.code}")
