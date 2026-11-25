"""Lightweight memory-trimming helpers for long-running Linux containers."""
from __future__ import annotations

import ctypes
import gc
import sys
import threading
import time
from typing import Any

_TRIM_LOCK = threading.Lock()
_LAST_TRIM: float | None = None
_MALLOC_TRIM: Any = None
_LIBC_CHECKED = False


def _get_malloc_trim():
    global _MALLOC_TRIM, _LIBC_CHECKED
    if not _LIBC_CHECKED:
        _LIBC_CHECKED = True
        if sys.platform == "linux":
            try:
                libc = ctypes.CDLL("libc.so.6")
                fn = libc.malloc_trim
                fn.argtypes = [ctypes.c_size_t]
                fn.restype = ctypes.c_int
                _MALLOC_TRIM = fn
            except (OSError, AttributeError):
                _MALLOC_TRIM = None
    return _MALLOC_TRIM


def trim_memory(*, collect: bool = True) -> bool:
    """Run Python GC (optionally) and ask glibc to return free heap pages to the OS."""
    if collect:
        gc.collect()
    fn = _get_malloc_trim()
    if fn is None:
        return False
    try:
        return bool(fn(0))
    except Exception:
        return False


def maybe_trim_memory(*, min_interval: float = 15.0, collect: bool = True) -> bool:
    """Rate-limited `trim_memory()` safe to call from worker threads or the event loop."""
    global _LAST_TRIM
    now = time.monotonic()
    with _TRIM_LOCK:
        if _LAST_TRIM is not None and min_interval > 0 and now - _LAST_TRIM < min_interval:
            return False
        _LAST_TRIM = now
    return trim_memory(collect=collect)
