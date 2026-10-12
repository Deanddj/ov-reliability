"""Loading and validating config.toml."""

import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceConfig(_Section):
    endpoint: str = Field(min_length=1)
    topics: tuple[str, ...] = Field(min_length=1)

    @field_validator("topics")
    @classmethod
    def _check_topics(cls, topics: tuple[str, ...]) -> tuple[str, ...]:
        if any(not topic for topic in topics):
            raise ValueError("topics must not be empty")
        if len(set(topics)) != len(topics):
            raise ValueError("topics must be unique")
        return topics


class ArchiveConfig(_Section):
    directory: Path
    retention_days: int = Field(ge=1)
    flush_seconds: float = Field(ge=1)
    disk_warning_gb: float = Field(ge=0)


class MonitoringConfig(_Section):
    service_hours: tuple[time, time]
    topic_silence_minutes: int = Field(ge=1)
    restart_silence_minutes: int = Field(ge=1)
    watched_topics: tuple[str, ...] = Field(min_length=1)

    @field_validator("service_hours", mode="before")
    @classmethod
    def _parse_service_hours(cls, value: object) -> tuple[time, time]:
        if not isinstance(value, list | tuple) or len(value) != 2:
            raise ValueError('service_hours must be two "HH:MM" strings')
        start, end = value
        return _parse_hhmm(start), _parse_hhmm(end)


OPERATOR = re.compile(r"^[a-z0-9-]+$")


class TimetablesConfig(_Section):
    base_url: str
    operators: tuple[str, ...] = Field(min_length=1)
    directory: Path
    check_minutes: int = Field(ge=15)
    timeout_seconds: float = Field(ge=1)
    disk_warning_gb: float = Field(ge=0)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, base_url: str) -> str:
        if not base_url.startswith("https://") or not base_url.endswith("/"):
            raise ValueError('base_url must start with "https://" and end with "/"')
        return base_url

    @field_validator("operators")
    @classmethod
    def _check_operators(cls, operators: tuple[str, ...]) -> tuple[str, ...]:
        # Operators become directory names, so only plain lower-case names are allowed.
        for operator in operators:
            if not OPERATOR.match(operator):
                raise ValueError(f"operator {operator!r} must match {OPERATOR.pattern}")
        if len(set(operators)) != len(operators):
            raise ValueError("operators must be unique")
        return operators


DATA_OWNER = re.compile(r"^[A-Z0-9]+$")
RECORDER = re.compile(r"^[a-z0-9-]+$")


class ArchiveSource(_Section):
    recorder: str
    directory: Path

    @field_validator("recorder")
    @classmethod
    def _check_recorder(cls, recorder: str) -> str:
        if not RECORDER.match(recorder):
            raise ValueError(f"recorder {recorder!r} must match {RECORDER.pattern}")
        return recorder


class LoaderConfig(_Section):
    data_owners: tuple[str, ...] = Field(min_length=1)
    window_days: int = Field(ge=1)
    poll_seconds: float = Field(ge=1)
    archives: tuple[ArchiveSource, ...] = Field(min_length=1)

    @field_validator("data_owners")
    @classmethod
    def _check_data_owners(cls, owners: tuple[str, ...]) -> tuple[str, ...]:
        # KV6 writes the owner in capitals; a lower-case entry would silently match nothing.
        for owner in owners:
            if not DATA_OWNER.match(owner):
                raise ValueError(f"data owner {owner!r} must match {DATA_OWNER.pattern}")
        if len(set(owners)) != len(owners):
            raise ValueError("data_owners must be unique")
        return owners

    @field_validator("archives")
    @classmethod
    def _check_archives(cls, archives: tuple[ArchiveSource, ...]) -> tuple[ArchiveSource, ...]:
        recorders = [archive.recorder for archive in archives]
        if len(set(recorders)) != len(recorders):
            raise ValueError("archive recorders must be unique")
        return archives


MODE = re.compile(r"^[a-z]+$")
CODE = re.compile(r"^[^/\s*]+$")


@dataclass(frozen=True)
class LinePattern:
    """A line as "operator/mode/code"; mode and code may be "*" for any."""

    operator: str
    mode: str
    code: str

    @classmethod
    def parse(cls, value: object) -> "LinePattern":
        parts = value.split("/") if isinstance(value, str) else []
        if (
            len(parts) != 3
            or not OPERATOR.match(parts[0])
            or not (parts[1] == "*" or MODE.match(parts[1]))
            or not (parts[2] == "*" or CODE.match(parts[2]))
        ):
            raise ValueError(
                f'line {value!r} must be "operator/mode/code", "*" for any mode or code'
            )
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.operator}/{self.mode}/{self.code}"

    def matches(self, operator: str, mode: str, code: str) -> bool:
        return self.covers(LinePattern(operator, mode, code))

    def covers(self, other: "LinePattern") -> bool:
        """True when every line `other` matches is matched by this pattern."""
        return (
            self.operator == other.operator
            and self.mode in ("*", other.mode)
            and self.code in ("*", other.code)
        )

    def overlaps(self, other: "LinePattern") -> bool:
        return self.operator == other.operator and all(
            "*" in (mine, theirs) or mine == theirs
            for mine, theirs in ((self.mode, other.mode), (self.code, other.code))
        )


type LinePatterns = tuple[Annotated[LinePattern, BeforeValidator(LinePattern.parse)], ...]

PLANNING_NUMBER = re.compile(r"^([a-z0-9-]+)/([^/\s]+)$")


