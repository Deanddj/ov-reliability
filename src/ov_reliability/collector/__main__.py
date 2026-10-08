"""Entry point: python -m ov_reliability.collector."""

import logging
import sys
from importlib.metadata import version

from ov_reliability.collector.archive import ArchiveWriter
from ov_reliability.collector.recorder import Recorder
from ov_reliability.collector.stream import ZmqStream
from ov_reliability.config import load_config
from ov_reliability.runtime import (
    EXIT_BAD_CONFIG,
    config_path,
    setup_logging,
    stop_on_signal,
    utc_now,
)

log = logging.getLogger("ov_reliability.collector")


def main() -> int:
    setup_logging()
    path = config_path()
    try:
        config = load_config(path)
    except (OSError, ValueError) as exc:
        log.error("Cannot use configuration %s: %s", path, exc)
        return EXIT_BAD_CONFIG

    should_stop = stop_on_signal()

    config.archive.directory.mkdir(parents=True, exist_ok=True)
    stream = ZmqStream(config.source.endpoint, config.source.topics, utc_now)
    writer = ArchiveWriter(config.archive.directory, config.archive.flush_seconds)
    recorder = Recorder(config, writer, utc_now, should_stop, version("ov-reliability"))
    try:
        return recorder.run(stream)
    finally:
        stream.close()


if __name__ == "__main__":
    sys.exit(main())
