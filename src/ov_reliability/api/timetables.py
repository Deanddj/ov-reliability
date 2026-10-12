"""The archived timetables as the API reads them, with parsed files kept in memory."""

from datetime import date
from functools import lru_cache
from pathlib import Path

from ov_reliability.timetables.netex import TimetableFile, read_timetable
from ov_reliability.timetables.plan import load_timetables

# A parsed HTM file holds about 26 MB; eight files cover a week of requests across a change
# of timetable.
MAX_FILES = 8


class PlanCache:
    def __init__(self, directory: Path, max_files: int = MAX_FILES) -> None:
        self.directory = directory
        # Stored files never change, so a file's name is enough to know its content (decision 080).
        self._read = lru_cache(maxsize=max_files)(read_timetable)

    def timetables(self, operator: str, day: date) -> list[TimetableFile]:
        return load_timetables(self.directory, operator, day, self._read)
