"""Tests for parallel Telegram media transfers (512 KiB multi-connection) and transfer timing logs."""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from telethon import errors, functions, types
from telethon.crypto import AuthKey
from telethon.tl.alltlobjects import LAYER

from bridge import core, tgtransfer, wechat
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig, load_config
from bridge.wechat import Media


def _unwrap_request(req):
    """Unwrap InvokeWithLayer -> InvokeWithoutUpdates -> InitConnection wrappers, returning (inner, meta)."""
    meta = {"layer": None, "no_updates": False, "init_conn": False}
    cur = req
    if isinstance(cur, functions.InvokeWithLayerRequest):
        meta["layer"] = cur.layer
        cur = cur.query
    if isinstance(cur, functions.InvokeWithoutUpdatesRequest):
        meta["no_updates"] = True
        cur = cur.query
    if isinstance(cur, functions.InitConnectionRequest):
        meta["init_conn"] = True
        cur = cur.query
    if isinstance(cur, functions.InvokeWithoutUpdatesRequest):
        meta["no_updates"] = True
        cur = cur.query
    return cur, meta


class FakeMTProtoSender:
    """In-memory MTProtoSender double that enforces Telegram file chunking rules and tracks concurrency."""

    def __init__(self, auth_key, *, handler, dc_id: int, is_borrowed: bool = False):
        self.auth_key = auth_key or AuthKey(b"\x11" * 256)
        self.dc_id = dc_id
        self.is_borrowed = is_borrowed
        self._handler = handler
        self.connected = False
        self.disconnect_calls = 0
        self.calls = 0
        self.first_meta: dict | None = None

    async def connect(self, connection):
        self.connection = connection
        self.connected = True
        return True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self):
        self.connected = False
        self.disconnect_calls += 1

    def send(self, request, ordered=False):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        async def _run():
            try:
                if not self.connected:
                    raise ConnectionError("sender disconnected")
                self.calls += 1
                inner, meta = _unwrap_request(request)
                if self.calls == 1:
                    self.first_meta = meta
                res = await self._handler(self, inner, meta)
                if not fut.done():
                    fut.set_result(res)
            except BaseException as exc:
                if not fut.done():
                    fut.set_exception(exc)

        loop.create_task(_run())
        return fut


class FakeTelegramClient:
    """Minimal TelegramClient double exposing the exact internal surface tgtransfer interacts with."""

    def __init__(self, *, dc_id: int = 2, flood_sleep_threshold: int = 60, handler=None,
                 messages_by_id: dict | None = None):
        self.session = SimpleNamespace(
            dc_id=dc_id,
            server_address="149.154.167.50",
            port=443,
            auth_key=AuthKey(b"\x22" * 256),
        )
        self._log = {}
        self._proxy = None
        self._local_addr = None
        self._connection = lambda ip, port, dc, **kw: (ip, port, dc, kw)
        self._init_request = functions.InitConnectionRequest(
            api_id=12345,
            device_model="TestDevice",
            system_version="Linux",
            app_version="1.0",
            system_lang_code="en",
            lang_pack="",
            lang_code="en",
            query=functions.help.GetConfigRequest(),
        )
        self.flood_sleep_threshold = flood_sleep_threshold
        self._handler = handler
        self._messages_by_id = messages_by_id or {}
        self.created_senders: list[FakeMTProtoSender] = []
        self.borrowed_senders: list[FakeMTProtoSender] = []
        self.returned_senders: list[FakeMTProtoSender] = []
        self.get_messages_calls = 0

    async def _get_dc(self, dc_id: int, cdn: bool = False):
        return SimpleNamespace(id=dc_id, ip_address=f"149.154.167.{dc_id}", port=443)

    async def _borrow_exported_sender(self, dc_id: int):
        sender = FakeMTProtoSender(AuthKey(bytes([dc_id]) * 256), handler=self._handler,
                                   dc_id=dc_id, is_borrowed=True)
        sender.connected = True
        self.borrowed_senders.append(sender)
        return sender

    async def _return_exported_sender(self, sender):
        self.returned_senders.append(sender)

    async def get_messages(self, entity, ids=None):
        self.get_messages_calls += 1
        return self._messages_by_id[ids]


