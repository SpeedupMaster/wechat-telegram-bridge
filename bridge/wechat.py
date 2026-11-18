"""Minimal async client for the agent-wechat daemon HTTP API (/v1)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit

import httpx

from .stickers import WX_STICKER_LEGACY_SIZE, WX_STICKER_SIZE, WX_STICKER_STATIC_SIZE

log = logging.getLogger("wechat")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WS_FRAME = 4 * 1024 * 1024

# WeChat numeric message types (NativeMessage.type)
TEXT, IMAGE, VOICE, VIDEO, STICKER, LOCATION, APP, SYSTEM = 1, 3, 34, 43, 47, 48, 49, 10000
APP_FILE, APP_CHANNELS, APP_QUOTE, APP_UPLOAD_NOTICE = 6, 51, 57, 74

SEND_DONE = {"accepted", "completed", "succeeded", "available", "failed", "needs_review", "cancelled"}
SEND_OK = {"accepted", "completed", "succeeded", "available"}
JOB_DONE = {"accepted", "completed", "succeeded", "available", "failed", "partial", "cancelled",
            "expired", "needs_review", "unsupported"}
OP_DONE = {"succeeded", "completed", "failed", "expired", "rejected", "cancelled", "needs_review"}
OP_OK = {"succeeded", "completed"}
STICKER_PREP_TTL_SECONDS = 20 * 3600
STICKER_PREP_MAX_ENTRIES = 512
_EXPIRED_PREP_CODES = {"sticker_preparation_expired", "sticker_preparation_not_found"}


class WeChatError(Exception):
    def __init__(self, status: int, code: str, retryable: bool = False):
        super().__init__(f"agent-wechat {status} {code}")
        self.status, self.code, self.retryable = status, code, retryable


class CursorExpired(Exception):
    pass


class NoAccountYet(Exception):
    pass


@dataclass
class Media:
    data: bytes
    mime: str
    filename: str
    # Large downloads live in a private temp file (named `filename`) instead of memory; `data` is then b"".
    path: str | None = None

    @property
    def size(self) -> int:
        if self.path is not None:
            try:
                return os.path.getsize(self.path)
            except OSError:
                return 0
        return len(self.data)

    def read(self) -> bytes:
        """The whole payload as bytes (loads a file-backed download into memory)."""
        if self.path is None:
            return self.data
        with open(self.path, "rb") as f:
            return f.read()

    def tg_file(self) -> str | io.BytesIO:
        """What to hand Telethon: the temp file's path, or a fresh named in-memory buffer."""
        if self.path is not None:
            return self.path
        f = io.BytesIO(self.data)
        f.name = self.filename
        return f

    def close(self) -> None:
        """Delete the temp file of a file-backed download (no-op for in-memory media)."""
        if self.path is None:
            return
        path, self.path = self.path, None
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


# Downloads up to this size stay in memory; larger ones are streamed to a temp file.
MEDIA_SPILL_BYTES = 8 * 1024 * 1024


class _MediaTooLarge(Exception):
    pass


