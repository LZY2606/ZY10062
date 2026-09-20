"""Sample capture generator (used by the CLI, never hardcoded into the UI)."""

from __future__ import annotations

import json

from .capture import build_frame
from .protocols import FLAG_FRAG, get_protocol

MSG = {"HELLO": 0x01, "HELLO_ACK": 0x02, "DATA": 0x03,
       "FIN": 0x04, "FIN_ACK": 0x05}


def sample_records(version: str = "1.0") -> list[dict]:
    spec = get_protocol(version)
    t = 1_700_000_000_000.0

    def rec(ts_off, direction, frame):
        return {"ts": t + ts_off, "dir": direction, "data": frame.hex()}

    hello = build_frame(spec, 7, 0, bytes([MSG["HELLO"]]))
    hello_ack = build_frame(spec, 7, 0, bytes([MSG["HELLO_ACK"]]))
    text = "link budget ok, 温度正常".encode()
    half = len(text) // 2
    frag1 = build_frame(spec, 7, 1, bytes([MSG["DATA"]]) + text[:half],
                        flags=FLAG_FRAG)
    frag2 = build_frame(spec, 7, 2, text[half:])
    data2 = build_frame(spec, 7, 3, bytes([MSG["DATA"]]) + "second msg".encode())
    dup = data2  # exact duplicate retransmission
    corrupt = bytearray(build_frame(spec, 7, 4, bytes([MSG["DATA"]]) + b"bad"))
    corrupt[-1] ^= 0xFF  # break the checksum
    gap_frame = build_frame(spec, 7, 6, bytes([MSG["DATA"]]) + "after gap".encode())
    fin = build_frame(spec, 7, 7, bytes([MSG["FIN"]]))
    fin_ack = build_frame(spec, 7, 1, bytes([MSG["FIN_ACK"]]))

    return [
        rec(0, "c2s", hello),
        rec(12, "s2c", hello_ack),
        rec(30, "c2s", frag1),
        rec(31, "c2s", frag2),
        rec(50, "c2s", data2),
        rec(51, "c2s", dup),
        rec(60, "c2s", bytes(corrupt)),
        rec(70, "c2s", gap_frame),
        rec(80, "c2s", fin),
        rec(90, "s2c", fin_ack),
    ]


def sample_capture_jsonl(version: str = "1.0") -> str:
    return "\n".join(json.dumps(r) for r in sample_records(version)) + "\n"
