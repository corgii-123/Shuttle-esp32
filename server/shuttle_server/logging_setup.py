"""Rotating file logging (+ optional console). Unattended Mac: logs must not grow forever."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingConfig

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(cfg: LoggingConfig) -> Path | None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    formatter = logging.Formatter(FORMAT)

    log_path: Path | None = None
    if cfg.dir:
        log_dir = Path(cfg.dir).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "shuttle.log"
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
    if cfg.console or not cfg.dir:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(formatter)
        root.addHandler(sh)
    # aiohttp access logs are noisy (health checks); keep warnings only.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return log_path
