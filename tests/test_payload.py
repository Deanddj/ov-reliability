import base64
import gzip
from datetime import UTC, datetime

import pytest
from archived import archived

from ov_reliability.collector.records import BadPayload, Message
from ov_reliability.kv6.payload import BadMessageError, decode_payload

AT = datetime(2026, 10, 9, 8, tzinfo=UTC)


def local(tag: str) -> str:
    return tag.rpartition("}")[2]


@pytest.mark.parametrize(
    "name",
    [
        "kv6_htm_departure.jsonl",
        "kv17_cxx_two_blocks.jsonl",
        "kv17_gvb_local_time.jsonl",
        "kv17_htm_shorten.jsonl",
        "kv6_keolis_bom.jsonl",
    ],
)
def test_archived_payloads_decode(name: str) -> None:
    assert local(decode_payload(archived(name)).tag) == "VV_TM_PUSH"


def test_gzip_cp1252_payload_decodes() -> None:
    record = archived("kv15_cxx_gzip_cp1252.jsonl")
    assert isinstance(record, BadPayload)
    root = decode_payload(record)
    assert "Andalusië" in "".join(root.itertext())


def test_gzip_payload_as_text_decodes() -> None:
    data = base64.b64encode(gzip.compress(b"<a>\xc3\xab</a>")).decode()
    assert decode_payload(BadPayload(AT, "/X/KV6posinfo", data, "")).text == "ë"


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (Message(AT, "/X/KV6posinfo", '<!DOCTYPE a [<!ENTITY b "c">]><a>&b;</a>'), "DTD"),
        (Message(AT, "/X/KV6posinfo", "<a><b></a>"), "not XML"),
        (BadPayload(AT, "/X/KV6posinfo", "not base64!", ""), "base64"),
        (BadPayload(AT, "/X/KV6posinfo", base64.b64encode(b"\x1f\x8bxx").decode(), ""), "gzip"),
        (BadPayload(AT, "/X/KV6posinfo", base64.b64encode(b"\x81<a>").decode(), ""), "not XML"),
    ],
)
def test_bad_payloads_are_rejected(record: Message | BadPayload, reason: str) -> None:
    with pytest.raises(BadMessageError, match=reason):
        decode_payload(record)
