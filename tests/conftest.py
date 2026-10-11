import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from ov_reliability.config import Config
from ov_reliability.loader.store import migrate

FIXTURES = Path(__file__).parent / "fixtures"


def config_data(archive_dir: Path, timetables_dir: Path | None = None) -> dict[str, Any]:
    return {
        "source": {
            "endpoint": "tcp://127.0.0.1:1",
            "topics": ["/RIG/KV6posinfo", "/RIG/KV17cvlinfo", "/GVB/KV6posinfo"],
        },
        "archive": {
            "directory": str(archive_dir),
            "retention_days": 30,
            "flush_seconds": 5,
            "disk_warning_gb": 0,
        },
        "monitoring": {
            "service_hours": ["06:00", "00:00"],
            "topic_silence_minutes": 15,
            "restart_silence_minutes": 10,
            "watched_topics": ["/RIG/KV6posinfo", "/GVB/KV6posinfo"],
        },
        "timetables": {
            "base_url": "https://example.org/netex/",
            "operators": ["gvb", "htm"],
            "directory": str(timetables_dir or archive_dir.parent / "timetables"),
            "check_minutes": 60,
            "timeout_seconds": 30,
            "disk_warning_gb": 0,
        },
        "loader": {
            "data_owners": ["HTM", "RET"],
            "window_days": 90,
            "poll_seconds": 5,
            "archives": [{"recorder": "collector", "directory": str(archive_dir)}],
        },
    }


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate(config_data(tmp_path / "archive"))


@contextmanager
def fresh_schema(url: str) -> Iterator[str]:
    """A new schema with the loader's tables; yields a URL whose connections use it."""
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            scoped = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            with psycopg.connect(scoped, autocommit=True) as conn:
                migrate(conn)
            yield scoped
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def database_url() -> str:
    # Postgres behaviour (partitions, ON CONFLICT, locks) can't be faked; CI must never skip
    # these tests (decision 081).
    url = os.environ.get("OV_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("OV_REQUIRE_DATABASE"):
            pytest.fail("OV_REQUIRE_DATABASE is set but OV_TEST_DATABASE_URL is not")
        pytest.skip("OV_TEST_DATABASE_URL is not set")
    return url


@pytest.fixture
def schema_url(database_url: str) -> Iterator[str]:
    with fresh_schema(database_url) as url:
        yield url


@pytest.fixture
def database(schema_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(schema_url, autocommit=True) as conn:
        yield conn
