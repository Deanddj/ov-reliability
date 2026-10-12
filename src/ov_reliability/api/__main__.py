"""Entry point: python -m ov_reliability.api."""

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import uvicorn

from ov_reliability.api.app import create_app
from ov_reliability.api.queries import Reader
from ov_reliability.api.timetables import PlanCache
from ov_reliability.config import load_api_config, load_timetables_config
from ov_reliability.runtime import EXIT_BAD_CONFIG, config_path, setup_logging, utc_now

log = logging.getLogger("ov_reliability.api")

PORT = 8000


def main() -> int:
    setup_logging()
    path = config_path()
    try:
        settings = load_api_config(path)
        directory = load_timetables_config(path).directory
    except (OSError, ValueError) as exc:
        log.error("Cannot use configuration %s: %s", path, exc)
        return EXIT_BAD_CONFIG
    if not directory.is_dir():
        log.error("Timetable directory %s does not exist", directory)
        return EXIT_BAD_CONFIG
    url = os.environ.get("OV_DATABASE_URL")
    if not url:
        log.error("OV_DATABASE_URL is not set")
        return EXIT_BAD_CONFIG

    @contextmanager
    def connect() -> Iterator[Reader]:
        # One connection per request (decision 080); it only reads.
        with psycopg.connect(url, autocommit=True) as conn:
            yield Reader(conn)

    app = create_app(settings, connect, PlanCache(directory), utc_now)
    log.info("Serving %s on port %d", ", ".join(map(str, settings.lines)), PORT)
    # log_config=None leaves uvicorn's loggers to the format of the other services.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
