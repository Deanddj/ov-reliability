"""Loading and validating config.toml."""

import tomllib
from datetime import datetime, time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    @field_validator("service_hours", mode="before")
    @classmethod
    def _parse_service_hours(cls, value: object) -> tuple[time, time]:
        if not isinstance(value, list | tuple) or len(value) != 2:
            raise ValueError('service_hours must be two "HH:MM" strings')
        start, end = value
        return _parse_hhmm(start), _parse_hhmm(end)


class Config(_Section):
    source: SourceConfig
    archive: ArchiveConfig
    monitoring: MonitoringConfig

    @property
    def kv6_topics(self) -> tuple[str, ...]:
        return tuple(topic for topic in self.source.topics if topic.endswith("/KV6posinfo"))


def load_config(path: Path) -> Config:
    with path.open("rb") as file:
        data = tomllib.load(file)
    return Config.model_validate(data)


def _parse_hhmm(value: object) -> time:
    if not isinstance(value, str):
        raise ValueError(f'expected "HH:MM", got {value!r}')
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise ValueError(f'expected "HH:MM", got {value!r}') from None
