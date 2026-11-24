"""Tests for bot command menus, /members, interactive /link, /find, and /open pickers."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from telethon import types

from tests.fakes import BridgeTestMixin, _CallbackEvent, tg_event


class CommandAndPickerTests(BridgeTestMixin, unittest.IsolatedAsyncioTestCase):
    async def test_group_mentions_and_members_command(self):
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.daemon.group_members["123@chatroom"] = [
            {"id": "lucifer231", "displayName": "永明-Sophia", "mentionName": "永明-Sophia", "roomDisplayName": None},
            {"id": "wxid_jessie", "displayName": "Jessie", "mentionName": "Jessie", "roomDisplayName": None},
            {"id": "wxid_uk", "displayName": "JP Office", "mentionName": "JP EU&UK Manchester Office",
             "roomDisplayName": "JP EU&UK Manchester Office"},
        ]
        self.daemon.add_message("g1", "123@chatroom", "hi", sender="wxid_jessie")
        await self.event("g1", "123@chatroom")
        chat = self.b.store.chat_for_thread("123@chatroom")

        await self.b.on_tg_message(tg_event(chat, 10, "/members"))
        self.assertIn("<code>@永明-Sophia</code>", self.b.bot.sent[-1].text)
        self.assertIn("<code>@JP EU&amp;UK Manchester Office</code>", self.b.bot.sent[-1].text)
        self.assertEqual(self.daemon.sends, [])

        await self.b.on_tg_message(tg_event(chat, 11, "/members@mybot jessie"))
        self.assertIn("<code>@Jessie</code>", self.b.bot.sent[-1].text)
        self.assertNotIn("永明-Sophia", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(
            chat, 12, "@jessie你好 @JP EU&UK Manchester Office 看下 @永明-Sophia 邮箱 test@Jessie.com"))
        body, _ = self.daemon.sends[-1]
        self.assertEqual(body["kind"], "text")
        self.assertEqual(body["mentions"], {"all": False, "userIds": ["wxid_jessie", "wxid_uk", "lucifer231"]})

        await self.b.on_tg_message(tg_event(chat, 13, "@所有人 开会了"))
        body, _ = self.daemon.sends[-1]
        self.assertEqual(body["mentions"], {"all": True, "userIds": []})

        await self.b.on_tg_message(tg_event(chat, 14, "mail me at foo@all.com"))
        body, _ = self.daemon.sends[-1]
        self.assertNotIn("mentions", body)

        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "hi")
        await self.event("m1", "wxid_bob")
        dm_chat = self.b.store.chat_for_thread("wxid_bob")
        await self.b.on_tg_message(tg_event(dm_chat, 20, "/members"))
        self.assertIn("只有在微信群聊里", self.b.bot.sent[-1].text)
        await self.b.on_tg_message(tg_event(dm_chat, 21, "@Jessie hi"))
        self.assertNotIn("mentions", self.daemon.sends[-1][0])

    async def test_group_member_search_nickname_and_alias(self):
        self.daemon.add_thread("room@chatroom", "Book Club", kind="group")
        self.daemon.group_members["room@chatroom"] = [
            {
                "id": "wxid_a1",
                "username": "wxid_a1",
                "displayName": "Book club",
                "roomDisplayName": "Lantern",
                "nickname": "Serena",
                "alias": "sreads42",
                "mentionName": "Lantern",
            },
            {
                "id": "wxid_b2",
                "username": "wxid_b2",
                "displayName": "Quiet",
                "roomDisplayName": None,
                "nickname": "Other",
                "alias": None,
                "mentionName": "Quiet",
            },
        ]
        self.daemon.add_message("rm1", "room@chatroom", "hi", sender="wxid_b2")
        await self.event("rm1", "room@chatroom")
        chat = self.b.store.chat_for_thread("room@chatroom")

        # 1) Unfiltered /members shows remark + nickname concisely (without noise from alias)
        await self.b.on_tg_message(tg_event(chat, 10, "/members"))
        self.assertIn("<code>@Lantern</code> （Book club · Serena）", self.b.bot.sent[-1].text)
        self.assertNotIn("sreads42", self.b.bot.sent[-1].text)
        self.assertIsNone(self.daemon.member_searches[-1][1])

        # 2) Searching by nickname / alias / multi-term AND sends `?search=` and displays matched alias
        await self.b.on_tg_message(tg_event(chat, 11, "/members serena"))
        self.assertEqual(self.daemon.member_searches[-1], ("room@chatroom", "serena"))
        self.assertIn("<code>@Lantern</code> （Book club · Serena）", self.b.bot.sent[-1].text)
        self.assertNotIn("Quiet", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 12, "/members sreads42"))
        self.assertEqual(self.daemon.member_searches[-1], ("room@chatroom", "sreads42"))
        self.assertIn("<code>@Lantern</code> （Book club · Serena · 微信号 sreads42）", self.b.bot.sent[-1].text)
        self.assertNotIn("Quiet", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 13, "/members serena lantern"))
        self.assertIn("<code>@Lantern</code>", self.b.bot.sent[-1].text)
        self.assertNotIn("Quiet", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 14, "/members serena quiet"))
        self.assertIn("没有匹配的群成员", self.b.bot.sent[-1].text)

        # 3) @nickname and @alias resolve to the member even behind a remark and roomDisplayName
        await self.b.on_tg_message(tg_event(chat, 16, "@Serena 看下这个"))
        self.assertEqual(self.daemon.sends[-1][0]["mentions"], {"all": False, "userIds": ["wxid_a1"]})
        await self.b.on_tg_message(tg_event(chat, 17, "ping @sreads42"))
        self.assertEqual(self.daemon.sends[-1][0]["mentions"], {"all": False, "userIds": ["wxid_a1"]})

    async def test_bot_commands_menu_and_management_commands(self):
        await self.b.sync_bot_commands()
        scopes = {s: cmds for s, cmds in self.b.bot.commands}
        self.assertIn(types.BotCommandScopeDefault, scopes)
        self.assertIn(types.BotCommandScopeUsers, scopes)
        self.assertIn(types.BotCommandScopeChats, scopes)
        self.assertIn("chats", [c for c, _ in scopes[types.BotCommandScopeUsers]])
        self.assertIn("rename", [c for c, _ in scopes[types.BotCommandScopeChats]])
        self.assertIn("link", [c for c, _ in scopes[types.BotCommandScopeChats]])

        replies = []

        async def private_cmd(text):
            ev = SimpleNamespace(raw_text=text)
            async def reply(msg, **kw):
                replies.append(msg)
            ev.reply = reply
            await self.b.on_bot_command(ev)

        await private_cmd("/chats")
        self.assertIn("还没有任何已连接的会话", replies[-1])

        self.daemon.add_thread("wxid_alice", "Alice Remark")
        self.daemon.add_message("m1", "wxid_alice", "hi")
        await self.event("m1", "wxid_alice")
        chat = self.b.store.chat_for_thread("wxid_alice")

        await private_cmd("/chats alice")
        self.assertIn("Alice Remark", replies[-1])
        self.assertIn("wxid_alice", replies[-1])

        await self.b.on_tg_message(tg_event(chat, 10, "/wx"))
        self.assertIn("Alice Remark", self.b.bot.sent[-1].text)
        self.assertIn("wxid_alice", self.b.bot.sent[-1].text)

        self.daemon.threads["wxid_alice"]["displayName"] = "Alice New"
        await self.b.on_tg_message(tg_event(chat, 11, "/rename"))
        self.assertEqual(self.b.user.titles[-1][1], "Alice New")
        self.assertIn("已更新群标题：Alice New", self.b.bot.sent[-1].text)
        self.assertEqual(self.b.store.list_threads()[0][2], "Alice New")

        await self.b.on_tg_message(tg_event(chat, 12, "/help"))
        self.assertIn("/rename", self.b.bot.sent[-1].text)
        self.assertIn("/unlink", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 13, "/unlink"))
        self.assertIsNone(self.b.store.thread_for_chat(chat))
        self.assertIn("已解除当前群与微信会话", self.b.bot.sent[-1].text)

        await private_cmd(f"/link wxid_alice {chat}")
        self.assertEqual(self.b.store.thread_for_chat(chat), "wxid_alice")
        self.assertIn("已绑定", replies[-1])

    async def test_interactive_link_private_and_group(self):
        self.b.tg_ready.set()
        self.b.owner_id = 999
        self.b.wx_ready = True
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_thread("123@chatroom", "Product Team", kind="group")
        self.daemon.add_contact("wxid_charlie", "Charlie Zhang")

        # Existing linked group + two unlinked Telegram groups
        self.b.store.link("wxid_alice", -501, "Alice")
        self.b.user.dialogs = [
            SimpleNamespace(id=-501, title="Alice", is_group=True),
            SimpleNamespace(id=-777, title="My Work Group", is_group=True),
            SimpleNamespace(id=-888, title="Family Chat", is_group=True),
            SimpleNamespace(id=12345, title="Private DM", is_group=False),
        ]

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        def make_private_ev(text):
            sent_box = []
            async def reply(msg, **kw):
                sent_box.append((msg, kw))
                return SimpleNamespace(text=msg, kwargs=kw)
            return SimpleNamespace(sender_id=999, raw_text=text, reply=reply), sent_box

        # 1) Private /link (no args): lists TG groups with unlinked first
        ev, box = make_private_ev("/link")
        await self.b.on_bot_private(ev)
        text, kw = box[-1]
        self.assertIn("点选下方 Telegram 群", text)
        buttons = kw["buttons"]
        self.assertEqual(buttons[0][0].text, "💬 My Work Group")
        self.assertIn("（已绑）", buttons[2][0].text)

        # Unauthorized callback click is rejected
        unauth = _CallbackEvent(111, btn_data(buttons[0][0]))
        await self.b.on_bot_callback(unauth)
        self.assertEqual(unauth.answers, [("只有桥接账号本人可以操作", True)])

        # Pick "My Work Group" -> enters WeChat search/recent picker
        cb_tg = _CallbackEvent(999, btn_data(buttons[0][0]))
        await self.b.on_bot_callback(cb_tg)
        self.assertIn("My Work Group", cb_tg.edits[-1][0])
        self.assertIsNotNone(self.b._link_wait)

        # User types a WeChat name in private chat instead of picking from recent
        ev_kw, box_kw = make_private_ev("Charlie")
        await self.b.on_bot_private(ev_kw)
        kw_text, kw_opts = box_kw[-1]
        self.assertIn("Charlie", kw_text)
        charlie_btn = kw_opts["buttons"][0][0]
        self.assertIn("Charlie Zhang", charlie_btn.text)

        # Click Charlie -> binds wxid_charlie <-> -777 and notifies the group
        cb_bind = _CallbackEvent(999, btn_data(charlie_btn))
        await self.b.on_bot_callback(cb_bind)
        self.assertEqual(self.b.store.thread_for_chat(-777), "wxid_charlie")
        self.assertIsNone(self.b._link_wait)
        self.assertIn("Charlie Zhang", cb_bind.edits[-1][0])
        self.assertEqual(self.b.bot.sent[-1].chat, -777)

        # 2) Private /link <keyword>: search WeChat first, then pick TG group
        ev2, box2 = make_private_ev("/link Product")
        await self.b.on_bot_private(ev2)
        wx_btn = box2[-1][1]["buttons"][0][0]
        self.assertIn("Product Team", wx_btn.text)
        cb_wx = _CallbackEvent(999, btn_data(wx_btn))
        await self.b.on_bot_callback(cb_wx)
        tg_btns = cb_wx.edits[-1][1]["buttons"]
        family_btn = next(r[0] for r in tg_btns if "Family Chat" in r[0].text)
        cb_bind2 = _CallbackEvent(999, btn_data(family_btn))
        await self.b.on_bot_callback(cb_bind2)
        self.assertEqual(self.b.store.thread_for_chat(-888), "123@chatroom")

        # 3) Sending /link directly inside an UNLINKED group where bot is not yet a member
        self.b.bot.dead_chats.add(-999)
        self.b.user.on_invite = lambda cid: self.b.bot.dead_chats.discard(cid)
        unlinked_other_cmd = tg_event(-999, 1, "/help")
        unlinked_other_cmd.is_private = False
        await self.b.on_tg_unlinked_command(unlinked_other_cmd)
        self.assertEqual(self.b.user.invited, [])  # unrelated /commands in unlinked groups are ignored

        unlinked_link = tg_event(-999, 2, "/link Alice")
        unlinked_link.is_private = False
        unlinked_link.chat = SimpleNamespace(title="New Manual Group")
        await self.b.on_tg_unlinked_command(unlinked_link)
        self.assertEqual(self.b.user.invited, [-999])
        group_picker = self.b.bot.sent[-1]
        self.assertEqual(group_picker.chat, -999)
        alice_btn = group_picker.kwargs["buttons"][0][0]
        cb_group_bind = _CallbackEvent(999, btn_data(alice_btn))
        await self.b.on_bot_callback(cb_group_bind)
        self.assertEqual(self.b.store.thread_for_chat(-999), "wxid_alice")
        self.assertIn("已将当前群绑定到微信会话", cb_group_bind.edits[-1][0])

        # 4) Sending /link inside an already-linked group switches its binding
        await self.b.on_tg_message(tg_event(-999, 3, "/link Charlie"))
        charlie_group_btn = self.b.bot.sent[-1].kwargs["buttons"][0][0]
        cb_rebind = _CallbackEvent(999, btn_data(charlie_group_btn))
        await self.b.on_bot_callback(cb_rebind)
        self.assertEqual(self.b.store.thread_for_chat(-999), "wxid_charlie")

    async def test_find_and_open_without_raw_ids(self):
        self.b.tg_ready.set()
        self.b.owner_id = 999
        self.b.wx_ready = True
        self.daemon.add_thread("wxid_alice", "Alice Wang")
        self.daemon.add_thread("wxid_alan", "Alan Wu")
        self.daemon.add_contact("wxid_david", "David Li")

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        boxes = []
        async def cmd(text):
            ev = SimpleNamespace(sender_id=999, raw_text=text)
            async def reply(msg, **kw):
                boxes.append((msg, kw))
                return SimpleNamespace(text=msg, kwargs=kw)
            ev.reply = reply
            await self.b.on_bot_private(ev)

        # /find with no args shows recent threads with one-click buttons
        await cmd("/find")
        text, kw = boxes[-1]
        self.assertIn("最近的微信会话", text)
        self.assertIn("Alice Wang", text)
        alice_btn = kw["buttons"][0][0]
        cb_open = _CallbackEvent(999, btn_data(alice_btn))
        await self.b.on_bot_callback(cb_open)
        self.assertIsNotNone(self.b.store.chat_for_thread("wxid_alice"))
        self.assertIn("已建立 Telegram 群", cb_open.edits[-1][0])

        # /open with unambiguous keyword opens directly without needing wxid
        await cmd("/open David")
        self.assertIn("已建群：<b>David Li</b>", boxes[-1][0])
        self.assertIsNotNone(self.b.store.chat_for_thread("wxid_david"))

        # /open with no args or ambiguous keyword shows buttons
        await cmd("/open")
        self.assertIn("请点选要发起聊天的微信会话", boxes[-1][0])
        await cmd("/open A")
        self.assertIn("找到多个匹配", boxes[-1][0])
        self.assertGreaterEqual(len(boxes[-1][1]["buttons"]), 3)  # Alice + Alan + Cancel
