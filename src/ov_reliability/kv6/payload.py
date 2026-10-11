"""Turning an archived payload into an XML element."""

import base64
import binascii
import gzip
import xml.etree.ElementTree as ET
import zlib

from ov_reliability.collector.records import BadPayload, Message

_GZIP_MAGIC = b"\x1f\x8b"


class BadMessageError(ValueError):
    pass


def decode_payload(record: Message | BadPayload) -> ET.Element:
    if isinstance(record, Message):
        data = record.payload.encode("utf-8")
    else:
        try:
            data = base64.b64decode(record.payload_b64, validate=True)
        except binascii.Error:
            raise BadMessageError("payload is not valid base64") from None
    if data.startswith(_GZIP_MAGIC):
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error) as exc:
            raise BadMessageError(f"broken gzip payload: {exc}") from None
    # Entity attacks need a DTD, and no source has ever sent one (decision 076).
    if b"<!DOCTYPE" in data:
        raise BadMessageError("payload has a DTD")
    error: Exception
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        error = exc
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        # One CXX message was cp1252 without an XML declaration saying so.
        try:
            return ET.fromstring(data.decode("cp1252"))
        except (UnicodeDecodeError, ET.ParseError) as exc:
            error = exc
    raise BadMessageError(f"not XML: {error}")
