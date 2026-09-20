"""Engine behaviour: reassembly, checksums, state machine, diagnostics."""
import pytest

from framebench import engine, samplegen

MSG = samplegen.MSG


def F(fid, ts, direction, data, session="s1", arrival=None):
    return {
        "frame_id": fid,
        "session": session,
        "ts": ts,
        "direction": direction,
        "data": data,
        "arrival": arrival if arrival is not None else int(fid[1:]),
    }


def handshake_frames():
    return [
        F("f1", 1.0, "c2s", samplegen.segment(MSG["HELLO"], 0, b"node-a")),
        F("f2", 1.1, "s2c", samplegen.segment(MSG["CHALLENGE"], 0, b"\x01\x02")),
        F("f3", 1.2, "c2s", samplegen.segment(MSG["RESPONSE"], 1, b"\x03")),
        F("f4", 1.3, "s2c", samplegen.segment(MSG["ACCEPT"], 1)),
        F("f5", 1.4, "c2s", samplegen.segment(MSG["DATA"], 2, b"ping")),
        F("f6", 1.5, "c2s", samplegen.segment(MSG["FIN"], 3)),
    ]


def kinds(result):
    return [e["kind"] for e in result["events"]]


def test_clean_handshake_is_accepted_and_deterministic():
    proto = engine.base_protocol(1)
    first = engine.analyze(handshake_frames(), proto)
    second = engine.analyze(handshake_frames(), proto)
    assert first["sessions"]["s1"]["status"] == "accepted"
    assert first["sessions"]["s1"]["state"] == "CLOSED"
    assert first["digest"] == second["digest"]
    assert [e["id"] for e in first["events"]] == [e["id"] for e in second["events"]]
    transitions = [e for e in first["events"] if e["kind"] == "state_transition"]
    assert [t["state_to"] for t in transitions] == [
        "HELLO_SENT", "CHALLENGED", "RESPONDED", "ESTABLISHED", "ESTABLISHED",
        "CLOSED",
    ]


def test_reassembly_across_frame_boundaries():
    seg = samplegen.segment(MSG["HELLO"], 0, b"split-name")
    half = len(seg) // 2
    frames = [F("f1", 1.0, "c2s", seg[:half]), F("f2", 1.1, "c2s", seg[half:])]
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "message" in kinds(result)
    assert result["sessions"]["s1"]["state"] == "HELLO_SENT"


def test_duplicate_frame_is_recoverable_warning():
    frames = handshake_frames()
    frames.insert(2, F("f9", 1.15, "s2c",
                       samplegen.segment(MSG["CHALLENGE"], 0, b"\x01\x02")))
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "duplicate_frame" in kinds(result)
    assert result["sessions"]["s1"]["status"] == "accepted"


def test_out_of_order_segments_are_buffered():
    frames = handshake_frames()
    frames[4], frames[5] = frames[5], frames[4]  # FIN arrives before DATA
    for i, fr in enumerate(frames):
        fr["arrival"] = i
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "out_of_order" in kinds(result)
    assert result["sessions"]["s1"]["status"] == "accepted"
    assert result["sessions"]["s1"]["state"] == "CLOSED"


def test_gap_is_reported_without_fabricating_bytes():
    frames = [
        F("f1", 1.0, "c2s", samplegen.segment(MSG["HELLO"], 0, b"n")),
        # seq 1 (RESPONSE) never arrives; DATA jumps within reorder window
        F("f2", 1.1, "c2s", samplegen.segment(MSG["DATA"], 2, b"x")),
        F("f3", 1.2, "c2s", samplegen.segment(MSG["FIN"], 3)),
    ]
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "unflushed_gap" in kinds(result)
    assert result["sessions"]["s1"]["status"] == "recovering"
    # no message event may reference the missing seq 1
    messages = [e for e in result["events"] if e["kind"] == "message"]
    assert all("seq=1" not in e["message"] for e in messages)


def test_large_gap_resyncs_with_warning():
    frames = [
        F("f1", 1.0, "c2s", samplegen.segment(MSG["HELLO"], 0, b"n")),
        F("f2", 1.1, "c2s", samplegen.segment(MSG["DATA"], 9, b"x")),
    ]
    result = engine.analyze(frames, engine.base_protocol(1))
    gap = [e for e in result["events"] if e["kind"] == "gap"]
    assert gap and "1-8" in gap[0]["message"]


def test_bad_checksum_is_recoverable_not_fatal():
    broken = bytearray(samplegen.segment(MSG["DATA"], 0, b"corrupt"))
    broken[9] ^= 0xFF
    frames = [F("f1", 1.0, "c2s", bytes(broken))]
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "checksum" in kinds(result)
    assert result["sessions"]["s1"]["status"] == "recovering"
    assert not any(e["severity"] == "fatal" for e in result["events"])


def test_time_rollback_warns_but_keeps_timestamp():
    frames = handshake_frames()
    frames[3]["ts"] = 0.5
    result = engine.analyze(frames, engine.base_protocol(1))
    rollback = [e for e in result["events"] if e["kind"] == "time_rollback"]
    assert rollback and rollback[0]["ts"] == 0.5


def test_truncated_utf8_field_is_recoverable():
    frames = [F("f1", 1.0, "c2s",
                samplegen.segment(MSG["HELLO"], 0, "客户端".encode()[:-1]))]
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "utf8" in kinds(result)
    assert result["sessions"]["s1"]["status"] == "recovering"


def test_unexpected_message_is_fatal_and_rejects_session():
    frames = [
        F("f1", 1.0, "c2s", samplegen.segment(MSG["DATA"], 0, b"early")),
        F("f2", 1.1, "c2s", samplegen.segment(MSG["HELLO"], 1, b"late")),
    ]
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "unexpected_message" in kinds(result)
    assert result["sessions"]["s1"]["status"] == "rejected"
    assert "ignored" in kinds(result)


def test_unknown_message_type_is_fatal():
    frames = [F("f1", 1.0, "c2s", samplegen.segment(99, 0, b"?"))]
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "unknown_message" in kinds(result)
    assert result["sessions"]["s1"]["status"] == "rejected"


def test_resync_after_garbage_byte():
    frames = [F("f1", 1.0, "c2s",
                b"\x00" + samplegen.segment(MSG["HELLO"], 0, b"n"))]
    result = engine.analyze(frames, engine.base_protocol(1))
    assert "resync" in kinds(result)
    assert result["sessions"]["s1"]["state"] == "HELLO_SENT"


def test_byte_ranges_stay_inside_real_frames():
    frames = handshake_frames()
    result = engine.analyze(frames, engine.base_protocol(1))
    sizes = {f["frame_id"]: len(f["data"]) for f in frames}
    for event in result["events"]:
        if event["byte_start"] is None or event["byte_end"] is None:
            continue
        assert 0 <= event["byte_start"] < event["byte_end"]
        assert event["byte_end"] <= sizes[event["frame_id"]]