def _make_doc_message(client, payload_size: int, *, dc_id: int = 2, doc_id: int = 9001,
                      file_ref: bytes = b"ref-v1", msg_id: int = 42, input_chat: object = "chat-1"):
    doc = types.Document(
        id=doc_id,
        access_hash=123456789,
        file_reference=file_ref,
        date=None,
        mime_type="application/vnd.android.package-archive",
        size=payload_size,
        dc_id=dc_id,
        attributes=[types.DocumentAttributeFilename("app.apk")],
    )
    media = types.MessageMediaDocument(document=doc)
    return SimpleNamespace(
        id=msg_id,
        client=client,
        media=media,
        input_chat=input_chat,
        file=SimpleNamespace(size=payload_size, mime_type=doc.mime_type, ext=".apk", name="app.apk"),
        photo=None,
        sticker=None,
        voice=None,
        gif=None,
        video=None,
        video_note=None,
    )


def _assert_valid_get_file_request(req: functions.upload.GetFileRequest):
    assert isinstance(req, functions.upload.GetFileRequest), f"unexpected request {type(req)}"
    assert req.limit > 0 and req.limit <= 524288 and req.limit % 4096 == 0, f"bad limit {req.limit}"
    assert 1048576 % req.limit == 0, f"limit {req.limit} does not divide 1 MiB"
    assert req.offset >= 0 and req.offset % req.limit == 0, f"offset {req.offset} not aligned to {req.limit}"
    assert req.offset // 1048576 == (req.offset + req.limit - 1) // 1048576, (
        f"chunk [{req.offset}, {req.offset + req.limit}) crosses 1 MiB boundary"
    )


class WorkerCountTests(unittest.TestCase):
    def test_worker_count_scales_with_size_and_respects_overrides(self):
        # Below threshold -> 1 (falls back to stock Telethon)
        self.assertEqual(tgtransfer.worker_count(1 * 1024 * 1024), 1)
        # Download scaling
        self.assertEqual(tgtransfer.worker_count(4 * 1024 * 1024), 4)
        self.assertEqual(tgtransfer.worker_count(12 * 1024 * 1024), 6)
        self.assertEqual(tgtransfer.worker_count(58 * 1024 * 1024), 8)
        # Upload scaling
        self.assertEqual(tgtransfer.worker_count(4 * 1024 * 1024, upload=True), 4)
        self.assertEqual(tgtransfer.worker_count(58 * 1024 * 1024, upload=True), 6)
        # Never exceeds number of 512 KiB chunks
        self.assertEqual(tgtransfer.worker_count(2 * 1024 * 1024, max_workers=12), 4)
        # Explicit disable (1) or explicit cap
        self.assertEqual(tgtransfer.worker_count(58 * 1024 * 1024, max_workers=1), 1)
        self.assertEqual(tgtransfer.worker_count(58 * 1024 * 1024, max_workers=3), 3)


