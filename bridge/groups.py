"""Managing WeChat groups from Telegram: /group card, create, rename, members, my name, mute, announcement, leave.

The daemon's group changes need an admin-scoped token and an Idempotency-Key; each returns an operation we wait on.
A press of the same confirm button reuses its key, so a double tap or a retry never makes a second change.
"""
from __future__ import annotations

import hashlib
import html
import logging
import secrets

from telethon import Button

from .contacts import CONTACT_ERRORS
from .emojis import replace_wx_emojis
from .wechat import OP_DONE, OP_OK, WeChatError, is_group_id

log = logging.getLogger("groups")

# Commands handled here inside a bridged chat (group or topic).
GROUP_COMMANDS = ("/group", "/groupinfo", "/groupname", "/myname", "/invite", "/kick", "/mute", "/unmute", "/leave")

GROUP_ERRORS = {
    "forbidden": "daemon 令牌没有 admin 权限，不能修改群（请给桥接用的令牌加上 admin 范围）",
    "insufficient_scope": "daemon 令牌没有 admin 权限，不能修改群（请给桥接用的令牌加上 admin 范围）",
    "needs_review": "结果未知：微信可能已经收到了请求。请在手机微信上确认，不要重复操作",
    "operation_interrupted": "操作被中断，请在手机微信上确认结果",
    "not_found": "找不到这个群（可能已经退出，或本地还没有同步）",
    "invalid_group_identity": "这不是一个有效的微信群",
    "ordinary_group_required": "只支持普通微信群（不支持企业微信群等）",
    "ordinary_contact_required": "只支持普通微信联系人",
    "ordinary_thread_required": "只支持普通微信联系人或群",
    "thread_session_unavailable": "这个会话暂时不可用，请稍后再试",
    "group_mutation_unavailable": "当前 agent-wechat 版本不支持修改群",
    "idempotency_key_conflict": "这个操作刚刚已经提交过，请点「查看结果」",
    "mutation_in_progress": "这个群还有一个修改在处理中，请稍后再试",
    "rejected": "微信拒绝了这个操作（可能没有权限）",
    "remote_rejected": "微信拒绝了这个操作（可能没有权限）",
    "failed": "微信没有完成这个操作",
    "cancelled": "操作已取消",
    "expired": "操作已过期，请重新操作",
}

PICK_PAGE = 10        # members shown per picker page (two per row)
PICK_MAX = 40         # WeChat takes at most 40 members per change
NAME_MAX_BYTES = 512  # group name / my display name limit
_SYSTEM_IDS = {"filehelper", "weixin", "fmessage", "medianote", "floatbottle", "newsapp", "qqmail", "qmessage"}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def group_error(code: str | None) -> str:
    code = str(code or "unknown")
    return GROUP_ERRORS.get(code) or CONTACT_ERRORS.get(code) or code


def _api_error(e: WeChatError) -> str:
    if e.status == 403 and e.code not in GROUP_ERRORS:
        return GROUP_ERRORS["forbidden"]
    return group_error(e.code)