class _DownloadSink:
    """Collects a download in memory and spills it to a private temp file once it grows past `spill`."""

    def __init__(self, spill: int):
        self.spill = spill
        self.size = 0
        self._buf = bytearray()
        self._dir: str | None = None
        self._file = None

    def write(self, chunk: bytes) -> None:
        if self._file is None and len(self._buf) + len(chunk) > self.spill:
            self._dir = tempfile.mkdtemp(prefix="wx-media-")
            self._file = open(os.path.join(self._dir, "download.part"), "wb")
            self._file.write(self._buf)
            self._buf = bytearray()
        if self._file is not None:
            self._file.write(chunk)
        else:
            self._buf += chunk
        self.size += len(chunk)

    def reset(self) -> None:
        self.discard()
        self.size = 0

    def discard(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._buf = bytearray()

    def finish(self, mime: str, filename: str) -> Media:
        if self._file is None:
            return Media(bytes(self._buf), mime, filename)
        self._file.close()
        self._file = None
        assert self._dir is not None
        path = os.path.join(self._dir, _safe_filename(filename))
        os.replace(os.path.join(self._dir, "download.part"), path)
        self._dir = None
        return Media(b"", mime, filename, path=path)


def _safe_filename(name: str) -> str:
    """A plain file name for the temp copy (Telethon uses its basename as the document name)."""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", base).strip().lstrip(".-")
    if not base or base == "download.part":
        base = "file.bin"
    if len(base.encode("utf-8")) > 200:
        stem, ext = os.path.splitext(base)
        ext = ext if len(ext) <= 16 else ""
        while stem and len((stem + ext).encode("utf-8")) > 200:
            stem = stem[:-1]
        base = (stem or "file") + ext
    return base


_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_SEND_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_READ_METHODS = {"GET", "HEAD"}
_RETRYABLE_HTTP_ERRORS = (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError)


def _id(value: str) -> str:
    return quote(value, safe="")


def _parse_semver(raw: Any) -> tuple[int, int, int] | None:
    if not isinstance(raw, str):
        return None
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", raw)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


class WeChatClient:
    def __init__(self, base_url: str, token: str, account: str = "",
                 *, read_retry_delays: tuple[float, ...] = (1.0, 2.0)):
        self.base = base_url.rstrip("/") + "/v1/"
        self._token = token
        self.account = account
        self._supports_stickers: bool | None = None
        self._daemon_version: tuple[int, int, int] | None = None
        self._sticker_preps: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
        self._read_retry_delays = read_retry_delays
        # trust_env=False: never route the local daemon (and its token) through a system HTTP proxy
        self.http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False,
                                      timeout=_DEFAULT_TIMEOUT)

    async def close(self) -> None:
        await self.http.aclose()

    # --- plumbing -----------------------------------------------------------
    def _a(self, path: str) -> str:
        return f"accounts/{_id(self.account)}/{path}"

    async def _req(self, method: str, path: str, *, raw: bool = False, **kw) -> Any:
        delays = self._read_retry_delays if method.upper() in _READ_METHODS else ()
        for attempt in range(len(delays) + 1):
            try:
                r = await self.http.request(method, self.base + path, **kw)
                if r.status_code >= 400:
                    raise _response_error(r)
                if raw:
                    return r
                return r.json() if r.content else None
            except _RETRYABLE_HTTP_ERRORS as e:
                if attempt >= len(delays):
                    raise
                delay = delays[attempt]
                log.warning("WeChat %s %s failed (%s: %s); retrying in %.1fs (%d/%d)",
                            method, path, type(e).__name__, e, delay, attempt + 1, len(delays))
                if delay > 0:
                    await asyncio.sleep(delay)
            except WeChatError as e:
                if attempt >= len(delays) or not (e.retryable and e.status >= 500):
                    raise
                delay = delays[attempt]
                log.warning("WeChat %s %s failed (%s); retrying in %.1fs (%d/%d)",
                            method, path, e, delay, attempt + 1, len(delays))
                if delay > 0:
                    await asyncio.sleep(delay)

    async def _stream_content(self, path: str, params: dict, max_bytes: int,
                              spill: int) -> tuple[_DownloadSink, httpx.Headers]:
        """GET a media body chunk by chunk into a sink, resuming with `Range` after a dropped connection.

        Raises _MediaTooLarge once more than `max_bytes` arrive. A daemon that ignores `Range`
        (answers 200 instead of 206) simply makes the download start over.
        """
        sink = _DownloadSink(spill)
        headers: httpx.Headers | None = None
        delays = self._read_retry_delays
        attempt = 0
        try:
            while True:
                # identity: resume offsets must count the bytes as stored, not a compressed transfer
                req_headers = {"Accept-Encoding": "identity"}
                if sink.size:
                    req_headers["Range"] = f"bytes={sink.size}-"
                try:
                    async with self.http.stream("GET", self.base + path, params=params, headers=req_headers,
                                                timeout=httpx.Timeout(600.0, connect=10.0)) as r:
                        if r.status_code == 416 and sink.size:
                            await r.aread()
                            sink.reset()  # the daemon no longer agrees on the size: start over
                            raise WeChatError(416, "range_not_satisfiable", True)
                        if r.status_code >= 400:
                            await r.aread()
                            raise _response_error(r)
                        if r.status_code == 206 and _range_start(r.headers.get("content-range")) != sink.size:
                            await r.aread()
                            sink.reset()  # not the piece we asked for: start over
                            raise WeChatError(416, "range_mismatch", True)
                        if r.status_code != 206 or headers is None:
                            if sink.size:
                                sink.reset()
                            headers = r.headers
                        async for chunk in r.aiter_bytes():  # unbuffered, so a resume offset counts every byte received
                            if sink.size + len(chunk) > max_bytes:
                                raise _MediaTooLarge()
                            sink.write(chunk)
                    return sink, headers
                except _RETRYABLE_HTTP_ERRORS as e:
                    if attempt >= len(delays):
                        raise
                    why = f"{type(e).__name__}: {e}"
                except WeChatError as e:
                    if attempt >= len(delays) or not (e.retryable and (e.status >= 500 or e.status == 416)):
                        raise
                    why = str(e)
                delay = delays[attempt]
                attempt += 1
                log.warning("WeChat media download %s interrupted after %d bytes (%s); resuming in %.1fs (%d/%d)",
                            path, sink.size, why, delay, attempt, len(delays))
                if delay > 0:
                    await asyncio.sleep(delay)
        except BaseException:
            sink.discard()
            raise

    # --- account ------------------------------------------------------------
    async def resolve_account(self) -> str:
        data = await self._req("GET", "accounts")
        items = data.get("items", []) if isinstance(data, dict) else data
        ids = [str(i.get("id")) for i in items if i.get("id")]
        if not ids:
            raise NoAccountYet("no WeChat account yet — create one and log in with `wx accounts create` / `wx auth login`")
        if self.account:
            aliases = {str(i.get("alias")) for i in items if i.get("alias")}
            if self.account not in ids and self.account not in aliases:
                raise SystemExit(f"Account '{self.account}' not visible to this token. Available: {ids}")
        elif len(ids) == 1:
            self.account = ids[0]
        else:
            raise SystemExit(f"Set wechat.account in config.toml; this token sees accounts: {ids}")
        return self.account

    async def create_account(self, alias: str = "me") -> str:
        acc = await self._req("POST", "accounts", json={"alias": alias})
        self.account = str(acc["id"])
        return self.account

    async def account_info(self) -> dict:
        """Includes `authenticated` (bool) and `connection` (online / reconnecting / disconnected)."""
        return await self._req("GET", f"accounts/{_id(self.account)}")

    async def connect(self) -> dict:
        return await self._req("POST", self._a("connect"), json={})

    async def start_login(self, key: str, force_qr: bool = False) -> dict:
        body = {"strategy": "qr" if force_qr else "auto", "timeoutSeconds": 600,
                "qrRefresh": {"mode": "auto", "maxRefreshes": 3}}
        return await self._req("POST", self._a("auth/login"), json=body, headers={"Idempotency-Key": key})

    async def operation(self, op_id: str) -> dict:
        return await self._req("GET", self._a(f"operations/{_id(op_id)}"))

    async def cancel_operation(self, op_id: str) -> None:
        await self._req("DELETE", self._a(f"operations/{_id(op_id)}"))

    async def login_qr(self, op_id: str, generation: int | None = None) -> bytes:
        params = {"generation": generation} if generation else None
        r = await self._req("GET", self._a(f"operations/{_id(op_id)}/qr"), params=params, raw=True)
        return r.content

    # --- reads --------------------------------------------------------------
    async def thread(self, thread_id: str) -> dict:
        return await self._req("GET", self._a(f"threads/{_id(thread_id)}"))

    async def list_threads(self, limit: int = 20) -> list[dict]:
        data = await self._req("GET", self._a("threads"), params={"limit": limit})
        return data.get("items", [])

    async def message(self, message_id: str) -> dict:
        return await self._req("GET", self._a(f"messages/{_id(message_id)}"))

    async def thread_messages(self, thread_id: str, limit: int = 20) -> list[dict]:
        data = await self._req("GET", self._a(f"threads/{_id(thread_id)}/messages"), params={"limit": limit})
        return data.get("items", [])

    async def contact(self, username: str) -> dict:
        return await self._req("GET", self._a(f"contacts/{_id(username)}"))

    async def search_contacts(self, text: str, limit: int = 20, *, relation: str | None = None) -> list[dict]:
        """Search cached contacts. agent-wechat >= 2.7 lists only friends unless `relation` says otherwise
        (`all`, `friend`, `non_contact`, `unknown`); older daemons reject the filter, so it is dropped then."""
        params: dict[str, Any] = {"search": text, "limit": limit}
        if relation:
            params["relation"] = relation
        try:
            data = await self._req("GET", self._a("contacts"), params=params)
        except WeChatError as e:
            if not relation or e.status not in (400, 422):
                raise
            params.pop("relation")
            data = await self._req("GET", self._a("contacts"), params=params)
        return data.get("items", [])

    # --- friend requests and contact settings (agent-wechat >= 2.7) -----------
    async def contact_requests(self, *, direction: str | None = None, actionable: bool | None = None,
                               limit: int = 20, cursor: str | None = None) -> dict:
        """One page of request conversations: {"items", "pendingIncomingCount", "nextCursor", ...}.
        Tombstones of rows removed since the list was read are dropped from `items`."""
        params: dict[str, Any] = {"limit": limit}
        if direction:
            params["direction"] = direction
        if actionable is not None:
            params["actionable"] = "true" if actionable else "false"
        if cursor:
            params["cursor"] = cursor
        page = await self._req("GET", self._a("contact-requests"), params=params) or {}
        page["items"] = [i for i in page.get("items") or [] if isinstance(i, dict) and not i.get("deleted")]
        return page

    async def contact_request(self, request_id: str) -> dict:
        return await self._req("GET", self._a(f"contact-requests/{_id(request_id)}"))

    async def accept_contact_request(self, request_id: str, version: str, key: str,
                                     privacy: dict | None = None) -> dict:
        body: dict[str, Any] = {"expectedVersion": version}
        if privacy is not None:
            body["privacy"] = privacy
        op = await self._req("POST", self._a(f"contact-requests/{_id(request_id)}/accept"), json=body,
                             headers={"Idempotency-Key": key}, timeout=_SEND_TIMEOUT)
        return await self._wait_operation(op)

    async def delete_contact_request(self, request_id: str, version: str, key: str) -> dict:
        """Delete the local request history only; nothing is rejected or cancelled on WeChat."""
        op = await self._req("DELETE", self._a(f"contact-requests/{_id(request_id)}"),
                             json={"expectedVersion": version}, headers={"Idempotency-Key": key})
        return await self._wait_operation(op)

    async def discover_contact(self, query: str) -> dict:
        """Look a person up on WeChat by exact WeChat ID. Returns a short-lived `recipientRef`
        (null when WeChat offers no way to add them), plus `contactId` / `request` when already known."""
        return await self._req("POST", self._a("search/contacts"), json={"query": query}, timeout=_SEND_TIMEOUT)

    async def send_contact_request(self, recipient_ref: str, message: str, key: str,
                                   privacy: dict | None = None) -> dict:
        body: dict[str, Any] = {"recipientRef": recipient_ref, "message": message}
        if privacy is not None:
            body["privacy"] = privacy
        op = await self._req("POST", self._a("contact-requests"), json=body,
                             headers={"Idempotency-Key": key}, timeout=_SEND_TIMEOUT)
        return await self._wait_operation(op)

    async def contact_privacy(self, username: str) -> dict:
        """{"privacy": {"chatsOnly", "hideMyPosts", "hideTheirPosts"}, "version"}"""
        return await self._req("GET", self._a(f"contacts/{_id(username)}/privacy"))

    async def update_contact_privacy(self, username: str, privacy: dict, version: str, key: str) -> dict:
        op = await self._req("PATCH", self._a(f"contacts/{_id(username)}"),
                             json={"expectedVersion": version, "privacy": privacy},
                             headers={"Idempotency-Key": key}, timeout=_SEND_TIMEOUT)
        return await self._wait_operation(op)

    async def remove_contact(self, username: str, key: str) -> dict:
        """Remove the friendship; the chat history stays."""
        op = await self._req("DELETE", self._a(f"contacts/{_id(username)}"), headers={"Idempotency-Key": key},
                             timeout=_SEND_TIMEOUT)
        return await self._wait_operation(op)

    async def _wait_operation(self, op: Any, timeout: float = 120) -> dict:
        if not isinstance(op, dict):
            return {}
        if not op.get("id") or op.get("state") in OP_DONE:
            return op
        return await self._wait(op, lambda o: f"operations/{_id(o['id'])}", OP_DONE, timeout)

    async def search_groups(self, text: str, limit: int = 20) -> list[dict]:
        data = await self._req("GET", self._a("groups"), params={"search": text, "limit": limit})
        return data.get("items", [])

    async def group_member(self, room: str, username: str) -> dict | None:
        data = await self._req("GET", self._a(f"groups/{_id(room)}/members/{_id(username)}"))
        return data.get("member")

    async def group_members(self, room: str, *, search: str | None = None) -> list[dict]:
        items: list[dict] = []
        seen: set[str] = set()
        cursor: str | None = None
        q = (search or "").strip()
        while True:
            params: dict[str, Any] = {"limit": 500}
            if q:
                params["search"] = q
            if cursor:
                params["cursor"] = cursor
            data = await self._req("GET", self._a(f"groups/{_id(room)}/members"), params=params)
            added = 0
            for m in data.get("items", []):
                uid = m.get("id")
                if uid and uid not in seen:
                    seen.add(uid)
                    items.append(m)
                    added += 1
            nxt = data.get("nextCursor")
            if not nxt or nxt == cursor or added == 0:
                break
            cursor = str(nxt)
        return items

    # --- group management (needs an admin-scoped daemon token) -------------------
    async def group(self, room: str) -> dict:
        return await self._req("GET", self._a(f"groups/{_id(room)}"))

    async def _group_mutation(self, method: str, path: str, key: str, body: dict | None = None,
                              timeout: float = 90) -> dict:
        kw: dict[str, Any] = {"headers": {"Idempotency-Key": key}, "timeout": _SEND_TIMEOUT}
        if body is not None:
            kw["json"] = body
        op = await self._req(method, self._a(path), **kw)
        return await self._wait_operation(op, timeout)

    async def create_group(self, members: list[str], name: str | None, key: str) -> dict:
        body: dict[str, Any] = {"members": members}
        if name:
            body["name"] = name
        return await self._group_mutation("POST", "groups", key, body)

    async def rename_group(self, room: str, name: str, key: str) -> dict:
        return await self._group_mutation("PATCH", f"groups/{_id(room)}", key, {"name": name})

    async def leave_group(self, room: str, key: str) -> dict:
        return await self._group_mutation("POST", f"groups/{_id(room)}/leave", key)

    async def add_group_members(self, room: str, members: list[str], key: str) -> dict:
        return await self._group_mutation("POST", f"groups/{_id(room)}/members", key, {"members": members})

    async def remove_group_members(self, room: str, members: list[str], key: str) -> dict:
        return await self._group_mutation("DELETE", f"groups/{_id(room)}/members", key, {"members": members})

    async def set_my_group_name(self, room: str, name: str, key: str) -> dict:
        return await self._group_mutation("PUT", f"groups/{_id(room)}/my-name", key, {"displayName": name})

    async def clear_group_announcement(self, room: str, key: str) -> dict:
        return await self._group_mutation("DELETE", f"groups/{_id(room)}/announcement", key)

    async def set_thread_muted(self, thread_id: str, muted: bool, key: str) -> dict:
        return await self._group_mutation("PUT", f"threads/{_id(thread_id)}/notifications", key,
                                          {"muted": bool(muted)})

    async def group_mutation_by_key(self, key: str) -> dict:
        return await self._req("GET", self._a("group-mutations/by-idempotency-key"), params={"key": key})

    async def refresh_group(self, room: str, *, members: bool = False, timeout: float = 45) -> dict:
        """Ask WeChat for the latest group details (or member list) and wait for the operation."""
        path = f"groups/{_id(room)}/members/refresh" if members else f"groups/{_id(room)}/refresh"
        op = await self._req("POST", self._a(path), timeout=_SEND_TIMEOUT)
        return await self._wait_operation(op, timeout)

    async def avatar(self, username: str) -> bytes | None:
        try:
            r = await self._req("GET", self._a(f"avatars/{_id(username)}"), raw=True)
        except WeChatError:
            return None
        return r.content if r.headers.get("content-type", "").startswith("image/") else None

    async def fetch_avatar(self, username: str, wait: float = 60.0,
                           retry_delays: tuple[float, ...] = (1, 2, 4)) -> tuple[bytes | None, str]:
        """(avatar, "") — or (None, why not). The daemon only caches avatars it has seen, so ask WeChat first."""
        if data := await self.avatar(username):
            return data, ""
        try:
            op = await self._req("POST", self._a(f"avatars/{_id(username)}/refresh"), json={})
        except WeChatError as e:
            return None, f"refresh refused: {e.code}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while op.get("state") not in ("succeeded", "failed", "expired", "rejected", "cancelled", "needs_review") \
                and loop.time() < deadline:
            await asyncio.sleep(1.5)
            op = await self.operation(op["id"])
        state = op.get("state")
        if state != "succeeded":
            result = op.get("result") or {}
            code = result.get("code") if isinstance(result, dict) else None
            return None, f"refresh {state or 'unknown'}: {code or result or 'no details'}"
        for delay in (0, *retry_delays):  # the daemon's profile reader can lag a moment behind the write
            await asyncio.sleep(delay)
            if data := await self.avatar(username):
                return data, ""
        return None, "refresh succeeded but the avatar is still not readable"

    async def mark_read(self, thread_id: str) -> None:
        t = await self.thread(thread_id)
        if t.get("unreadCount"):
            await self._req("POST", self._a(f"threads/{_id(thread_id)}/read-state"),
                            json={"expectedVersion": t["readStateVersion"]})

    # --- events -------------------------------------------------------------
    async def latest_cursor(self) -> str:
        """Walk the event log to its end without handling anything (first start)."""
        cursor = "0"
        while True:
            page = await self._req("GET", self._a("events"), params={"after": cursor, "limit": 200})
            nxt = str(page.get("nextCursor") or cursor)
            if not page.get("items") or nxt == cursor:
                return nxt
            cursor = nxt

    async def ack(self, consumer: str, cursor: str) -> None:
        await self._req("POST", self._a(f"consumers/{_id(consumer)}/ack"), json={"cursor": cursor})

    async def stream(self, after: str) -> AsyncIterator[dict]:
        """Server-sent events after `after`. Ends when the connection drops."""
        timeout = httpx.Timeout(10.0, read=60.0)  # server keepalives every ~15s
        async with self.http.stream("GET", self.base + self._a("events/stream"), params={"after": after},
                                    headers={"Accept": "text/event-stream"}, timeout=timeout) as r:
            if r.status_code >= 400:
                await r.aread()
                raise WeChatError(r.status_code, "stream_failed", True)
            data: list[str] = []
            ev_type = ev_id = ""
            async for line in r.aiter_lines():
                if line == "":
                    if data:
                        payload = json.loads("\n".join(data))
                        if ev_type == "error":
                            code = (payload.get("error") or {}).get("code", "stream_failed")
                            if code == "event_cursor_expired":
                                raise CursorExpired()
                            raise WeChatError(500, code, True)
                        payload.setdefault("cursor", ev_id)
                        yield payload
                    data, ev_type, ev_id = [], "", ""
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
                elif line.startswith("event:"):
                    ev_type = line[6:].strip()
                elif line.startswith("id:"):
                    ev_id = line[3:].strip()

    # --- sending ------------------------------------------------------------
    async def send_text(self, thread_id: str, text: str, key: str, quote_ref: dict | None = None,
                        mentions: dict | None = None) -> dict:
        body: dict = {"threadId": thread_id, "text": text}
        if quote_ref or mentions:
            body = {"kind": "text", **body}
        if quote_ref:
            body["quote"] = quote_ref
        if mentions:
            body["mentions"] = mentions
        intent = await self._req("POST", self._a("sends"), json=body, headers={"Idempotency-Key": key},
                                 timeout=_SEND_TIMEOUT)
        return await self._wait(intent, lambda i: f"sends/{_id(i['id'])}", SEND_DONE, 120)

    async def supports_stickers(self) -> bool:
        if self._supports_stickers is not None:
            return self._supports_stickers
        try:
            caps = await self._req("GET", self._a("capabilities"))
            self._supports_stickers = bool(isinstance(caps, dict) and caps.get("stickers"))
        except (WeChatError, httpx.HTTPError):
            return False
        return self._supports_stickers

    async def daemon_version(self) -> tuple[int, int, int] | None:
        if self._daemon_version is not None:
            return self._daemon_version
        try:
            st = await self._req("GET", "status")
        except (WeChatError, httpx.HTTPError):
            return None
        vinfo = st.get("version") if isinstance(st, dict) else None
        ver = _parse_semver(vinfo.get("version") if isinstance(vinfo, dict) else None)
        if ver is not None:
            self._daemon_version = ver
        return ver

    async def sticker_edge_limits(self) -> tuple[int, int]:
        """Return (animated_edge, static_edge): 384/512 on agent-wechat >= 2.5.0, else 240/240."""
        ver = await self.daemon_version()
        if ver is not None and ver >= (2, 5, 0):
            return WX_STICKER_SIZE, WX_STICKER_STATIC_SIZE
        return WX_STICKER_LEGACY_SIZE, WX_STICKER_LEGACY_SIZE

    async def prepare_sticker(self, media: Media, key: str, *, fit: bool = True) -> dict:
        files = {"file": (media.filename, media.data, media.mime)}
        source = await self._req("POST", self._a("uploads"), files=files, timeout=httpx.Timeout(600.0))
        job = await self._req("POST", self._a("sticker-preparations"),
                              json={"sourceId": source["id"], "fit": fit, "trim": None},
                              headers={"Idempotency-Key": key}, timeout=_SEND_TIMEOUT)
        return await self._wait(job, lambda j: f"sticker-preparations/{_id(j['id'])}", JOB_DONE, 300, max_delay=0.8)

    def _get_cached_prep(self, cache_key: tuple[str, str]) -> str | None:
        entry = self._sticker_preps.get(cache_key)
        if not entry:
            return None
        prep_id, expires_at = entry
        if time.monotonic() >= expires_at:
            self._sticker_preps.pop(cache_key, None)
            return None
        self._sticker_preps.move_to_end(cache_key)
        return prep_id

    def _put_cached_prep(self, cache_key: tuple[str, str], prep_id: str) -> None:
        self._sticker_preps[cache_key] = (prep_id, time.monotonic() + STICKER_PREP_TTL_SECONDS)
        self._sticker_preps.move_to_end(cache_key)
        while len(self._sticker_preps) > STICKER_PREP_MAX_ENTRIES:
            self._sticker_preps.popitem(last=False)

    async def send_sticker(self, thread_id: str, media: Media, key: str) -> dict:
        cache_key = (self.account or "", hashlib.sha256(media.data).hexdigest())
        prep_id = self._get_cached_prep(cache_key)
        used_cached = prep_id is not None
        if not prep_id:
            prep = await self.prepare_sticker(media, f"{key}-prep", fit=True)
            if prep.get("state") != "available":
                return prep
            prep_id = prep["id"]
            self._put_cached_prep(cache_key, prep_id)

        send_key = key
        for attempt in range(2):
            body = {"kind": "sticker", "threadId": thread_id,
                    "source": {"kind": "prepared", "preparationId": prep_id}}
            stale = False
            res: dict = {}
            try:
                job = await self._req("POST", self._a("sends"), json=body,
                                      headers={"Idempotency-Key": send_key}, timeout=_SEND_TIMEOUT)
                res = await self._wait(job, lambda j: f"jobs/{_id(j['id'])}", JOB_DONE, 3600, max_delay=0.8)
                reason = res.get("reason") or (res.get("error") or {}).get("code")
                if res.get("state") == "failed" and reason in _EXPIRED_PREP_CODES:
                    stale = True
                else:
                    return res
            except WeChatError as e:
                if used_cached and attempt == 0 and e.code in _EXPIRED_PREP_CODES:
                    stale = True
                else:
                    raise
            if not (stale and used_cached and attempt == 0):
                return res
            self._sticker_preps.pop(cache_key, None)
            prep = await self.prepare_sticker(media, f"{key}-reprep", fit=True)
            if prep.get("state") != "available":
                return prep
            prep_id = prep["id"]
            self._put_cached_prep(cache_key, prep_id)
            used_cached = False
            send_key = f"{key}-retry"
        return {}

    async def send_media(self, thread_id: str, kind: str, media: Media, key: str) -> dict:
        files = {"file": (media.filename, media.data, media.mime)}
        source = await self._req("POST", self._a("uploads"), files=files, timeout=httpx.Timeout(600.0))
        body = {"threadId": thread_id, "kind": kind, "sourceId": source["id"], "standardSourceId": None,
                "thumbnailSourceId": None, "playSeconds": None, "durationMs": None}
        if kind == "image":
            body["imageQuality"] = "original"
        if kind == "video":
            body["videoQuality"] = "original"  # 720p playback copy plus the untouched original
        try:
            job = await self._req("POST", self._a("sends"), json=body, headers={"Idempotency-Key": key},
                                  timeout=_SEND_TIMEOUT)
        except WeChatError as e:
            if kind == "image" and e.status in (400, 422) and "imageQuality" in body:
                body.pop("imageQuality", None)
                job = await self._req("POST", self._a("sends"), json=body,
                                      headers={"Idempotency-Key": f"{key}-std"}, timeout=_SEND_TIMEOUT)
            else:
                raise
        return await self._wait(job, lambda j: f"jobs/{_id(j['id'])}", JOB_DONE, 3600)

    async def recall_message(self, message_id: str, server_id: str, key: str) -> dict:
        op = await self._req("POST", self._a(f"messages/{_id(message_id)}/recall"),
                             json={"serverId": str(server_id)}, headers={"Idempotency-Key": key})
        done = {"succeeded", "completed", "failed", "expired", "rejected", "cancelled", "needs_review"}
        if isinstance(op, dict) and op.get("id") and op.get("state") not in done:
            op = await self._wait(op, lambda o: f"operations/{_id(o['id'])}", done, 30)
        return op if isinstance(op, dict) else {}

    async def _wait(self, item: dict, path, done: set[str], timeout: float, *, max_delay: float = 2.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline, delay = loop.time() + timeout, 0.3
        while item.get("state") not in done and loop.time() < deadline:
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, max_delay)
            item = await self._req("GET", self._a(path(item)))
        return item

    # --- receiving media ----------------------------------------------------
    async def download_channels_video(self, message: dict, max_bytes: int) -> Media | str | None:
        """Fetch a Channels card's playable video if present, returning None when none is offered."""
        return await self._download_media(message, max_bytes, channels=True)

    async def download_media(self, message: dict, max_bytes: int) -> Media | str:
        """Fetch the best available copy of a message attachment, or return a reason string."""
        res = await self._download_media(message, max_bytes, channels=False)
        assert res is not None
        return res

    async def _download_media(self, message: dict, max_bytes: int, *, channels: bool) -> Media | str | None:
        mid = message["id"]
        mtype = VIDEO if channels else message["type"]
        quality = "normal" if mtype in (VOICE, VIDEO) else "best"

        def eligible(a: dict) -> bool:
            if channels and a.get("kind") != "video":
                return False
            return _eligible_asset(a, mtype)

        async def assets() -> list[dict]:
            return (await self._req("GET", self._a(f"messages/{_id(mid)}/media"))).get("items", [])

        items = await assets()
        if channels and not any(eligible(a) for a in items):
            return None
        asset = None
        last_job: dict = {}
        variant_order = (
            ("original", "standard", "thumbnail") if mtype == IMAGE
            else ("normal", "original") if mtype == VIDEO
            else ("normal",) if mtype == VOICE
            else ()
        )
        if variant_order and items:
            # Prefer full-resolution `original` for images, playable `normal` WAV for voice,
            # and actual video variants over cached thumbnails:
            # `agent-wechat` may already hold a cached thumbnail/preview while the full media is `not_cached`.
            for variant in variant_order:
                cands = [
                    a for a in items
                    if eligible(a) and (a.get("actualVariant") or a.get("variant")) == variant
                ]
                if not cands:
                    continue
                avail = [a for a in cands if a.get("state") == "available" and (a.get("byteLength") or 0) <= max_bytes]
                if avail:
                    asset = avail[0]
                    break
                fetchable = [
                    a for a in cands
                    if a.get("state") not in ("expired", "unsupported", "recalled", "available")
                    and (a.get("advertisedByteLength") or 0) <= max_bytes
                ]
                if not fetchable:
                    continue
                cand = fetchable[0]
                last_job = await self._req(
                    "POST", self._a(f"messages/{_id(mid)}/media-transfers"),
                    json={"messageId": mid, "assetId": cand.get("id"), "quality": variant},
                )
                last_job = await self._wait(last_job, lambda j: f"jobs/{_id(j['id'])}", JOB_DONE, 300)
                items = await assets()
                avail = [
                    a for a in items
                    if eligible(a)
                    and (a.get("id") == cand.get("id") or (a.get("actualVariant") or a.get("variant")) == variant)
                    and a.get("state") == "available" and (a.get("byteLength") or 0) <= max_bytes
                ]
                if avail:
                    asset = avail[0]
                    break
        if not asset:
            pool = [a for a in items if eligible(a)]
            asset = _pick(pool, mtype)
        if not asset:
            eligible_items = [a for a in items if eligible(a)]
            states = {a.get("state") for a in eligible_items}
            if states & {"expired", "unsupported", "recalled"} and not states & {"pending", "not_cached"}:
                reasons = [a["reason"] for a in eligible_items if a.get("reason")]
                return reasons[0] if reasons else "已过期" if "expired" in states else "不支持"
            big = [
                a for a in eligible_items
                if (a.get("advertisedByteLength") or 0) > max_bytes or (a.get("byteLength") or 0) > max_bytes
            ]
            if eligible_items and len(big) == len(eligible_items):
                return "文件太大"
            if last_job:
                reason = last_job.get("reason") or last_job.get("state")
                return reason if reason and reason != "available" else "下载失败"
            job = await self._req("POST", self._a(f"messages/{_id(mid)}/media-transfers"),
                                  json={"messageId": mid, "assetId": None, "quality": quality})
            job = await self._wait(job, lambda j: f"jobs/{_id(j['id'])}", JOB_DONE, 300)
            items = await assets()
            asset = _pick([a for a in items if eligible(a)], mtype)
            if not asset:
                reason = job.get("reason") or job.get("state")
                return reason if reason and reason != "available" else "下载失败"
        if (asset.get("byteLength") or 0) > max_bytes:
            return "文件太大"
        mime = (asset.get("contentType") or "application/octet-stream").split(";")[0].strip().lower()
        params = {}
        if mime == "image/x-wxgf" and asset.get("preview"):  # WeChat's own image format: use the JPEG/PNG preview
            params = {"representation": "preview"}
            mime = asset["preview"]["contentType"]
        try:
            sink, headers = await self._stream_content(self._a(f"media/{_id(asset['id'])}/content"), params,
                                                       max_bytes, MEDIA_SPILL_BYTES)
        except _MediaTooLarge:
            return "文件太大"
        if not params and mime == "application/octet-stream":
            hdr_ct = headers.get("content-type", "").split(";")[0].strip().lower()
            if hdr_ct and hdr_ct != "application/octet-stream":
                mime = hdr_ct
            elif mtype == VIDEO and ((fname := (asset.get("filename") or "").lower()).endswith(".mp4") or "." not in fname):
                mime = "video/mp4"
        name = asset.get("filename") or f"{mid[-12:]}{_ext(mime)}"
        if params:
            name = name.rsplit(".", 1)[0] + _ext(mime)
        return sink.finish(mime, name)

    async def download_link_thumbnail(self, message: dict, max_bytes: int = 4 * 1024 * 1024) -> bytes | None:
        """Fetch a shared link's thumbnail image bytes when available, or return None."""
        mid = message["id"]
        try:
            items = (await self._req("GET", self._a(f"messages/{_id(mid)}/media"))).get("items", [])
        except (WeChatError, httpx.HTTPError):
            return None
        cands = [
            a for a in items
            if a.get("kind") in (None, "image")
            and (a.get("actualVariant") or a.get("variant")) in ("thumbnail", None)
        ]
        if not cands:
            cands = [a for a in items if a.get("kind") == "image"]
        if not cands:
            return None

        def _usable(a: dict) -> bool:
            return a.get("state") == "available" and (a.get("byteLength") or 0) <= max_bytes

        asset = next((a for a in cands if _usable(a)), None)
        if not asset:
            fetchable = [
                a for a in cands
                if a.get("state") not in ("expired", "unsupported", "recalled", "available")
                and (a.get("advertisedByteLength") or 0) <= max_bytes
            ]
            if not fetchable:
                return None
            cand = fetchable[0]
            try:
                job = await self._req(
                    "POST", self._a(f"messages/{_id(mid)}/media-transfers"),
                    json={"messageId": mid, "assetId": cand.get("id"), "quality": cand.get("variant") or "thumbnail"},
                )
                await self._wait(job, lambda j: f"jobs/{_id(j['id'])}", JOB_DONE, 20)
                items = (await self._req("GET", self._a(f"messages/{_id(mid)}/media"))).get("items", [])
            except (WeChatError, httpx.HTTPError):
                return None
            asset = next(
                (a for a in items if a.get("id") == cand.get("id") and _usable(a)),
                None,
            ) or next(
                (a for a in items if a.get("kind") in (None, "image") and _usable(a)),
                None,
            )
        if not asset:
            return None
        mime = (asset.get("contentType") or "").split(";")[0].strip().lower()
        params = {}
        if mime == "image/x-wxgf" and asset.get("preview"):
            params = {"representation": "preview"}
            mime = (asset["preview"].get("contentType") or "").split(";")[0].strip().lower()
        if mime and not mime.startswith("image/"):
            return None
        try:
            r = await self._req("GET", self._a(f"media/{_id(asset['id'])}/content"), params=params, raw=True)
        except (WeChatError, httpx.HTTPError):
            return None
        return r.content or None