class ParallelDownloadTests(unittest.IsolatedAsyncioTestCase):
    def _patch_sender_factory(self, client: FakeTelegramClient):
        def _factory(auth_key, *, loggers, **kw):
            s = FakeMTProtoSender(auth_key, handler=client._handler, dc_id=client.session.dc_id)
            client.created_senders.append(s)
            return s

        return mock.patch.object(tgtransfer, "MTProtoSender", side_effect=_factory)

    async def test_same_dc_parallel_download_reassembles_concurrently_and_preserves_init_request(self):
        # 6 chunks of 512 KiB + 12345 trailing bytes = 3,158,073 bytes (> 2 MiB threshold)
        payload = bytes((i * 31 + (i >> 9)) % 251 for i in range(6 * 512 * 1024 + 12345))
        in_flight = 0
        max_in_flight = 0
        seen_limits: set[int] = set()

        async def handler(sender, req, meta):
            nonlocal in_flight, max_in_flight
            _assert_valid_get_file_request(req)
            seen_limits.add(req.limit)
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                # Reverse-ish sleep so chunks finish out of order and overlap in flight
                await asyncio.sleep(0.03 if req.offset == 0 else 0.01)
                return types.upload.File(
                    type=types.storage.FileUnknown(),
                    mtime=0,
                    bytes=payload[req.offset:req.offset + req.limit],
                )
            finally:
                in_flight -= 1

        client = FakeTelegramClient(dc_id=2, handler=handler)
        orig_query = client._init_request.query
        msg = _make_doc_message(client, len(payload), dc_id=2)

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "app.apk")
            with self._patch_sender_factory(client):
                ok, workers = await tgtransfer.download_document_parallel(client, msg, dst)

            self.assertTrue(ok)
            self.assertEqual(workers, 4)
            self.assertGreaterEqual(max_in_flight, 2)
            self.assertEqual(seen_limits, {512 * 1024})
            self.assertEqual(Path(dst).read_bytes(), payload)

        # Must not borrow exported sender on same DC, must not mutate client._init_request, must disconnect all senders
        self.assertEqual(client.borrowed_senders, [])
        self.assertIs(client._init_request.query, orig_query)
        self.assertEqual(len(client.created_senders), 4)
        for s in client.created_senders:
            self.assertFalse(s.connected)
            self.assertEqual(s.disconnect_calls, 1)
            self.assertEqual(s.auth_key, client.session.auth_key)
            self.assertEqual(s.first_meta, {"layer": LAYER, "no_updates": True, "init_conn": True})

    async def test_cross_dc_download_borrows_one_sender_shares_auth_key_and_returns_it(self):
        payload = b"X" * (5 * 512 * 1024 + 777)

        async def handler(sender, req, meta):
            _assert_valid_get_file_request(req)
            await asyncio.sleep(0.005)
            return types.upload.File(
                type=types.storage.FileUnknown(),
                mtime=0,
                bytes=payload[req.offset:req.offset + req.limit],
            )

        client = FakeTelegramClient(dc_id=2, handler=handler)
        msg = _make_doc_message(client, len(payload), dc_id=5)

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "cross.apk")
            with self._patch_sender_factory(client):
                ok, workers = await tgtransfer.download_document_parallel(client, msg, dst)

            self.assertTrue(ok)
            self.assertEqual(workers, 4)
            self.assertEqual(Path(dst).read_bytes(), payload)

        self.assertEqual(len(client.borrowed_senders), 1)
        self.assertEqual(client.returned_senders, client.borrowed_senders)
        self.assertEqual(len(client.created_senders), 3)
        exported_key = client.borrowed_senders[0].auth_key
        for s in client.created_senders:
            self.assertEqual(s.auth_key, exported_key)
            self.assertFalse(s.connected)

    async def test_short_reads_issue_valid_aligned_followup_requests_without_data_loss(self):
        payload = bytes((i * 17 + 3) % 253 for i in range(4 * 512 * 1024 + 5000))

        async def handler(sender, req, meta):
            _assert_valid_get_file_request(req)
            # Force awkward short reads (128 KiB or 64 KiB) on the first two 512 KiB chunks
            if req.offset < 1024 * 1024:
                cap = min(req.limit, 128 * 1024 if req.offset == 0 else 64 * 1024)
            else:
                cap = req.limit
            return types.upload.File(
                type=types.storage.FileUnknown(),
                mtime=0,
                bytes=payload[req.offset:req.offset + cap],
            )

        client = FakeTelegramClient(dc_id=2, handler=handler)
        msg = _make_doc_message(client, len(payload), dc_id=2)

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "short.apk")
            with self._patch_sender_factory(client):
                ok, _ = await tgtransfer.download_document_parallel(client, msg, dst)
            self.assertTrue(ok)
            self.assertEqual(Path(dst).read_bytes(), payload)

    async def test_file_reference_expired_refreshes_once_and_completes(self):
        payload = bytes((i % 200) for i in range(5 * 512 * 1024))
        valid_ref = b"ref-v2"

        async def handler(sender, req, meta):
            _assert_valid_get_file_request(req)
            if req.location.file_reference != valid_ref:
                raise errors.FileReferenceExpiredError(request=req)
            return types.upload.File(
                type=types.storage.FileUnknown(),
                mtime=0,
                bytes=payload[req.offset:req.offset + req.limit],
            )

        client = FakeTelegramClient(dc_id=2, handler=handler)
        fresh_msg = _make_doc_message(client, len(payload), dc_id=2, file_ref=valid_ref)
        client._messages_by_id[42] = fresh_msg
        stale_msg = _make_doc_message(client, len(payload), dc_id=2, file_ref=b"ref-v1")

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "refreshed.apk")
            with self._patch_sender_factory(client):
                ok, _ = await tgtransfer.download_document_parallel(client, stale_msg, dst)
            self.assertTrue(ok)
            self.assertEqual(Path(dst).read_bytes(), payload)

        # Coalesced under lock: 4 workers hit expired ref concurrently, only 1 get_messages call needed
        self.assertEqual(client.get_messages_calls, 1)

    async def test_file_migrate_switches_dc_and_completes(self):
        payload = b"M" * (4 * 512 * 1024 + 100)

        async def handler(sender, req, meta):
            _assert_valid_get_file_request(req)
            if not sender.is_borrowed and sender.auth_key == client.session.auth_key:
                raise errors.FileMigrateError(request=req, capture=5)
            return types.upload.File(
                type=types.storage.FileUnknown(),
                mtime=0,
                bytes=payload[req.offset:req.offset + req.limit],
            )

        client = FakeTelegramClient(dc_id=2, handler=handler)
        msg = _make_doc_message(client, len(payload), dc_id=2)

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "migrated.apk")
            with self._patch_sender_factory(client):
                ok, _ = await tgtransfer.download_document_parallel(client, msg, dst)
            self.assertTrue(ok)
            self.assertEqual(Path(dst).read_bytes(), payload)

        self.assertEqual(len(client.borrowed_senders), 1)
        self.assertEqual(client.borrowed_senders[0].dc_id, 5)
        self.assertEqual(client.returned_senders, client.borrowed_senders)
        for s in client.created_senders:
            self.assertFalse(s.connected)

    async def test_cdn_redirect_falls_back_cleanly_and_removes_partial_file(self):
        payload_size = 4 * 512 * 1024

        async def handler(sender, req, meta):
            return types.upload.FileCdnRedirect(
                dc_id=203,
                file_token=b"tok",
                encryption_key=b"\x00" * 32,
                encryption_iv=b"\x00" * 16,
                file_hashes=[],
            )

        client = FakeTelegramClient(dc_id=2, handler=handler)
        msg = _make_doc_message(client, payload_size, dc_id=2)

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "cdn.apk")
            with self._patch_sender_factory(client):
                ok, workers = await tgtransfer.download_document_parallel(client, msg, dst)
            self.assertFalse(ok)
            self.assertEqual(workers, 0)
            self.assertFalse(os.path.exists(dst))

        for s in client.created_senders:
            self.assertFalse(s.connected)

    async def test_transient_timeout_and_dead_worker_requeue_succeeds(self):
        payload = bytes((i * 13) % 251 for i in range(6 * 512 * 1024 + 999))
        timed_out_once = False
        killed_sender = None

        async def handler(sender, req, meta):
            nonlocal timed_out_once, killed_sender
            _assert_valid_get_file_request(req)
            if not timed_out_once and req.offset == 0:
                timed_out_once = True
                raise errors.TimedOutError(request=req, message="Timeout")
            if killed_sender is None and req.offset == 512 * 1024:
                killed_sender = sender
                sender.connected = False
                raise ConnectionError("socket reset")
            return types.upload.File(
                type=types.storage.FileUnknown(),
                mtime=0,
                bytes=payload[req.offset:req.offset + req.limit],
            )

        client = FakeTelegramClient(dc_id=2, handler=handler)
        msg = _make_doc_message(client, len(payload), dc_id=2)

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "retry.apk")
            with self._patch_sender_factory(client), mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock):
                ok, _ = await tgtransfer.download_document_parallel(client, msg, dst)
            self.assertTrue(ok)
            self.assertEqual(Path(dst).read_bytes(), payload)


