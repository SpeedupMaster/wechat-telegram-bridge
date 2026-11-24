"""Static HTML/JS web assets for Mini App call and reader pages."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_WEB_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_text(name: str) -> str:
    """Read a UTF-8 static asset from bridge/web/ and cache it in memory."""
    return (_WEB_DIR / name).read_text(encoding="utf-8")
