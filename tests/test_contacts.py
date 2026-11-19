"""Friend requests, /add, /contact and relation-aware search (agent-wechat >= 2.7)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from tests.test_bridge import FakeBot, FakeDaemon, FakeUser, _CallbackEvent, tg_event


class ContactsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        await self.make_bridge()

    async def make_bridge(self, **bridge_kw):
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False, **bridge_kw),
                     Path(self.tmp.name) / "config.toml")
        self.daemon = FakeDaemon()
        self.b = Bridge(cfg)
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                           headers={"Authorization": "Bearer secret-token"})
        self.b.user, self.b.bot = FakeUser(), FakeBot()
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.owner_id = 999
        self.b.wx_ready = True
        self.b.tg_ready.set()
        await self.b.wx.resolve_account()

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def fr_event(self, rid, **data):
        await self.b.handle_event({"cursor": "1", "kind": "friend_request.updated",
                                   "data": {"requestId": rid, **data}})

    async def cmd(self, text):
        box = []

        async def reply(msg, **kw):
            box.append((msg, kw))
            return SimpleNamespace(text=msg, kwargs=kw)

        await self.b.on_bot_command(SimpleNamespace(sender_id=999, raw_text=text, reply=reply))
        return box[-1]

    async def click(self, buttons, label):
        btn = next(b for row in buttons for b in row if label in b.text)
        cb = _CallbackEvent(999, btn.type.data if hasattr(btn, "type") else btn.data)
        await self.b.on_bot_callback(cb)
        return cb

    @staticmethod
    def labels(buttons):
        return [[b.text for b in row] for row in buttons or []]

    async def test_incoming_request_pushes_one_card(self):
        self.daemon.add_request("r1", "wxid_new", "New <Friend>", message="你好，我是小王",
                                history=[{"message": "之前的消息", "direction": "incoming", "createdAt": 0}])
        await self.fr_event("r1", source="remote_sync")
        await self.fr_event("r1", source="remote_sync")  # same version: no second card
        self.assertEqual(len(self.b.bot.sent), 1)
        card = self.b.bot.sent[0]
        self.assertEqual(card.chat, 999)
        self.assertIn("新的微信好友请求", card.text)
        self.assertIn("New &lt;Friend&gt;", card.text)
        self.assertIn("wxid_new", card.text)
        self.assertIn("你好，我是小王", card.text)
        self.assertIn("之前的消息", card.text)
        self.assertEqual(self.labels(card.kwargs["buttons"]),
                         [["✅ 通过", "💬 通过并聊天"], ["🔒 通过（仅聊天）", "🗑 删除记录"]])

        self.daemon.requests["r1"]["version"] = "v2"  # a new greeting: shown again
        await self.fr_event("r1", source="remote_sync")
        self.assertEqual(len(self.b.bot.sent), 2)

    async def test_request_events_that_need_no_card(self):
        self.daemon.add_request("out", "wxid_x", "X", direction="outgoing")
        self.daemon.add_request("old", "wxid_o", "Old", attention=False)
        self.daemon.add_request("done", "wxid_d", "Done", actionable=False)
        await self.fr_event("out")
        await self.fr_event("old", source="contact_sync")
        await self.fr_event("done")
        await self.fr_event("missing")
        await self.fr_event("old", operationId="op1", action="accept")
        self.assertEqual(self.b.bot.sent, [])

        await self.b.close()
        self.b.store.db.close()
        await self.make_bridge(friend_requests=False)
        self.daemon.add_request("r1", "wxid_new", "New")
        await self.fr_event("r1")
        self.assertEqual(self.b.bot.sent, [])

    async def test_accept_and_chat_opens_a_group(self):
        self.daemon.add_request("r1", "wxid_new", "New Friend")
        await self.fr_event("r1")
        cb = await self.click(self.b.bot.sent[0].kwargs["buttons"], "通过并聊天")
        method, path, body, key = self.daemon.request_calls[-1]
        self.assertEqual((method, path), ("POST", "contact-requests/r1/accept"))
        self.assertEqual(body, {"expectedVersion": "v1"})
        self.assertTrue(key)
        self.assertEqual(self.b.user.created, ["New Friend"])
        self.assertIsNotNone(self.b.store.chat_for_thread("wxid_new"))
        self.assertIn("已通过 <b>New Friend</b>", cb.edits[-1][0])
        self.assertIn("已建立 Telegram 群", cb.edits[-1][0])

        # Pressing it again: already friends, no second accept
        cb2 = await self.click(self.b.bot.sent[0].kwargs["buttons"], "✅ 通过")
        self.assertEqual(len(self.daemon.request_calls), 1)
        self.assertIn("已经是你的微信好友", cb2.edits[-1][0])

    async def test_accept_chats_only_and_failure_keeps_the_card(self):
        self.daemon.add_request("r1", "wxid_new", "New Friend")
        await self.fr_event("r1")
        buttons = self.b.bot.sent[0].kwargs["buttons"]

        self.daemon.contact_op = {"id": "ctop", "state": "failed", "result": {"code": "contact_privacy_unresolved"}}
        cb = await self.click(buttons, "仅聊天")
        self.assertEqual(self.daemon.request_calls[-1][2],
                         {"expectedVersion": "v1",
                          "privacy": {"chatsOnly": True, "hideMyPosts": False, "hideTheirPosts": False}})
        text, kw = cb.edits[-1]
        self.assertIn("操作失败：暂时读不到和 TA 的权限设置", text)
        self.assertIn("New Friend", text)
        self.assertEqual(len(kw["buttons"]), 2)  # can try again

        self.daemon.contact_op = {"id": "ctop", "state": "succeeded"}
        cb = await self.click(buttons, "仅聊天")
        text, kw = cb.edits[-1]
        self.assertIn("已通过 <b>New Friend</b> 的好友请求（仅聊天）", text)
        self.assertIn("发起聊天", kw["buttons"][0][0].text)

    async def test_delete_request_record(self):
        self.daemon.add_request("r1", "wxid_new", "New Friend")
        await self.fr_event("r1")
        cb = await self.click(self.b.bot.sent[0].kwargs["buttons"], "删除记录")
        self.assertEqual(self.daemon.request_calls[-1][:3],
                         ("DELETE", "contact-requests/r1", {"expectedVersion": "v1"}))
        self.assertNotIn("r1", self.daemon.requests)
        self.assertIn("不会拒绝对方", cb.edits[-1][0])

    async def test_requests_command(self):
        text, kw = await self.cmd("/requests")
        self.assertIn("没有待处理的微信好友请求", text)
        self.assertIn("/add", text)

        self.daemon.add_request("s1", "wxid_s", "Sent One", direction="outgoing", actionable=False)
        text, _ = await self.cmd("/requests")
        self.assertIn("等待对方通过", text)
        self.assertIn("Sent One", text)

        self.daemon.add_request("r1", "wxid_a", "Alice", message="hello")
        text, kw = await self.cmd("/requests")  # just one: the card itself
        self.assertIn("微信好友请求", text)
        self.assertEqual(len(kw["buttons"]), 2)

        self.daemon.add_request("r2", "wxid_b", "Bob", message="")
        text, kw = await self.cmd("/requests")
        self.assertIn("待处理的微信好友请求（2）", text)
        self.assertIn("「hello」", text)
        self.assertEqual(self.labels(kw["buttons"]), [["👤 1. Alice"], ["👤 2. Bob"]])
        cb = await self.click(kw["buttons"], "Bob")
        self.assertIn("<b>Bob</b>", cb.edits[-1][0])
        self.assertEqual(len(cb.edits[-1][1]["buttons"]), 2)

        text, _ = await self.cmd("/status")
        self.assertIn("好友请求：2 条待处理", text)

    async def test_add_friend_flow(self):
        text, _ = await self.cmd("/add")
        self.assertIn("用法", text)

        self.daemon.discovery = {"recipientRef": "ref-1", "expiresAt": 0, "source": "wechat_id",
                                 "request": None, "contactId": None}
        text, kw = await self.cmd("/add carol_88 你好 我是同事")
        self.assertEqual(self.daemon.request_calls[-1][1:3], ("search/contacts", {"query": "carol_88"}))
        self.assertIn("carol_88", text)
        self.assertIn("「你好 我是同事」", text)
        self.assertEqual(self.labels(kw["buttons"]), [["📨 发送好友请求", "🔒 发送（仅聊天）"], ["❌ 取消"]])
        cb = await self.click(kw["buttons"], "📨 发送好友请求")
        method, path, body, key = self.daemon.request_calls[-1]
        self.assertEqual((method, path, body),
                         ("POST", "contact-requests", {"recipientRef": "ref-1", "message": "你好 我是同事"}))
        self.assertTrue(key.startswith("fr-send-"))
        self.assertIn("已向 <code>carol_88</code> 发送好友请求", cb.edits[-1][0])

        self.daemon.contact_op = {"id": "ctop", "state": "failed", "result": {"code": "stale_recipient_reference"}}
        cb = await self.click(kw["buttons"], "仅聊天")
        self.assertIn("搜索结果已过期", cb.edits[-1][0])

        self.daemon.discovery = {"recipientRef": None, "contactId": "wxid_carol", "request": None}
        text, kw = await self.cmd("/add carol_88")
        self.assertIn("已经是你的微信好友", text)
        self.assertIn("发起聊天", kw["buttons"][0][0].text)

        self.daemon.discovery = {"recipientRef": None, "contactId": None, "request": None}
        text, kw = await self.cmd("/add carol_88")
        self.assertIn("暂时无法从这里添加", text)
        self.assertIsNone(kw.get("buttons"))

        self.daemon.discovery = (502, "contact_discovery_rejected")
        text, _ = await self.cmd("/add nobody")
        self.assertIn("微信没有找到这个用户", text)

    async def test_contact_card_privacy_and_removal(self):
        self.daemon.add_thread("wxid_bob", "Bob Remark")
        self.daemon.add_contact("wxid_bob", "Bob Remark", alias="bob123", nickname="Bobby", remark="Bob Remark",
                                relation="friend")
        self.daemon.privacy["wxid_bob"] = {"privacy": {"chatsOnly": False, "hideMyPosts": False,
                                                       "hideTheirPosts": True}, "version": "p1"}
        self.b.store.link("wxid_bob", -600, "Bob Remark")
        await self.b.on_tg_message(tg_event(-600, 10, "/contact"))
        card = self.b.bot.sent[-1]
        self.assertIn("bob123", card.text)
        self.assertIn("昵称：Bobby", card.text)
        self.assertIn("✅ 微信好友", card.text)
        self.assertIn("朋友圈不看 TA", card.text)
        self.assertEqual(self.labels(card.kwargs["buttons"]),
                         [["🔘 聊天、朋友圈等", "⚪ 仅聊天"], ["⬜ 不让 TA 看我", "✅ 不看 TA"],
                          ["🔄 刷新", "🗑 删除好友"]])

        # Pressing the option already chosen changes nothing
        calls = len(self.daemon.request_calls)
        cb = await self.click(card.kwargs["buttons"], "聊天、朋友圈等")
        self.assertEqual(len(self.daemon.request_calls), calls)
        self.assertEqual((cb.edits, cb.answers), ([], [("当前已经是这个设置", False)]))

        cb = await self.click(card.kwargs["buttons"], "不让 TA 看我")
        self.assertEqual(self.daemon.request_calls[-1][:3], (
            "PATCH", "contacts/wxid_bob",
            {"expectedVersion": "p1", "privacy": {"chatsOnly": False, "hideMyPosts": True, "hideTheirPosts": True}}))
        text, kw = cb.edits[-1]
        self.assertIn("已更新", text)
        self.assertEqual(kw["buttons"][1][0].text, "✅ 不让 TA 看我")
        self.assertIn("朋友圈互不可见", text)

        # A stale card (old version) gets refreshed with the reason
        cb = await self.click(card.kwargs["buttons"], "仅聊天")
        self.assertIn("权限刚在别处改过", cb.edits[-1][0])

        cb = await self.click(kw["buttons"], "删除好友")
        self.assertIn("确定要删除微信好友", cb.edits[-1][0])
        confirm = await self.click(cb.edits[-1][1]["buttons"], "确认删除")
        self.assertEqual(self.daemon.request_calls[-1][:2], ("DELETE", "contacts/wxid_bob"))
        self.assertIn("已删除微信好友 <b>Bob Remark</b>", confirm.edits[-1][0])
        self.assertEqual(self.b.store.thread_for_chat(-600), "wxid_bob")  # binding kept

        # Groups have no friend profile
        self.daemon.add_thread("9@chatroom", "Team", kind="group")
        self.b.store.link("9@chatroom", -601, "Team")
        await self.b.on_tg_message(tg_event(-601, 11, "/info"))
        self.assertIn("这是微信群聊", self.b.bot.sent[-1].text)

    async def test_contact_card_reads_privacy_from_contact_and_refreshes_remark(self):
        # agent-wechat 2.8: privacy comes with the contact; the old /privacy route is gone
        self.daemon.add_thread("wxid_amy", "Amy Nick")
        self.daemon.add_contact("wxid_amy", "Amy Nick", nickname="Amy Nick", remark=None, relation="friend",
                                privacy={"chatsOnly": True, "hideMyPosts": False, "hideTheirPosts": False},
                                privacyVersion="v28")
        self.b.store.link("wxid_amy", -610, "Amy Nick")
        self.assertEqual(await self.b._contact_names("wxid_amy"), ("", "Amy Nick"))
        await self.b.on_tg_message(tg_event(-610, 10, "/contact"))
        card = self.b.bot.sent[-1]
        self.assertNotIn("备注", card.text)
        # Chats-only: WeChat has no moments settings then, so neither does the card
        self.assertEqual(self.labels(card.kwargs["buttons"]),
                         [["⚪ 聊天、朋友圈等", "🔘 仅聊天"], ["🔄 刷新", "🗑 删除好友"]])
        self.assertIn("仅聊天（互相看不到朋友圈）", card.text)
        self.assertNotIn("权限暂时读不到", card.text)

        cb = await self.click(card.kwargs["buttons"], "聊天、朋友圈等")
        self.assertEqual(self.daemon.request_calls[-1][2], {
            "expectedVersion": "v28", "privacy": {"chatsOnly": False, "hideMyPosts": False, "hideTheirPosts": False}})

        # 🔄 asks WeChat for the latest details, so a remark missing from the saved profile shows up
        self.daemon.contacts["wxid_amy"].update(remark="艾米", displayName="艾米")
        cb = await self.click(card.kwargs["buttons"], "刷新")
        self.assertIn(("POST", "contacts/wxid_amy/refresh"), [c[:2] for c in self.daemon.request_calls])
        self.assertIn("备注：艾米", cb.edits[-1][0])
        self.assertEqual(await self.b._contact_names("wxid_amy"), ("艾米", "Amy Nick"))

        # A friend whose privacy can't be edited says why
        self.daemon.add_contact("wxid_svc", "Svc", relation="friend", privacy=None, privacyVersion=None,
                                privacyUnavailable="ordinary_contact_required")
        self.daemon.add_thread("wxid_svc", "Svc")
        self.b.store.link("wxid_svc", -611, "Svc")
        await self.b.on_tg_message(tg_event(-611, 11, "/contact"))
        self.assertIn("这类联系人不支持设置朋友圈权限", self.b.bot.sent[-1].text)

    async def test_remark_command_sets_and_clears_the_remark(self):
        self.b._remark_poll = 0
        self.daemon.add_thread("wxid_cat", "Cat Nick")
        self.daemon.add_contact("wxid_cat", "Cat Nick", nickname="Cat Nick", remark=None, relation="friend",
                                privacyVersion="v1")
        self.b.store.link("wxid_cat", -620, "Cat Nick")
        await self.b._contact_names("wxid_cat")  # a cached name that must not outlive the change

        await self.b.on_tg_message(tg_event(-620, 10, "/remark"))
        self.assertIn("当前没有备注", self.b.bot.sent[-1].text)
        self.assertEqual(self.daemon.request_calls, [])

        await self.b.on_tg_message(tg_event(-620, 11, "/remark 小猫"))
        method, path, body, key = self.daemon.request_calls[-1]
        self.assertEqual((method, path, body), ("PATCH", "contacts/wxid_cat", {"expectedVersion": "v1", "remark": "小猫"}))
        self.assertTrue(key)
        self.assertIn("已把备注改为 <b>小猫</b>", self.b.bot.sent[-1].text)
        self.assertEqual(self.b.user.titles[-1][1], "小猫")  # the Telegram title follows
        self.assertEqual(self.b._link_title("wxid_cat"), "小猫")
        self.assertEqual(await self.b._contact_names("wxid_cat"), ("小猫", "Cat Nick"))

        await self.b.on_tg_message(tg_event(-620, 12, "/remark 小猫"))
        self.assertIn("备注没有变化", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(-620, 13, "/remark -"))
        self.assertEqual(self.daemon.request_calls[-1][2], {"expectedVersion": "v1", "remark": None})
        self.assertIn("已清除备注", self.b.bot.sent[-1].text)
        self.assertEqual(self.b._link_title("wxid_cat"), "Cat Nick")

        # An old daemon (no privacyVersion) and a WeChat group can't take a remark
        self.daemon.contacts["wxid_cat"].pop("privacyVersion")
        await self.b.on_tg_message(tg_event(-620, 14, "/remark 喵"))
        self.assertIn("需要 agent-wechat 2.8", self.b.bot.sent[-1].text)
        self.daemon.add_thread("8@chatroom", "Team", kind="group")
        self.b.store.link("8@chatroom", -621, "👥 Team")
        await self.b.on_tg_message(tg_event(-621, 15, "/remark 喵"))
        self.assertIn("只能改好友的备注", self.b.bot.sent[-1].text)

    async def test_syncnames_pulls_the_catalog_without_renaming_chats(self):
        self.daemon.add_thread("wxid_dan", "Dan Nick")
        self.daemon.add_contact("wxid_dan", "Dan Nick", nickname="Dan Nick", remark=None, relation="friend")
        self.daemon.add_thread("wxid_eve", "Eve")
        self.daemon.add_contact("wxid_eve", "Eve", nickname="Eve", remark=None, relation="friend")
        self.b.store.link("wxid_dan", -630, "Dan Nick")
        self.b.store.link("wxid_eve", -631, "Eve")
        self.assertEqual(await self.b._contact_names("wxid_dan"), ("", "Dan Nick"))
        # The first round stops part-way (10 pages / 2 minutes); the next one resumes and finishes
        self.daemon.sync_rounds = [{"complete": False, "resumeAvailable": True},
                                   {"complete": True, "resumeAvailable": False, "failedItems": ["wxid_x"]}]
        self.daemon.sync_remarks = {"wxid_dan": "阿丹"}

        text, _ = await self.cmd("/syncnames")
        self.assertEqual(self.daemon.contact_syncs, 2)
        self.assertIn("已从微信重新同步", text)
        self.assertIn("有 1 个联系人这次没拉到", text)
        # Titles are only listed, never edited: renaming many chats at once hits Telegram's flood limits
        self.assertIn("以下 1 个群的标题与微信不一致", text)
        self.assertIn("• Dan Nick → <b>阿丹</b>", text)
        self.assertEqual(self.b.user.titles, [])
        self.assertEqual(self.b._link_title("wxid_dan"), "Dan Nick")
        self.assertEqual(await self.b._contact_names("wxid_dan"), ("阿丹", "Dan Nick"))

        # The daemon's contacts.updated events for the sync don't rename either
        await self.b.handle_event({"cursor": "9", "kind": "contacts.updated", "data": {"contactId": "wxid_dan"}})
        self.assertEqual(self.b.user.titles, [])

        # Once things are quiet again, a real remark change is followed as before
        self.b._names_quiet_until = 0
        await self.b.handle_event({"cursor": "10", "kind": "contacts.updated", "data": {"contactId": "wxid_dan"}})
        self.assertEqual(self.b.user.titles, [(630, "阿丹")])

        # Nothing left to resume counts as done; titles already match
        self.daemon.sync_rounds = [{"complete": False, "resumeAvailable": False}]
        text, _ = await self.cmd("/syncnames")
        self.assertIn("已从微信重新同步", text)
        self.assertIn("群标题都已是最新", text)

    async def test_search_includes_non_friends(self):
        self.daemon.add_contact("wxid_f", "Zed Friend")
        self.daemon.add_contact("wxid_n", "Zed Stranger", relation="non_contact")
        text, _ = await self.cmd("/find Zed")
        self.assertEqual(self.daemon.contact_searches[-1]["relation"], "all")
        self.assertIn("Zed Stranger</b>（非好友）", text)
        self.assertLess(text.index("Zed Friend"), text.index("Zed Stranger"))

        self.daemon.relations_supported = False  # agent-wechat < 2.7
        text, _ = await self.cmd("/find Zed")
        self.assertNotIn("relation", self.daemon.contact_searches[-1])
        self.assertIn("Zed Friend", text)

    async def test_follows_wechat_renames(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -600, "Bob")
        await self.b.handle_event({"cursor": "1", "kind": "contacts.updated", "data": {"contactId": "wxid_bob"}})
        self.assertEqual(self.b.user.titles, [])  # nothing changed

        self.daemon.threads["wxid_bob"]["displayName"] = "Bob Boss"
        await self.b.handle_event({"cursor": "2", "kind": "contacts.updated", "data": {"contactId": "wxid_bob"}})
        self.assertEqual(self.b.user.titles, [(600, "Bob Boss")])
        await self.b.handle_event({"cursor": "3", "kind": "contacts.updated", "data": {"contactId": "wxid_bob"}})
        self.assertEqual(len(self.b.user.titles), 1)

        self.daemon.add_thread("9@chatroom", "Team", kind="group")
        self.b.store.link("9@chatroom", -601, "👥 Team")
        self.daemon.threads["9@chatroom"]["displayName"] = "Team 2"
        await self.b.handle_event({"cursor": "4", "kind": "group.updated", "data": {"groupId": "9@chatroom"}})
        self.assertEqual(self.b.user.titles[-1], (601, "👥 Team 2"))
        await self.b.handle_event({"cursor": "5", "kind": "contacts.updated", "data": {"contactId": "wxid_unbound"}})
        self.assertEqual(len(self.b.user.titles), 2)

    async def test_auto_rename_can_be_turned_off(self):
        await self.b.close()
        self.b.store.db.close()
        await self.make_bridge(auto_rename=False)
        self.daemon.add_thread("wxid_bob", "Bob Boss")
        self.b.store.link("wxid_bob", -600, "Bob")
        await self.b.handle_event({"cursor": "1", "kind": "contacts.updated", "data": {"contactId": "wxid_bob"}})
        self.assertEqual(self.b.user.titles, [])

    async def test_contact_buttons_need_wechat_login(self):
        self.daemon.add_request("r1", "wxid_new", "New Friend")
        await self.fr_event("r1")
        self.b.wx_ready = False
        btn = self.b.bot.sent[0].kwargs["buttons"][0][0]
        cb = _CallbackEvent(999, btn.type.data if hasattr(btn, "type") else btn.data)
        await self.b.on_bot_callback(cb)
        self.assertEqual(cb.answers, [("微信还没有登录，请先 /wxlogin", True)])
        self.assertEqual(self.daemon.request_calls, [])


if __name__ == "__main__":
    unittest.main()
