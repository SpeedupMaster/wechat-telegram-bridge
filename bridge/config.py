"""Settings: environment variables (.env in Docker) plus an optional config.toml for advanced options."""
from __future__ import annotations

import math
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


MODES = {"groups": "groups", "group": "groups", "topics": "topics", "topic": "topics", "forum": "topics"}


@dataclass
class BridgeConfig:
    data_dir: str = "data"
    # "groups": one Telegram group per WeChat chat, created by your Telegram account (needs account login).
    # "topics": one forum supergroup, one topic per WeChat chat, run by the bot alone.
    mode: str = "groups"
    forum_chat: int = 0  # topics mode: the forum supergroup id (-100...); 0 = set it with /forum in the group
    title_direct: str = "{name}"
    title_group: str = "👥 {name}"
    bridge_groups: bool = True
    bridge_official_accounts: bool = True
    # Optional forum supergroup id (-100...) to route official/service accounts as individual topics
    # (0 = disabled, or set dynamically by sending /oaforum in a forum supergroup).
    official_accounts_forum_chat: int = 0
    forward_outgoing: bool = True
    outgoing_silent: bool = True
    sync_avatar: bool = True
    mark_read_on_reply: bool = True
    create_interval_seconds: float = 20.0
    max_media_mb: int = 200
    call_notifications: bool = True
    # Answering WeChat calls in a Telegram Mini App / web page (empty URL = off):
    # call_web_url is the public HTTPS address your reverse proxy forwards to call_web_listen.
    call_web_url: str = ""
    call_web_listen: str = "127.0.0.1:8780"
    call_web_app: str = ""  # optional BotFather Mini App short name: buttons open t.me/<bot>/<app> instead
    link_card_images: bool = True
    # With call_web_app set, Official Account article cards open in a built-in reader Mini App (no link prompt).
    article_reader: bool = True
    # Same reader for Xiaohongshu notes (video plays inline); falls back to the original page when blocked.
    xhs_reader: bool = True
    # Douyin video cards open Douyin's official embed player in the same Mini App (图文/profiles open the link).
    douyin_reader: bool = True
    # Optional proxy used only when fetching mp.weixin.qq.com articles (independent of telegram.proxy).
    article_proxy: str = ""
    friend_requests: bool = True  # push incoming WeChat friend requests to the bot chat
    auto_rename: bool = True  # follow WeChat remark/group-name changes in Telegram titles
    gif_as_sticker: bool = True
    emoji_to_wechat: bool = False  # convert common Telegram Unicode emojis to WeChat [emoji] codes when sending
    # Optional one-shot auto-relogin after WeChat signs out (requires "自动登录该设备" enabled on mobile WeChat).
    auto_relogin: bool = False
    auto_relogin_delay_min: float = 45.0       # minimum human-like wait (seconds) before attempting auto-relogin
    auto_relogin_delay_max: float = 180.0      # maximum human-like wait (seconds) before attempting auto-relogin
    auto_relogin_timeout_seconds: float = 60.0  # how long to wait for silent auto-login to complete before giving up
    auto_relogin_cooldown_seconds: float = 3600.0  # minimum interval (seconds) between auto-relogin attempts
    skip_threads: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        mode = MODES.get(str(self.mode).strip().lower())
        if mode is None:
            raise ValueError(f"[bridge] mode must be \"groups\" or \"topics\", not {self.mode!r}")
        self.mode = mode
        self.forum_chat = int(self.forum_chat or 0)
        if not isinstance(self.bridge_official_accounts, bool):
            raise ValueError(
                f"[bridge] bridge_official_accounts must be true or false, not {self.bridge_official_accounts!r}"
            )
        if isinstance(self.official_accounts_forum_chat, bool):
            raise ValueError(
                f"[bridge] official_accounts_forum_chat must be a number, not {self.official_accounts_forum_chat!r}"
            )
        self.official_accounts_forum_chat = int(self.official_accounts_forum_chat or 0)
        self.call_web_url = str(self.call_web_url or "").strip().rstrip("/")
        if self.call_web_url and not self.call_web_url.lower().startswith(("https://", "http://")):
            raise ValueError(f"[bridge] call_web_url must start with https://, not {self.call_web_url!r}")
        self.call_web_app = str(self.call_web_app or "").strip().strip("/")
        host, sep, port = str(self.call_web_listen or "").strip().rpartition(":")
        if not sep or not port.isdigit() or not host:
            raise ValueError(f"[bridge] call_web_listen must look like \"127.0.0.1:8780\", not {self.call_web_listen!r}")
        for name in ("xhs_reader", "douyin_reader"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"[bridge] {name} must be true or false, not {getattr(self, name)!r}")
        if not isinstance(self.article_reader, bool):
            raise ValueError(f"[bridge] article_reader must be true or false, not {self.article_reader!r}")
        self.article_proxy = str(self.article_proxy or "").strip()
        if self.article_proxy and not self.article_proxy.lower().startswith(
                ("http://", "https://", "socks5://", "socks5h://")):
            raise ValueError(
                f"[bridge] article_proxy must start with http://, https://, socks5:// or socks5h://, "
                f"not {self.article_proxy!r}"
            )
        if not isinstance(self.emoji_to_wechat, bool):
            raise ValueError(f"[bridge] emoji_to_wechat must be true or false, not {self.emoji_to_wechat!r}")
        if not isinstance(self.auto_relogin, bool):
            raise ValueError(f"[bridge] auto_relogin must be true or false, not {self.auto_relogin!r}")
        for name in (
            "auto_relogin_delay_min",
            "auto_relogin_delay_max",
            "auto_relogin_timeout_seconds",
            "auto_relogin_cooldown_seconds",
        ):
            raw_val = getattr(self, name)
            if isinstance(raw_val, bool):
                raise ValueError(f"[bridge] {name} must be a number, not {raw_val!r}")
            try:
                num = float(raw_val)
            except (TypeError, ValueError):
                raise ValueError(f"[bridge] {name} must be a number, not {raw_val!r}") from None
            if not math.isfinite(num):
                raise ValueError(f"[bridge] {name} must be finite, not {raw_val!r}")
            setattr(self, name, num)
        if self.auto_relogin_delay_min < 0 or self.auto_relogin_delay_max < self.auto_relogin_delay_min:
            raise ValueError(
                "[bridge] auto_relogin_delay_min/max must satisfy 0 <= min <= max, "
                f"not {self.auto_relogin_delay_min!r}..{self.auto_relogin_delay_max!r}"
            )
        if self.auto_relogin_timeout_seconds <= 0:
            raise ValueError(
                f"[bridge] auto_relogin_timeout_seconds must be > 0, not {self.auto_relogin_timeout_seconds!r}"
            )
        if self.auto_relogin_cooldown_seconds < 0:
            raise ValueError(
                f"[bridge] auto_relogin_cooldown_seconds must be >= 0, not {self.auto_relogin_cooldown_seconds!r}"
            )


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
    "BRIDGE_MODE": ("bridge", "mode"),
    "TG_FORUM_CHAT": ("bridge", "forum_chat"),
    "BRIDGE_OFFICIAL_ACCOUNTS": ("bridge", "bridge_official_accounts"),
    "TG_OA_FORUM_CHAT": ("bridge", "official_accounts_forum_chat"),
    "CALL_WEB_URL": ("bridge", "call_web_url"),
    "CALL_WEB_LISTEN": ("bridge", "call_web_listen"),
    "CALL_WEB_APP": ("bridge", "call_web_app"),
    "ARTICLE_PROXY": ("bridge", "article_proxy"),
    "EMOJI_TO_WECHAT": ("bridge", "emoji_to_wechat"),
    "AUTO_RELOGIN": ("bridge", "auto_relogin"),
}
INT_KEYS = {"api_id", "forum_chat", "official_accounts_forum_chat"}
BOOL_KEYS = {"auto_relogin", "bridge_official_accounts", "emoji_to_wechat"}


def _parse_bool(var: str, value: str) -> bool:
    low = value.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{var} must be true/false (or 1/0), not {value!r}")


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
            if key in INT_KEYS:
                try:
                    parsed: object = int(value)
                except ValueError:
                    raise ValueError(f"{var} must be a number, not {value!r}") from None
            elif key in BOOL_KEYS:
                parsed = _parse_bool(var, value)
            else:
                parsed = value
            raw.setdefault(section, {})[key] = parsed
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
