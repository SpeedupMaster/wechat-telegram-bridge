"""Run with:  python -m bridge [path/to/config.toml]"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import sys

from .config import load_config
from .core import Bridge


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge", description="WeChat <-> Telegram bridge")
    parser.add_argument("config", nargs="?", default="config.toml",
                        help="optional config.toml with advanced settings (environment variables override it)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        sys.exit(str(e))

    async def run() -> None:
        # Cap worker threads so glibc does not spawn many long-lived malloc arenas for asyncio.to_thread().
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge-worker"))
        bridge = Bridge(cfg)
        try:
            await bridge.run()
        finally:
            for client in (bridge.user, bridge.bot):
                if client is not None and client.is_connected():
                    await client.disconnect()
            await bridge.close()
            if (st_mod := sys.modules.get("bridge.stickers")) is not None:
                st_mod.shutdown_media_executor(wait=False)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