def _range_start(content_range: str | None) -> int | None:
    m = re.match(r"^\s*bytes\s+(\d+)-\d+/(?:\d+|\*)\s*$", content_range or "")
    return int(m.group(1)) if m else None


def _response_error(r: httpx.Response) -> WeChatError:
    code, retryable = "request_failed", r.status_code >= 500
    try:
        err = r.json().get("error", {})
        code = err.get("code", code)
        retryable = err.get("retryable", retryable)
    except Exception:
        pass
    return WeChatError(r.status_code, code, retryable)


def _eligible_asset(asset: dict, mtype: int) -> bool:
    """Reject preview thumbnails/images when downloading a video or voice message."""
    variant = asset.get("actualVariant") or asset.get("variant")
    kind = asset.get("kind")
    ct = (asset.get("contentType") or "").split(";")[0].strip().lower()
    if mtype == VIDEO:
        if variant == "thumbnail" or (kind is not None and kind != "video") or ct.startswith("image/"):
            return False
    elif mtype == VOICE:
        # agent-wechat exposes MediaKind::Voice as "voice" (with "normal" audio/wav and "encoded" audio/silk).
        if (
            variant in ("thumbnail", "encoded")
            or (kind is not None and kind not in ("voice", "audio"))
            or ct.startswith("image/")
            or ct == "audio/silk"
        ):
            return False
    return True


