import pytest

from framebench.analyzer import analyze
from framebench.capture import build_frame, to_records
from framebench.protocols import FLAG_FRAG, get_protocol

SPEC = get_protocol("1.0")
HELLO, HELLO_ACK, DATA, FIN, FIN_ACK = 0x01, 0x02, 0x03, 0x04, 0x05


def rec(i, ts, direction, frame):
    return {"ts": ts, "dir": direction, "data": frame.hex()}


def run(records):
    return analyze(to_records(records), SPEC)


def kinds(result):
    return [e["kind"] for e in result["events"]]


def test_clean_handshake_and_close():
    result = run([
        rec(0, 0, "c2s", build_frame(SPEC, 1, 0, bytes([HELLO]))),
        rec(1, 1, "s2c", build_frame(SPEC, 1, 0, bytes([HELLO_ACK]))),
        rec(2, 2, "c2s", build_frame(SPEC, 1, 1, bytes([DATA]) + b"ok")),
        rec(3, 3, "c2s", build_frame(SPEC, 1, 2, bytes([FIN]))),
        rec(4, 4, "s2c", build_frame(SPEC, 1, 1, bytes([FIN_ACK]))),
    ])
    assert "handshake_complete" in kinds(result)
    assert "session_closed" in kinds(result)
    assert all(f["status"] == "accepted" for f in result["frames"])
    assert result["sessions"] == {"1": "CLOSED"}


def test_duplicate_frame_is_recoverable_warning():
    f = build_frame(SPEC, 1, 0, bytes([HELLO]))
    result = run([rec(0, 0, "c2s", f), rec(1, 1, "c2s", f)])
    ev = next(e for e in result["events"] if e["kind"] == "duplicate_frame")
    assert ev["severity"] == "warning"
    assert result["frames"][1]["status"] == "recovering"


def test_conflicting_retransmission_is_terminal_for_frame():
    a = build_frame(SPEC, 1, 0, bytes([HELLO]))
    b = build_frame(SPEC, 1, 0, bytes([DATA]) + b"forged")
    result = run([rec(0, 0, "c2s", a), rec(1, 1, "c2s", b)])
    ev = next(e for e in result["events"] if e["kind"] == "conflicting_frame")
    assert ev["severity"] == "error"
    assert result["frames"][1]["status"] == "rejected"


def test_out_of_order_delivery_recovers():
    result = run([
        rec(0, 0, "c2s", build_frame(SPEC, 1, 0, bytes([HELLO]))),
        rec(1, 1, "c2s", build_frame(SPEC, 1, 2, bytes([DATA]) + b"b")),
        rec(2, 2, "c2s", build_frame(SPEC, 1, 1, bytes([DATA]) + b"a")),
    ])
    assert "gap_detected" in kinds(result)  # seq 2 arrived before seq 1
    assert "out_of_order" in kinds(result)
    oo = next(e for e in result["events"] if e["kind"] == "out_of_order")
    assert oo["severity"] == "warning"
    assert result["frames"][2]["status"] == "recovering"


def test_gap_is_reported_never_filled():
    result = run([
        rec(0, 0, "c2s", build_frame(SPEC, 1, 0, bytes([HELLO]))),
        rec(1, 1, "c2s", build_frame(SPEC, 1, 3, bytes([DATA]) + b"x")),
    ])
    gap = next(e for e in result["events"] if e["kind"] == "gap_detected")
    assert gap["severity"] == "error"
    assert gap["detail"]["missing"] == 2
    # no event may reference bytes outside the two real records
    for ev in result["events"]:
        for r in ev["byte_ranges"]:
            assert r["record"] in (0, 1)


def test_checksum_mismatch_rejects_frame():
    bad = bytearray(build_frame(SPEC, 1, 0, bytes([HELLO])))
    bad[-1] ^= 0xFF
    result = run([rec(0, 0, "c2s", bytes(bad))])
    assert kinds(result) == ["checksum_mismatch"]
    assert result["events"][0]["severity"] == "error"
    assert result["frames"][0]["status"] == "rejected"


def test_malformed_frame_rejected():
    result = run([rec(0, 0, "c2s", b"\x00\x01\x02")])
    assert kinds(result) == ["malformed_frame"]
    assert result["frames"][0]["status"] == "rejected"


def test_time_rollback_warning():
    result = run([
        rec(0, 100, "c2s", build_frame(SPEC, 1, 0, bytes([HELLO]))),
        rec(1, 50, "s2c", build_frame(SPEC, 1, 0, bytes([HELLO_ACK]))),
    ])
    rb = next(e for e in result["events"] if e["kind"] == "time_rollback")
    assert rb["severity"] == "warning"
    assert rb["detail"]["ts"] == 50


def test_partial_utf8_field_flagged_not_fabricated():
    text = "温度".encode()  # 6 bytes; cut the last char in half
    payload = bytes([DATA]) + text[:-1]
    result = run([
        rec(0, 0, "c2s", build_frame(SPEC, 1, 0, bytes([HELLO]))),
        rec(1, 1, "s2c", build_frame(SPEC, 1, 0, bytes([HELLO_ACK]))),
        rec(2, 2, "c2s", build_frame(SPEC, 1, 1, payload)),
    ])
    ev = next(e for e in result["events"] if e["kind"] == "utf8_truncated")
    assert ev["severity"] == "warning"
    assert ev["detail"]["valid_prefix_bytes"] == 3  # only 温 survived


def test_fragment_reassembly_and_abandon():
    frag = build_frame(SPEC, 1, 0, bytes([DATA]) + b"hel", flags=FLAG_FRAG)
    tail = build_frame(SPEC, 1, 1, b"lo")
    ok = run([rec(0, 0, "c2s", frag), rec(1, 1, "c2s", tail)])
    assert "reassembled" in kinds(ok)
    assert all(f["status"] == "accepted" for f in ok["frames"])

    abandoned = run([rec(0, 0, "c2s", frag)])
    ev = next(e for e in abandoned["events"] if e["kind"] == "fragment_abandoned")
    assert ev["severity"] == "error"
    assert abandoned["frames"][0]["status"] == "rejected"


def test_unexpected_message_does_not_change_state():
    result = run([rec(0, 0, "c2s", build_frame(SPEC, 1, 0, bytes([FIN_ACK])))])
    ev = next(e for e in result["events"] if e["kind"] == "unexpected_message")
    assert ev["severity"] == "warning"
    assert result["sessions"] == {}


def test_deterministic_event_ids():
    records = [
        rec(0, 0, "c2s", build_frame(SPEC, 1, 0, bytes([HELLO]))),
        rec(1, 1, "s2c", build_frame(SPEC, 1, 0, bytes([HELLO_ACK]))),
        rec(2, 2, "c2s", build_frame(SPEC, 1, 1, bytes([DATA]) + b"z")),
    ]
    a, b = run(records), run(records)
    assert [e["id"] for e in a["events"]] == [e["id"] for e in b["events"]]
    assert [e["digest"] for e in a["events"]] == [e["digest"] for e in b["events"]]


def test_event_byte_ranges_point_into_records():
    result = run([rec(0, 0, "c2s", build_frame(SPEC, 1, 0, bytes([HELLO])))])
    ev = result["events"][0]
    assert ev["byte_ranges"] == [{"record": 0, "start": 0, "end": 17}]
