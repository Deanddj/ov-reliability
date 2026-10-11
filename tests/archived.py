"""Loading the archived records kept as test fixtures."""

from pathlib import Path

from ov_reliability.collector.records import BadPayload, Message, decode

FIXTURES = Path(__file__).parent / "fixtures"


def archived(name: str) -> Message | BadPayload:
    record = decode((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(record, Message | BadPayload)
    return record
