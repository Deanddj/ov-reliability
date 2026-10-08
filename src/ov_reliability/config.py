"""Loading and validating config.toml."""

import re
import tomllib
from datetime import datetime, time
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
