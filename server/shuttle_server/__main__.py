"""Entry point: python -m shuttle_server --config config.yaml"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from aiohttp.web_runner import GracefulExit

from . import __version__
from .config import ConfigError, load_config
from .logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shuttle-server", description="搞快 Shuttle Mac server")
    ap.add_argument("-c", "--config", default=os.environ.get("SHUTTLE_CONFIG", "config.yaml"))
    ap.add_argument("--check", action="store_true", help="validate the config, print it (redacted) and exit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config if os.path.exists(os.path.expanduser(args.config)) else None)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps(cfg.redacted(), indent=2, ensure_ascii=False))
        return 0

    log_path = setup_logging(cfg.logging)
    log = logging.getLogger("shuttle")
    if not os.path.exists(os.path.expanduser(args.config)):
        log.warning("config %s not found, using defaults + environment", args.config)
    log.info("starting shuttle-server %s (log file: %s)", __version__, log_path)

    from .app import serve  # imported late so --check works without optional deps

    try:
        asyncio.run(serve(cfg))
    except (KeyboardInterrupt, GracefulExit):
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
