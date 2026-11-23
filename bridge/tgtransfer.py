"""Parallel 512 KiB multi-connection Telegram MTProto file download and upload.

Telethon's built-in `download_media` and `upload_file` default to 128 KiB chunks (for files <= 100 MiB)
sent strictly one-at-a-time over a single MTProto connection. On high-RTT routes (e.g. overseas VPS to
DC5 Singapore) or per-connection Telegram throttling, a 50-60 MiB file can take several minutes.

This module uses Telegram's maximum 512 KiB chunk size across multiple temporary MTProtoSender
connections (reusing the home DC auth key or a single borrowed exported DC auth key), keeps RAM
bounded to `workers * 512 KiB`, disconnects temporary senders immediately after the transfer, and
cleanly falls back to Telethon's stock path on any unsupported or unrecoverable condition.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import os
from collections import deque
from dataclasses import dataclass

from telethon import errors, functions, helpers, types
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.alltlobjects import LAYER

log = logging.getLogger("bridge")

MIN_CHUNK_SIZE = 4096
CHUNK_SIZE = 512 * 1024  # Telegram MTProto max chunk size (divides 1 MiB evenly)
PARALLEL_MIN_BYTES = 2 * 1024 * 1024
BIG_FILE_THRESHOLD = 10 * 1024 * 1024
LOG_MEDIA_MIN_BYTES = 1 * 1024 * 1024
MAX_WORKERS_LIMIT = 16
_MAX_CHUNK_RETRIES = 4
_CHUNK_TIMEOUT = 45.0

_RETRYABLE_RPC_ERRORS = (
    errors.TimedOutError,
    errors.ServerError,
    errors.RpcCallFailError,
    errors.RpcMcgetFailError,
    errors.InterdcCallErrorError,
    errors.InterdcCallRichErrorError,
    errors.InvalidBufferError,
    asyncio.IncompleteReadError,
    asyncio.TimeoutError,
)
_FLOOD_ERRORS = (errors.FloodWaitError, errors.FloodPremiumWaitError)


class _Fallback(Exception):
    """Abort parallel transfer and let stock Telethon handle the file."""


class _MigrateDC(Exception):
    def __init__(self, new_dc: int):
        super().__init__(f"migrate to DC {new_dc}")
        self.new_dc = new_dc


@dataclass
class _SenderSlot:
    sender: object
    needs_init: bool
    is_borrowed: bool


def format_mib(size_bytes: int) -> str:
    return f"{size_bytes / 1048576:.1f} MiB"


def format_rate(size_bytes: int, elapsed: float) -> str:
    return f"{(size_bytes / 1048576) / max(elapsed, 0.001):.2f} MiB/s"


def worker_count(file_size: int, *, upload: bool = False, max_workers: int = 0) -> int:
    """Choose parallel MTProto connection count for a file size (1 = use stock Telethon)."""
    if file_size < PARALLEL_MIN_BYTES:
        return 1
    if max_workers == 1:
        return 1
    chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    if chunks < 2:
        return 1
    if max_workers > 1:
        target = min(max_workers, MAX_WORKERS_LIMIT)
    elif upload:
        target = 6 if file_size >= 16 * 1024 * 1024 else 4
    else:
        if file_size >= 24 * 1024 * 1024:
            target = 8
        elif file_size >= 8 * 1024 * 1024:
            target = 6
        else:
            target = 4
    return max(1, min(target, chunks))


def _supports_parallel(client) -> bool:
    if client is None:
        return False
    session = getattr(client, "session", None)
    auth_key = getattr(session, "auth_key", None)
    return bool(
        session is not None
        and auth_key
        and getattr(session, "dc_id", None)
        and getattr(session, "server_address", None)
        and getattr(session, "port", None)
        and callable(getattr(client, "_connection", None))
        and callable(getattr(client, "_get_dc", None))
        and callable(getattr(client, "_borrow_exported_sender", None))
        and callable(getattr(client, "_return_exported_sender", None))
        and getattr(client, "_init_request", None) is not None
    )


def _clone_init_request(client, query):
    init = copy.copy(client._init_request)
    init.query = query
    return init


async def _connect_extra_sender(client, ip: str, port: int, dc_id: int, auth_key):
    sender = MTProtoSender(
        auth_key, loggers=client._log, retries=2, delay=1, auto_reconnect=False, connect_timeout=10,
    )
    try:
        conn = client._connection(
            ip,
            port,
            dc_id,
            loggers=client._log,
            proxy=getattr(client, "_proxy", None),
            local_addr=getattr(client, "_local_addr", None),
        )
        await asyncio.wait_for(sender.connect(conn), timeout=20.0)
        return sender
    except Exception:
        try:
            await sender.disconnect()
        except Exception:
            pass
        raise


async def _open_senders(client, dc_id: int, count: int) -> tuple[list[_SenderSlot], object | None]:
    """Open `count` MTProtoSender connections to `dc_id` without mutating client state."""
    same_dc = dc_id == client.session.dc_id
    borrowed = None
    slots: list[_SenderSlot] = []
    try:
        if same_dc:
            ip, port = client.session.server_address, client.session.port
            auth_key = client.session.auth_key
            extra_count = count
        else:
            borrowed = await client._borrow_exported_sender(dc_id)
            dc = await client._get_dc(dc_id)
            ip, port = dc.ip_address, dc.port
            auth_key = borrowed.auth_key
            slots.append(_SenderSlot(borrowed, needs_init=False, is_borrowed=True))
            extra_count = max(0, count - 1)

        if extra_count > 0:
            results = await asyncio.gather(
                *(_connect_extra_sender(client, ip, port, dc_id, auth_key) for _ in range(extra_count)),
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, BaseException):
                    if isinstance(res, asyncio.CancelledError):
                        raise res
                    log.debug("Parallel Telegram worker connection to DC %s failed: %s", dc_id, res)
                    continue
                slots.append(_SenderSlot(res, needs_init=True, is_borrowed=False))

        if len(slots) < 2:
            await _close_senders(client, slots, borrowed)
            return [], None
        return slots, borrowed
    except BaseException:
        await _close_senders(client, slots, borrowed)
        raise


async def _close_senders(client, slots: list[_SenderSlot], borrowed: object | None) -> None:
    extras = [s.sender for s in slots if not s.is_borrowed]
    if extras:
        await asyncio.gather(*(s.disconnect() for s in extras), return_exceptions=True)
    if borrowed is not None:
        try:
            await client._return_exported_sender(borrowed)
        except Exception:
            pass


async def _call_slot(slot: _SenderSlot, client, req):
    if slot.needs_init:
        wrapped = functions.InvokeWithLayerRequest(
            LAYER,
            functions.InvokeWithoutUpdatesRequest(_clone_init_request(client, req)),
        )
        res = await asyncio.wait_for(slot.sender.send(wrapped), timeout=_CHUNK_TIMEOUT)
        slot.needs_init = False
        return res
    if not slot.is_borrowed:
        return await asyncio.wait_for(
            slot.sender.send(functions.InvokeWithoutUpdatesRequest(req)),
            timeout=_CHUNK_TIMEOUT,
        )
    return await asyncio.wait_for(slot.sender.send(req), timeout=_CHUNK_TIMEOUT)


def _aligned_sub_limit(offset: int, max_limit: int = CHUNK_SIZE) -> int:
    """Largest Telegram-legal GetFile limit (<= max_limit, dividing 1 MiB and `offset`)."""
    if offset % MIN_CHUNK_SIZE != 0:
        return 0
    if offset == 0:
        return max_limit
    # Largest power of two dividing `offset`, capped at max_limit (which is 512 KiB = 2^19).
    step = offset & -offset
    limit = min(max_limit, step)
    if limit < MIN_CHUNK_SIZE or (1048576 % limit) != 0:
        return 0
    return limit


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def download_document_parallel(
    client,
    msg,
    dst: str,
    *,
    max_workers: int = 0,
) -> tuple[bool, int]:
    """Download a Telegram document message to `dst` in parallel 512 KiB chunks.

    Returns `(True, workers_used)` on verified completion, or `(False, 0)` when the caller should
    fall back to Telethon's stock `msg.download_media(file=dst)`.
    """
    if not _supports_parallel(client):
        return False, 0
    media = getattr(msg, "media", None)
    if not isinstance(media, types.MessageMediaDocument):
        return False, 0
    doc = getattr(media, "document", None)
    if not isinstance(doc, types.Document) or not doc.size or not doc.dc_id:
        return False, 0
    file_size = int(doc.size)
    desired_workers = worker_count(file_size, upload=False, max_workers=max_workers)
    if desired_workers < 2:
        return False, 0

    location = types.InputDocumentFileLocation(
        id=doc.id,
        access_hash=doc.access_hash,
        file_reference=doc.file_reference,
        thumb_size="",
    )
    target_dc = int(doc.dc_id)
    flood_cap = getattr(client, "flood_sleep_threshold", 60)

    for _migration_attempt in range(2):
        slots: list[_SenderSlot] = []
        borrowed = None
        try:
            slots, borrowed = await _open_senders(client, target_dc, desired_workers)
            if not slots:
                return False, 0

            workers_used = len(slots)
            pending: deque[tuple[int, int, int]] = deque(
                (off, min(CHUNK_SIZE, file_size - off), 0)
                for off in range(0, file_size, CHUNK_SIZE)
            )
            cond = asyncio.Condition()
            in_flight = 0
            active_workers = workers_used
            bytes_written = 0
            fatal: BaseException | None = None
            ref_lock = asyncio.Lock()
            ref_version = 0

            async def _refresh_file_ref(seen_version: int) -> None:
                nonlocal ref_version
                input_chat = getattr(msg, "input_chat", None)
                msg_id = getattr(msg, "id", None)
                if not input_chat or msg_id is None:
                    raise _Fallback("missing_msg_data_for_ref_refresh")
                async with ref_lock:
                    if ref_version != seen_version:
                        return
                    refreshed = await client.get_messages(input_chat, ids=msg_id)
                    rmedia = getattr(refreshed, "media", None)
                    if not isinstance(rmedia, types.MessageMediaDocument):
                        raise _Fallback("refreshed_media_changed")
                    rdoc = getattr(rmedia, "document", None)
                    if not isinstance(rdoc, types.Document) or rdoc.id != doc.id:
                        raise _Fallback("refreshed_doc_changed")
                    location.file_reference = rdoc.file_reference
                    ref_version += 1

            async def _fetch_range(slot: _SenderSlot, start: int, expected: int) -> bytes:
                out = bytearray()
                cur = start
                remaining = expected
                while remaining > 0:
                    req_limit = _aligned_sub_limit(cur, CHUNK_SIZE)
                    if req_limit == 0:
                        raise _Fallback(f"unaligned_short_read:{cur}")
                    req = functions.upload.GetFileRequest(location=location, offset=cur, limit=req_limit)
                    res = await _call_slot(slot, client, req)
                    if isinstance(res, types.upload.FileCdnRedirect):
                        raise _Fallback("cdn_redirect")
                    chunk = getattr(res, "bytes", None)
                    if not chunk:
                        raise _Fallback(f"unexpected_eof:{cur}")
                    if len(chunk) > remaining:
                        raise _Fallback(f"oversized_chunk:{cur}:{len(chunk)}>{remaining}")
                    out.extend(chunk)
                    cur += len(chunk)
                    remaining -= len(chunk)
                return bytes(out)

            with open(dst, "wb") as fp:
                async def _worker(slot: _SenderSlot) -> None:
                    nonlocal in_flight, active_workers, bytes_written, fatal
                    try:
                        while True:
                            async with cond:
                                while not pending and in_flight > 0 and fatal is None:
                                    await cond.wait()
                                if fatal is not None or not pending:
                                    return
                                offset, expected, attempts = pending.popleft()
                                in_flight += 1

                            done = False
                            requeue_attempts: int | None = None
                            worker_dead = False
                            try:
                                while True:
                                    seen_ref = ref_version
                                    try:
                                        data = await _fetch_range(slot, offset, expected)
                                        if fatal is None:
                                            fp.seek(offset)
                                            fp.write(data)
                                            bytes_written += len(data)
                                        done = True
                                        break
                                    except (_Fallback, _MigrateDC) as exc:
                                        async with cond:
                                            if fatal is None:
                                                fatal = exc
                                            cond.notify_all()
                                        return
                                    except errors.FileMigrateError as exc:
                                        async with cond:
                                            if fatal is None:
                                                fatal = _MigrateDC(exc.new_dc)
                                            cond.notify_all()
                                        return
                                    except (errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError):
                                        if attempts >= _MAX_CHUNK_RETRIES:
                                            async with cond:
                                                if fatal is None:
                                                    fatal = _Fallback("file_ref_expired")
                                                cond.notify_all()
                                            return
                                        attempts += 1
                                        try:
                                            await _refresh_file_ref(seen_ref)
                                        except BaseException as exc:
                                            if isinstance(exc, asyncio.CancelledError):
                                                raise
                                            async with cond:
                                                if fatal is None:
                                                    fatal = _Fallback(f"ref_refresh_failed:{exc}")
                                                cond.notify_all()
                                            return
                                    except _FLOOD_ERRORS as exc:
                                        if exc.seconds > flood_cap or attempts >= _MAX_CHUNK_RETRIES:
                                            async with cond:
                                                if fatal is None:
                                                    fatal = _Fallback(f"flood_wait:{exc.seconds}")
                                                cond.notify_all()
                                            return
                                        attempts += 1
                                        await asyncio.sleep(max(1, exc.seconds))
                                    except (*_RETRYABLE_RPC_ERRORS, OSError, ConnectionError) as exc:
                                        attempts += 1
                                        if attempts > _MAX_CHUNK_RETRIES:
                                            async with cond:
                                                if fatal is None:
                                                    fatal = _Fallback(f"chunk_retries_exhausted:{type(exc).__name__}")
                                                cond.notify_all()
                                            return
                                        if not slot.sender.is_connected():
                                            requeue_attempts = attempts
                                            worker_dead = True
                                            break
                                        await asyncio.sleep(min(2.0, 0.5 * attempts))
                                    except BaseException as exc:
                                        if isinstance(exc, asyncio.CancelledError):
                                            raise
                                        async with cond:
                                            if fatal is None:
                                                fatal = _Fallback(f"unexpected:{type(exc).__name__}:{exc}")
                                            cond.notify_all()
                                        return
                            finally:
                                async with cond:
                                    in_flight -= 1
                                    if requeue_attempts is not None:
                                        pending.append((offset, expected, requeue_attempts))
                                    if worker_dead:
                                        active_workers -= 1
                                        if active_workers == 0 and (pending or in_flight > 0) and fatal is None:
                                            fatal = _Fallback("all_workers_disconnected")
                                    if fatal is not None or (not pending and in_flight == 0) or requeue_attempts is not None:
                                        cond.notify_all()
                            if worker_dead:
                                return
                    except asyncio.CancelledError:
                        raise

                tasks = [asyncio.create_task(_worker(slot)) for slot in slots]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                fp.flush()

            if isinstance(fatal, _MigrateDC):
                _remove_quietly(dst)
                target_dc = fatal.new_dc
                continue
            if fatal is not None:
                log.info("Parallel Telegram download fell back to stock downloader (%s)", fatal)
                _remove_quietly(dst)
                return False, 0
            if bytes_written != file_size or not os.path.isfile(dst) or os.path.getsize(dst) != file_size:
                log.info("Parallel Telegram download size mismatch (%d vs %d); falling back", bytes_written, file_size)
                _remove_quietly(dst)
                return False, 0
            return True, workers_used
        except asyncio.CancelledError:
            _remove_quietly(dst)
            raise
        except Exception as exc:
            log.info("Parallel Telegram download failed (%s); falling back to stock downloader", exc)
            _remove_quietly(dst)
            return False, 0
        finally:
            await _close_senders(client, slots, borrowed)

    _remove_quietly(dst)
    return False, 0


async def upload_file_parallel(
    client,
    path: str,
    file_name: str,
    file_size: int,
    *,
    max_workers: int = 0,
) -> tuple[types.InputFile | types.InputFileBig | None, int]:
    """Upload a local file to Telegram in parallel 512 KiB chunks.

    Returns `(InputFile | InputFileBig, workers_used)` on success, or `(None, 0)` when the caller
    should fall back to Telethon's stock `send_file`/`upload_file`.
    """
    if not _supports_parallel(client) or not path or file_size <= 0:
        return None, 0
    desired_workers = worker_count(file_size, upload=True, max_workers=max_workers)
    if desired_workers < 2:
        return None, 0

    dc_id = int(client.session.dc_id)
    part_count = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    is_big = file_size > BIG_FILE_THRESHOLD
    file_id = helpers.generate_random_long()
    flood_cap = getattr(client, "flood_sleep_threshold", 60)

    slots: list[_SenderSlot] = []
    borrowed = None
    try:
        slots, borrowed = await _open_senders(client, dc_id, desired_workers)
        if not slots:
            return None, 0

        workers_used = len(slots)
        retry_queue: deque[tuple[int, bytes, int]] = deque()
        next_part = 0
        in_flight = 0
        uploaded_parts = 0
        active_workers = workers_used
        fatal: BaseException | None = None
        cond = asyncio.Condition()
        hash_md5 = None if is_big else hashlib.md5()

        with open(path, "rb") as fp:
            async def _worker(slot: _SenderSlot) -> None:
                nonlocal next_part, in_flight, uploaded_parts, active_workers, fatal
                try:
                    while True:
                        async with cond:
                            while not retry_queue and next_part >= part_count and in_flight > 0 and fatal is None:
                                await cond.wait()
                            if fatal is not None:
                                return
                            if retry_queue:
                                part_idx, data, attempts = retry_queue.popleft()
                            elif next_part < part_count:
                                part_idx = next_part
                                next_part += 1
                                data = fp.read(CHUNK_SIZE)
                                if not data or (part_idx < part_count - 1 and len(data) != CHUNK_SIZE):
                                    fatal = _Fallback(f"unexpected_read_size:{part_idx}:{len(data)}")
                                    cond.notify_all()
                                    return
                                if hash_md5 is not None:
                                    hash_md5.update(data)
                                attempts = 0
                            else:
                                return
                            in_flight += 1

                        requeue_attempts: int | None = None
                        worker_dead = False
                        succeeded = False
                        try:
                            req = (
                                functions.upload.SaveBigFilePartRequest(file_id, part_idx, part_count, data)
                                if is_big
                                else functions.upload.SaveFilePartRequest(file_id, part_idx, data)
                            )
                            while True:
                                try:
                                    ok = await _call_slot(slot, client, req)
                                    if not ok:
                                        raise _Fallback(f"part_rejected:{part_idx}")
                                    succeeded = True
                                    break
                                except _Fallback as exc:
                                    async with cond:
                                        if fatal is None:
                                            fatal = exc
                                        cond.notify_all()
                                    return
                                except _FLOOD_ERRORS as exc:
                                    if exc.seconds > flood_cap or attempts >= _MAX_CHUNK_RETRIES:
                                        async with cond:
                                            if fatal is None:
                                                fatal = _Fallback(f"flood_wait:{exc.seconds}")
                                            cond.notify_all()
                                        return
                                    attempts += 1
                                    await asyncio.sleep(max(1, exc.seconds))
                                except (*_RETRYABLE_RPC_ERRORS, OSError, ConnectionError) as exc:
                                    attempts += 1
                                    if attempts > _MAX_CHUNK_RETRIES:
                                        async with cond:
                                            if fatal is None:
                                                fatal = _Fallback(f"part_retries_exhausted:{type(exc).__name__}")
                                            cond.notify_all()
                                        return
                                    if not slot.sender.is_connected():
                                        requeue_attempts = attempts
                                        worker_dead = True
                                        break
                                    await asyncio.sleep(min(2.0, 0.5 * attempts))
                                except BaseException as exc:
                                    if isinstance(exc, asyncio.CancelledError):
                                        raise
                                    async with cond:
                                        if fatal is None:
                                            fatal = _Fallback(f"unexpected:{type(exc).__name__}:{exc}")
                                        cond.notify_all()
                                    return
                        finally:
                            async with cond:
                                in_flight -= 1
                                if succeeded:
                                    uploaded_parts += 1
                                if requeue_attempts is not None:
                                    retry_queue.append((part_idx, data, requeue_attempts))
                                if worker_dead:
                                    active_workers -= 1
                                    if active_workers == 0 and (retry_queue or next_part < part_count or in_flight > 0):
                                        if fatal is None:
                                            fatal = _Fallback("all_workers_disconnected")
                                if (
                                    fatal is not None
                                    or requeue_attempts is not None
                                    or (not retry_queue and next_part >= part_count and in_flight == 0)
                                ):
                                    cond.notify_all()
                        if worker_dead:
                            return
                except asyncio.CancelledError:
                    raise

            tasks = [asyncio.create_task(_worker(slot)) for slot in slots]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        if fatal is not None or uploaded_parts != part_count:
            log.info("Parallel Telegram upload fell back to stock uploader (%s)", fatal or "incomplete")
            return None, 0

        if is_big:
            return types.InputFileBig(file_id, part_count, file_name), workers_used
        assert hash_md5 is not None
        return types.InputFile(file_id, part_count, file_name, hash_md5.hexdigest()), workers_used
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("Parallel Telegram upload failed (%s); falling back to stock uploader", exc)
        return None, 0
    finally:
        await _close_senders(client, slots, borrowed)
