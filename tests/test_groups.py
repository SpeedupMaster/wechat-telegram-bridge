"""Managing WeChat groups from Telegram: /group, /groupname, /myname, /invite, /kick, /mute, /leave, /newgroup."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from tests.test_bridge import ACC, FakeBot, FakeDaemon, FakeUser, _CallbackEvent, tg_event

ROOM = "room@chatroom"


class GroupDaemon(FakeDaemon):
    def __init__(self):
        super().__init__()
        self.groups: dict[str, dict] = {}
        self.mutations: list[tuple[str, str, dict | None, str | None]] = []  # (method, path, body, key)
        self.op: dict = {"state": "succeeded", "result": {"remoteOutcome": "accepted"}}
        self.ops_by_key: dict[str, dict] = {}
        self.refreshed: list[str] = []
        self.mutation_error: tuple[int, str] | None = None

    def add_group(self, gid, name, **extra):
        self.add_thread(gid, name, kind="group")
        self.groups[gid] = {"id": gid, "roomId": gid, "name": name, "displayName": name, "remark": None,
                            "ownerId": "wxid_owner", "administratorIds": [], "muted": False,
                            "myDisplayName": None, "announcement": None, "isInGroup": True, **extra}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        rest = path.split("/")[2:]
        if not rest:
            return super().handler(request)
        m = request.method
        key = request.headers.get("idempotency-key")
        mutation = (
            (rest[0] == "groups" and m in ("POST", "PATCH", "PUT", "DELETE") and not rest[-1] == "refresh")
            or (rest[0] == "threads" and len(rest) == 3 and rest[2] == "notifications")
        )
        if mutation:
            if self.mutation_error:
                st, code = self.mutation_error
                return httpx.Response(st, json={"error": {"code": code}})
            assert key, "group changes need an Idempotency-Key"
            body = json.loads(request.content) if request.content else None
            self.mutations.append((m, "/".join(rest), body, key))
            op = {"id": "gmop", "accountId": ACC, "kind": "group_mutation", **self.op}
            self.ops_by_key[key] = op
            if op["state"] == "succeeded" and rest[0] == "threads" and rest[1] in self.groups:
                self.groups[rest[1]]["muted"] = body["muted"]
            return httpx.Response(202, json={"id": "gmop", "state": "queued"})
        if rest[:2] == ["operations", "gmop"]:
            return httpx.Response(200, json={"id": "gmop", **self.op})
        if rest[0] == "groups" and rest[-1] == "refresh" and m == "POST":
            self.refreshed.append("/".join(rest))
            return httpx.Response(202, json={"id": "gmop", "state": "queued"})
        if rest[0] == "group-mutations":
            op = self.ops_by_key.get(request.url.params.get("key"))
            return httpx.Response(200, json=op) if op else httpx.Response(404, json={"error": {"code": "not_found"}})
        if rest[0] == "groups" and len(rest) == 2 and m == "GET":
            g = self.groups.get(rest[1])
            return httpx.Response(200, json=g) if g else httpx.Response(404, json={"error": {"code": "not_found"}})
        return super().handler(request)


class GroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False),
                     Path(self.tmp.name) / "config.toml")
        self.daemon = GroupDaemon()
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
        self.b._self_wxid = "wxid_me"
        await self.b.wx.resolve_account()

        self.daemon.add_group(ROOM, "Book Club", announcement="周五读书会", administratorIds=["wxid_admin"])
        self.daemon.group_members[ROOM] = [
            {"id": "wxid_me", "displayName": "Me"},
            {"id": "wxid_owner", "displayName": "Olivia"},
            {"id": "wxid_admin", "displayName": "Adam"},
            {"id": "wxid_bob", "displayName": "Bob", "roomDisplayName": "Bobby"},
        ]
        for cid, name in (("wxid_bob", "Bob"), ("wxid_carol", "Carol"), ("wxid_cara", "Cara"),
                          ("wxid_dave", "Dave")):
            self.daemon.add_contact(cid, name, relation="friend")
        self.daemon.add_message("g1", ROOM, "hi", sender="wxid_bob")
        await self.b.handle_event({"cursor": "1", "kind": "message.committed",
                                   "data": {"messageId": "g1", "threadId": ROOM, "origin": "live"}})
        self.chat = self.b.store.chat_for_thread(ROOM)
        self.msg_id = 50

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def send(self, text, chat=None):
        self.msg_id += 1
        await self.b.on_tg_message(tg_event(chat or self.chat, self.msg_id, text))
        return self.b.bot.sent[-1]

    async def private(self, text):
        box = []

        async def reply(msg, **kw):
            box.append((msg, kw))

        await self.b.on_bot_command(SimpleNamespace(sender_id=999, raw_text=text, reply=reply))
        return box[-1]

    async def click(self, buttons, label):
        btn = next(b for row in buttons for b in row if label in b.text)
        cb = _CallbackEvent(999, btn.type.data if hasattr(btn, "type") else btn.data)
        await self.b.on_bot_callback(cb)
        return cb

    @staticmethod
    def labels(buttons):
        return [b.text for row in buttons or [] for b in row]

    async def test_group_card_and_mute_toggle(self):
        card = await self.send("/group")
        self.assertIn("<b>Book Club</b>", card.text)
        self.assertIn("成员：4 人", card.text)
        self.assertIn("群主：Olivia", card.text)
        self.assertIn("管理员：Adam", card.text)
        self.assertIn("周五读书会", card.text)
        self.assertIn("🔔 未开启", card.text)
        self.assertEqual(self.labels(card.kwargs["buttons"]),
                         ["🔕 开启免打扰", "🔄 从微信刷新", "➕ 邀请成员", "➖ 移出成员", "🧹 清空群公告", "🚪 退出群聊"])

        cb = await self.click(card.kwargs["buttons"], "开启免打扰")
        self.assertEqual(self.daemon.mutations[-1][:3], ("PUT", f"threads/{ROOM}/notifications", {"muted": True}))
        self.assertIn("已开启消息免打扰", cb.edits[-1][0])
        self.assertIn("🔕 已开启", cb.edits[-1][0])
        self.assertIn("🔔 关闭免打扰", self.labels(cb.edits[-1][1]["buttons"]))

        cb = await self.click(cb.edits[-1][1]["buttons"], "从微信刷新")
        self.assertEqual(self.daemon.refreshed, [f"groups/{ROOM}/refresh", f"groups/{ROOM}/members/refresh"])
        self.assertIn("已从微信刷新", cb.edits[-1][0])

    async def test_mute_commands_work_in_direct_chats_too(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("d1", "wxid_bob", "hey")
        await self.b.handle_event({"cursor": "2", "kind": "message.committed",
                                   "data": {"messageId": "d1", "threadId": "wxid_bob", "origin": "live"}})
        dm = self.b.store.chat_for_thread("wxid_bob")
        reply = await self.send("/mute", chat=dm)
        self.assertIn("已开启微信消息免打扰", reply.text)
        self.assertEqual(self.daemon.mutations[-1][:3], ("PUT", "threads/wxid_bob/notifications", {"muted": True}))
        reply = await self.send("/unmute", chat=dm)
        self.assertEqual(self.daemon.mutations[-1][2], {"muted": False})
        reply = await self.send("/group", chat=dm)
        self.assertIn("这是单聊", reply.text)
        self.assertEqual(len(self.daemon.mutations), 2)

    async def test_rename_and_my_name(self):
        usage = await self.send("/groupname")
        self.assertIn("/groupname 新群名", usage.text)
        self.assertIn("当前群名：Book Club", usage.text)
        self.assertEqual(self.daemon.mutations, [])

        done = await self.send("/groupname  Reading   Circle ")
        method, path, body, key = self.daemon.mutations[-1]
        self.assertEqual((method, path, body), ("PATCH", f"groups/{ROOM}", {"name": "Reading Circle"}))
        self.assertTrue(key.startswith("grp-rename-"))
        self.assertIn("已把微信群名改为「Reading Circle」", done.text)

        done = await self.send("/myname 小王")
        self.assertEqual(self.daemon.mutations[-1][:3], ("PUT", f"groups/{ROOM}/my-name", {"displayName": "小王"}))
        self.assertIn("我在本群的昵称改为「小王」", done.text)

        self.daemon.op = {"state": "failed", "result": {"code": "rejected"}}
        failed = await self.send("/groupname X")
        self.assertIn("修改群名", failed.text)
        self.assertIn("微信拒绝了这个操作", failed.text)

    async def test_admin_scope_missing(self):
        self.daemon.mutation_error = (403, "forbidden")
        reply = await self.send("/groupname X")
        self.assertIn("admin 权限", reply.text)

    async def test_invite_picker_preselects_and_reuses_its_key(self):
        picker = await self.send("/invite carol car bob")
        # Bob is already in the group; "carol" names exactly one friend, so it is ticked.
        self.assertEqual(self.labels(picker.kwargs["buttons"]), ["✅ Carol", "⬜ Cara", "➕ 邀请（1 人）", "❌ 取消"])
        cb = await self.click(picker.kwargs["buttons"], "Cara")
        buttons = cb.edits[-1][1]["buttons"]
        self.assertIn("✅ Cara", self.labels(buttons))
        self.daemon.op = {"state": "succeeded",
                          "result": {"remoteOutcome": "accepted",
                                     "members": [{"username": "wxid_carol", "status": 0},
                                                 {"username": "wxid_cara", "status": 4}]}}
        cb = await self.click(buttons, "邀请（2 人）")
        method, path, body, key = self.daemon.mutations[-1]
        self.assertEqual((method, path, body), ("POST", f"groups/{ROOM}/members",
                                                {"members": ["wxid_carol", "wxid_cara"]}))
        self.assertIn("已邀请 Carol、Cara 进群", cb.edits[-1][0])
        self.assertIn("没有直接加入", cb.edits[-1][0])
        self.assertIn("Cara", cb.edits[-1][0].split("没有直接加入")[1])

        # The picker is done; pressing confirm again does nothing new.
        again = await self.click(buttons, "邀请（2 人）")
        self.assertEqual(len(self.daemon.mutations), 1)
        self.assertTrue(again.answers[-1][1])

    async def test_kick_needs_owner_or_admin(self):
        refused = await self.send("/kick")
        self.assertIn("只有群主或群管理员", refused.text)
        self.assertEqual(self.daemon.mutations, [])

        self.daemon.groups[ROOM]["ownerId"] = "wxid_me"
        picker = await self.send("/kick")
        # displayName is my remark when I set one, so it wins over their own name in the group ("Bobby").
        self.assertEqual(self.labels(picker.kwargs["buttons"])[:3], ["⬜ Olivia", "⬜ Adam", "⬜ Bob"])
        self.assertNotIn("Me", " ".join(self.labels(picker.kwargs["buttons"])))
        cb = await self.click(picker.kwargs["buttons"], "Bob")
        cb = await self.click(cb.edits[-1][1]["buttons"], "移出（1 人）")
        self.assertEqual(self.daemon.mutations[-1][:3], ("DELETE", f"groups/{ROOM}/members",
                                                         {"members": ["wxid_bob"]}))
        self.assertIn("已把 Bob 移出群聊", cb.edits[-1][0])

    async def test_leave_then_unlink(self):
        prompt = await self.send("/leave")
        self.assertIn("确定要退出微信群 <b>Book Club</b>", prompt.text)
        cb = await self.click(prompt.kwargs["buttons"], "确认退出")
        self.assertEqual(self.daemon.mutations[-1][:2], ("POST", f"groups/{ROOM}/leave"))
        self.assertIn("已退出微信群", cb.edits[-1][0])
        cb = await self.click(cb.edits[-1][1]["buttons"], "解除 Telegram")
        self.assertIsNone(self.b.store.chat_for_thread(ROOM))
        self.assertIn("解除了 Telegram 群绑定", cb.edits[-1][0])

    async def test_unknown_outcome_can_be_checked_by_key(self):
        self.daemon.groups[ROOM]["ownerId"] = "wxid_me"
        card = await self.send("/group")
        ask = await self.click(card.kwargs["buttons"], "清空群公告")
        self.daemon.op = {"state": "needs_review", "result": {"remoteOutcome": "unknown"}}
        cb = await self.click(ask.edits[-1][1]["buttons"], "确认清空")
        key = self.daemon.mutations[-1][3]
        self.assertEqual(self.daemon.mutations[-1][:2], ("DELETE", f"groups/{ROOM}/announcement"))
        self.assertIn("不要重复操作", cb.edits[-1][0])

        self.daemon.ops_by_key[key] = {"id": "gmop", "state": "succeeded", "result": {"remoteOutcome": "accepted"}}
        check = await self.click(cb.edits[-1][1]["buttons"], "查看结果")
        self.assertIn("清空群公告已完成", check.edits[-1][0])
        self.assertEqual(len(self.daemon.mutations), 1)

    async def test_new_group_from_private_chat(self):
        text, kw = await self.private("/newgroup carol car dave | 读书会")
        self.assertIn("新建微信群</b>：读书会", text)
        self.assertEqual(self.labels(kw["buttons"]), ["✅ Carol", "⬜ Cara", "✅ Dave", "👥 建群（2 人）", "❌ 取消"])
        self.daemon.op = {"state": "succeeded", "result": {"remoteOutcome": "accepted", "groupId": "new@chatroom"}}
        cb = await self.click(kw["buttons"], "建群（2 人）")
        self.assertEqual(self.daemon.mutations[-1][:3], ("POST", "groups",
                                                         {"members": ["wxid_carol", "wxid_dave"], "name": "读书会"}))
        self.assertIn("已创建微信群 <b>读书会</b>", cb.edits[-1][0])
        self.assertIn("new@chatroom", cb.edits[-1][0])
        self.assertIn("发起聊天", self.labels(cb.edits[-1][1]["buttons"])[0])

        text, kw = await self.private("/newgroup carol")
        cb = await self.click(kw["buttons"], "建群（1 人）")
        self.assertIn("至少要选 2 位好友", cb.answers[-1][0])
        self.assertEqual(len(self.daemon.mutations), 1)

    async def test_incoming_mentions_are_tagged(self):
        self.daemon.add_message("g2", ROOM, "看下", sender="wxid_bob",
                                mentions={"state": "known", "userIds": ["wxid_me"], "self": True, "all": False})
        self.daemon.add_message("g3", ROOM, "开会", sender="wxid_bob",
                                mentions={"state": "known", "userIds": [], "self": False, "all": True})
        for mid in ("g2", "g3"):
            await self.b.handle_event({"cursor": "3", "kind": "message.committed",
                                       "data": {"messageId": mid, "threadId": ROOM, "origin": "live"}})
        self.assertIn("🔔<b>@我</b>", self.b.bot.sent[-2].text)
        self.assertIn("📢<b>@所有人</b>", self.b.bot.sent[-1].text)
        self.assertNotIn("@我", self.b.bot.sent[0].text)


if __name__ == "__main__":
    unittest.main()
