"""Settings: environment variables (.env in Docker) plus an optional config.toml for advanced options."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WeChatConfig:
    base_url: str = "http://127.0.0.1:6174"
    token: str = ""
    token_file: str = ""  # file holding the token (used by Docker Compose); `token` wins when both are set
    account: str = ""  # empty = use the only account; created automatically when there is none
    consumer: str = "telegram-bridge"

    def read_token(self) -> str:
        if self.token:
            return self.token
        if self.token_file:
            return Path(self.token_file).read_text(encoding="utf-8").strip()
        return ""


@dataclass
class TelegramConfig:
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    proxy: str = ""  # e.g. socks5://127.0.0.1:7890 or http://127.0.0.1:7890


@dataclass
class BridgeConfig:
    data_dir: str = "data"
    title_direct: str = "{name}"
    title_group: str = "👥 {name}"
    bridge_groups: bool = True
    bridge_official_accounts: bool = False
    forward_outgoing: bool = True
    outgoing_silent: bool = True
    sync_avatar: bool = True
    mark_read_on_reply: bool = True
    create_interval_seconds: float = 20.0
    max_media_mb: int = 200
    skip_threads: list[str] = field(default_factory=list)


@dataclass
class Config:
    wechat: WeChatConfig
    telegram: TelegramConfig
    bridge: BridgeConfig
    path: Path

    @property
    def data_dir(self) -> Path:
        p = Path(self.bridge.data_dir)
        if not p.is_absolute():
            p = self.path.parent / p
        p.mkdir(parents=True, exist_ok=True)
        return p


# environment variable -> (section, key)
ENV = {
    "TG_API_ID": ("telegram", "api_id"),
    "TG_API_HASH": ("telegram", "api_hash"),
    "TG_BOT_TOKEN": ("telegram", "bot_token"),
    "TG_PROXY": ("telegram", "proxy"),
    "WECHAT_URL": ("wechat", "base_url"),
    "WECHAT_TOKEN": ("wechat", "token"),
    "WECHAT_TOKEN_FILE": ("wechat", "token_file"),
    "WECHAT_ACCOUNT": ("wechat", "account"),
    "BRIDGE_DATA_DIR": ("bridge", "data_dir"),
}


def _section(cls, raw: dict | None):
    raw = raw or {}
    known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"[{cls.__name__}] unknown keys: {', '.join(sorted(unknown))}")
    return cls(**known)


def load_config(path: str | Path, environ: dict | None = None) -> Config:
    environ = os.environ if environ is None else environ
    path = Path(path).resolve()
    raw: dict = {}
    if path.is_file():
        with path.open("rb") as f:
            raw = tomllib.load(f)
    for var, (section, key) in ENV.items():
        value = environ.get(var, "").strip()
        if value:
            raw.setdefault(section, {})[key] = int(value) if key == "api_id" else value
    cfg = Config(
        wechat=_section(WeChatConfig, raw.get("wechat")),
        telegram=_section(TelegramConfig, raw.get("telegram")),
        bridge=_section(BridgeConfig, raw.get("bridge")),
        path=path,
    )
    missing = [name for name, value in (
        ("TG_API_ID", cfg.telegram.api_id),
        ("TG_API_HASH", cfg.telegram.api_hash),
        ("TG_BOT_TOKEN", cfg.telegram.bot_token),
        ("WECHAT_TOKEN or WECHAT_TOKEN_FILE", cfg.wechat.token or cfg.wechat.token_file),
    ) if not value]
    if missing:
        raise ValueError(f"Missing settings: {', '.join(missing)} "
                         f"(set them as environment variables / in .env, or in {path.name})")
    return cfg
