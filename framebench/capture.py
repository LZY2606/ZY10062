"""Capture upload parsing and frame construction helpers."""

from __future__ import annotations

import json
import zlib

from .analyzer import Record
from .protocols import get_protocol


def parse_capture(text: str) -> list[dict]:
    """Parse an uploaded capture (JSONL: {"ts", "dir", "data"} per line)."""
    records = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            data = bytes.fromhex(obj["data"])
            direction = obj["dir"]
            if direction not in ("c2s", "s2c"):
                raise ValueError(f"bad dir {direction!r}")
            records.append({"ts": float(obj["ts"]), "dir": direction, "data": data.hex()})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"capture line {lineno}: {exc}") from exc
    if not records:
        raise ValueError("capture is empty")
    return records


def to_records(raw: list[dict]) -> list[Record]:
    return [
        Record(id=i, ts=r["ts"], dir=r["dir"], data=bytes.fromhex(r["data"]))
        for i, r in enumerate(raw)
    ]


def build_frame(spec: dict, session: int, seq: int, payload: bytes,
                flags: int = 0, channel: int = 0) -> bytes:
    """Build a wire frame for the given protocol version (tests/examples)."""
    f = spec["fields"]
    header = bytearray(spec["header_len"])
    header[f["magic"][0]:f["magic"][0] + 2] = bytes.fromhex(spec["magic"])
    header[f["version"][0]] = int(spec["version"].split(".")[0])
    header[f["session"][0]:f["session"][0] + 2] = session.to_bytes(2, "big")
    if "channel" in f:
        header[f["channel"][0]:f["channel"][0] + 2] = channel.to_bytes(2, "big")
    header[f["seq"][0]:f["seq"][0] + 4] = seq.to_bytes(4, "big")
    header[f["flags"][0]] = flags
    header[f["plen"][0]:f["plen"][0] + 2] = len(payload).to_bytes(2, "big")
    body = bytes(header) + payload
    return body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