def _pick(assets: list[dict], mtype: int) -> dict | None:
    order = ["normal"] if mtype == VOICE else ["normal", "original"] if mtype == VIDEO else \
        ["original", "standard", "thumbnail", "encoded", "normal"]
    ok = [a for a in assets if a.get("state") == "available" and _eligible_asset(a, mtype)]
    ok.sort(key=lambda a: order.index(v) if (v := a.get("actualVariant") or a.get("variant")) in order else 99)
    return ok[0] if ok else None


def _ext(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
            "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            "audio/amr": ".amr", "audio/silk": ".silk", "audio/wav": ".wav"}.get(mime, ".bin")


async def _read_http_error(reader: asyncio.StreamReader, headers: dict[str, str], status: int) -> WeChatError:
    code, retryable = "request_failed", status >= 500
    raw = b""
    try:
        if cl := headers.get("content-length"):
            n = min(int(cl), 65536)
            if n > 0:
                raw = await asyncio.wait_for(reader.readexactly(n), timeout=1.0)
        else:
            raw = await asyncio.wait_for(reader.read(4096), timeout=0.3)
    except Exception:
        pass
    if raw:
        try:
            err = json.loads(raw).get("error", {})
            code = err.get("code", code)
            retryable = err.get("retryable", retryable)
        except Exception:
            pass
    return WeChatError(status, code, retryable)


def _ws_mask(payload: bytes, mask: bytes) -> bytes:
    """XOR `payload` with the repeating 4-byte WebSocket `mask` (fast path for audio-rate traffic)."""
    n = len(payload)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(key, "little")).to_bytes(n, "little")


