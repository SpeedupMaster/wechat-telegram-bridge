"""Tests for topics mode (forum supergroup, standalone groups, and official-accounts forum)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from telethon import errors, types

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from tests.fakes import (
    FORUM,
    OA_FORUM,
    OWNER,
    FakeBot,
    FakeDaemon,
    FakeForumBot,
    FakeUser,
    _CallbackEvent,
    forum_event,
    tg_event,
)


class TopicModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.daemon = FakeDaemon()
        self.b = await self.make_bridge(forum_chat=FORUM)

    async def make_bridge(self, **bridge_kw):
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(mode="topics", create_interval_seconds=0, **bridge_kw),
                     Path(self.tmp.name) / "config.toml")
        b = Bridge(cfg, user_client=None, bot_client=FakeForumBot())
        await b.wx.http.aclose()
        b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                      headers={"Authorization": "Bearer secret-token"})
        b.wx._poll_initial_delay = 0.0
        b.wx._avatar_poll_interval = 0.0
        b.bot_me = SimpleNamespace(username="mybot", id=1)
        b.owner_id = OWNER
        b.wx_ready = True
        await b.wx.resolve_account()
        return b

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def event(self, mid, tid, direction="incoming"):
        await self.b.handle_event({"cursor": "1", "kind": "message.committed",
                                   "data": {"messageId": mid, "threadId": tid, "origin": "live",
                                            "direction": direction}})

    def topic_of(self, tid):
        return self.b.store.topic_for_thread(tid)[1]

    async def test_one_topic_per_chat_and_messages_land_in_it(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.daemon.add_message("m1", "wxid_bob", "hello")
        self.daemon.add_message("m2", "wxid_bob", "again")
        self.daemon.add_message("g1", "123@chatroom", "hey", sender="wxid_alice")
        await self.event("m1", "wxid_bob")
        await self.event("m2", "wxid_bob")
        await self.event("g1", "123@chatroom")

        bob, team = self.topic_of("wxid_bob"), self.topic_of("123@chatroom")
        self.assertEqual(self.b.bot.topics, {bob: ("Bob", 0x6FB9F0), team: ("👥 Team", 0x8EEE98)})
        self.assertEqual(self.b.store.chat_for_thread("wxid_bob"), None, "groups-mode bindings untouched")
        self.assertTrue(all(s.chat == FORUM for s in self.b.bot.sent))
        by_text = [(s.text, s.kwargs.get("reply_to")) for s in self.b.bot.sent]
        self.assertIn("已连接微信联系人", by_text[0][0])
        self.assertEqual(by_text[0][1], bob)
        self.assertEqual(by_text[1:3], [("hello", bob), ("again", bob)])
        self.assertEqual(by_text[-1], ("<b>Alice in group</b>：\nhey", team))
        self.assertEqual(sorted(where for _, _, where in self.b._link_rows()),
                         sorted([f"{FORUM}#{bob}", f"{FORUM}#{team}"]))

    async def test_your_messages_in_a_topic_go_to_its_wechat_chat(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "question", server="777")
        await self.event("m1", "wxid_bob")
        bob = self.topic_of("wxid_bob")
        bridged = self.b.bot.sent[-1].id

        await self.b.on_forum_message(forum_event(FORUM, 9000, "plain", topic=bob))
        body, key = self.daemon.sends[-1]
        self.assertEqual((body["text"], key), ("plain", f"tg-{FORUM}-9000"))
        self.assertNotIn("quote", body, "the topic marker is not a reply")

        await self.b.on_forum_message(forum_event(FORUM, 9001, "answer", topic=bob, reply_to=bridged))
        self.assertEqual(self.daemon.sends[-1][0]["quote"], {"messageId": "m1", "serverId": "777"})

        n = len(self.daemon.sends)
        await self.b.on_forum_message(forum_event(FORUM, 9002, "in General"))
        await self.b.on_forum_message(forum_event(FORUM, 9003, "unbound topic", topic=bob + 5))
        await self.b.on_forum_message(forum_event(-1001234, 9004, "other group", topic=bob))
        self.assertEqual(len(self.daemon.sends), n)

        await self.b.on_forum_message(forum_event(FORUM, 9005, "/wx", topic=bob))
        self.assertIn("wxid_bob", self.b.bot.sent[-1].text)
        self.assertEqual(self.b.bot.sent[-1].kwargs["reply_to"], 9005)

        # Echo of our own send is dropped, and WeChat quoting it maps back into the topic.
        n_sent = len(self.b.bot.sent)
        self.daemon.add_message("echo", "wxid_bob", "plain", direction="outgoing", server="901")
        await self.event("echo", "wxid_bob", direction="outgoing")
        self.assertEqual(len(self.b.bot.sent), n_sent)

    async def test_deleted_topic_is_recreated_and_rename_edits_topic(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "one")
        self.daemon.add_message("m2", "wxid_bob", "two")
        await self.event("m1", "wxid_bob")
        old = self.topic_of("wxid_bob")
        self.b.bot.deleted_topics.add(old)
        await self.event("m2", "wxid_bob")
        new = self.topic_of("wxid_bob")
        self.assertNotEqual(new, old)
        self.assertEqual((self.b.bot.sent[-1].text, self.b.bot.sent[-1].kwargs["reply_to"]), ("two", new))
        self.assertIsNone(self.b.store.tg_for_wx("wxid_bob", "m1", None), "messages of the deleted topic forgotten")

        self.daemon.threads["wxid_bob"]["displayName"] = "Bobby"
        await self.b.on_forum_message(forum_event(FORUM, 9100, "/rename", topic=new))
        self.assertEqual(self.b.bot.topic_edits, [(new, "Bobby")])
        self.assertIn("已更新话题标题", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(FORUM, 9101, "/rename", topic=new))
        self.assertIn("话题标题已是最新", self.b.bot.sent[-1].text)

        await self.b.on_forum_message(forum_event(FORUM, 9102, "/unlink", topic=new))
        self.assertIsNone(self.b.store.topic_for_thread("wxid_bob"))

    async def test_link_binds_a_topic_and_forum_command(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        await self.b.on_forum_message(forum_event(FORUM, 9200, "/link wxid_bob", topic=555))
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (FORUM, 555))
        self.assertIn("已将当前话题绑定", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(FORUM, 9201, "/link", topic=None))
        self.assertIn("请打开要绑定的话题", self.b.bot.sent[-1].text)

        # /forum: refused while TG_FORUM_CHAT pins another group, and in a group without topics.
        await self.b.on_forum_message(forum_event(-1007777, 9202, "/forum"))
        self.assertIn("TG_FORUM_CHAT", self.b.bot.sent[-1].text)
        self.b.cfg.bridge.forum_chat = 0
        await self.b.on_forum_message(forum_event(-1007777, 9203, "/forum@mybot", is_forum=False))
        self.assertIn("还没有开启「话题」", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(-1007777, 9204, "/forum"))
        self.assertEqual((self.b.forum_chat, self.b.store.get("forum_chat")), (-1007777, "-1007777"))
        self.assertIsNone(self.b._dest_for_thread("wxid_bob"), "bindings of the old forum are kept but inactive")
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (FORUM, 555))

    async def test_no_forum_yet_tells_owner_once(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.b = await self.make_bridge()
        self.assertIsNone(self.b.forum_chat)
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "lost")
        self.daemon.add_message("m2", "wxid_bob", "lost too")
        await self.event("m1", "wxid_bob")
        await self.event("m2", "wxid_bob")
        self.assertEqual(self.b.bot.topics, {})
        warnings = [s for s in self.b.bot.sent if s.chat == OWNER]
        self.assertEqual(len(warnings), 1)
        self.assertIn("/forum", warnings[0].text)

    async def test_pairing_makes_owner_without_account_login(self):
        self.b.owner_id = None
        replies = []

        async def reply(t, **kw):
            replies.append(t)
        self.b.start_pairing()
        code = self.b.pairing_code
        await self.b.on_bot_private(SimpleNamespace(raw_text=f"/login {code}", sender_id=77, chat_id=77,
                                                    reply=reply, respond=reply))
        self.assertTrue(self.b.tg_ready.is_set())
        self.assertEqual((self.b.owner_id, self.b.store.get("owner")), (77, "77"))
        self.assertIn("配对成功", replies[-1])
        self.assertIsNone(self.b.login_step, "no Telegram account login in topics mode")

    async def test_topics_bot_commands_menu(self):
        await self.b.sync_bot_commands()
        menus = dict(self.b.bot.commands)
        group = [c for c, _ in menus[types.BotCommandScopeChats]]
        self.assertIn("forum", group)
        self.assertIn("oaforum", group)
        self.assertIn("avatar", group)
        self.assertIn("link", [c for c, _ in menus[types.BotCommandScopeUsers]])

    async def test_on_demand_standalone_group_link_messaging_quotes_recall_and_unlink(self):
        self.b.tg_ready.set()
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_thread("wxid_carol", "Carol")
        # An old groups-mode binding in `threads` must stay untouched and inactive in topics mode.
        self.b.store.link("wxid_carol", -501, "Carol Old Group")
        self.daemon.add_message("m0", "wxid_bob", "in topic first", server="700")
        await self.event("m0", "wxid_bob")
        old_topic = self.topic_of("wxid_bob")

        # User creates a normal standalone basic group (-601), adds bot, and runs `/link Bob`.
        await self.b.on_forum_message(forum_event(-601, 9300, "/link Bob", is_forum=False))
        picker = self.b.bot.sent[-1]
        self.assertEqual(picker.chat, -601)
        btn = picker.kwargs["buttons"][0][0]
        btn_data = btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data
        cb = _CallbackEvent(OWNER, btn_data)
        await self.b.on_bot_callback(cb)
        self.assertIn("已将当前群绑定到微信会话", cb.edits[-1][0])
        self.assertIn("已解除原话题绑定并保留历史", cb.edits[-1][0])
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (-601, 0))
        self.assertIsNone(self.b.store.chat_for_thread("wxid_bob"), "groups-mode threads table untouched")
        self.assertEqual(self.b.store.chat_for_thread("wxid_carol"), -501)
        self.assertIsNone(self.b._dest_for_thread("wxid_carol"))

        # Old topic is now unbound: posting in it does not send to WeChat.
        sends_before = len(self.daemon.sends)
        await self.b.on_forum_message(forum_event(FORUM, 9301, "old topic msg", topic=old_topic))
        self.assertEqual(len(self.daemon.sends), sends_before)

        # Incoming WeChat message for Bob now lands in standalone group -601 (no topic reply_to).
        self.daemon.add_message("m1", "wxid_bob", "hi in standalone", server="701")
        await self.event("m1", "wxid_bob")
        m1_sent = self.b.bot.sent[-1]
        self.assertEqual((m1_sent.chat, m1_sent.text, m1_sent.kwargs.get("reply_to")),
                         (-601, "hi in standalone", None))

        # Sending plain text and replying to m1 in basic standalone group -601 works (bot-side msg ids).
        await self.b.on_forum_message(forum_event(-601, 9302, "plain from group", is_forum=False))
        self.assertEqual(self.daemon.sends[-1][0]["text"], "plain from group")
        self.assertNotIn("quote", self.daemon.sends[-1][0])

        await self.b.on_forum_message(forum_event(-601, 9303, "reply to m1", reply_to=m1_sent.id, is_forum=False))
        self.assertEqual(self.daemon.sends[-1][0]["quote"], {"messageId": "m1", "serverId": "701"})

        # Echo of our sent message is dropped and links wx_msg, so WeChat quoting it replies to 9302 in -601.
        sent_count = len(self.b.bot.sent)
        self.daemon.add_message("echo_9302", "wxid_bob", "plain from group", direction="outgoing", server="901")
        await self.event("echo_9302", "wxid_bob", direction="outgoing")
        self.assertEqual(len(self.b.bot.sent), sent_count)
        self.daemon.add_message("q_back", "wxid_bob", "quoted yours", type_=49, appMessageType=57,
                                quote={"messageId": "echo_9302", "serverId": "901", "embeddedText": "plain from group"})
        await self.event("q_back", "wxid_bob")
        self.assertEqual((self.b.bot.sent[-1].chat, self.b.bot.sent[-1].kwargs.get("reply_to")), (-601, 9302))

        # /recall and /wx in basic standalone group -601 use bot-side ids directly.
        await self.b.on_forum_message(forum_event(-601, 9304, "/recall", reply_to=9302, is_forum=False))
        self.assertEqual(self.daemon.recalls[-1][0], "echo_9302")
        self.assertEqual(self.b.bot.sent[-1].kwargs.get("reply_to"), 9302)
        await self.b.on_forum_message(forum_event(-601, 9305, "/wx", is_forum=False))
        self.assertIn("wxid_bob", self.b.bot.sent[-1].text)
        self.assertEqual(self.b.bot.sent[-1].kwargs.get("reply_to"), 9305)

        # /status, /chats, /find, /open reflect standalone group accurately.
        replies = []
        async def reply(t, **kw):
            replies.append((t, kw))
        await self.b.on_bot_command(SimpleNamespace(raw_text="/status", reply=reply))
        self.assertIn("另含 1 个独立群", replies[-1][0])
        await self.b.on_bot_command(SimpleNamespace(raw_text="/chats", reply=reply))
        self.assertIn("-601", replies[-1][0])
        await self.b.on_bot_command(SimpleNamespace(raw_text="/find Bob", reply=reply))
        self.assertIn("（已绑群）", replies[-1][0])
        await self.b.on_bot_command(SimpleNamespace(raw_text="/open Bob", reply=reply))
        self.assertIn("这个会话已经有群了", replies[-1][0])

        # /unlink in standalone group -601 falls back to creating a topic on the next WeChat message.
        await self.b.on_forum_message(forum_event(-601, 9306, "/unlink", is_forum=False))
        self.assertIn("自动回到话题群", self.b.bot.sent[-1].text)
        self.assertIsNone(self.b.store.topic_for_thread("wxid_bob"))
        self.daemon.add_message("m2", "wxid_bob", "back to forum")
        await self.event("m2", "wxid_bob")
        new_topic = self.topic_of("wxid_bob")
        self.assertGreater(new_topic, 0)
        self.assertEqual((self.b.bot.sent[-1].chat, self.b.bot.sent[-1].kwargs.get("reply_to")), (FORUM, new_topic))

    async def test_standalone_group_rename_avatar_migration_and_dead_chat_fallback(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        await self.b.on_forum_message(forum_event(-602, 9400, "/link wxid_bob", is_forum=False))
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (-602, 0))

        # /rename and /avatar work via self.bot (self.user is None in topics mode).
        self.daemon.threads["wxid_bob"]["displayName"] = "Bobby"
        await self.b.on_forum_message(forum_event(-602, 9401, "/rename", is_forum=False))
        self.assertEqual(self.b.bot.title_edits[-1], (602, "Bobby"))
        self.assertEqual(self.b._link_title("wxid_bob"), "Bobby")
        self.assertIn("已更新群标题", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(-602, 9402, "/rename", is_forum=False))
        self.assertIn("群标题已是最新", self.b.bot.sent[-1].text)

        await self.b.on_forum_message(forum_event(-602, 9403, "/avatar", is_forum=False))
        self.assertEqual(len(self.b.bot.photo_edits), 1)
        self.assertEqual(self.b.bot.photo_edits[-1][0], 602)
        self.assertEqual(self.b.bot.sent[-1].text, "已更新群头像")

        # Auto-rename on contact change updates the standalone group title via bot.
        self.daemon.add_contact("wxid_bob", "Bob Remark", remark="Bob Remark")
        await self.b.handle_event({"cursor": "2", "kind": "contacts.updated", "data": {"contactId": "wxid_bob"}})
        self.assertEqual(self.b.bot.title_edits[-1], (602, "Bob Remark"))

        # Basic group -> supergroup migration updates topics table and msgmap.
        mig = types.UpdateNewMessage(
            message=types.MessageService(
                id=9404, peer_id=types.PeerChat(602), date=None,
                action=types.MessageActionChatMigrateTo(channel_id=6020000000),
            ),
            pts=1, pts_count=1,
        )
        await self.b.on_raw_update(mig)
        super_id = -1006020000000
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (super_id, 0))
        self.daemon.add_contact("wxid_bob", "Bob Super", remark="Bob Super")
        await self.b.on_forum_message(forum_event(super_id, 9405, "/rename", is_forum=False))
        self.assertEqual(self.b.bot.title_edits[-1], (6020000000, "Bob Super"))

        # If the standalone group becomes unreachable (e.g. bot kicked), fall back to a topic and notify owner.
        self.b.bot.dead_chats.add(super_id)
        self.daemon.add_message("m_dead", "wxid_bob", "after kick")
        await self.event("m_dead", "wxid_bob")
        fallback_dest = self.b.store.topic_for_thread("wxid_bob")
        self.assertIsNotNone(fallback_dest)
        self.assertEqual(fallback_dest[0], FORUM)
        self.assertGreater(fallback_dest[1], 0)
        self.assertEqual((self.b.bot.sent[-1].chat, self.b.bot.sent[-1].text), (FORUM, "after kick"))
        owner_notices = [s.text for s in self.b.bot.sent if s.chat == OWNER]
        self.assertTrue(any("已解除独立群绑定并退回话题群" in t for t in owner_notices))

    async def test_standalone_group_guards_private_link_and_permission_warning(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_thread("gh_news", "Daily News")
        self.daemon.threads["gh_news"]["classification"] = {"category": "business"}

        # 1) Unbound non-forum group ignores unrelated text/commands; unbound forum rejects /link.
        before = len(self.b.bot.sent)
        await self.b.on_forum_message(forum_event(-603, 9500, "random chat", is_forum=False))
        await self.b.on_forum_message(forum_event(-603, 9501, "/help", is_forum=False))
        self.assertEqual(len(self.b.bot.sent), before)
        await self.b.on_forum_message(forum_event(-1007777, 9502, "/link wxid_bob", is_forum=True))
        self.assertIn("开启了「话题」", self.b.bot.sent[-1].text)
        self.assertIsNone(self.b.store.topic_for_thread("wxid_bob"))

        # 2) Permission warning when privacy mode is on and bot is not admin; silent when bot is admin.
        admin_flag = False
        async def fake_perms(chat_id, user):
            return SimpleNamespace(is_admin=admin_flag)
        self.b.bot.get_permissions = fake_perms
        await self.b.on_forum_message(forum_event(-603, 9503, "/link wxid_bob", is_forum=False))
        self.assertIn("设为本群<b>管理员</b>", self.b.bot.sent[-1].text)
        admin_flag = True
        await self.b.on_forum_message(forum_event(-603, 9504, "/link wxid_bob", is_forum=False))
        self.assertNotIn("设为本群<b>管理员</b>", self.b.bot.sent[-1].text)

        # 3) Switching /forum preserves standalone group binding while old forum topics become inactive.
        self.b.store.link_topic("wxid_alice", FORUM, 555, "Alice")
        self.b.cfg.bridge.forum_chat = 0
        await self.b.on_forum_message(forum_event(-1007778, 9505, "/forum"))
        self.assertIsNone(self.b._dest_for_thread("wxid_alice"))
        self.assertEqual(self.b._dest_for_thread("wxid_bob"), (-603, None))

        # 4) /forum and /oaforum reject a chat currently bound as a standalone group.
        await self.b.on_forum_message(forum_event(-603, 9506, "/forum", is_forum=True))
        self.assertIn("当前已绑定到一个普通微信会话", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(-603, 9507, "/oaforum", is_forum=True))
        self.assertIn("当前已绑定到一个普通微信会话", self.b.bot.sent[-1].text)

        # 5) Private /link in topics mode: guide without args, direct bind with 2 args + guards.
        replies = []
        async def reply(t, **kw):
            replies.append(t)
        async def fake_get_entity(cid):
            return SimpleNamespace(forum=(cid == -1007779))
        self.b.bot.get_entity = fake_get_entity
        await self.b.on_bot_command(SimpleNamespace(raw_text="/link", reply=reply))
        self.assertIn("绑定到独立新群", replies[-1])
        await self.b.on_bot_command(SimpleNamespace(raw_text="/link wxid_bob 12345", reply=reply))
        self.assertIn("应为负数", replies[-1])
        await self.b.on_bot_command(SimpleNamespace(raw_text="/link wxid_bob -1007778", reply=reply))
        self.assertIn("是当前话题群", replies[-1])
        await self.b.on_bot_command(SimpleNamespace(raw_text="/link wxid_bob -1007779", reply=reply))
        self.assertIn("开启了「话题」", replies[-1])
        await self.b.on_bot_command(SimpleNamespace(raw_text="/link wxid_bob -1008888", reply=reply))
        self.assertIn("已绑定", replies[-1])
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (-1008888, 0))
        self.assertEqual(self.b.bot.sent[-1].chat, -1008888)

        # 6) Official account linked to a standalone group overrides oa_forum_chat; /unlink returns it.
        self.b.oa_forum_chat = OA_FORUM
        await self.b.on_forum_message(forum_event(-604, 9508, "/link gh_news", is_forum=False))
        self.assertEqual(self.b._dest_for_thread("gh_news"), (-604, None))
        self.daemon.add_message("oa1", "gh_news", "oa in group")
        await self.event("oa1", "gh_news")
        self.assertEqual((self.b.bot.sent[-1].chat, self.b.bot.sent[-1].kwargs.get("reply_to")), (-604, None))
        await self.b.on_forum_message(forum_event(-604, 9509, "/unlink", is_forum=False))
        self.daemon.add_message("oa2", "gh_news", "oa back to oa forum")
        await self.event("oa2", "gh_news")
        self.assertEqual(self.b.store.topic_for_thread("gh_news")[0], OA_FORUM)

        # 7) Re-linking wxid_bob into a forum topic unbinds standalone group -1008888.
        await self.b.on_forum_message(forum_event(-1007778, 9510, "/link wxid_bob", topic=888))
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (-1007778, 888))
        self.assertIsNone(self.b.store.thread_for_topic_group(-1008888))

        # 8) Switching back to groups mode ignores topics-mode standalone groups and keeps `threads` bindings intact.
        self.b.store.link_topic_group("wxid_carol", -605, "Carol Topic-Mode Group")
        self.b.store.link("wxid_carol", -501, "Carol Groups-Mode Group")
        cfg_g = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                       BridgeConfig(mode="groups", create_interval_seconds=0, official_accounts_forum_chat=OA_FORUM),
                       Path(self.tmp.name) / "config.toml")
        gb = Bridge(cfg_g, user_client=None, bot_client=None)
        try:
            self.assertEqual(gb._dest_for_thread("wxid_carol"), (-501, None))
            self.assertEqual(dict((w, dst) for w, _, dst in gb._link_rows()).get("wxid_carol"), "-501")
        finally:
            await gb.close()
            gb.store.db.close()


class OfficialAccountTopicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.daemon = FakeDaemon()
        self.b = await self.make_bridge(bridge_official_accounts=True, official_accounts_forum_chat=OA_FORUM)

    async def make_bridge(self, *, mode: str = "groups", **bridge_kw):
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(mode=mode, create_interval_seconds=0, sync_avatar=True, **bridge_kw),
                     Path(self.tmp.name) / "config.toml")
        b = Bridge(cfg, user_client=None if mode == "topics" else FakeUser(), bot_client=FakeForumBot())
        await b.wx.http.aclose()
        b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                      headers={"Authorization": "Bearer secret-token"})
        b.wx._poll_initial_delay = 0.0
        b.wx._avatar_poll_interval = 0.0
        b.bot_me = SimpleNamespace(username="mybot", id=1)
        b.bot_input = "bot_in"
        b.owner_id = OWNER
        b.wx_ready = True
        await b.wx.resolve_account()
        return b

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def event(self, mid, tid, direction="incoming"):
        await self.b.handle_event({"cursor": "1", "kind": "message.committed",
                                   "data": {"messageId": mid, "threadId": tid, "origin": "live",
                                            "direction": direction}})

    async def test_groups_mode_routes_official_accounts_to_dedicated_forum_topics(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_thread("gh_news", "Daily News")
        self.daemon.threads["gh_news"]["classification"] = {"category": "business"}
        self.daemon.add_thread("newsapp", "Tencent News")
        self.daemon.threads["newsapp"]["classification"] = {"category": "business"}
        self.daemon.add_message("m1", "wxid_bob", "hi bob")
        self.daemon.add_message("o1", "gh_news", "morning brief")
        self.daemon.add_message("o2", "newsapp", "breaking")

        await self.event("m1", "wxid_bob")
        await self.event("o1", "gh_news")
        await self.event("o2", "newsapp")

        # Friend gets a standalone Telegram group; official accounts get individual topics in OA_FORUM.
        self.assertEqual(self.b.user.created, ["Bob"])
        bob_chat = self.b.store.chat_for_thread("wxid_bob")
        self.assertIsNotNone(bob_chat)
        self.assertIsNone(self.b.store.chat_for_thread("gh_news"))
        self.assertIsNone(self.b.store.chat_for_thread("newsapp"))

        news_dest = self.b.store.topic_for_thread("gh_news")
        app_dest = self.b.store.topic_for_thread("newsapp")
        self.assertIsNotNone(news_dest)
        self.assertIsNotNone(app_dest)
        self.assertEqual(news_dest[0], OA_FORUM)
        self.assertEqual(app_dest[0], OA_FORUM)
        self.assertNotEqual(news_dest[1], app_dest[1])
        self.assertEqual(
            self.b.bot.topics,
            {news_dest[1]: ("Daily News", 0xFFD67E), app_dest[1]: ("Tencent News", 0xFFD67E)},
        )
        # Never try to sync avatar onto the forum supergroup.
        self.assertNotIn("gh_news", self.daemon.avatar_refreshed)
        self.assertNotIn("newsapp", self.daemon.avatar_refreshed)

        oa_msgs = [(s.text, s.kwargs.get("reply_to")) for s in self.b.bot.sent if s.chat == OA_FORUM]
        self.assertIn(("morning brief", news_dest[1]), oa_msgs)
        self.assertIn(("breaking", app_dest[1]), oa_msgs)
        self.assertTrue(any("已连接微信公众号/服务号" in t and rt == news_dest[1] for t, rt in oa_msgs))

        # Both standalone groups and OA topics appear in _link_rows() and /status.
        rows = dict((wx, where) for wx, _, where in self.b._link_rows())
        self.assertEqual(rows["wxid_bob"], str(bob_chat))
        self.assertEqual(rows["gh_news"], f"{OA_FORUM}#{news_dest[1]}")
        self.assertEqual(rows["newsapp"], f"{OA_FORUM}#{app_dest[1]}")
        replies = []
        async def reply(t, **kw):
            replies.append(t)
        await self.b.on_bot_command(SimpleNamespace(raw_text="/status", reply=reply))
        self.assertIn(f"公众号话题群：<code>{OA_FORUM}</code>", replies[-1])

    async def test_oa_topic_reply_commands_rename_delete_and_no_double_link(self):
        self.daemon.add_thread("gh_news", "Daily News")
        self.daemon.threads["gh_news"]["classification"] = {"category": "business"}
        self.daemon.add_message("o1", "gh_news", "push 1", server="501")
        await self.event("o1", "gh_news")
        topic = self.b.store.topic_for_thread("gh_news")[1]
        bridged = self.b.bot.sent[-1].id

        # Sending a message / reply in the OA topic goes to WeChat.
        await self.b.on_forum_message(forum_event(OA_FORUM, 9500, "关键词", topic=topic))
        self.assertEqual(self.daemon.sends[-1][0]["threadId"], "gh_news")
        self.assertEqual(self.daemon.sends[-1][0]["text"], "关键词")
        await self.b.on_forum_message(forum_event(OA_FORUM, 9501, "回复", topic=topic, reply_to=bridged))
        self.assertEqual(self.daemon.sends[-1][0]["quote"], {"messageId": "o1", "serverId": "501"})

        # /avatar is refused for topics; /rename edits the topic (never the forum group).
        await self.b.on_forum_message(forum_event(OA_FORUM, 9502, "/avatar", topic=topic))
        self.assertIn("话题不支持设置头像", self.b.bot.sent[-1].text)
        self.daemon.threads["gh_news"]["displayName"] = "Daily News Pro"
        await self.b.on_forum_message(forum_event(OA_FORUM, 9503, "/rename", topic=topic))
        self.assertEqual(self.b.bot.topic_edits, [(topic, "Daily News Pro")])
        self.assertIn("已更新话题标题", self.b.bot.sent[-1].text)

        # User-side unlinked command handler must ignore OA_FORUM so /link is not handled twice.
        before = len(self.b.bot.sent)
        await self.b.on_tg_unlinked_command(forum_event(OA_FORUM, 9504, "/link gh_news", topic=topic))
        self.assertEqual(len(self.b.bot.sent), before)

        # Deleted OA topic is recreated on the next push.
        self.b.bot.deleted_topics.add(topic)
        self.daemon.add_message("o2", "gh_news", "push 2")
        await self.event("o2", "gh_news")
        new_topic = self.b.store.topic_for_thread("gh_news")[1]
        self.assertNotEqual(new_topic, topic)
        self.assertEqual((self.b.bot.sent[-1].text, self.b.bot.sent[-1].kwargs["reply_to"]), ("push 2", new_topic))

        # /unlink removes topic binding; /link inside a topic rebinds it into topics table.
        await self.b.on_forum_message(forum_event(OA_FORUM, 9505, "/unlink", topic=new_topic))
        self.assertIsNone(self.b.store.topic_for_thread("gh_news"))
        await self.b.on_forum_message(forum_event(OA_FORUM, 9506, "/link gh_news", topic=777))
        self.assertEqual(self.b.store.topic_for_thread("gh_news"), (OA_FORUM, 777))
        self.assertIsNone(self.b.store.chat_for_thread("gh_news"))

    async def test_oaforum_command_and_open_noun_and_separate_forum_in_topics_mode(self):
        # Fixed by TG_OA_FORUM_CHAT -> cannot change via /oaforum.
        await self.b.on_forum_message(forum_event(-1009000000003, 9600, "/oaforum"))
        self.assertIn("TG_OA_FORUM_CHAT", self.b.bot.sent[-1].text)

        # Unpin config -> non-forum rejected, forum accepted, warning shown if bridge_official_accounts=False.
        self.b.cfg.bridge.official_accounts_forum_chat = 0
        self.b.cfg.bridge.bridge_official_accounts = False
        await self.b.on_forum_message(forum_event(-1009000000003, 9601, "/oaforum", is_forum=False))
        self.assertIn("还没有开启「话题」", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(-1009000000003, 9602, "/oaforum"))
        self.assertEqual((self.b.oa_forum_chat, self.b.store.get("oa_forum_chat")),
                         (-1009000000003, "-1009000000003"))
        self.assertIn("BRIDGE_OFFICIAL_ACCOUNTS=true", self.b.bot.sent[-1].text)

        # /open reports "话题" for OA and "群" for a normal friend.
        self.daemon.add_thread("gh_news", "Daily News")
        self.daemon.threads["gh_news"]["classification"] = {"category": "business"}
        self.daemon.add_thread("wxid_bob", "Bob")
        replies = []
        async def reply(t, **kw):
            replies.append(t)
        await self.b.on_bot_command(SimpleNamespace(raw_text="/open gh_news", reply=reply))
        self.assertIn("已建话题", replies[-1])
        self.assertEqual(self.b.store.topic_for_thread("gh_news")[0], -1009000000003)
        await self.b.on_bot_command(SimpleNamespace(raw_text="/open wxid_bob", reply=reply))
        self.assertIn("已建群", replies[-1])
        self.assertIsNotNone(self.b.store.chat_for_thread("wxid_bob"))

        # /oaforum off clears the binding.
        await self.b.on_forum_message(forum_event(-1009000000003, 9603, "/oaforum off"))
        self.assertIsNone(self.b.oa_forum_chat)
        self.assertEqual(self.b.store.get("oa_forum_chat"), "")

        # Topics mode with a separate OA forum splits normal chats and OAs into two forums.
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.b = await self.make_bridge(
            mode="topics", forum_chat=FORUM, bridge_official_accounts=True, official_accounts_forum_chat=OA_FORUM,
        )
        self.daemon.add_message("m1", "wxid_bob", "hi")
        self.daemon.add_message("o1", "gh_news", "article")
        await self.event("m1", "wxid_bob")
        await self.event("o1", "gh_news")
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob")[0], FORUM)
        self.assertEqual(self.b.store.topic_for_thread("gh_news")[0], OA_FORUM)