class ParallelUploadTests(unittest.IsolatedAsyncioTestCase):
    def _patch_sender_factory(self, client: FakeTelegramClient):
        def _factory(auth_key, *, loggers, **kw):
            s = FakeMTProtoSender(auth_key, handler=client._handler, dc_id=client.session.dc_id)
            client.created_senders.append(s)
            return s

        return mock.patch.object(tgtransfer, "MTProtoSender", side_effect=_factory)

    async def test_big_file_parallel_upload_uses_save_big_file_part_concurrently(self):
        payload = bytes((i * 29 + 7) % 251 for i in range(5 * 512 * 1024 + 3210))
        parts: dict[int, bytes] = {}
        seen_file_ids: set[int] = set()
        seen_totals: set[int] = set()
        in_flight = 0
        max_in_flight = 0

        async def handler(sender, req, meta):
            nonlocal in_flight, max_in_flight
            assert isinstance(req, functions.upload.SaveBigFilePartRequest)
            seen_file_ids.add(req.file_id)
            seen_totals.add(req.file_total_parts)
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02 if req.file_part == 0 else 0.005)
                parts[req.file_part] = req.bytes
                return True
            finally:
                in_flight -= 1

        client = FakeTelegramClient(dc_id=2, handler=handler)

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "release.apk")
            Path(src).write_bytes(payload)
            # Lower BIG_FILE_THRESHOLD in test so 2.5 MiB exercises InputFileBig without allocating 11 MiB
            with self._patch_sender_factory(client), mock.patch.object(tgtransfer, "BIG_FILE_THRESHOLD", 1024 * 1024):
                handle, workers = await tgtransfer.upload_file_parallel(client, src, "release.apk", len(payload))

        self.assertIsInstance(handle, types.InputFileBig)
        self.assertEqual(handle.parts, 6)
        self.assertEqual(handle.name, "release.apk")
        self.assertEqual(workers, 4)
        self.assertGreaterEqual(max_in_flight, 2)
        self.assertEqual(seen_file_ids, {handle.id})
        self.assertEqual(seen_totals, {6})
        reassembled = b"".join(parts[i] for i in range(6))
        self.assertEqual(reassembled, payload)
        for s in client.created_senders:
            self.assertFalse(s.connected)

    async def test_medium_file_parallel_upload_uses_save_file_part_with_exact_md5(self):
        payload = bytes((i * 19 + 5) % 249 for i in range(4 * 512 * 1024 + 4321))
        parts: dict[int, bytes] = {}

        async def handler(sender, req, meta):
            assert isinstance(req, functions.upload.SaveFilePartRequest)
            await asyncio.sleep(0.005)
            parts[req.file_part] = req.bytes
            return True

        client = FakeTelegramClient(dc_id=2, handler=handler)

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "doc.pdf")
            Path(src).write_bytes(payload)
            with self._patch_sender_factory(client):
                handle, workers = await tgtransfer.upload_file_parallel(client, src, "doc.pdf", len(payload))

        self.assertIsInstance(handle, types.InputFile)
        self.assertEqual(handle.parts, 5)
        self.assertEqual(handle.name, "doc.pdf")
        self.assertEqual(handle.md5_checksum, hashlib.md5(payload).hexdigest())
        self.assertEqual(workers, 4)
        self.assertEqual(b"".join(parts[i] for i in range(5)), payload)
        for s in client.created_senders:
            self.assertFalse(s.connected)

    async def test_upload_unrecoverable_error_returns_none_and_disconnects_senders(self):
        payload = b"Z" * (4 * 512 * 1024)

        async def handler(sender, req, meta):
            return False  # Telegram rejected part

        client = FakeTelegramClient(dc_id=2, handler=handler)

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "bad.bin")
            Path(src).write_bytes(payload)
            with self._patch_sender_factory(client), mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock):
                handle, workers = await tgtransfer.upload_file_parallel(client, src, "bad.bin", len(payload))

        self.assertIsNone(handle)
        self.assertEqual(workers, 0)
        for s in client.created_senders:
            self.assertFalse(s.connected)

    async def test_upload_requeues_part_when_worker_disconnects_and_preserves_md5(self):
        payload = bytes((i * 23 + 11) % 251 for i in range(5 * 512 * 1024 + 222))
        parts: dict[int, bytes] = {}
        killed_sender = None

        async def handler(sender, req, meta):
            nonlocal killed_sender
            assert isinstance(req, functions.upload.SaveFilePartRequest)
            if killed_sender is None and req.file_part == 1:
                killed_sender = sender
                sender.connected = False
                raise ConnectionError("upload socket reset")
            parts[req.file_part] = req.bytes
            return True

        client = FakeTelegramClient(dc_id=2, handler=handler)

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "requeue.bin")
            Path(src).write_bytes(payload)
            with self._patch_sender_factory(client), mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock):
                handle, _ = await tgtransfer.upload_file_parallel(client, src, "requeue.bin", len(payload))

        self.assertIsInstance(handle, types.InputFile)
        self.assertEqual(handle.md5_checksum, hashlib.md5(payload).hexdigest())
        self.assertEqual(b"".join(parts[i] for i in range(6)), payload)


