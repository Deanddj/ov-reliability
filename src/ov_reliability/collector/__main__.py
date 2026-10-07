"""Entry point: python -m ov_reliability.collector."""

import logging
import os
import signal
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import FrameType

from ov_reliability.collector.archive import ArchiveWriter
from ov_reliability.collector.recorder import Recorder
from ov_reliability.collector.stream import ZmqStream
from ov_reliability.config import load_config

log = logging.getLogger("ov_reliability.collector")

EXIT_BAD_CONFIG = 2


def utc_now() -> datetime:
    return datetime.now(UTC)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    config_path = Path(os.environ.get("OV_CONFIG", "config.toml"))
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        log.error("Cannot use configuration %s: %s", config_path, exc)
        return EXIT_BAD_CONFIG

    stop_requested = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        # Only set a flag: stopping inside the handler could interrupt a gzip write.
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    config.archive.directory.mkdir(parents=True, exist_ok=True)
    stream = ZmqStream(config.source.endpoint, config.source.topics, utc_now)
    writer = ArchiveWriter(config.archive.directory, config.archive.flush_seconds)
    recorder = Recorder(config, writer, utc_now, lambda: stop_requested, version("ov-reliability"))
    try:
        return recorder.run(stream)
    finally:
        stream.close()


if __name__ == "__main__":
    sys.exit(main())
