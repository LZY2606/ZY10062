import json

import pytest

from framebench.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_idempotency_returns_first_result(store):
    calls = []

    def fn():
        calls.append(1)
        return 201, {"id": "abc"}

    s1, b1 = store.idempotent("key-1", "POST /x", fn)
    s2, b2 = store.idempotent("key-1", "POST /x", fn)
    assert (s1, b1) == (s2, b2) == (201, {"id": "abc"})
    assert len(calls) == 1  # side effect ran once


def test_journal_records_every_state_change(store):
    cap = store.add_capture("c1", [{"ts": 0, "dir": "c2s", "data": "00"}])
    branch = store.create_branch(cap["id"], "trunk", "1.0")
    store.apply_op(branch["id"], {"op": "mask_frame", "record": 0})
    ops = [j["op"] for j in store.journal()]
    assert ops == ["add_capture", "create_branch", "apply_op"]
    assert all(j["status"] == "done" for j in store.journal())


def test_interrupted_write_old_state_readable(store):
    cap = store.add_capture("c1", [{"ts": 0, "dir": "c2s", "data": "00"}])
    # simulate a crash: intent committed, data write never happened
    seq = store._journal_begin(
        "add_capture", {"id": "ghost"}, {"table": "captures", "id": "ghost"})
    store.db.close()  # process "dies" before the data transaction

    reopened = Store(store.path)
    assert [c["name"] for c in reopened.list_captures()] == ["c1"]
    report = reopened.recover()
    assert report["reconciled"] == [
        {"seq": seq, "op": "add_capture", "resolution": "discarded"}]
    assert reopened.get_capture("ghost") is None
    assert [c["name"] for c in reopened.list_captures()] == ["c1"]
    reopened.close()


def test_recovery_marks_landed_intent_done(store):
    cap = store.add_capture("c1", [{"ts": 0, "dir": "c2s", "data": "00"}])
    # crash after data commit but before journal done-mark
    seq = store._journal_begin(
        "add_capture", {"id": "c2"}, {"table": "captures", "id": "c2"})
    store.db.execute(
        "INSERT INTO captures VALUES (?,?,?,?)",
        ("c2", "c2", json.dumps([]), 0.0))
    store.db.commit()
    report = store.recover()
    assert report["reconciled"] == [
        {"seq": seq, "op": "add_capture", "resolution": "done"}]
    assert store.get_capture("c2") is not None


def test_branch_ops_append_and_run_invalidation(store):
    cap = store.add_capture("c1", [{"ts": 0, "dir": "c2s", "data": "00"}])
    b = store.create_branch(cap["id"], "trunk", "1.0")
    store.save_run(b["id"], 0, {"events": []})
    b = store.apply_op(b["id"], {"op": "mask_frame", "record": 0})
    assert b["ops"] == [{"op": "mask_frame", "record": 0}]
    assert store.get_run(b["id"], 1) is None  # stale cache dropped
