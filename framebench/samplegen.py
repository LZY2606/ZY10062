"""Helpers to build well-formed wire segments.

Used by the test-suite and by ``python3 -m framebench gen-example`` so the
web page itself never carries hard-coded sample data.
"""
from __future__ import annotations

from .engine import crc16_ccitt

MSG = {"HELLO": 0, "CHALLENGE": 1, "RESPONSE": 2, "ACCEPT": 3, "DATA": 4,
       "FIN": 5}


def segment(msg_type, seq, payload=b"", *, magic=0xFB, version=1, flags=0,
            crc_init=0xFFFF):
    body = bytes([
        magic, version, msg_type, flags,
        (seq >> 8) & 0xFF, seq & 0xFF,
        (len(payload) >> 8) & 0xFF, len(payload) & 0xFF,
    ]) + payload
    crc = crc16_ccitt(body, crc_init)
    return body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def frame(fid, session, ts, direction, data):
    return {
        "id": fid,
        "session": session,
        "ts": ts,
        "dir": direction,
        "data": data.hex(),
    }


def demo_frames():
    """A complete, valid handshake session plus a noisy second session."""
    name = "边缘节点-α".encode("utf-8")
    frames = [
        frame("f1", "sess-1", 1.00, "c2s", segment(MSG["HELLO"], 0, name)),
        frame("f2", "sess-1", 1.10, "s2c",
              segment(MSG["CHALLENGE"], 0, b"\x01\x02\x03\x04")),
        frame("f3", "sess-1", 1.20, "c2s",
              segment(MSG["RESPONSE"], 1, b"\x09\x08")),
        frame("f4", "sess-1", 1.30, "s2c", segment(MSG["ACCEPT"], 1)),
        frame("f5", "sess-1", 1.40, "c2s", segment(MSG["DATA"], 2, b"ping")),
        frame("f6", "sess-1", 1.50, "c2s", segment(MSG["FIN"], 3)),
    ]
    # sess-2: truncated UTF-8 in HELLO + one corrupted DATA frame.
    broken = bytearray(segment(MSG["DATA"], 1, b"noise"))
    broken[9] ^= 0xFF
    frames += [
        frame("g1", "sess-2", 2.00, "c2s",
              segment(MSG["HELLO"], 0, "客户端".encode("utf-8")[:-1])),
        frame("g2", "sess-2", 2.10, "s2c",
              segment(MSG["CHALLENGE"], 0, b"\xaa\xbb")),
        frame("g3", "sess-2", 2.20, "c2s", bytes(broken)),
    ]
    return frames