class TransferConfigTests(unittest.TestCase):
    def test_tg_transfer_workers_env_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(
                Path(tmp) / "missing.toml",
                environ={
                    "TG_API_ID": "1",
                    "TG_API_HASH": "h",
                    "TG_BOT_TOKEN": "1:a",
                    "WECHAT_TOKEN": "tok",
                    "TG_TRANSFER_WORKERS": "6",
                },
            )
            self.assertEqual(cfg.bridge.tg_transfer_workers, 6)

            with self.assertRaises(ValueError):
                BridgeConfig(tg_transfer_workers=-1)
            with self.assertRaises(ValueError):
                BridgeConfig(tg_transfer_workers=17)
            with self.assertRaises(ValueError):
                BridgeConfig(tg_transfer_workers=True)  # type: ignore[arg-type]


class BridgeIntegrationAndTimingLogTests(unittest.IsolatedAsyncioTestCase):
    def _make_bridge(self, tmp: str) -> core.Bridge:
        cfg = Config(
            WeChatConfig(token="tok"),
            TelegramConfig(api_id=1, api_hash="h", bot_token="1:a"),
            BridgeConfig(mode="topics"),
            Path(tmp) / "config.toml",
        )
        return core.Bridge(cfg)

    async def test_tg_media_uses_parallel_download_and_logs_timing_for_large_files(self):
        payload = b"A" * 256

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            b = self._make_bridge(tmp)
            try:
                stock_called = False

                async def stock_download(file=None):
                    nonlocal stock_called
                    stock_called = True
                    return file

                async def fake_parallel(client, msg, dst, *, max_workers=0):
                    Path(dst).write_bytes(payload)
                    return True, 6

                msg = SimpleNamespace(
                    client=b.bot,
                    file=SimpleNamespace(size=len(payload), mime_type="application/vnd.android.package-archive",
                                         ext=".apk", name="demo.apk"),
                    photo=None, sticker=None, voice=None, gif=None, video=None, video_note=None,
                    download_media=stock_download,
                )

                with mock.patch("bridge.wechat.MEDIA_SPILL_BYTES", 64), \
                     mock.patch("bridge.tgtransfer.LOG_MEDIA_MIN_BYTES", 64), \
                     mock.patch("bridge.tgtransfer.download_document_parallel", side_effect=fake_parallel), \
                     self.assertLogs("bridge", level="INFO") as logs:
                    kind, media = await b._tg_media(msg)

                self.assertFalse(stock_called)
                self.assertEqual(kind, "file")
                self.assertIsInstance(media, Media)
                self.assertEqual(media.read(), payload)
                media.close()
                self.assertTrue(any("demo.apk" in line and "6x512KiB" in line for line in logs.output), logs.output)
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()

    async def test_wechat_send_media_logs_local_upload_and_cdn_send_breakdown(self):
        payload = b"B" * 256

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/uploads"):
                return httpx.Response(200, json={"id": "src-1"})
            if request.url.path.endswith("/sends"):
                return httpx.Response(200, json={"id": "job-1", "state": "completed"})
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        wx = wechat.WeChatClient("http://127.0.0.1:6174", "tok", "acc1")
        try:
            await wx.http.aclose()
            wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            media = Media(payload, "application/vnd.android.package-archive", "demo.apk")
            with mock.patch("bridge.wechat.LOG_MEDIA_MIN_BYTES", 64), \
                 self.assertLogs("wechat", level="INFO") as logs:
                res = await wx.send_media("wxid_bob", "file", media, "k-log")
            self.assertEqual(res["state"], "completed")
            self.assertTrue(
                any("demo.apk" in line and "local_upload=" in line and "wx_send=" in line for line in logs.output),
                logs.output,
            )
        finally:
            await wx.close()

    async def test_send_tg_preuploads_large_document_and_reuses_handle_on_fallback(self):
        payload = b"C" * 256
        uploaded_handle = types.InputFileBig(id=777, parts=2, name="demo.apk")
        seen_files: list[object] = []
        calls = 0

        async def fake_send_file(chat_id, file, **kw):
            nonlocal calls
            calls += 1
            seen_files.append(file)
            if calls == 1:
                raise errors.RPCError(request=None, message="MEDIA_INVALID")
            return SimpleNamespace(id=101, message="", media=None)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            b = self._make_bridge(tmp)
            try:
                media_dir = Path(tmp) / "m"
                media_dir.mkdir()
                media_path = media_dir / "demo.mp4"
                media_path.write_bytes(payload)
                media = Media(b"", "video/mp4", "demo.mp4", path=str(media_path))

                b.bot.send_file = fake_send_file
                with mock.patch("bridge.tgtransfer.upload_file_parallel",
                                return_value=(uploaded_handle, 4)) as mock_up, \
                     mock.patch("bridge.stickers.extract_video_meta_and_thumb", return_value=None):
                    out = await b._send_tg(
                        -1001, {"id": "wxid_bob"}, "", "",
                        {"id": "m1", "type": wechat.VIDEO}, media, None, False,
                    )

                self.assertEqual(len(out), 1)
                self.assertEqual(mock_up.call_count, 1)
                # Both the initial attempt and the force_document fallback reuse the already-uploaded InputFileBig!
                self.assertEqual(seen_files, [uploaded_handle, uploaded_handle])
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()

    async def test_tg_media_falls_back_to_stock_download_when_parallel_returns_false(self):
        payload = b"F" * 256

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            b = self._make_bridge(tmp)
            try:
                stock_called = False

                async def stock_download(file=None):
                    nonlocal stock_called
                    stock_called = True
                    Path(file).write_bytes(payload)
                    return file

                msg = SimpleNamespace(
                    client=b.bot,
                    file=SimpleNamespace(size=len(payload), mime_type="application/octet-stream",
                                         ext=".bin", name="fallback.bin"),
                    photo=None, sticker=None, voice=None, gif=None, video=None, video_note=None,
                    download_media=stock_download,
                )

                with mock.patch("bridge.wechat.MEDIA_SPILL_BYTES", 64), \
                     mock.patch("bridge.tgtransfer.LOG_MEDIA_MIN_BYTES", 64), \
                     mock.patch("bridge.tgtransfer.download_document_parallel", return_value=(False, 0)), \
                     self.assertLogs("bridge", level="INFO") as logs:
                    kind, media = await b._tg_media(msg)

                self.assertTrue(stock_called)
                self.assertEqual(kind, "file")
                self.assertEqual(media.read(), payload)
                media.close()
                self.assertTrue(any("fallback.bin" in line and "stock" in line for line in logs.output), logs.output)
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()

    async def test_send_tg_skips_parallel_upload_for_photos_so_telethon_can_resize(self):
        payload = b"\xff\xd8\xff\xe0" + b"P" * 256
        seen_files: list[object] = []

        async def fake_send_file(chat_id, file, **kw):
            seen_files.append(file)
            return SimpleNamespace(id=102, message="", media=None)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            b = self._make_bridge(tmp)
            try:
                media_dir = Path(tmp) / "p"
                media_dir.mkdir()
                media_path = media_dir / "big.jpg"
                media_path.write_bytes(payload)
                media = Media(b"", "image/jpeg", "big.jpg", path=str(media_path))

                b.bot.send_file = fake_send_file
                with mock.patch("bridge.tgtransfer.upload_file_parallel") as mock_up:
                    out = await b._send_tg(
                        -1001, {"id": "wxid_bob"}, "", "",
                        {"id": "m2", "type": wechat.IMAGE}, media, None, False,
                    )

                self.assertEqual(len(out), 1)
                self.assertEqual(mock_up.call_count, 0)
                self.assertEqual(seen_files, [str(media_path)])
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()


if __name__ == "__main__":
    unittest.main()
