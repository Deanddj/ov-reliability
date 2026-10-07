from pathlib import Path
from typing import Any

import pytest

from ov_reliability.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


def config_data(archive_dir: Path) -> dict[str, Any]:
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
        },
    }


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate(config_data(tmp_path / "archive"))
