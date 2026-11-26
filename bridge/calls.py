"""WeChat voice/video calls signaling loop, call cards, incoming reject, and outgoing /call & /vcall."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

import httpx
from telethon import Button, errors

from . import tghtml
from .formatting import (
    CALL_INVITEES_SHOWN,
    DIAL_PICK_PAGE,
    DIAL_PICK_SHOWN,
    _call_time_line,
    _card,
    _esc,
    _member_name,
    _short,
)
from .wechat import WeChatError, is_group_id

log = logging.getLogger("bridge")


def _callweb():
    """Lazily import bridge.callweb (and aiohttp) only when the Mini App / call page is used."""
    from . import callweb
    return callweb


class CallsMixin:
    """Mixin on Bridge handling WeChat incoming/outgoing calls and group conference calls."""

    async def calls_loop(self) -> None:
        backoff = 1.0
        warned_disabled = False
        while True:
            sock = None
            try:
                sock, hello = await self.wx.calls_socket()
                self._calls_sock = sock
                backoff = 1.0
                warned_disabled = False
                await self.handle_call_event(hello)
                async for ev in sock.events():
                    try:
                        await self.handle_call_event(ev)
                    except Exception:
                        log.exception("Failed to handle WeChat call event %s", ev.get("type"))
                log.warning("Calls WebSocket ended; reconnecting")
            except WeChatError as e:
                if e.status in (403, 404, 501) or e.code in (
                    "forbidden", "unsupported", "unsupported_protocol_version", "invalid_protocol_version"
                ):
                    if not warned_disabled:
                        log.info("WeChat calls WebSocket unavailable (%s); incoming call notifications paused", e.code)
                        warned_disabled = True
                    await asyncio.sleep(self._calls_retry_sleep)
                    continue
                log.warning("Calls WebSocket error: %s; retrying in %.0fs", e, backoff)
            except (OSError, asyncio.TimeoutError, httpx.HTTPError) as e:
                log.warning("Calls WebSocket connection error: %s; retrying in %.0fs", e, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Calls WebSocket unexpected error: %s; retrying in %.0fs", e, backoff)
            finally:
                if sock is not None:
                    if self._calls_sock is sock:
                        self._calls_sock = None
                    await sock.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def handle_call_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "hello":
            if fmt := event.get("audioFormat"):
                self._calls_audio_format = fmt
            self._calls_client_id = event.get("clientId") or ""
            self._calls_caps = event.get("capabilities") or {}
            self._calls_video_caps = event.get("videoCapabilities") or {}
            live_ids = {
                x.get("callId")
                for x in (event.get("currentOffer"), event.get("currentPendingOffer"), event.get("currentCall"))
                if isinstance(x, dict) and x.get("callId")
            }
            for cid, entry in list(self._active_calls.items()):
                if not entry["terminal"] and cid not in live_ids:
                    entry["terminal"] = True
                    entry["stage"] = "ended"
                    if not self._call_status_final(entry):
                        entry["status_text"] = (
                            "⚪ 通话已结束" if entry["was_connected"]
                            else "⚪ 呼叫已结束或状态已重置" if entry.get("direction") == "outgoing"
                            else "⚪ 来电已结束或状态已重置")
                    entry["offered_event"].set()
                    await self._sync_call_msg(entry)
            if offer := event.get("currentOffer"):
                await self._on_call_offer(offer, stage="offered")
            if pending := event.get("currentPendingOffer"):
                await self._on_call_offer(pending, stage="pending")
            if call := event.get("currentCall"):
                await self._on_call_state(call)
            await self._on_conf_invitations(event.get("conferenceInvitations") or [])
        elif etype == "call.pending":
            if offer := event.get("offer"):
                await self._on_call_offer(offer, stage="pending")
            else:
                for entry in list(self._active_calls.values()):
                    if not entry["terminal"] and entry["stage"] == "pending":
                        entry["terminal"] = True
                        entry["stage"] = "ended"
                        if not self._call_status_final(entry):
                            entry["status_text"] = "⚪ 来电已取消或已在其他设备处理"
                        entry["offered_event"].set()
                        await self._sync_call_msg(entry)
        elif etype == "call.offered":
            if offer := event.get("offer"):
                await self._on_call_offer(offer, stage="offered")
        elif etype == "call.state":
            if call := event.get("call"):
                await self._on_call_state(call)
        elif etype == "conference.invitations":
            await self._on_conf_invitations(event.get("invitations") or [])
        elif etype == "call.invitation_result":
            await self._on_invitation_result(event)
        elif etype in ("track.added", "track.updated", "track.removed"):
            if self.call_web is not None:
                await self.call_web.on_track_event(event)
        elif etype == "call.control":
            await self._on_call_control(event)
        elif etype == "participant.updated":
            await self._on_participant_updated(event)
        elif etype == "call.error":
            log.warning("WeChat call error event: %s", event.get("code"))

    async def _resolve_call_target(self, thread_id: str, caller_id: str,
                                   kind: str) -> tuple[dict | None, str]:
        """(thread, caller's name, my remark first); thread None when the chat is not bridged."""
        try:
            thread = await self.wx.thread(thread_id)
        except WeChatError:
            name = thread_id
            if not is_group_id(thread_id):
                try:
                    name = (await self.wx.contact(thread_id)).get("displayName") or thread_id
                except WeChatError:
                    pass
            thread = {"id": thread_id, "kind": "group" if kind == "conference" or is_group_id(thread_id) else "direct",
                      "displayName": name}
        if not self._allowed(thread):
            return None, ""
        if caller_id == thread_id and not is_group_id(thread_id):
            caller_name = (await self._named_thread(thread)).get("displayName") or caller_id
        else:
            caller_name = await self._sender_name({"threadId": thread_id, "senderId": caller_id})
        return thread, caller_name or caller_id

    async def _send_call_tg_msg(self, thread: dict, text: str, buttons=None) -> tuple[int, int] | None:
        chat = await self.ensure_chat(thread)
        if chat is None:
            return None
        for retry in (False, True):
            topic = self._topic_for(chat, thread["id"])
            try:
                sent = await self._rich_call_msg(self.bot.send_message, chat, text, buttons=buttons,
                                                 link_preview=False, **({"reply_to": topic} if topic else {}))
                return chat, sent.id
            except Exception as e:
                if retry or not await self._drop_dead_dest(chat, thread, e):
                    raise
                chat = await self.ensure_chat(thread)
                if not chat:
                    return None
        return None

    async def _rich_call_msg(self, send, *args, **kw):
        """Send or edit a call card with Telegram's date entity; if Telegram refuses it, drop it for good."""
        text = args[-1]
        if self._tg_time_ok or "<tg-time" not in text:
            try:
                return await send(*args, parse_mode=tghtml.HTML, **kw)
            except errors.MessageNotModifiedError:
                raise
            except errors.BadRequestError as e:
                if "<tg-time" not in text or "ENTIT" not in str(getattr(e, "message", "") or e).upper():
                    raise
                log.warning("Telegram refused a date entity (%s); call cards show plain times from now on", e)
                self._tg_time_ok = False
        return await send(*args[:-1], tghtml.strip(text), parse_mode="html", **kw)

    @staticmethod
    def _call_status_final(entry: dict) -> bool:
        """The call ended by our own action: keep that status instead of a generic "ended"."""
        return bool(entry.get("rejected_by_tg") or entry.get("hungup_by_tg"))

    def _render_call(self, entry: dict) -> tuple[str, list | None]:
        outgoing = entry.get("direction") == "outgoing"
        video = entry.get("call_type") == "video"
        if entry["kind"] == "conference" and outgoing:
            title, who = ("📹 <b>微信群视频通话</b>" if video else "📞 <b>微信群语音通话</b>"), "群"
        elif entry["kind"] == "conference":
            title, who = ("📹 <b>微信群视频通话</b>" if video else "📞 <b>微信群语音通话</b>"), "发起人"
        elif outgoing:
            title, who = ("📹 <b>微信视频通话</b>" if video else "📞 <b>微信语音通话</b>"), "呼叫"
        elif video:
            title, who = "📹 <b>微信视频来电</b>", "来电人"
        else:
            title, who = "📞 <b>微信语音来电</b>", "来电人"
        lines = [f"{who}：<b>{_esc(entry['caller_name'])}</b>",
                 f"状态：{entry['status_text']}"]
        if when := _call_time_line(entry):
            lines.append(when)
        text = _card(title, lines) + self._render_group_progress(entry)
        rows: list[list] = []
        web = self.call_web
        if not entry["terminal"]:
            # One button per row: full-width buttons are much easier to hit on a phone.
            if entry["stage"] in ("pending", "offered"):
                if web is not None and web.can_answer(entry):
                    rows.append([Button.url("📹 接听" if video else "📞 接听", web.link(web.token_for_call(entry)))])
                rows.append([Button.inline("📵 拒接", f"callrej:{entry['token']}".encode())])
            elif web is not None and entry.get("answered_by_tg") and web.has_session(entry["call_id"]):
                rows.append([Button.url("📹 通话界面" if video else "🎧 通话界面", web.link(web.token_for_call(entry)))])
                if web.can_invite(entry):
                    rows.append([Button.inline("➕ 邀请更多人", f"callinv:{entry['token']}".encode())])
                label = "📴 取消呼叫" if outgoing and entry["stage"] in ("dialing", "ringing") else "📴 挂断"
                rows.append([Button.inline(label, f"callhup:{entry['token']}".encode())])
        return text, rows or None

    @staticmethod
    def _render_group_progress(entry: dict) -> str:
        """Extra lines for a group call we started: who was invited, who joined, whose invitation failed."""
        invitees: dict = entry.get("invitees") or {}
        if not invitees:
            return ""
        names = list(invitees.values())
        shown = "、".join(_esc(n) for n in names[:CALL_INVITEES_SHOWN])
        more = f" 等 {len(names)} 人" if len(names) > CALL_INVITEES_SHOWN else ""
        out = [f"邀请（{len(names)} 人）：{shown}{more}"]
        joined = [p for p in entry.get("participants") or [] if p.get("membership") == "joined"
                  and p.get("contactId") in invitees]
        if joined:
            out.append(f"已加入：{len(joined)} 人")
        failed = entry.get("invite_failed") or {}
        if failed:
            out.append("邀请异常：" + "、".join(f"{_esc(n)}（{why}）" for n, why in failed.values()))
        # A long list folds away: Telegram shows the first lines and a tap expands the rest.
        return "\n<blockquote expandable>" + "\n".join(out) + "</blockquote>"

    @staticmethod
    def _apply_call_control(entry: dict, ctrl: dict, *, participant_id: str = "", self_wxid: str = "") -> None:
        if not isinstance(ctrl, dict):
            return
        if participant_id and self_wxid and participant_id == self_wxid:
            return
        kind = ctrl.get("kind")
        if kind == "camera" and "enabled" in ctrl:
            val = bool(ctrl["enabled"])
            entry["remote_camera"] = val
            if participant_id and entry.get("participants"):
                for p in entry["participants"]:
                    if isinstance(p, dict) and participant_id in (p.get("participantId"), p.get("contactId")):
                        p["cameraEnabled"] = val
        elif kind == "microphone" and "muted" in ctrl:
            muted = bool(ctrl["muted"])
            entry["remote_muted"] = muted
            if participant_id and entry.get("participants"):
                for p in entry["participants"]:
                    if isinstance(p, dict) and participant_id in (p.get("participantId"), p.get("contactId")):
                        p["microphoneEnabled"] = not muted

    async def _warm_participant_names(self, entry: dict) -> None:
        if entry.get("kind") != "conference" or not entry.get("participants"):
            return
        invitees = entry.get("invitees") or {}
        pnames = entry.setdefault("participant_names", {})
        thread_id = entry.get("thread_id") or ""
        for p in entry["participants"]:
            if not isinstance(p, dict):
                continue
            cid = str(p.get("contactId") or "").strip()
            if not cid or cid == self._self_wxid or cid in invitees or cid in pnames:
                continue
            try:
                name = await self._sender_name({"threadId": thread_id, "senderId": cid})
                if name:
                    pnames[cid] = name
            except Exception:
                pass

    async def _on_call_control(self, event: dict) -> None:
        entry = self._active_calls.get(event.get("callId") or "")
        if entry is None:
            return
        self._apply_call_control(
            entry, event.get("control") or {},
            participant_id=str(event.get("participantId") or ""),
            self_wxid=self._self_wxid,
        )
        if self.call_web is not None:
            await self.call_web.push_state(entry)

    async def _on_participant_updated(self, event: dict) -> None:
        entry = self._active_calls.get(event.get("callId") or "")
        part = event.get("participant")
        if entry is None or not isinstance(part, dict):
            return
        if event.get("revision") is not None:
            entry["revision"] = event["revision"]
        parts = list(entry.get("participants") or [])
        pid, cid = part.get("participantId"), part.get("contactId")
        replaced = False
        for i, old in enumerate(parts):
            if (pid and old.get("participantId") == pid) or (cid and old.get("contactId") == cid):
                parts[i] = {**old, **part}
                replaced = True
                break
        if not replaced:
            parts.append(part)
        entry["participants"] = parts
        await self._warm_participant_names(entry)
        await self._sync_call_msg(entry)

    async def _on_invitation_result(self, event: dict) -> None:
        call_id = event.get("callId") or ""
        entry = self._active_calls.get(call_id)
        if entry is None:
            # The results of a group call we are starting can beat the dial command's own result.
            if call_id:
                self._orphan_invite_results.setdefault(call_id, []).append(event)
                while len(self._orphan_invite_results) > 20:
                    self._orphan_invite_results.pop(next(iter(self._orphan_invite_results)))
            return
        invitees = entry.get("invitees") or {}
        failed = entry.setdefault("invite_failed", {})
        for r in event.get("results") or []:
            cid = r.get("contactId") or ""
            status = r.get("status")
            if r.get("verificationRequired"):
                why = "需要验证"
            elif status == "rejected":
                why = "被拒绝"
            elif status in ("unknown", "uncertain"):
                why = "结果未知"
            else:
                failed.pop(cid, None)
                continue
            failed[cid] = (invitees.get(cid) or cid, why)
        await self._sync_call_msg(entry)

    @staticmethod
    def _render_key(text: str, buttons: list | None) -> tuple:
        return text, tuple(getattr(b, "text", "") for row in (buttons or []) for b in row)

    async def _sync_call_msg(self, entry: dict) -> None:
        if self.call_web is not None:
            try:
                await self.call_web.on_entry_changed(entry)
            except Exception:
                log.exception("Could not update the call page for %s", entry.get("call_id"))
        if entry["terminal"] and entry.get("direction") == "outgoing" and entry["kind"] == "conference":
            await self._close_call_invites(entry)
        if entry["terminal"] and entry.get("connected_at") and not entry.get("ended_at"):
            entry["ended_at"] = time.time()
        if not entry.get("chat_id") or not entry.get("msg_id"):
            return
        text, buttons = self._render_call(entry)
        render_key = self._render_key(text, buttons)
        if entry.get("_last_render") == render_key:
            return
        try:
            await self._rich_call_msg(self.bot.edit_message, entry["chat_id"], entry["msg_id"], text,
                                      buttons=buttons, link_preview=False)
            entry["_last_render"] = render_key
        except errors.MessageNotModifiedError:
            entry["_last_render"] = render_key
        except Exception as e:
            log.info("Could not update call notification message for %s: %s", entry["call_id"], e)

    def _prune_calls(self) -> None:
        if len(self._active_calls) <= 200:
            return
        for cid, entry in list(self._active_calls.items()):
            if entry["terminal"] and not (self.call_web and self.call_web.has_session(cid)):
                self._active_calls.pop(cid, None)
                self._call_tokens.pop(entry["token"], None)
            if len(self._active_calls) <= 150:
                break

    def _new_call_entry(self, call_id: str, *, kind: str, call_type: str, caller_id: str, caller_name: str,
                        thread_id: str, stage: str, revision: int, phase: str | None = None) -> dict:
        self._prune_calls()
        token = secrets.token_hex(6)
        entry = {
            "call_id": call_id,
            "token": token,
            "kind": kind,
            "call_type": call_type,
            "caller_id": caller_id,
            "caller_name": caller_name,
            "thread_id": thread_id,
            "stage": stage,
            "phase": phase,
            "revision": revision,
            "chat_id": None,
            "msg_id": None,
            "terminal": False,
            "rejected_by_tg": False,
            "rejecting": False,
            "answering": False,
            "answered_by_tg": False,
            "hungup_by_tg": False,
            "was_connected": False,
            "connected_at": None,
            "offered_event": asyncio.Event(),
            "status_text": "🔔 响铃中…",
            "_last_render": None,
        }
        if stage == "offered":
            entry["offered_event"].set()
        self._active_calls[call_id] = entry
        self._call_tokens[token] = call_id
        return entry

    async def _on_call_offer(self, offer: dict, *, stage: str) -> None:
        call_id = offer.get("callId")
        if not call_id:
            return
        if existing := self._active_calls.get(call_id):
            if not existing["terminal"]:
                if stage == "offered" or existing["stage"] == "pending":
                    existing["stage"] = stage
                if offer.get("callType") in ("voice", "video"):
                    existing["call_type"] = offer["callType"]
                existing["revision"] = offer.get("revision", existing["revision"])
                existing["phase"] = offer.get("phase")
                if stage == "offered":
                    existing["offered_event"].set()
                await self._sync_call_msg(existing)
            return

        caller_id = offer.get("callerId") or ""
        kind = offer.get("kind") or "direct"
        thread_id = offer.get("groupId") or caller_id
        if not thread_id:
            return
        thread, caller_name = await self._resolve_call_target(thread_id, caller_id or thread_id, kind)
        if thread is None:
            return

        entry = self._new_call_entry(
            call_id, kind=kind, call_type=offer.get("callType") or "voice", caller_id=caller_id,
            caller_name=caller_name, thread_id=thread["id"], stage=stage,
            revision=offer.get("revision", 0),
            phase=offer.get("phase"),
        )
        text, buttons = self._render_call(entry)
        res = await self._send_call_tg_msg(thread, text, buttons=buttons)
        if not res:
            return
        entry["chat_id"], entry["msg_id"] = res
        entry["_last_render"] = self._render_key(text, buttons)
        # In case call state updated while ensure_chat was creating the group:
        await self._sync_call_msg(entry)

    async def _replay_orphan_call_state(self, call_id: str) -> None:
        """A call we just claimed may have reported its state before we knew about it."""
        if call := self._orphan_call_states.pop(call_id, None):
            await self._on_call_state(call)
        for ev in self._orphan_invite_results.pop(call_id, []):
            await self._on_invitation_result(ev)

    async def _on_call_state(self, call: dict) -> None:
        call_id = call.get("callId")
        if not call_id:
            return
        state = call.get("state") or ""
        entry = self._active_calls.get(call_id)
        if entry is None:
            if call.get("direction") != "incoming" or state in ("ending", "ended", "uncertain"):
                # Maybe a group call we are joining right now: keep it for _replay_orphan_call_state.
                self._orphan_call_states[call_id] = call
                while len(self._orphan_call_states) > 20:
                    self._orphan_call_states.pop(next(iter(self._orphan_call_states)))
                return
            caller_id = next(
                (c.get("participantId") for c in (call.get("controls") or []) if c.get("participantId")), ""
            )
            thread_id = call.get("groupId") or caller_id
            if not thread_id:
                return
            await self._on_call_offer({
                "callId": call_id,
                "revision": call.get("revision", 0),
                "kind": call.get("kind") or "direct",
                "callType": call.get("callType") or "voice",
                "callerId": caller_id,
                "groupId": call.get("groupId"),
            }, stage=state)
            entry = self._active_calls.get(call_id)
            if entry is None:
                return

        entry["revision"] = call.get("revision", entry["revision"])
        if entry["terminal"]:
            if state in ("ended", "uncertain"):
                entry["stage"] = state
            await self._sync_call_msg(entry)
            return
        conf = entry["kind"] == "conference"
        outgoing = entry.get("direction") == "outgoing"
        group_out = conf and outgoing
        if call.get("callType") in ("voice", "video"):
            entry["call_type"] = call["callType"]
        if conf and isinstance(call.get("participants"), list):
            entry["participants"] = call["participants"]
            await self._warm_participant_names(entry)
        for item in call.get("controls") or []:
            if isinstance(item, dict):
                self._apply_call_control(
                    entry, item.get("control") or {},
                    participant_id=str(item.get("participantId") or ""),
                    self_wxid=self._self_wxid,
                )
        if state == "offered":
            entry["stage"] = "offered"
            entry["status_text"] = "🔔 响铃中…"
            entry["offered_event"].set()
        elif state in ("dialing", "ringing") and outgoing:
            entry["stage"] = state
            entry["status_text"] = ("📞 正在发起群通话…" if group_out else
                                    "📞 正在呼叫…" if state == "dialing" else "🔔 对方响铃中…")
        elif state in ("dialing", "ringing"):
            entry["stage"] = state
            entry["status_text"] = "🟡 正在加入…" if entry.get("answered_by_tg") else "🔔 响铃中…"
        elif state == "connecting":
            entry["stage"] = "connecting"
            entry["status_text"] = ("🟡 正在发起群通话…" if group_out else
                                    "🟡 正在加入…" if conf and entry.get("answered_by_tg") else "🟡 接通中…")
            entry["offered_event"].set()
        elif state == "connected":
            entry["stage"] = "connected"
            entry["was_connected"] = True
            entry["connected_at"] = entry.get("connected_at") or time.time()
            entry["status_text"] = ("🟢 群通话中（Telegram 发起）" if group_out else
                                    "🟢 通话中（Telegram 发起）" if outgoing else
                                    "🟢 通话中（Telegram 接听）" if entry.get("answered_by_tg") else "🟢 通话中")
            entry["offered_event"].set()
        elif state in ("ending", "ended"):
            entry["stage"] = state
            entry["terminal"] = True
            if not self._call_status_final(entry):
                entry["status_text"] = ("⚪ 通话已结束" if entry["was_connected"] else
                                        "⚪ 群通话未能发起" if group_out else
                                        "⚪ 呼叫未接通（对方未接听、已拒绝或忙线）" if outgoing else
                                        "⚪ 来电已结束（未接听或已取消）")
            entry["offered_event"].set()
        elif state == "uncertain":
            entry["stage"] = "uncertain"
            entry["terminal"] = True
            if not self._call_status_final(entry):
                entry["status_text"] = "⚠️ 通话状态未确认"
            entry["offered_event"].set()
        await self._sync_call_msg(entry)

    def _render_invite(self, inv: dict) -> tuple[str, list | None]:
        web = self.call_web
        if inv.get("call_type") == "video":
            title = "📹 <b>微信群视频通话邀请</b>"
        elif web is not None and web.can_video(True):
            title = "📞 <b>微信群通话邀请</b>"
        else:
            title = "📞 <b>微信群语音通话邀请</b>"
        text = _card(title,
                     [f"发起人：<b>{_esc(inv['caller_name'])}</b>",
                      f"状态：{inv['status_text']}"])
        if web is not None and inv.get("live") and web.can_join():
            return text, [[Button.url("📞 加入", web.link(web.token_for_invite(inv)))]]
        return text, None

    async def _on_conf_invitations(self, invitations: list[dict]) -> None:
        current = {inv.get("invitationId"): inv for inv in invitations if inv.get("invitationId")}
        for inv_id in list(self._conf_invites):
            if inv_id in current:
                continue
            old = self._conf_invites.pop(inv_id)
            old["live"] = False
            if old.get("joined_call"):
                continue  # the invitation message now shows the call we joined
            old["status_text"] = "⚪ 邀请已结束或已过期"
            if self.call_web is not None:
                await self.call_web.on_invite_changed(old)
            text, _ = self._render_invite(old)
            try:
                await self.bot.edit_message(old["chat_id"], old["msg_id"], text, buttons=None,
                                            parse_mode="html", link_preview=False)
            except errors.MessageNotModifiedError:
                pass
            except Exception as e:
                log.info("Could not update conference invitation %s: %s", inv_id, e)

        for inv_id, inv in current.items():
            group_id = inv.get("groupId") or ""
            caller_id = inv.get("callerId") or ""
            if known := self._conf_invites.get(inv_id):
                known["revision"] = inv.get("revision", known.get("revision", 0))
                if inv.get("callType") in ("voice", "video"):
                    known["call_type"] = inv["callType"]
                continue
            if not group_id:
                continue
            thread, caller_name = await self._resolve_call_target(group_id, caller_id, "conference")
            if thread is None:
                continue
            entry = {
                "invitation_id": inv_id,
                "revision": inv.get("revision", 0),
                "call_type": inv.get("callType") or "",
                "group_id": group_id,
                "group_name": thread.get("displayName") or group_id,
                "caller_id": caller_id,
                "caller_name": caller_name,
                "status_text": "🔔 邀请中…",
                "live": True,
                "chat_id": None,
                "msg_id": None,
            }
            text, buttons = self._render_invite(entry)
            res = await self._send_call_tg_msg(thread, text, buttons=buttons)
            if res:
                entry["chat_id"], entry["msg_id"] = res
                self._conf_invites[inv_id] = entry

    async def _on_call_reject_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        call_id = self._call_tokens.get(token)
        entry = self._active_calls.get(call_id) if call_id else None
        if not entry or entry["terminal"] or entry["stage"] not in ("pending", "offered"):
            await event.answer("来电已结束或已处理")
            if entry:
                await self._sync_call_msg(entry)
            return
        if entry["rejecting"] or entry.get("answering"):
            await event.answer("正在处理中…")
            return
        sock = self._calls_sock
        if sock is None or sock.closed:
            await event.answer("通话信令未连接，无法拒接", alert=True)
            return
        entry["rejecting"] = True  # before the first await: a double tap must not reject twice
        await event.answer("正在拒接…")
        await self._reject_call(entry)

    async def _on_call_hangup_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        call_id = self._call_tokens.get(token)
        entry = self._active_calls.get(call_id) if call_id else None
        if not entry or entry["terminal"] or self.call_web is None:
            await event.answer("通话已结束")
            if entry:
                await self._sync_call_msg(entry)
            return
        await event.answer("正在挂断…")
        try:
            await self.call_web.hangup(entry)
        except _callweb().CallActionError as e:
            await self._report_call_reject_failure(entry, str(e))

    async def _reject_call(self, entry: dict) -> None:
        """Reject a pending/offered incoming call; failures are reported in the call's Telegram chat."""
        sock = self._calls_sock
        if sock is None or sock.closed:
            entry["rejecting"] = False
            await self._report_call_reject_failure(entry, "通话信令未连接，无法拒接")
            return
        call_id = entry["call_id"]
        entry["rejecting"] = True
        readied = False
        held = False
        try:
            if entry["stage"] == "pending":
                self._reject_ready_holds = getattr(self, "_reject_ready_holds", 0) + 1
                held = True
                entry["offered_event"].clear()
                res = await sock.command({
                    "type": "call.ready",
                    "ready": True,
                    "format": self._calls_audio_format,
                    "scope": {
                        "kind": "offer",
                        "callId": call_id,
                        "expectedRevision": entry["revision"],
                    },
                })
                readied = True
                if res.get("status") == "error":
                    code = res.get("code") or "error"
                    if code == "offer_expired" and entry["stage"] == "pending" and not entry["terminal"]:
                        # Revision may have advanced to preparing; give the socket a moment to deliver call.offered
                        try:
                            await asyncio.wait_for(entry["offered_event"].wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            pass
                    if not (entry["stage"] == "offered" or entry["terminal"]):
                        await self._finish_reject_error(entry, code)
                        return
                if entry["stage"] == "pending" and not entry["terminal"]:
                    try:
                        await asyncio.wait_for(entry["offered_event"].wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        await self._report_call_reject_failure(entry, "等待来电就绪超时")
                        return

            if entry["terminal"] or entry["stage"] != "offered":
                await self._sync_call_msg(entry)
                return

            res = await sock.command({
                "type": "call.reject",
                "callId": call_id,
                "expectedRevision": entry["revision"],
            })
            if res.get("status") == "error" and res.get("code") == "stale" and not entry["terminal"] and entry["stage"] == "offered":
                res = await sock.command({
                    "type": "call.reject",
                    "callId": call_id,
                    "expectedRevision": entry["revision"],
                })
            if res.get("status") in ("accepted", "replayed"):
                entry["rejected_by_tg"] = True
                entry["terminal"] = True
                entry["stage"] = "ended"
                entry["status_text"] = "📵 已在 Telegram 拒接"
                await self._sync_call_msg(entry)
            else:
                await self._finish_reject_error(entry, res.get("code") or "error")
        except (OSError, WeChatError, asyncio.TimeoutError) as e:
            await self._report_call_reject_failure(entry, f"拒接请求失败：{e}")
        finally:
            entry["rejecting"] = False
            if held:
                self._reject_ready_holds = max(0, getattr(self, "_reject_ready_holds", 1) - 1)
            if readied and not sock.closed:
                if self.call_web is not None:
                    await self.call_web.withdraw_readiness()
                elif getattr(self, "_reject_ready_holds", 0) == 0:
                    try:
                        await sock.command({
                            "type": "call.ready",
                            "ready": False,
                            "format": self._calls_audio_format,
                        })
                    except Exception:
                        pass

    async def _finish_reject_error(self, entry: dict, code: str) -> None:
        reasons = {
            "offer_expired": "来电已结束或已过期",
            "stale": "来电状态已变化，请重试",
            "already_claimed": "来电已被其他设备处理",
            "not_owner": "来电已被其他设备接管",
            "busy": "当前有其他未决通话",
            "forbidden": "API Token 缺少 calls 权限",
            "unsupported": "当前 agent-wechat 不支持拒接操作",
            "media_not_ready": "来电尚未准备就绪",
        }
        msg = reasons.get(code, f"拒接失败（{code}）")
        if code in ("offer_expired", "already_claimed", "not_owner"):
            entry["terminal"] = True
            entry["stage"] = "ended"
            entry["status_text"] = f"⚪ {msg}"
            await self._sync_call_msg(entry)
            return
        await self._report_call_reject_failure(entry, msg)

    async def _report_call_reject_failure(self, entry: dict, text: str) -> None:
        if entry.get("chat_id"):
            await self._report(entry["chat_id"], entry.get("msg_id"), text)

    # ---------------------------------------------------------------- outgoing calls (/call, /vcall)
    def _dial_unavailable(self, thread_id: str, *, video: bool = False) -> str:
        conf = is_group_id(thread_id)
        if thread_id == "filehelper" or thread_id.startswith("gh_"):
            return "文件传输助手和公众号不能接听微信电话。"
        if self.call_web is None:
            return ("没有配置通话页面（<code>CALL_WEB_URL</code>），无法在 Telegram 打微信电话。"
                    "配置方法见 README「在 Telegram 接听与拨打微信语音通话」。")
        if not self.wx_ready:
            return "🔐 微信尚未登录，登录后再打电话。"
        if self._calls_sock is None or self._calls_sock.closed:
            return "通话信令未连接（agent-wechat 通话功能不可用或正在重连），请稍后重试。"
        if video:
            if not self.call_web.can_dial(conf, video=True):
                what = "群视频通话" if conf else "视频通话"
                return f"当前 agent-wechat 不支持发起微信{what}，请升级到 3.0 或更新版本。"
            return ""
        if not self.call_web.can_dial(conf):
            what = "群语音通话" if conf else "语音通话"
            return f"当前 agent-wechat 不支持发起微信{what}，请检查 daemon 版本或通话能力。"
        return ""

    def _render_dial(self, req: dict) -> tuple[str, list | None]:
        if req.get("kind") == "conference":
            return self._render_group_dial(req)
        video = req.get("call_type") == "video"
        title = "📹 <b>微信视频通话</b>" if video else "📞 <b>微信语音通话</b>"
        text = _card(title,
                     [f"呼叫：<b>{_esc(req['name'])}</b>", f"状态：{req['status_text']}"])
        web = self.call_web
        if not req["live"] or web is None:
            return text, None
        if video:
            rows = [[Button.url("📹 开始视频呼叫", web.link(web.token_for_dial(req, video=True)))]]
        elif web.can_dial(False, video=True):
            rows = [
                [Button.url("📞 语音呼叫", web.link(web.token_for_dial(req, video=False)))],
                [Button.url("📹 视频呼叫", web.link(web.token_for_dial(req, video=True)))],
            ]
        else:
            rows = [[Button.url("📞 开始呼叫", web.link(web.token_for_dial(req)))]]
        rows.append([Button.inline("✖️ 取消", f"dialx:{req['dial_id']}".encode())])
        return text, rows

    def _render_group_dial(self, req: dict) -> tuple[str, list | None]:
        selected = req["selected"]
        names = req["names"]
        inviting = bool(req.get("invite_call"))
        video = req.get("call_type") == "video"
        if inviting:
            title = "➕ <b>邀请加入群视频通话</b>" if video else "➕ <b>邀请加入群语音通话</b>"
        elif video:
            title = "📹 <b>微信群视频通话</b>"
        else:
            title = "📞 <b>微信群语音通话</b>"
        lines = [f"群：<b>{_esc(req['name'])}</b>"]
        if selected:
            shown = "、".join(_esc(names.get(cid) or cid) for cid in selected[:DIAL_PICK_SHOWN])
            more = f" 等 {len(selected)} 人" if len(selected) > DIAL_PICK_SHOWN else ""
            lines.append(f"邀请（{len(selected)} 人）：{shown}{more}")
        else:
            lines.append("邀请：尚未选择")
        lines.append(f"状态：{req['status_text']}")
        text = _card(title, lines)
        web = self.call_web
        if not req["live"] or web is None:
            return text, None
        did = req["dial_id"]
        members = req["members"]
        pages = max(1, -(-len(members) // DIAL_PICK_PAGE))
        page = min(req["page"], pages - 1)
        rows: list[list] = []
        chunk = list(enumerate(members))[page * DIAL_PICK_PAGE:(page + 1) * DIAL_PICK_PAGE]
        chosen = set(selected)
        for i in range(0, len(chunk), 2):
            rows.append([
                Button.inline(f"{'✅' if m['id'] in chosen else '▫️'} {_short(m['label'], 12)}",
                              f"dialp:{did}:t{idx}".encode())
                for idx, m in chunk[i:i + 2]
            ])
        if pages > 1:
            rows.append([Button.inline("◀️", f"dialp:{did}:p{(page - 1) % pages}".encode()),
                         Button.inline(f"{page + 1}/{pages}", f"dialp:{did}:n".encode()),
                         Button.inline("▶️", f"dialp:{did}:p{(page + 1) % pages}".encode())])
        rows.append([Button.inline("☑️ 全选", f"dialp:{did}:a".encode()),
                     Button.inline("🔄 清空", f"dialp:{did}:c".encode())])
        if selected and inviting:
            # The call page already has the microphone: inviting is a plain command, no page needed.
            rows.append([Button.inline(f"➕ 发送邀请（{len(selected)} 人）", f"dialp:{did}:go".encode())])
        elif selected:
            if video:
                rows.append([Button.url(f"📹 开始视频呼叫（{len(selected)} 人）",
                                        web.link(web.token_for_dial(req, video=True)))])
            elif web.can_dial(True, video=True):
                rows.append([Button.url(f"📞 语音呼叫（{len(selected)} 人）",
                                        web.link(web.token_for_dial(req, video=False)))])
                rows.append([Button.url(f"📹 视频呼叫（{len(selected)} 人）",
                                        web.link(web.token_for_dial(req, video=True)))])
            else:
                rows.append([Button.url(f"📞 开始呼叫（{len(selected)} 人）", web.link(web.token_for_dial(req)))])
        rows.append([Button.inline("✖️ 取消", f"dialx:{did}".encode())])
        return text, rows

    async def _group_dial_members(self, group_id: str) -> list[dict]:
        members = await self.wx.group_members(group_id)
        out = []
        for m in members:
            cid = m.get("id") or m.get("username")
            if not cid or cid == self._self_wxid or is_group_id(cid):
                continue
            out.append({"id": cid, "label": self._known_remark(cid) or _member_name(m) or cid,
                        "search": " ".join(str(m.get(k) or "") for k in
                                           ("mentionName", "roomDisplayName", "displayName", "nickname",
                                            "alias", "username", "id")).lower()})
        return out

    async def _start_dial(self, chat_id: int, reply_to: int | None, thread_id: str, query: str = "",
                          *, video: bool = False) -> None:
        if why := self._dial_unavailable(thread_id, video=video):
            await self.bot.send_message(chat_id, why, parse_mode="html", reply_to=reply_to)
            return
        conf = is_group_id(thread_id)
        name = self._link_title(thread_id) or thread_id
        try:
            name = await self._thread_name(thread_id)
        except Exception:
            pass
        req = {
            "dial_id": secrets.token_hex(6),
            "kind": "conference" if conf else "direct",
            "call_type": "video" if video else "voice",
            "contact_id": thread_id,
            "thread_id": thread_id,
            "name": name,
            "chat_id": chat_id,
            "msg_id": None,
            "status_text": ("点击「开始视频呼叫」，在通话页面授权摄像头与麦克风后拨出" if video
                            else "点击下方按钮，在通话页面授权设备后拨出"),
            "live": True,
            "dialing": False,
            "call_id": None,
            "expire_task": None,
        }
        if conf:
            try:
                members = await self._group_dial_members(thread_id)
            except WeChatError as e:
                await self.bot.send_message(chat_id, f"获取群成员失败：{_esc(e.code)}", parse_mode="html",
                                            reply_to=reply_to)
                return
            if not members:
                await self.bot.send_message(chat_id, "没有拿到群成员，无法发起群通话", reply_to=reply_to)
                return
            terms = query.lower().split()
            picked = [m for m in members if any(t in m["search"] for t in terms)] if terms else []
            if terms and not picked:
                await self.bot.send_message(chat_id, f"没有匹配「{_esc(query)}」的群成员", parse_mode="html",
                                            reply_to=reply_to)
                return
            picked = picked[:_callweb().MAX_INVITEES]
            ids = {m["id"] for m in picked}
            req.update(
                members=picked + [m for m in members if m["id"] not in ids],  # matches first
                names={m["id"]: m["label"] for m in members},
                selected=[m["id"] for m in picked],
                page=0,
                status_text=("勾选要邀请的成员，再点「开始视频呼叫」，在通话页面授权设备后发起" if video
                             else "勾选要邀请的成员，再点下方呼叫按钮发起"),
            )
        for old in list(self._dial_requests.values()):
            if (old["live"] and old["thread_id"] == thread_id and not old.get("dialing")
                    and not old.get("invite_call")):
                await self._close_dial_request(old, "⚪ 已发起新的呼叫，此链接已失效")
        await self._post_dial_request(req, reply_to)

    async def _post_dial_request(self, req: dict, reply_to: int | None) -> None:
        while len(self._dial_requests) > 50:
            self._dial_requests.pop(next(iter(self._dial_requests)))
        text, buttons = self._render_dial(req)
        sent = await self.bot.send_message(req["chat_id"], text, parse_mode="html", buttons=buttons,
                                           link_preview=False, reply_to=reply_to)
        req["msg_id"] = sent.id
        req["_last_render"] = self._render_key(text, buttons)
        self._dial_requests[req["dial_id"]] = req
        req["expire_task"] = task = asyncio.create_task(self._expire_dial(req))
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def _sync_dial_msg(self, req: dict) -> None:
        if self.call_web is not None:
            await self.call_web.on_dial_changed(req)
        text, buttons = self._render_dial(req)
        key = self._render_key(text, buttons)
        if req.get("_last_render") == key:
            return
        try:
            await self.bot.edit_message(req["chat_id"], req["msg_id"], text, parse_mode="html", buttons=buttons,
                                        link_preview=False)
            req["_last_render"] = key
        except errors.MessageNotModifiedError:
            req["_last_render"] = key
        except Exception as e:
            log.info("Could not update call request %s: %s", req["dial_id"], e)

    async def _on_dial_pick_callback(self, event, payload: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        dial_id, _, op = payload.partition(":")
        req = self._dial_requests.get(dial_id)
        if req is None or not req["live"] or req.get("kind") != "conference":
            await event.answer("呼叫已开始或已结束")
            return
        if req.get("dialing"):
            await event.answer("正在发起群通话…" if not req.get("invite_call") else "正在发送邀请…")
            return
        if req.get("invite_call"):
            entry = self._active_calls.get(req["invite_call"])
            if entry is None or self.call_web is None or not self.call_web.can_invite(entry):
                await event.answer("群通话已结束")
                await self._close_dial_request(req, "⚪ 群通话已结束，无法再邀请")
                return
            if op == "go":
                await self._send_call_invite(event, req, entry)
                return
        selected: list[str] = req["selected"]
        members: list[dict] = req["members"]
        limit = _callweb().MAX_INVITEES
        note = None
        if op.startswith("t") and op[1:].isdigit() and int(op[1:]) < len(members):
            cid = members[int(op[1:])]["id"]
            if cid in selected:
                selected.remove(cid)
            elif len(selected) >= limit:
                await event.answer(f"一次最多邀请 {limit} 人", alert=True)
                return
            else:
                selected.append(cid)
        elif op.startswith("p") and op[1:].isdigit():
            req["page"] = int(op[1:])
        elif op == "a":
            chosen = set(selected)
            for m in members:
                if len(selected) >= limit:
                    note = f"一次最多邀请 {limit} 人，已选前 {limit} 人"
                    break
                if m["id"] not in chosen:
                    selected.append(m["id"])
        elif op == "c":
            selected.clear()
        if note:
            await event.answer(note, alert=True)
        else:
            await event.answer()
        await self._sync_dial_msg(req)

    async def _expire_dial(self, req: dict) -> None:
        await asyncio.sleep(_callweb().DIAL_REQUEST_TTL)
        while req.get("dialing"):
            await asyncio.sleep(1.0)
        if req["live"] and not req.get("call_id"):
            await self._close_dial_request(req, "⚪ 邀请列表已过期，可在通话消息上重新点「邀请更多人」"
                                           if req.get("invite_call") else "⚪ 呼叫链接已过期，请重新发送 /call")

    async def _close_dial_request(self, req: dict, status: str) -> None:
        req["live"] = False
        req["status_text"] = status
        task = req.get("expire_task")
        req["expire_task"] = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if self.call_web is not None:
            await self.call_web.on_dial_changed(req)
        text, _ = self._render_dial(req)
        req.pop("members", None)
        if names := req.get("names"):
            shown_ids = (req.get("selected") or ())[:DIAL_PICK_SHOWN]
            req["names"] = {cid: names[cid] for cid in shown_ids if cid in names}
        try:
            await self.bot.edit_message(req["chat_id"], req["msg_id"], text, parse_mode="html", buttons=None,
                                        link_preview=False)
        except errors.MessageNotModifiedError:
            pass
        except Exception as e:
            log.info("Could not update call request %s: %s", req["dial_id"], e)

    def _finish_dial_request(self, req: dict) -> None:
        """The call was placed: its message and page now follow the call entry."""
        self._dial_requests.pop(req["dial_id"], None)
        task = req.get("expire_task")
        if task is not None and not task.done():
            task.cancel()

    async def _on_dial_cancel_callback(self, event, dial_id: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        req = self._dial_requests.get(dial_id)
        if req is None or not req["live"]:
            await event.answer("呼叫已开始或已结束")
            return
        if req.get("dialing"):
            await event.answer("正在呼叫，请在通话界面取消")
            return
        await event.answer("已取消")
        await self._close_dial_request(req, "⚪ 已取消")

    # ---------------------------------------------------------------- inviting more people (call.invite)
    async def _on_call_invite_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        call_id = self._call_tokens.get(token)
        entry = self._active_calls.get(call_id) if call_id else None
        web = self.call_web
        if not entry or web is None or not web.can_invite(entry):
            await event.answer("群通话已结束，或当前 agent-wechat 不支持追加邀请", alert=True)
            if entry:
                await self._sync_call_msg(entry)
            return
        try:
            members = await self._group_dial_members(entry["thread_id"])
        except WeChatError as e:
            await event.answer(f"获取群成员失败：{e.code}", alert=True)
            return
        # Leave out whoever is in the call or still has a pending invitation; failed or left ones can be retried.
        state = {p.get("contactId"): p.get("membership") for p in entry.get("participants") or []}
        failed = entry.get("invite_failed") or {}
        busy = {cid for cid, m in state.items() if m in ("joined", "invited")}
        busy |= {cid for cid in entry.get("invitees") or {} if cid not in failed and state.get(cid) != "left"}
        members = [m for m in members if m["id"] not in busy]
        if not members:
            await event.answer("群里没有可以再邀请的成员了", alert=True)
            return
        for old in list(self._dial_requests.values()):
            if old["live"] and old.get("invite_call") == call_id and not old.get("dialing"):
                await self._close_dial_request(old, "⚪ 已打开新的邀请列表")
        await event.answer()
        req = {
            "dial_id": secrets.token_hex(6),
            "kind": "conference",
            "call_type": entry.get("call_type", "voice"),
            "invite_call": call_id,
            "contact_id": entry["thread_id"],
            "thread_id": entry["thread_id"],
            "name": entry["caller_name"],
            "chat_id": entry["chat_id"],
            "msg_id": None,
            "status_text": "勾选要邀请的成员，再点「发送邀请」",
            "live": True,
            "dialing": False,
            "call_id": None,
            "expire_task": None,
            "members": members,
            "names": {m["id"]: m["label"] for m in members},
            "selected": [],
            "page": 0,
        }
        await self._post_dial_request(req, entry.get("msg_id"))

    async def _send_call_invite(self, event, req: dict, entry: dict) -> None:
        selected = list(req["selected"])
        if not selected:
            await event.answer("还没有选择要邀请的成员")
            return
        req["dialing"] = True  # before the first await: a double tap must not invite twice
        await event.answer("正在发送邀请…")
        try:
            await self.call_web.invite(entry, selected, req["names"])
        except _callweb().CallActionError as e:
            req["dialing"] = False
            await self._report(req["chat_id"], req["msg_id"], f"邀请失败：{e}")
            return
        req["dialing"] = False
        await self._close_dial_request(req, f"✅ 已邀请 {len(selected)} 人，结果见通话消息")

    async def _close_call_invites(self, entry: dict) -> None:
        for req in list(self._dial_requests.values()):
            if req["live"] and req.get("invite_call") == entry["call_id"] and not req.get("dialing"):
                await self._close_dial_request(req, "⚪ 群通话已结束，无法再邀请")
