"""The check loop: list every operator, download what is new, report how it went."""

import http.client
import logging
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from ov_reliability.config import TimetablesConfig
from ov_reliability.timetables.listing import ListingFormatError, parse_listing
from ov_reliability.timetables.store import BadDownloadError, StoreOutcome, TimetableStore

log = logging.getLogger(__name__)

EXIT_OK = 0
_WAIT_STEP_SECONDS = 1.0
USER_AGENT = "ov-reliability timetable archive"


class FetchError(Exception):
    pass


type Fetch = Callable[[str], bytes]


def http_fetch(timeout: float) -> Fetch:
    def fetch(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body: bytes = response.read()
                return body
        # http.client raises IncompleteRead when the body is shorter than Content-Length,
        # so a cut-off download never reaches the store.
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from None

    return fetch


@dataclass
class CheckSummary:
    operators: int = 0
    listed: int = 0
    new: int = 0
    new_bytes: int = 0
    republished: int = 0
    errors: int = 0
    failed_operators: int = 0


class Archiver:
    def __init__(
        self,
        config: TimetablesConfig,
        store: TimetableStore,
        fetch: Fetch,
        free_bytes: Callable[[], int],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._store = store
        self._fetch = fetch
        self._free_bytes = free_bytes
        self._sleep = sleep

    def run(self, should_stop: Callable[[], bool]) -> int:
        operators = ", ".join(self._config.operators)
        log.info("Checking %s every %d minutes", operators, self._config.check_minutes)
        while not should_stop():
            self._report(self.check(should_stop))
            self._wait(self._config.check_minutes * 60, should_stop)
        log.info("Stopped")
        return EXIT_OK

    def check(self, should_stop: Callable[[], bool]) -> CheckSummary:
        summary = CheckSummary()
        for operator in self._config.operators:
            if should_stop():
                break
            summary.operators += 1
            if not self._check_operator(operator, summary, should_stop):
                summary.failed_operators += 1
        return summary

    def _wait(self, seconds: float, should_stop: Callable[[], bool]) -> None:
        # Short steps instead of one long wait, so a stop request is seen within a second.
        deadline = time.monotonic() + seconds
        while not should_stop() and (remaining := deadline - time.monotonic()) > 0:
            self._sleep(min(_WAIT_STEP_SECONDS, remaining))

    def _check_operator(
        self, operator: str, summary: CheckSummary, should_stop: Callable[[], bool]
    ) -> bool:
        url = f"{self._config.base_url}{operator}/"
        try:
            entries = parse_listing(self._fetch(url).decode("utf-8", errors="replace"))
        except FetchError as exc:
            log.warning("Cannot list %s: %s", url, exc)
            summary.errors += 1
            return False
        except ListingFormatError as exc:
            log.error("Listing format not recognised for %s: %s", url, exc)
            summary.errors += 1
            return False
        if not entries:
            log.warning("No NeTEx files listed for %s", operator)
        summary.listed += len(entries)

        attempted = failed = 0
        for entry in entries:
            if should_stop():
                break
            if not self._store.needs_download(operator, entry):
                continue
            attempted += 1
            try:
                data = self._fetch(url + quote(entry.name))
                outcome = self._store.store(operator, entry, data)
            except (FetchError, BadDownloadError) as exc:
                log.warning("Download of %s/%s failed: %s", operator, entry.name, exc)
                summary.errors += 1
                failed += 1
                continue
            if outcome is not StoreOutcome.UNCHANGED:
                summary.new += 1
                summary.new_bytes += len(data)
            if outcome is StoreOutcome.REPUBLISHED:
                summary.republished += 1
        # Listings that work while every download fails still mean nothing is being archived.
        return attempted == 0 or failed < attempted

    def _report(self, summary: CheckSummary) -> None:
        free_gb = self._free_bytes() / 1e9
        line = (
            f"check done: {summary.operators} operators, {summary.listed} listed, "
            f"{summary.new} new ({summary.new_bytes / 1e6:.1f} MB), "
            f"{summary.republished} republished, {summary.errors} errors, {free_gb:.1f} GB free"
        )
        if summary.operators and summary.failed_operators == summary.operators:
            log.error("%s; every operator failed", line)
        elif free_gb < self._config.disk_warning_gb:
            log.warning(
                "%s; low free disk, warning below %.0f GB", line, self._config.disk_warning_gb
            )
        else:
            log.info(line)
