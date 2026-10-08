"""Entry point: python -m ov_reliability.timetables."""

import logging
import shutil
import sys
from importlib.metadata import version

from ov_reliability.config import load_timetables_config
from ov_reliability.runtime import (
    EXIT_BAD_CONFIG,
    config_path,
    setup_logging,
    stop_on_signal,
    utc_now,
)
from ov_reliability.timetables.archiver import Archiver, http_fetch
from ov_reliability.timetables.store import ArchiveMismatchError, TimetableStore

log = logging.getLogger("ov_reliability.timetables")

EXIT_ARCHIVE_MISMATCH = 1


def main() -> int:
    setup_logging()
    path = config_path()
    try:
        settings = load_timetables_config(path)
    except (OSError, ValueError) as exc:
        log.error("Cannot use configuration %s: %s", path, exc)
        return EXIT_BAD_CONFIG

    should_stop = stop_on_signal()

    log.info("Started version %s", version("ov-reliability"))
    store = TimetableStore(settings.directory, utc_now)
    try:
        store.open()
    except ArchiveMismatchError as exc:
        log.error("Timetable archive does not match its index: %s", exc)
        return EXIT_ARCHIVE_MISMATCH

    archiver = Archiver(
        settings,
        store,
        http_fetch(settings.timeout_seconds),
        lambda: shutil.disk_usage(settings.directory).free,
    )
    return archiver.run(should_stop)


if __name__ == "__main__":
    sys.exit(main())
