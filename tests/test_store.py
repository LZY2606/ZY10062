"""Persistence: journaling, crash recovery, idempotency, failure paths."""
import json

import pytest

from framebench.store import DomainError, Store, StoreError


def applier(state, op, payload):
    if op == "put":
        state["captures"][payload["key"]] = payload["value"]
        return {"stored": payload["key"]}
    raise DomainError("unknown op")


def make_store(path):
    return Store(path, applier)


def test_roundtrip_through_journal(tmp_path):
    store = make_store(tmp_path)
    store.execute("put", {"key": "a", "value": 1})
    store.execute("put", {"key": "b", "value": 2})
    reloaded = make_store(tmp_path)
    assert reloaded.state["captures"] == {"a": 1, "b": 2}
    assert reloaded.state["seq"] == 2


def test_torn_journal_tail_is_ignored(tmp_path):
    store = make_store(tmp_path)
    store.execute("put", {"key": "a", "value": 1})
    with open(tmp_path / "journal.log", "a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "op": "put", "paylo')  # interrupted write
    reloaded = make_store(tmp_path)
    assert reloaded.state["captures"] == {"a": 1}


def test_corrupt_snapshot_falls_back_to_backup(tmp_path):
    store = make_store(tmp_path)
    store.execute("put", {"key": "a", "value": 1})
    store.snapshot()
    store.execute("put", {"key": "b", "value": 2})
    store.snapshot()
    # simulate an interrupted snapshot write
    (tmp_path / "snapshot.json").write_text('{"seq": 2, "captures": {"a"', "utf-8")
    reloaded = make_store(tmp_path)
    assert "a" in reloaded.state["captures"]


def test_snapshot_then_journal_replay_combines(tmp_path):
    store = make_store(tmp_path)
    store.execute("put", {"key": "a", "value": 1})
    store.snapshot()
    store.execute("put", {"key": "b", "value": 2})
    reloaded = make_store(tmp_path)
    assert reloaded.state["captures"] == {"a": 1, "b": 2}


def test_idempotent_replay_returns_first_result(tmp_path):
    store = make_store(tmp_path)
    first = store.execute("put", {"key": "a", "value": 1}, idem="key-1")
    second = store.execute("put", {"key": "a", "value": 999}, idem="key-1")
    assert first == {"stored": "a"}
    assert second["replayed"] is True
    assert store.state["captures"]["a"] == 1
    # idempotency survives a restart
    reloaded = make_store(tmp_path)
    third = reloaded.execute("put", {"key": "a", "value": 5}, idem="key-1")
    assert third["replayed"] is True
    assert reloaded.state["captures"]["a"] == 1


def test_failed_write_preserves_readable_state(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.execute("put", {"key": "a", "value": 1})

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", boom)
    with pytest.raises(StoreError):
        store.execute("put", {"key": "b", "value": 2})
    # old state is still fully readable in memory
    assert store.state["captures"] == {"a": 1}
    monkeypatch.undo()
    # and on disk: the failed op never became visible
    reloaded = make_store(tmp_path)
    assert reloaded.state["captures"] == {"a": 1}


def test_domain_error_does_not_write_journal(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(DomainError):
        store.execute("nope", {})
    assert store.state["seq"] == 0
    assert not (tmp_path / "journal.log").exists()
