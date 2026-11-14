"""Minimal async client for the agent-wechat daemon HTTP API (/v1)."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

log = logging.getLogger("wechat")

# WeChat numeric message types (NativeMessage.type)
TEXT, IMAGE, VOICE, VIDEO, STICKER, LOCATION, APP, SYSTEM = 1, 3, 34, 43, 47, 48, 49, 10000
APP_FILE, APP_CHANNELS, APP_QUOTE, APP_UPLOAD_NOTICE = 6, 51, 57, 74

SEND_DONE = {"accepted", "completed", "succeeded", "available", "failed", "needs_review", "cancelled"}
SEND_OK = {"accepted", "completed", "succeeded", "available"}
JOB_DONE = {"accepted", "completed", "succeeded", "available", "failed", "partial", "cancelled",
            "expired", "needs_review", "unsupported"}


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


def _id(value: str) -> str:
    return quote(value, safe="")


class WeChatClient:
    def __init__(self, base_url: str, token: str, account: str = ""):
        self.base = base_url.rstrip("/") + "/v1/"
        self.account = account
        self._supports_stickers: bool | None = None
        # trust_env=False: never route the local daemon (and its token) through a system HTTP proxy
        self.http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False,
                                      timeout=httpx.Timeout(30.0, connect=10.0))

    async def close(self) -> None:
        await self.http.aclose()

    # --- plumbing -----------------------------------------------------------
    def _a(self, path: str) -> str:
        return f"accounts/{_id(self.account)}/{path}"

    async def _req(self, method: str, path: str, *, raw: bool = False, **kw) -> Any:
        r = await self.http.request(method, self.base + path, **kw)
        if r.status_code >= 400:
            code, retryable = "request_failed", r.status_code >= 500
            try:
                err = r.json().get("error", {})
                code = err.get("code", code)
                retryable = err.get("retryable", retryable)
            except Exception:
                pass
            raise WeChatError(r.status_code, code, retryable)
        if raw:
            return r
        return r.json() if r.content else None

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

    async def message(self, message_id: str) -> dict:
        return await self._req("GET", self._a(f"messages/{_id(message_id)}"))

    async def thread_messages(self, thread_id: str, limit: int = 20) -> list[dict]:
        data = await self._req("GET", self._a(f"threads/{_id(thread_id)}/messages"), params={"limit": limit})
        return data.get("items", [])

    async def contact(self, username: str) -> dict:
        return await self._req("GET", self._a(f"contacts/{_id(username)}"))

    async def search_contacts(self, text: str, limit: int = 20) -> list[dict]:
        data = await self._req("GET", self._a("contacts"), params={"search": text, "limit": limit})
        return data.get("items", [])

    async def search_groups(self, text: str, limit: int = 20) -> list[dict]:
        data = await self._req("GET", self._a("groups"), params={"search": text, "limit": limit})
        return data.get("items", [])

    async def group_member(self, room: str, username: str) -> dict | None:
        data = await self._req("GET", self._a(f"groups/{_id(room)}/members/{_id(username)}"))
        return data.get("member")

    async def group_members(self, room: str) -> list[dict]:
        items: list[dict] = []
        seen: set[str] = set()
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 500}
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
        intent = await self._req("POST", self._a("sends"), json=body, headers={"Idempotency-Key": key})
        return await self._wait(intent, lambda i: f"sends/{_id(i['id'])}", SEND_DONE, 120)

    async def supports_stickers(self) -> bool:
        if self._supports_stickers is not None:
            return self._supports_stickers
        try:
            caps = await self._req("GET", self._a("capabilities"))
            self._supports_stickers = bool(isinstance(caps, dict) and caps.get("stickers"))
        except (WeChatError, httpx.HTTPError):
            self._supports_stickers = False
        return self._supports_stickers

    async def prepare_sticker(self, media: Media, key: str, *, fit: bool = True) -> dict:
        files = {"file": (media.filename, media.data, media.mime)}
        source = await self._req("POST", self._a("uploads"), files=files, timeout=httpx.Timeout(600.0))
        job = await self._req("POST", self._a("sticker-preparations"),
                              json={"sourceId": source["id"], "fit": fit, "trim": None},
                              headers={"Idempotency-Key": key})
        return await self._wait(job, lambda j: f"sticker-preparations/{_id(j['id'])}", JOB_DONE, 300)

    async def send_sticker(self, thread_id: str, media: Media, key: str) -> dict:
        prep = await self.prepare_sticker(media, f"{key}-prep", fit=True)
        if prep.get("state") != "available":
            return prep
        body = {"kind": "sticker", "threadId": thread_id,
                "source": {"kind": "prepared", "preparationId": prep["id"]}}
        job = await self._req("POST", self._a("sends"), json=body, headers={"Idempotency-Key": key})
        return await self._wait(job, lambda j: f"jobs/{_id(j['id'])}", JOB_DONE, 3600)

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
            job = await self._req("POST", self._a("sends"), json=body, headers={"Idempotency-Key": key})
        except WeChatError as e:
            if kind == "image" and e.status in (400, 422) and "imageQuality" in body:
                body.pop("imageQuality", None)
                job = await self._req("POST", self._a("sends"), json=body, headers={"Idempotency-Key": f"{key}-std"})
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

    async def _wait(self, item: dict, path, done: set[str], timeout: float) -> dict:
        loop = asyncio.get_running_loop()
        deadline, delay = loop.time() + timeout, 0.3
        while item.get("state") not in done and loop.time() < deadline:
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 2.0)
            item = await self._req("GET", self._a(path(item)))
        return item

    # --- receiving media ----------------------------------------------------
    async def download_media(self, message: dict, max_bytes: int) -> Media | str:
        """Fetch the best available copy of a message attachment, or return a reason string."""
        mid, mtype = message["id"], message["type"]
        quality = "normal" if mtype in (VOICE, VIDEO) else "best"

        async def assets() -> list[dict]:
            return (await self._req("GET", self._a(f"messages/{_id(mid)}/media"))).get("items", [])

        items = await assets()
        asset = None
        last_job: dict = {}
        if mtype == IMAGE and items:
            # Prefer full-resolution `original` over cached `standard`/`thumbnail` previews:
            # `agent-wechat` may already hold a cached thumbnail/standard while `original` is `not_cached`.
            for variant in ("original", "standard", "thumbnail"):
                cands = [a for a in items if (a.get("actualVariant") or a.get("variant")) == variant]
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
                    if (a.get("id") == cand.get("id") or (a.get("actualVariant") or a.get("variant")) == variant)
                    and a.get("state") == "available" and (a.get("byteLength") or 0) <= max_bytes
                ]
                if avail:
                    asset = avail[0]
                    break
        if not asset:
            asset = _pick(items, mtype)
        if not asset:
            states = {a.get("state") for a in items}
            if states & {"expired", "unsupported", "recalled"} and not states & {"pending"}:
                reasons = [a["reason"] for a in items if a.get("reason")]
                return reasons[0] if reasons else "已过期" if "expired" in states else "不支持"
            big = [a for a in items if (a.get("advertisedByteLength") or 0) > max_bytes or (a.get("byteLength") or 0) > max_bytes]
            if items and len(big) == len(items):
                return "文件太大"
            if last_job:
                return last_job.get("reason") or last_job.get("state") or "下载失败"
            job = await self._req("POST", self._a(f"messages/{_id(mid)}/media-transfers"),
                                  json={"messageId": mid, "assetId": None, "quality": quality})
            job = await self._wait(job, lambda j: f"jobs/{_id(j['id'])}", JOB_DONE, 300)
            items = await assets()
            asset = _pick(items, mtype)
            if not asset:
                return job.get("reason") or job.get("state") or "下载失败"
        if (asset.get("byteLength") or 0) > max_bytes:
            return "文件太大"
        mime = (asset.get("contentType") or "application/octet-stream").split(";")[0].strip().lower()
        params = {}
        if mime == "image/x-wxgf" and asset.get("preview"):  # WeChat's own image format: use the JPEG/PNG preview
            params = {"representation": "preview"}
            mime = asset["preview"]["contentType"]
        r = await self._req("GET", self._a(f"media/{_id(asset['id'])}/content"), params=params, raw=True,
                            timeout=httpx.Timeout(600.0))
        name = asset.get("filename") or f"{mid[-12:]}{_ext(mime)}"
        if params:
            name = name.rsplit(".", 1)[0] + _ext(mime)
        return Media(r.content, mime, name)


def _pick(assets: list[dict], mtype: int) -> dict | None:
    order = ["normal"] if mtype == VOICE else ["normal", "original"] if mtype == VIDEO else \
        ["original", "standard", "thumbnail", "encoded", "normal"]
    ok = [a for a in assets if a.get("state") == "available"]
    ok.sort(key=lambda a: order.index(v) if (v := a.get("actualVariant") or a.get("variant")) in order else 99)
    return ok[0] if ok else None


def _ext(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
            "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            "audio/amr": ".amr", "audio/silk": ".silk", "audio/wav": ".wav"}.get(mime, ".bin")