class CallsSocket:
    """Minimal RFC 6455 WebSocket client for the calls control socket and a call's media socket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._write_lock = asyncio.Lock()
        self._pending_cmds: dict[str, asyncio.Future[dict]] = {}
        self._queue: asyncio.Queue[dict | Exception | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._closing = False

    @property
    def closed(self) -> bool:
        return self._closed or self._closing

    @classmethod
    async def connect(cls, url: str, token: str, timeout: float = 10.0) -> CallsSocket:
        u = urlsplit(url)
        use_ssl = u.scheme == "https"
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if use_ssl else 80)
        host_fmt = f"[{host}]" if ":" in host else host
        host_hdr = f"{host_fmt}:{u.port}" if u.port else host_fmt
        target = (u.path or "/") + (f"?{u.query}" if u.query else "")

        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=use_ssl), timeout=timeout)
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {host_hdr}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n"
            "\r\n"
        ).encode("utf-8")
        try:
            writer.write(req)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
            lines = head[:-4].decode("latin-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            if len(parts) < 2 or not parts[1].isdigit():
                raise WeChatError(502, "invalid_websocket_response", True)
            status = int(parts[1])
            headers = {k.strip().lower(): v.strip() for line in lines[1:] if ":" in line
                       for k, _, v in [line.partition(":")]}
            if status != 101:
                raise await _read_http_error(reader, headers, status)
            expected = base64.b64encode(hashlib.sha1((sec_key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != expected:
                raise WeChatError(502, "invalid_websocket_handshake", True)
            return cls(reader, writer)
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WeChatError(503, "closed", True)
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head.extend(struct.pack("!H", n))
        else:
            head.append(0x80 | 127)
            head.extend(struct.pack("!Q", n))
        mask = os.urandom(4)
        head.extend(mask)
        masked = _ws_mask(payload, mask)
        async with self._write_lock:
            self.writer.write(bytes(head) + masked)
            await self.writer.drain()

    async def send_json(self, obj: dict) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        await self._send_frame(0x1, data)

    async def _recv_text(self, read_timeout: float = 90.0) -> str | None:
        chunks: list[bytes] = []
        data_opcode: int | None = None
        while True:
            try:
                hdr = await asyncio.wait_for(self.reader.readexactly(2), timeout=read_timeout)
            except (asyncio.IncompleteReadError, ConnectionError, EOFError):
                return None
            b0, b1 = hdr[0], hdr[1]
            fin = (b0 & 0x80) != 0
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", await asyncio.wait_for(self.reader.readexactly(2), timeout=read_timeout))[0]
            elif length == 127:
                length = struct.unpack("!Q", await asyncio.wait_for(self.reader.readexactly(8), timeout=read_timeout))[0]
            if length > _MAX_WS_FRAME:
                raise WeChatError(502, "frame_too_large", True)
            mask = await asyncio.wait_for(self.reader.readexactly(4), timeout=read_timeout) if masked else b""
            payload = await asyncio.wait_for(self.reader.readexactly(length), timeout=read_timeout) if length else b""
            if masked and payload:
                payload = _ws_mask(payload, mask)

            if opcode == 0x8:  # Close
                if not self._closing:
                    self._closing = True
                    try:
                        await self._send_frame(0x8, payload[:2])
                    except Exception:
                        pass
                return None
            if opcode == 0x9:  # Ping -> immediate Pong
                try:
                    await self._send_frame(0xA, payload)
                except Exception:
                    return None
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode in (0x1, 0x2):
                data_opcode = opcode
                chunks = [payload]
            elif opcode == 0x0:
                chunks.append(payload)
            else:
                continue
            if fin:
                body = b"".join(chunks)
                chunks.clear()
                if data_opcode == 0x1:
                    return body.decode("utf-8")
                data_opcode = None

    async def handshake(self, timeout: float = 10.0) -> dict:
        await self.send_json({"type": "call.hello", "supportedProtocolVersions": [2]})
        raw = await self._recv_text(read_timeout=timeout)
        if raw is None:
            raise WeChatError(503, "closed", True)
        msg = json.loads(raw)
        if msg.get("type") in ("protocol.error", "call.error"):
            code = msg.get("code") or "call_error"
            status = 403 if code == "forbidden" else 400
            raise WeChatError(status, code, False)
        if msg.get("type") != "hello":
            raise WeChatError(502, "unexpected_hello", True)
        self._reader_task = asyncio.create_task(self._read_loop())
        return msg

    async def attach_media(self, client_id: str, owner_token: str, timeout: float = 10.0) -> dict:
        """Media socket handshake: media.attach -> media.ready (the send and receive tracks)."""
        await self.send_json({
            "type": "media.attach",
            "supportedProtocolVersions": [2],
            "clientId": client_id,
            "ownerToken": owner_token,
        })
        while True:
            raw = await self._recv_text(read_timeout=timeout)
            if raw is None:
                raise WeChatError(503, "closed", True)
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype in ("protocol.error", "call.error"):
                code = msg.get("code") or "call_error"
                raise WeChatError(403 if code in ("forbidden", "not_owner") else 400, code, False)
            if mtype == "media.ready":
                break
        if msg.get("protocolVersion") != 2:
            raise WeChatError(400, "unsupported_protocol_version", False)
        self._reader_task = asyncio.create_task(self._read_loop())
        return msg

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._recv_text()
                if raw is None:
                    break
                ev = json.loads(raw)
                if ev.get("type") == "command.result":
                    req_id = ev.get("requestId")
                    if (fut := self._pending_cmds.get(req_id)) and not fut.done():
                        fut.set_result(ev)
                    continue
                await self._queue.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._queue.put(e)
        finally:
            self._closed = True
            for fut in list(self._pending_cmds.values()):
                if not fut.done():
                    fut.set_exception(WeChatError(503, "closed", True))
            await self._queue.put(None)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def command(self, payload: dict, timeout: float = 10.0) -> dict:
        if self.closed:
            raise WeChatError(503, "closed", True)
        req_id = str(payload.get("requestId") or f"bridge-{uuid.uuid4().hex[:12]}")
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending_cmds[req_id] = fut
        try:
            await self.send_json({**payload, "requestId": req_id})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_cmds.pop(req_id, None)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if not self._closed:
                await self._send_frame(0x8, b"\x03\xe8")
        except Exception:
            pass
        self._closed = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


# Attach calls_socket to WeChatClient
async def _open_calls_socket(self: WeChatClient, timeout: float = 10.0) -> tuple[CallsSocket, dict]:
    sock = await CallsSocket.connect(self.base + self._a("calls/socket"), self._token, timeout=timeout)
    try:
        hello = await sock.handshake(timeout=timeout)
        return sock, hello
    except Exception:
        await sock.close()
        raise


async def _open_call_media(self: WeChatClient, call_id: str, client_id: str, owner_token: str,
                           timeout: float = 10.0) -> tuple[CallsSocket, dict]:
    sock = await CallsSocket.connect(self.base + self._a(f"calls/{_id(call_id)}/media"), self._token, timeout=timeout)
    try:
        ready = await sock.attach_media(client_id, owner_token, timeout=timeout)
        return sock, ready
    except Exception:
        await sock.close()
        raise


WeChatClient.calls_socket = _open_calls_socket  # type: ignore[attr-defined]
WeChatClient.call_media = _open_call_media  # type: ignore[attr-defined]
