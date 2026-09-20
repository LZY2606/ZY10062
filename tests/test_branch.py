import pytest

from framebench.branch import (apply_ops, divergence, execute,
                               migration_report, validate_op)
from framebench.capture import build_frame
from framebench.examples import sample_records
from framebench.protocols import get_protocol


@pytest.fixture
def capture():
    return {"records": sample_records()}


def branch(ops=None, version="1.0"):
    return {"id": "b1", "protocol_version": version, "ops": ops or []}


def test_mask_frame_changes_events(capture):
    base = execute(capture, branch())
    masked = execute(capture, branch([{"op": "mask_frame", "record": 0}]))
    assert "frame_masked" in [e["kind"] for e in masked["events"]]
    assert "handshake_complete" not in [e["kind"] for e in masked["events"]]
    assert len(masked["events"]) != len(base["events"])


def test_retime_triggers_rollback_event(capture):
    run = execute(capture, branch([{"op": "retime", "record": 5, "ts": -1}]))
    assert any(e["kind"] == "time_rollback" for e in run["events"])


def test_replace_payload(capture):
    spec = get_protocol("1.0")
    new = build_frame(spec, 7, 0, b"\x06")  # RESET instead of HELLO
    run = execute(capture, branch(
        [{"op": "replace_payload", "record": 0, "data": new.hex()}]))
    kinds = [e["kind"] for e in run["events"]]
    assert "session_closed" in kinds and "handshake_complete" not in kinds


def test_stable_event_numbering_across_reruns(capture):
    b = branch([{"op": "mask_frame", "record": 6}])
    a, c = execute(capture, b), execute(capture, b)
    assert [e["id"] for e in a["events"]] == [e["id"] for e in c["events"]]
    assert [e["digest"] for e in a["events"]] == [e["digest"] for e in c["events"]]


def test_divergence_point(capture):
    base = execute(capture, branch())
    same = execute(capture, branch())
    assert divergence(base, same)["diverges"] is False
    alt = execute(capture, branch([{"op": "mask_frame", "record": 9}]))
    d = divergence(base, alt)
    assert d["diverges"] is True
    assert d["event_a"]["digest"] != (d["event_b"] or {}).get("digest")


def test_old_branch_stays_on_old_protocol(capture):
    b = branch(version="1.0")
    assert execute(capture, b)["protocol_version"] == "1.0"


def test_migration_report_v1_to_v2(capture):
    report = migration_report(capture, branch(), "2.0")
    assert report["from"] == "1.0" and report["to"] == "2.0"
    assert any("header length" in f for f in report["static_findings"])
    # v1 frames do not parse under the v2 header layout
    assert report["rejected_frames"] > 0
    assert report["compatible"] is False


def test_migration_v2_to_v2_is_clean():
    spec = get_protocol("2.0")
    records = [
        {"ts": 0, "dir": "c2s", "data": build_frame(spec, 1, 0, b"\x01").hex()},
        {"ts": 1, "dir": "s2c", "data": build_frame(spec, 1, 0, b"\x02").hex()},
    ]
    capture = {"records": records}
    report = migration_report(capture, branch(version="2.0"), "2.0")
    assert report["compatible"] is True


def test_validate_op():
    assert validate_op({"op": "mask_frame", "record": 1}) is None
    assert validate_op({"op": "nope", "record": 1}) is not None
    assert validate_op({"op": "retime", "record": 1}) is not None
    assert validate_op({"op": "retime", "record": 1, "ts": 5}) is None
    assert validate_op({"op": "replace_payload", "record": 1, "data": "zz"}) is not None
