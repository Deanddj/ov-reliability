from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ov_reliability.api import __main__ as entry
from ov_reliability.runtime import EXIT_BAD_CONFIG

REPO_CONFIG = (Path(__file__).parents[1] / "config.toml").read_text(encoding="utf-8")
SERVED = """lines = [
  "htm/*/*", "gvb/tram/*", "gvb/bus/*", "gvb/metro/*", "ret/*/*", "uov/bus/*", "uov/tram/*",
]"""


def repo_config(tmp_path: Path) -> str:
    return REPO_CONFIG.replace("/data/timetables", f"{tmp_path}/timetables")


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def run(app: FastAPI, **options: Any) -> None:
        found["app"] = app
        found.update(options)

    monkeypatch.setattr(uvicorn, "run", run)
    return found


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("OV_CONFIG", str(path))
    monkeypatch.setenv("OV_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    timetables = tmp_path / "timetables"
    timetables.mkdir()
    return timetables


def test_serves_the_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, served: dict[str, Any]
) -> None:
    timetables = configure(
        monkeypatch,
        tmp_path,
        repo_config(tmp_path),
    )
    assert timetables.is_dir()
    assert entry.main() == 0
    assert (served["host"], served["port"]) == ("0.0.0.0", 8000)
    response = TestClient(served["app"]).get("/v1/lines/arr/bus/1/trips")
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("missing_after_minutes = 15", "missing_after_minutes = 0", "missing_after_minutes"),
        (SERVED, 'lines = ["htm/tram"]', "operator/mode/code"),
        (SERVED, 'lines = ["gvb/water/F1"]', "gvb/water/F1 is in no"),
        ('timetables"\ncheck_minutes', 'nonexistent"\ncheck_minutes', "does not exist"),
    ],
)
def test_bad_config_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    served: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    old: str,
    new: str,
    message: str,
) -> None:
    text = repo_config(tmp_path)
    assert old in text
    configure(monkeypatch, tmp_path, text.replace(old, new))
    assert entry.main() == EXIT_BAD_CONFIG
    assert message in caplog.text
    assert served == {}


def test_database_url_is_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, served: dict[str, Any]
) -> None:
    configure(
        monkeypatch,
        tmp_path,
        repo_config(tmp_path),
    )
    monkeypatch.delenv("OV_DATABASE_URL")
    assert entry.main() == EXIT_BAD_CONFIG
    assert served == {}
