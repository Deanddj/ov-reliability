"""Startup helpers shared by the services' entry points."""

import logging
import os
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

EXIT_BAD_CONFIG = 2


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def config_path() -> Path:
    return Path(os.environ.get("OV_CONFIG", "config.toml"))


def utc_now() -> datetime:
    return datetime.now(UTC)


def stop_on_signal() -> Callable[[], bool]:
    requested = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        # Only set a flag. The handler runs between two bytecodes of the main thread, which may
        # be halfway through a gzip write or holding a lock the handler would need.
        nonlocal requested
        requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return lambda: requested