class RealtimeSource(_Section):
    lines: LinePatterns = Field(min_length=1)
    recorder: str
    topics: tuple[str, ...] = Field(min_length=1)

    @field_validator("recorder")
    @classmethod
    def _check_recorder(cls, recorder: str) -> str:
        if not RECORDER.match(recorder):
            raise ValueError(f"recorder {recorder!r} must match {RECORDER.pattern}")
        return recorder

    @field_validator("topics")
    @classmethod
    def _check_topics(cls, topics: tuple[str, ...]) -> tuple[str, ...]:
        if any(not topic for topic in topics):
            raise ValueError("topics must not be empty")
        if len(set(topics)) != len(topics):
            raise ValueError("topics must be unique")
        return topics


class ApiConfig(_Section):
    default_range_minutes: int = Field(ge=1)
    max_range_days: int = Field(ge=1)
    missing_after_minutes: int = Field(ge=1)
    lines: LinePatterns = Field(min_length=1)
    no_realtime: LinePatterns
    # Kept as "operator/planning number" pairs (decision 069).
    ignore_planning_numbers: tuple[tuple[str, str], ...]
    realtime: tuple[RealtimeSource, ...]

    @field_validator("ignore_planning_numbers", mode="before")
    @classmethod
    def _parse_ignored(cls, value: object) -> tuple[tuple[str, str], ...]:
        if not isinstance(value, list | tuple):
            raise ValueError('ignore_planning_numbers must be a list of "operator/planning number"')
        pairs = []
        for entry in value:
            match = PLANNING_NUMBER.match(entry) if isinstance(entry, str) else None
            if not match:
                raise ValueError(f'ignored planning number {entry!r} must be "operator/number"')
            pairs.append((match[1], match[2]))
        return tuple(pairs)

    @model_validator(mode="after")
    def _check_lines(self) -> Self:
        sources = [pattern for source in self.realtime for pattern in source.lines]
        for i, pattern in enumerate(sources):
            for other in sources[i + 1 :]:
                # A line's gaps must come from exactly one recorder.
                if pattern.overlaps(other):
                    raise ValueError(f"realtime lines {pattern} and {other} overlap")
        for line in self.lines:
            live = any(source.covers(line) for source in sources)
            dead = any(pattern.covers(line) for pattern in self.no_realtime)
            if not live and not dead:
                raise ValueError(f"line {line} is in no [[api.realtime]] entry nor in no_realtime")
            if live and dead:
                raise ValueError(f"line {line} is in an [[api.realtime]] entry and in no_realtime")
        return self

    def serves(self, operator: str, mode: str, code: str) -> bool:
        return any(pattern.matches(operator, mode, code) for pattern in self.lines)

    def realtime_for(self, operator: str, mode: str, code: str) -> RealtimeSource | None:
        """Where a line's real-time data comes from; None when it has none."""
        # no_realtime may carve lines out of a realtime entry, so it is asked first.
        if any(pattern.matches(operator, mode, code) for pattern in self.no_realtime):
            return None
        for source in self.realtime:
            if any(pattern.matches(operator, mode, code) for pattern in source.lines):
                return source
        return None

    def ignored(self, operator: str) -> set[str]:
        return {number for owner, number in self.ignore_planning_numbers if owner == operator}


class Config(_Section):
    # Other services' sections are ignored here, so a mistake in one of them can't stop the
    # collector. Each service validates only the sections it uses.
    model_config = ConfigDict(extra="ignore", frozen=True)

    source: SourceConfig
    archive: ArchiveConfig
    monitoring: MonitoringConfig

    @model_validator(mode="after")
    def _check_watched_topics(self) -> Self:
        watched = self.monitoring.watched_topics
        if len(set(watched)) != len(watched):
            raise ValueError("watched_topics must be unique")
        for topic in watched:
            if topic not in self.source.topics:
                raise ValueError(f"watched topic {topic!r} is not in source.topics")
        return self


def load_config(path: Path) -> Config:
    return Config.model_validate(_read_toml(path))


def load_timetables_config(path: Path) -> TimetablesConfig:
    data = _read_toml(path)
    if "timetables" not in data:
        raise ValueError(f"{path} has no [timetables] section")
    return TimetablesConfig.model_validate(data["timetables"])


def load_loader_config(path: Path) -> LoaderConfig:
    data = _read_toml(path)
    if "loader" not in data:
        raise ValueError(f"{path} has no [loader] section")
    return LoaderConfig.model_validate(data["loader"])


def load_api_config(path: Path) -> ApiConfig:
    data = _read_toml(path)
    for section in ("api", "timetables", "loader"):
        if section not in data:
            raise ValueError(f"{path} has no [{section}] section")
    api = ApiConfig.model_validate(data["api"])
    timetables = TimetablesConfig.model_validate(data["timetables"])
    loader = LoaderConfig.model_validate(data["loader"])
    # The plan comes from the archived timetables and the gaps from what the loader stores, so
    # a line outside them would answer "unknown" forever instead of failing here.
    for line in api.lines:
        if line.operator not in timetables.operators:
            raise ValueError(f"line {line}: operator not in [timetables] operators")
    recorders = {archive.recorder for archive in loader.archives}
    for source in api.realtime:
        if source.recorder not in recorders:
            raise ValueError(f"realtime recorder {source.recorder!r} is not in [[loader.archives]]")
    return api


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _parse_hhmm(value: object) -> time:
    if not isinstance(value, str):
        raise ValueError(f'expected "HH:MM", got {value!r}')
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise ValueError(f'expected "HH:MM", got {value!r}') from None
