"""Branches, migration compatibility, export/verify, reload persistence."""
import copy

import pytest

from framebench import engine, samplegen
from framebench.app import Workbench, verify_bundle
from framebench.store import DomainError


def make_wb(tmp_path):
    wb = Workbench(tmp_path)
    wb.store.execute("create_capture", {
        "capture_id": "c1", "name": "demo", "protocol": "handshake@1"})
    wb.store.execute("add_frames", {
        "capture_id": "c1", "frames": samplegen.demo_frames()})
    return wb


def test_seed_protocols_registered(tmp_path):
    wb = Workbench(tmp_path)
    keys = [engine.protocol_key(d) for d in wb.list_protocols()]
    assert "handshake@1" in keys and "handshake@2" in keys


def test_session_statuses_are_separated(tmp_path):
    wb = make_wb(tmp_path)
    detail = wb.capture_detail("c1")
    assert detail["sessions"]["sess-1"]["status"] == "accepted"
    assert detail["sessions"]["sess-2"]["status"] == "recovering"


def test_branch_mask_frame_diverges_and_is_stable(tmp_path):
    wb = make_wb(tmp_path)
    wb.store.execute("create_branch", {
        "branch_id": "b1", "capture_id": "c1", "anchor_event": "e0000"})
    wb.store.execute("add_mutation", {
        "branch_id": "b1",
        "mutation": {"type": "mask_frame", "frame_id": "f4"}})
    diff = wb.diff_branch("b1")
    assert not diff["identical"]
    assert diff["diverges_at"]["event_id"].startswith("e")
    # re-execution is deterministic: identical event ids and digest
    again = wb.analyze_branch(wb.store.state["branches"]["b1"])
    fresh = Workbench(tmp_path)  # bypass in-memory cache entirely
    reexec = fresh.analyze_branch(fresh.store.state["branches"]["b1"])
    assert [e["id"] for e in again["events"]] == [e["id"] for e in reexec["events"]]
    assert again["digest"] == reexec["digest"]
    # the original capture is untouched by the branch
    assert len(wb.store.state["captures"]["c1"]["frames"]) == 9


def test_branch_retime_and_replace_payload(tmp_path):
    wb = make_wb(tmp_path)
    wb.store.execute("create_branch", {
        "branch_id": "b2", "capture_id": "c1", "anchor_event": None})
    wb.store.execute("add_mutation", {
        "branch_id": "b2",
        "mutation": {"type": "retime", "frame_id": "f2", "ts": 0.5}})
    detail = wb.branch_detail("b2")
    assert any(e["kind"] == "time_rollback" for e in detail["events"])
    wb.store.execute("add_mutation", {
        "branch_id": "b2",
        "mutation": {"type": "replace_payload", "frame_id": "f5",
                     "offset": 8, "data": "4141"}})
    detail = wb.branch_detail("b2")
    assert any(e["kind"] == "checksum" for e in detail["events"])


def test_mutation_out_of_range_rejected(tmp_path):
    wb = make_wb(tmp_path)
    wb.store.execute("create_branch", {
        "branch_id": "b3", "capture_id": "c1", "anchor_event": None})
    with pytest.raises(DomainError):
        wb.store.execute("add_mutation", {
            "branch_id": "b3",
            "mutation": {"type": "replace_payload", "frame_id": "f1",
                         "offset": 999, "data": "ff"}})


def test_migration_requires_compatible_protocol(tmp_path):
    wb = make_wb(tmp_path)
    wb.store.execute("create_branch", {
        "branch_id": "b4", "capture_id": "c1", "anchor_event": None})
    # handshake@2 uses a different CRC seed -> incompatible
    with pytest.raises(DomainError) as err:
        wb.store.execute("migrate_branch", {
            "branch_id": "b4", "version": "handshake@2"})
    assert err.value.status == 409
    assert err.value.report["compatible"] is False
    assert wb.store.state["branches"]["b4"]["protocol"] == "handshake@1"
    # forced migration rebinds explicitly
    wb.store.execute("migrate_branch", {
        "branch_id": "b4", "version": "handshake@2", "force": True})
    assert wb.store.state["branches"]["b4"]["protocol"] == "handshake@2"


def test_migration_compatible_when_behavior_identical(tmp_path):
    wb = make_wb(tmp_path)
    doc = engine.base_protocol(1)
    doc["version"] = 3  # same wire behaviour, new description version
    wb.store.execute("register_protocol", {"doc": doc})
    wb.store.execute("create_branch", {
        "branch_id": "b5", "capture_id": "c1", "anchor_event": None})
    result = wb.store.execute("migrate_branch", {
        "branch_id": "b5", "version": "handshake@3"})
    assert result["report"]["compatible"] is True
    assert wb.store.state["branches"]["b5"]["protocol"] == "handshake@3"


def test_export_bundle_verifies_offline(tmp_path):
    wb = make_wb(tmp_path)
    wb.store.execute("create_branch", {
        "branch_id": "b6", "capture_id": "c1", "anchor_event": "e0002"})
    wb.store.execute("add_mutation", {
        "branch_id": "b6",
        "mutation": {"type": "mask_frame", "frame_id": "g3"}})
    bundle = wb.export_bundle("c1")
    report = verify_bundle(bundle)
    assert report["ok"], report
    assert len(report["checks"]) == 2


def test_export_bundle_detects_tampering(tmp_path):
    wb = make_wb(tmp_path)
    bundle = wb.export_bundle("c1")
    tampered = copy.deepcopy(bundle)
    tampered["frames"][0]["data"] = "00" + tampered["frames"][0]["data"][2:]
    report = verify_bundle(tampered)
    assert not report["ok"]


def test_state_survives_reload(tmp_path):
    make_wb(tmp_path)
    fresh = Workbench(tmp_path)
    captures = fresh.list_captures()
    assert len(captures) == 1
    assert captures[0]["frames"] == 9
    assert captures[0]["statuses"]["accepted"] == 1
    assert captures[0]["statuses"]["recovering"] == 1