def _digest(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _member_label(m: dict | None, uid: str = "") -> str:
    """Best human name on a member or contact dict, skipping values that merely echo `uid`."""
    if not m:
        return ""
    for key in ("remark", "displayName", "roomDisplayName", "nickname", "mentionName"):
        val = str(m.get(key) or "").strip()
        if val and val != uid:
            return val
    return ""


def _label(m: dict) -> str:
    """A member or contact as shown to me: my remark first (agent-wechat's `displayName`), then their own names."""
    uid = str(m.get("id") or m.get("username") or "").strip()
    return _member_label(m, uid) or uid or "?"


def _member_id(m) -> str:
    if isinstance(m, dict):
        return str(m.get("username") or m.get("id") or m.get("userId") or "")
    return str(m or "")


def op_outcome(op: dict, names: dict[str, str] | None = None) -> tuple[str, str]:
    """("ok" | "pending" | "review" | "failed", detail lines) for a finished (or still running) group change."""
    names = names or {}
    state = op.get("state") or "unknown"
    result = op.get("result") if isinstance(op.get("result"), dict) else {}
    if state in OP_OK:
        notes: list[str] = []
        odd = [m for m in result.get("members") or []
               if isinstance(m, dict) and m.get("status") not in (None, 0)]
        unconfirmed = [_member_id(m) for m in result.get("unconfirmedMembers") or [] if _member_id(m)]
        if odd:
            who = "、".join(_esc(names.get(_member_id(m)) or _member_id(m)) for m in odd)
            notes.append(f"⚠️ 以下成员没有直接加入（可能需要对方确认邀请，或无法添加）：{who}")
        if unconfirmed:
            who = "、".join(_esc(names.get(u) or u) for u in unconfirmed)
            notes.append(f"❔ 以下成员的结果未确认，请在手机微信上查看：{who}")
        return "ok", "\n".join(notes)
    if state == "needs_review":
        return "review", group_error("needs_review")
    if state not in OP_DONE:
        return "pending", "⏳ 微信还在处理，稍后点「查看结果」"
    err = op.get("error") if isinstance(op.get("error"), dict) else {}
    return "failed", group_error(result.get("code") or op.get("reason") or err.get("code") or state)


class GroupsMixin:
    """WeChat group management commands and their buttons. Mixed into Bridge."""

    # --- commands in a bridged chat -------------------------------------------------
    async def on_group_command(self, chat_id: int, reply_to: int | None, thread_id: str, cmd: str, arg: str) -> None:
        async def say(text: str, buttons=None) -> None:
            await self.bot.send_message(chat_id, text, parse_mode="html", buttons=buttons, reply_to=reply_to,
                                        link_preview=False)

        if not self.wx_ready:
            await say("🔐 微信尚未登录，登录后再管理群聊。")
            return
        is_group = is_group_id(thread_id)
        try:
            if cmd in ("/mute", "/unmute"):
                await self._cmd_mute(say, thread_id, cmd == "/mute")
                return
            if not is_group:
                await say("这是单聊，不是微信群。单聊里可以用 <code>/mute</code> / <code>/unmute</code> 设置免打扰。")
                return
            if cmd in ("/group", "/groupinfo"):
                text, buttons = await self.group_card(thread_id)
                await say(text, buttons)
            elif cmd == "/groupname":
                await self._cmd_rename(say, thread_id, arg)
            elif cmd == "/myname":
                await self._cmd_my_name(say, thread_id, arg)
            elif cmd == "/invite":
                text, buttons = await self._start_pick("invite", thread_id, arg)
                await say(text, buttons)
            elif cmd == "/kick":
                text, buttons = await self._start_pick("kick", thread_id, arg)
                await say(text, buttons)
            elif cmd == "/leave":
                text, buttons = await self._leave_prompt(thread_id)
                await say(text, buttons)
        except WeChatError as e:
            await say(f"⚠️ 操作失败：{_esc(_api_error(e))}")

    async def cmd_new_group(self, event, arg: str) -> None:
        """Private /newgroup 关键词 [关键词…] [| 群名]."""
        terms, name = arg, ""
        for sep in ("|", "｜"):
            if sep in arg:
                terms, _, name = arg.partition(sep)
                break
        name = name.strip()
        if len(name.encode()) > NAME_MAX_BYTES:
            await event.reply("群名太长了（最多 512 字节）")
            return
        try:
            text, buttons = await self._start_pick("create", "", terms.strip(), name=name)
        except WeChatError as e:
            text, buttons = f"⚠️ 操作失败：{_esc(_api_error(e))}", None
        await event.reply(text, parse_mode="html", buttons=buttons)

    async def _group_member_name(self, room: str, uid: str, member: dict | None = None,
                                 *, fresh: bool = False) -> str:
        """How to show one group member (owner/admin) to me: '我', my remark, their group name, nickname, or ID."""
        if uid and uid == self._self_wxid:
            return "我"
        remark, nick = await self._contact_names(uid, fresh=fresh)
        if remark:
            return remark
        if member is None:
            try:
                member = await self.wx.group_member(room, uid)
            except WeChatError:
                member = None
        if label := _member_label(member, uid):
            return label
        return nick or uid

    # --- the group card ------------------------------------------------------------------
    async def group_card(self, room: str, *, note: str = "", fresh_names: bool = False) -> tuple[str, list | None]:
        g = await self.wx.group(room)
        members: list[dict] = []
        try:
            members = await self.wx.group_members(room)
        except WeChatError as e:
            log.info("Could not list members of %s: %s", room, e.code)
        members_by_id = {
            str(uid): m for m in members if (uid := m.get("id") or m.get("username"))
        }
        owner = str(g.get("ownerId") or "")
        admins = [str(a) for a in g.get("administratorIds") or [] if a and str(a) != owner]
        role_names: dict[str, str] = {}
        for uid in (owner, *admins[:5]):
            if uid and uid not in role_names:
                role_names[uid] = await self._group_member_name(
                    room, uid, members_by_id.get(uid), fresh=fresh_names)

        def who(uid: str) -> str:
            return _esc(role_names.get(uid) or uid)

        title = g.get("name") or g.get("displayName") or room
        lines = [f"👥 <b>{_esc(title)}</b>", f"ID：<code>{_esc(room)}</code>"]
        if (remark := g.get("remark")) and remark != title:
            lines.append(f"我的备注：{_esc(remark)}")
        if members:
            lines.append(f"成员：{len(members)} 人")
        if owner:
            lines.append(f"群主：{who(owner)}")
        if admins:
            shown = "、".join(who(a) for a in admins[:5]) + (f" 等 {len(admins)} 人" if len(admins) > 5 else "")
            lines.append(f"管理员：{shown}")
        lines.append(f"我的群昵称：{_esc(g.get('myDisplayName')) or '（未设置）'}")
        muted = g.get("muted")
        lines.append("消息免打扰：" + ("🔕 已开启" if muted else "🔔 未开启" if muted is False else "❔ 未知"))
        announcement = replace_wx_emojis((g.get("announcement") or "").strip())
        if announcement:
            short = announcement if len(announcement) <= 300 else announcement[:300] + "…"
            lines += ["", "📌 <b>群公告</b>", _esc(short)]
        in_group = g.get("isInGroup")
        if in_group is False:
            lines += ["", "⚠️ 你已经不在这个群里了。"]
        if note:
            lines += ["", note]
        lines += ["", "💡 <code>/groupname 新群名</code> 改群名 · <code>/myname 昵称</code> 改我的群昵称"]

        def tok(action: str, **kw) -> bytes:
            return self._make_ui_token({"action": action, "room": room, "title": title, **kw})

        buttons: list = [[
            Button.inline("🔔 关闭免打扰" if muted else "🔕 开启免打扰", tok("gp_mute", muted=not muted)),
            Button.inline("🔄 从微信刷新", tok("gp_refresh")),
        ]]
        if in_group is not False:
            buttons.append([Button.inline("➕ 邀请成员", tok("gp_pick", kind="invite")),
                            Button.inline("➖ 移出成员", tok("gp_pick", kind="kick"))])
            last = []
            if announcement:
                last.append(Button.inline("🧹 清空群公告", tok("gp_clear_ask")))
            last.append(Button.inline("🚪 退出群聊", tok("gp_leave_ask")))
            buttons.append(last)
        return "\n".join(lines), buttons

    # --- simple commands -------------------------------------------------------------------
    async def _cmd_mute(self, say, thread_id: str, muted: bool) -> None:
        key = f"grp-mute-{secrets.token_hex(8)}"
        op = await self.wx.set_thread_muted(thread_id, muted, key)
        status, detail = op_outcome(op)
        if status == "ok":
            await say("🔕 已开启微信消息免打扰" if muted else "🔔 已关闭微信消息免打扰")
        else:
            await self._say_outcome(say, status, detail, key, "设置免打扰")

    async def _cmd_rename(self, say, room: str, arg: str) -> None:
        name = " ".join(arg.split())
        if not name:
            try:
                current = (await self.wx.group(room)).get("name") or ""
            except WeChatError:
                current = ""
            await say("✏️ <b>修改微信群名</b>\n\n用法：<code>/groupname 新群名</code>"
                      + (f"\n当前群名：{_esc(current)}" if current else "")
                      + "\n\n群里所有人都会看到改名提示；群主开启了「仅群主可改群名」时只有群主能改。")
            return
        if len(name.encode()) > NAME_MAX_BYTES:
            await say("群名太长了（最多 512 字节）")
            return
        key = f"grp-rename-{secrets.token_hex(8)}"
        op = await self.wx.rename_group(room, name, key)
        status, detail = op_outcome(op)
        if status == "ok":
            await say(f"✅ 已把微信群名改为「{_esc(name)}」")
        else:
            await self._say_outcome(say, status, detail, key, "修改群名")

    async def _cmd_my_name(self, say, room: str, arg: str) -> None:
        name = " ".join(arg.split())
        if not name:
            try:
                current = (await self.wx.group(room)).get("myDisplayName") or ""
            except WeChatError:
                current = ""
            await say("🏷 <b>修改我在本群的昵称</b>\n\n用法：<code>/myname 新昵称</code>\n"
                      f"当前：{_esc(current) or '（未设置）'}")
            return
        if len(name.encode()) > NAME_MAX_BYTES:
            await say("昵称太长了（最多 512 字节）")
            return
        key = f"grp-myname-{secrets.token_hex(8)}"
        op = await self.wx.set_my_group_name(room, name, key)
        status, detail = op_outcome(op)
        if status == "ok":
            await say(f"✅ 已把我在本群的昵称改为「{_esc(name)}」")
        else:
            await self._say_outcome(say, status, detail, key, "修改群昵称")

    async def _say_outcome(self, say, status: str, detail: str, key: str, what: str) -> None:
        buttons = None
        if status in ("pending", "review"):
            buttons = [[Button.inline("🔄 查看结果", self._make_ui_token({"action": "gp_check", "key": key,
                                                                          "what": what}))]]
        await say(f"⚠️ {what}：{_esc(detail)}" if status == "failed" else f"{what}：{_esc(detail)}", buttons)

    async def _leave_prompt(self, room: str, title: str = "") -> tuple[str, list]:
        if not title:
            try:
                g = await self.wx.group(room)
                title = g.get("name") or g.get("displayName") or room
            except WeChatError:
                title = room
        noun = self._thread_noun(room)
        buttons = [[
            Button.inline("🚪 确认退出", self._make_ui_token(
                {"action": "gp_leave", "room": room, "title": title, "key": f"grp-leave-{secrets.token_hex(8)}"})),
            Button.inline("↩️ 取消", self._make_ui_token({"action": "cancel"})),
        ]]
        return (f"确定要退出微信群 <b>{_esc(title)}</b> 吗？\n\n退出后聊天记录仍会保留，当前 Telegram {noun}的绑定也不变；"
                "想再回来需要群成员重新邀请你。", buttons)

    # --- member picker (invite / kick / create) -------------------------------------------
    async def _start_pick(self, kind: str, room: str, query: str, *, name: str = "") -> tuple[str, list | None]:
        group: dict = {}
        if kind == "kick":
            group = await self.wx.group(room)
            if why := self._not_manager(group):
                return f"⚠️ {why}", None
            members = await self.wx.group_members(room, search=query or None)
            items = [{"id": str(m["id"]), "label": (self._known_remark(str(m["id"])) or _label(m))
                      + (" 👑" if m.get("id") == group.get("ownerId") else "")}
                     for m in members if m.get("id") and m.get("id") != self._self_wxid]
            preselect: set[str] = set()
        else:
            exclude: set[str] = set()
            if kind == "invite":
                try:
                    exclude = {str(m.get("id")) for m in await self.wx.group_members(room)}
                except WeChatError:
                    pass
            items, preselect = await self._friend_candidates(query, exclude)
        if not items:
            if kind == "kick":
                return ("没有匹配的群成员" if query else "没有可以移出的群成员"), None
            hint = ("没有找到匹配的微信好友" if query else "最近没有可选的单聊好友") + \
                "，可以换个关键词再发一次（例如 <code>{} 张三 李四</code>）。".format(
                    "/invite" if kind == "invite" else "/newgroup")
            return hint, None
        if len(self._group_picks) >= 50:
            for k in list(self._group_picks)[:25]:
                self._group_picks.pop(k, None)
        pid = secrets.token_hex(5)
        pick = {"id": pid, "kind": kind, "room": room, "name": name, "items": items[:200],
                "selected": [i["id"] for i in items if i["id"] in preselect][:PICK_MAX], "page": 0,
                "title": group.get("name") or group.get("displayName") or ""}
        self._group_picks[pid] = pick
        return self._pick_view(pick)

    def _not_manager(self, group: dict) -> str:
        """Why this account may not remove members / clear the announcement, when the saved data can tell."""
        me, owner = self._self_wxid, group.get("ownerId")
        admins = group.get("administratorIds")
        if not me or not owner or admins is None:
            return ""
        if me == owner or me in admins:
            return ""
        return "只有群主或群管理员可以这样做"

    async def _friend_candidates(self, query: str, exclude: set[str]) -> tuple[list[dict], set[str]]:
        items: list[dict] = []
        seen: set[str] = set(exclude)
        preselect: set[str] = set()

        def ok(c: dict) -> bool:
            cid = str(c.get("id") or "")
            category = (c.get("classification") or {}).get("category")
            return bool(cid) and cid not in seen and not is_group_id(cid) and not cid.startswith("gh_") \
                and cid not in _SYSTEM_IDS and cid != self._self_wxid and category not in ("business", "system")

        def add(c: dict) -> None:
            cid = str(c["id"])
            seen.add(cid)
            items.append({"id": cid, "label": _label(c)})

        terms = query.split()
        if not terms:
            for t in await self.wx.list_threads(limit=40):
                if t.get("kind") != "group" and ok(t):
                    add(t)
            return items[:30], preselect
        for term in terms:
            found = [c for c in await self.wx.search_contacts(term, limit=10, relation="friend")
                     if c.get("relation", "friend") == "friend"]
            for c in found:
                if ok(c):
                    add(c)
            # A keyword that names exactly one friend is ticked already.
            exact = [c for c in found if term.lower() in {str(c.get(k) or "").lower() for k in
                                                          ("displayName", "remark", "nickname", "alias", "id")}]
            single = exact if len(exact) == 1 else found if len(found) == 1 else []
            if single and any(i["id"] == str(single[0].get("id")) for i in items):
                preselect.add(str(single[0]["id"]))
        return items, preselect

    def _pick_view(self, pick: dict, note: str = "") -> tuple[str, list]:
        kind, items, selected = pick["kind"], pick["items"], pick["selected"]
        labels = {i["id"]: i["label"] for i in items}
        pages = max(1, -(-len(items) // PICK_PAGE))
        page = min(max(0, pick["page"]), pages - 1)
        pick["page"] = page
        if kind == "invite":
            head = "➕ <b>邀请好友进群</b>" + (f"：{_esc(pick['title'])}" if pick.get("title") else "")
        elif kind == "kick":
            head = "➖ <b>移出群成员</b>" + (f"：{_esc(pick['title'])}" if pick.get("title") else "")
        else:
            head = "👥 <b>新建微信群</b>" + (f"：{_esc(pick['name'])}" if pick.get("name") else "")
        lines = [head, "", "点名字勾选，选好后点下方确认。"]
        if kind == "create":
            lines.append("至少要选 2 位好友（不含你自己）" + ("" if pick.get("name") else "；不填群名时微信会用成员名字命名") + "。")
        if kind == "invite":
            lines.append("开启了「群聊邀请确认」的群，需要群主同意后对方才会进群。")
        if selected:
            lines += ["", f"已选 {len(selected)} 人：" + "、".join(_esc(labels.get(s, s)) for s in selected[:20])
                      + ("…" if len(selected) > 20 else "")]
        lines.append("\n💡 名单里没有要找的人？换个关键词重新发 <code>{}</code>。".format(
            {"invite": "/invite 关键词", "kick": "/kick 关键词", "create": "/newgroup 关键词 关键词 | 群名"}[kind]))
        if note:
            lines += ["", note]

        def tok(action: str, **kw) -> bytes:
            return self._make_ui_token({"action": action, "pick": pick["id"], **kw})

        chunk = list(enumerate(items))[page * PICK_PAGE:(page + 1) * PICK_PAGE]
        buttons: list = []
        row: list = []
        for idx, item in chunk:
            mark = "✅" if item["id"] in selected else "⬜"
            row.append(Button.inline(f"{mark} {item['label'][:20]}", tok("gp_toggle", i=idx)))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        if pages > 1:
            buttons.append([Button.inline("◀️", tok("gp_page", page=page - 1)),
                            Button.inline(f"{page + 1}/{pages}", tok("gp_page", page=page)),
                            Button.inline("▶️", tok("gp_page", page=page + 1))])
        verb = {"invite": "➕ 邀请", "kick": "⚠️ 移出", "create": "👥 建群"}[kind]
        buttons.append([Button.inline(f"{verb}（{len(selected)} 人）", tok("gp_pick_go")),
                        Button.inline("❌ 取消", tok("gp_pick_cancel"))])
        return "\n".join(lines), buttons

    # --- button presses -------------------------------------------------------------------
    async def on_group_ui(self, event, payload: dict) -> None:
        if not self.wx_ready:
            await event.answer("微信还没有登录，请先 /wxlogin", alert=True)
            return
        action = payload.get("action")
        handler = {
            "gp_mute": self._ui_mute,
            "gp_refresh": self._ui_refresh,
            "gp_card": self._ui_card,
            "gp_pick": self._ui_pick_open,
            "gp_toggle": self._ui_pick_toggle,
            "gp_page": self._ui_pick_page,
            "gp_pick_go": self._ui_pick_go,
            "gp_pick_cancel": self._ui_pick_cancel,
            "gp_clear_ask": self._ui_clear_ask,
            "gp_clear": self._ui_clear,
            "gp_leave_ask": self._ui_leave_ask,
            "gp_leave": self._ui_leave,
            "gp_unlink": self._ui_unlink,
            "gp_check": self._ui_check,
        }.get(action)
        if handler is None:
            await event.answer()
            return
        try:
            await handler(event, payload)
        except WeChatError as e:
            await self._edit_ui_msg(event, f"⚠️ 操作失败：{_esc(_api_error(e))}")
        except Exception as e:
            log.exception("Group action %s failed", action)
            await self._edit_ui_msg(event, f"⚠️ 操作失败：{_esc(type(e).__name__)}")

    def _back(self, p: dict) -> list:
        return [Button.inline("↩️ 返回群资料", self._make_ui_token({"action": "gp_card", "room": p["room"]}))]

    async def _ui_card(self, event, p: dict, note: str = "") -> None:
        await event.answer()
        text, buttons = await self.group_card(p["room"], note=note)
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_mute(self, event, p: dict) -> None:
        muted = bool(p.get("muted"))
        await event.answer("正在设置…")
        key = f"grp-mute-{secrets.token_hex(8)}"
        op = await self.wx.set_thread_muted(p["room"], muted, key)
        status, detail = op_outcome(op)
        note = ("🔕 已开启消息免打扰" if muted else "🔔 已关闭消息免打扰") if status == "ok" else f"⚠️ {_esc(detail)}"
        text, buttons = await self.group_card(p["room"], note=note)
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_refresh(self, event, p: dict) -> None:
        await event.answer("正在从微信刷新…")
        notes = []
        for members, what in ((False, "群资料"), (True, "成员名单")):
            try:
                status, detail = op_outcome(await self.wx.refresh_group(p["room"], members=members))
            except WeChatError as e:
                status, detail = "failed", _api_error(e)
            if status != "ok":
                notes.append(f"⚠️ 刷新{what}：{_esc(detail)}")
        text, buttons = await self.group_card(p["room"], note="\n".join(notes) or "✅ 已从微信刷新", fresh_names=True)
        await self._edit_ui_msg(event, text, buttons)

    def _pick_for(self, p: dict) -> dict | None:
        return self._group_picks.get(p.get("pick") or "")

    async def _ui_pick_open(self, event, p: dict) -> None:
        await event.answer()
        text, buttons = await self._start_pick(p["kind"], p["room"], "")
        await self._edit_ui_msg(event, text, buttons or [self._back(p)])

    async def _ui_pick_toggle(self, event, p: dict) -> None:
        pick = self._pick_for(p)
        if pick is None or pick.get("busy"):
            await event.answer("这个名单已过期，请重新发送命令" if pick is None else "正在处理…", alert=pick is None)
            return
        i = int(p.get("i", -1))
        if not 0 <= i < len(pick["items"]):
            await event.answer()
            return
        cid = pick["items"][i]["id"]
        if cid in pick["selected"]:
            pick["selected"].remove(cid)
        elif len(pick["selected"]) >= PICK_MAX:
            await event.answer(f"一次最多选 {PICK_MAX} 人", alert=True)
            return
        else:
            pick["selected"].append(cid)
        await event.answer()
        await self._edit_ui_msg(event, *self._pick_view(pick))

    async def _ui_pick_page(self, event, p: dict) -> None:
        pick = self._pick_for(p)
        if pick is None:
            await event.answer("这个名单已过期，请重新发送命令", alert=True)
            return
        await event.answer()
        if int(p.get("page", 0)) == pick["page"]:
            return
        pick["page"] = int(p.get("page", 0))
        await self._edit_ui_msg(event, *self._pick_view(pick))

    async def _ui_pick_cancel(self, event, p: dict) -> None:
        self._group_picks.pop(p.get("pick") or "", None)
        await event.answer("已取消")
        await self._edit_ui_msg(event, "已取消操作。")

    async def _ui_pick_go(self, event, p: dict) -> None:
        pick = self._pick_for(p)
        if pick is None:
            await event.answer("这个名单已过期，请重新发送命令", alert=True)
            return
        if pick.get("busy"):
            await event.answer("正在处理…")
            return
        kind, chosen = pick["kind"], list(pick["selected"])
        need = 2 if kind == "create" else 1
        if len(chosen) < need:
            await event.answer("至少要选 2 位好友" if kind == "create" else "请先勾选成员", alert=True)
            return
        labels = {i["id"]: i["label"] for i in pick["items"]}
        who = "、".join(labels.get(c, c) for c in chosen[:10]) + ("…" if len(chosen) > 10 else "")
        # Same pick + same selection = same key: a double tap or a retry never makes a second change.
        key = f"grp-{kind}-{pick['id']}-{_digest(*sorted(chosen), pick.get('name') or '')}"
        pick["busy"] = True
        await event.answer("正在提交…")
        verb = {"invite": "邀请", "kick": "移出", "create": "创建群聊"}[kind]
        await self._edit_ui_msg(event, f"⏳ 正在{verb}：{_esc(who)}…")
        try:
            if kind == "invite":
                op = await self.wx.add_group_members(pick["room"], chosen, key)
            elif kind == "kick":
                op = await self.wx.remove_group_members(pick["room"], chosen, key)
            else:
                op = await self.wx.create_group(chosen, pick.get("name") or None, key)
        except WeChatError as e:
            pick["busy"] = False
            await self._edit_ui_msg(event, *self._pick_view(pick, f"⚠️ {verb}失败：{_esc(_api_error(e))}"))
            return
        pick["busy"] = False
        status, detail = op_outcome(op, labels)
        if status == "failed":
            await self._edit_ui_msg(event, *self._pick_view(pick, f"⚠️ {verb}失败：{_esc(detail)}"))
            return
        self._group_picks.pop(pick["id"], None)
        if status != "ok":
            await self._edit_ui_msg(event, f"{verb}：{_esc(detail)}", [[Button.inline(
                "🔄 查看结果", self._make_ui_token({"action": "gp_check", "key": key, "what": verb}))]])
            return
        if kind == "create":
            result = op.get("result") if isinstance(op.get("result"), dict) else {}
            gid = str(result.get("groupId") or result.get("roomId") or "")
            title = pick.get("name") or result.get("name") or who
            text = f"✅ 已创建微信群 <b>{_esc(title)}</b>（{len(chosen)} 位好友）"
            if gid:
                text += f"\n<code>{_esc(gid)}</code>"
            text += f"\n{detail}" if detail else ""
            buttons = None
            if gid:
                noun = "话题" if self.topics else "群"
                buttons = [[Button.inline(f"💬 发起聊天（建{noun}）", self._make_ui_token(
                    {"action": "open_wx", "wx_id": gid, "name": title, "kind": "group"}))]]
            await self._edit_ui_msg(event, text, buttons)
            return
        text = (f"✅ 已邀请 {_esc(who)} 进群" if kind == "invite" else f"✅ 已把 {_esc(who)} 移出群聊")
        text += f"\n{detail}" if detail else ""
        await self._edit_ui_msg(event, text, [self._back({"room": pick["room"]})])

    async def _ui_clear_ask(self, event, p: dict) -> None:
        await event.answer()
        g = await self.wx.group(p["room"])
        if why := self._not_manager(g):
            await self._edit_ui_msg(event, f"⚠️ {why}", [self._back(p)])
            return
        buttons = [[Button.inline("🧹 确认清空", self._make_ui_token(
            {"action": "gp_clear", "room": p["room"], "key": f"grp-clear-{secrets.token_hex(8)}"})),
            *self._back(p)]]
        await self._edit_ui_msg(event, f"确定要清空 <b>{_esc(p.get('title'))}</b> 的群公告吗？群里所有人都会看到公告被清空。",
                                buttons)

    async def _ui_clear(self, event, p: dict) -> None:
        await event.answer("正在清空…")
        op = await self.wx.clear_group_announcement(p["room"], p["key"])
        status, detail = op_outcome(op)
        if status in ("pending", "review"):
            await self._edit_ui_msg(event, f"清空群公告：{_esc(detail)}", [[Button.inline(
                "🔄 查看结果", self._make_ui_token({"action": "gp_check", "key": p["key"], "what": "清空群公告"}))]])
            return
        note = "🧹 已清空群公告" if status == "ok" else f"⚠️ 清空失败：{_esc(detail)}"
        text, buttons = await self.group_card(p["room"], note=note)
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_leave_ask(self, event, p: dict) -> None:
        await event.answer()
        text, buttons = await self._leave_prompt(p["room"], p.get("title") or "")
        await self._edit_ui_msg(event, text, buttons)

    async def _ui_leave(self, event, p: dict) -> None:
        await event.answer("正在退出…")
        op = await self.wx.leave_group(p["room"], p["key"])
        status, detail = op_outcome(op)
        title = p.get("title") or p["room"]
        if status == "ok":
            noun = self._thread_noun(p["room"])
            buttons = None
            if self._dest_for_thread(p["room"]) is not None:
                buttons = [[Button.inline(f"🔗 同时解除 Telegram {noun}绑定", self._make_ui_token(
                    {"action": "gp_unlink", "room": p["room"], "title": title}))]]
            await self._edit_ui_msg(event, f"🚪 已退出微信群 <b>{_esc(title)}</b>，聊天记录保留。", buttons)
        elif status == "failed":
            await self._edit_ui_msg(event, f"⚠️ 退出失败：{_esc(detail)}")
        else:
            await self._edit_ui_msg(event, f"退出群聊：{_esc(detail)}", [[Button.inline(
                "🔄 查看结果", self._make_ui_token({"action": "gp_check", "key": p["key"], "what": "退出群聊"}))]])

    async def _ui_unlink(self, event, p: dict) -> None:
        await event.answer()
        noun = self._thread_noun(p["room"])
        self._unlink_thread(p["room"])
        await self._edit_ui_msg(event, f"🚪 已退出微信群 <b>{_esc(p.get('title'))}</b>，并解除了 Telegram {noun}绑定。")

    async def _ui_check(self, event, p: dict) -> None:
        await event.answer("正在查询…")
        what = p.get("what") or "操作"
        try:
            op = await self.wx.group_mutation_by_key(p["key"])
        except WeChatError as e:
            if e.status == 404:
                await self._edit_ui_msg(event, f"{_esc(what)}：daemon 没有收到这个请求，可以重新操作。")
                return
            raise
        status, detail = op_outcome(op)
        if status == "ok":
            await self._edit_ui_msg(event, f"✅ {_esc(what)}已完成" + (f"\n{detail}" if detail else ""))
            return
        buttons = None if status == "failed" else [[Button.inline("🔄 再次查看", self._make_ui_token(dict(p)))]]
        await self._edit_ui_msg(event, f"{'⚠️ ' if status == 'failed' else ''}{_esc(what)}：{_esc(detail)}", buttons)
