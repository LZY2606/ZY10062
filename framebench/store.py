"""SQLite persistence with journaled writes, crash recovery and idempotency.

Write protocol (every state change):
  1. commit an intent row into `journal` (status='pending');
  2. apply the data change in its own transaction;
  3. mark the intent 'done'.

If the process dies between steps, `recover()` reconciles: intents whose
change never landed are discarded (old state remains fully readable);
intents whose change did land are marked done. Journal rows are never
deleted, so every state change stays traceable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL,
    payload TEXT NOT NULL,
    verify TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    records TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL REFERENCES captures(id),
    name TEXT NOT NULL,
    parent_id TEXT,
    protocol_version TEXT NOT NULL,
    ops TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    branch_id TEXT PRIMARY KEY REFERENCES branches(id),
    ops_len INTEGER NOT NULL,
    result TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response TEXT NOT NULL,
    created REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def _locked(fn):
        def wrapper(self, *args, **kwargs):
            with self._lock:
                return fn(self, *args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper

    # -- journal -----------------------------------------------------------

    def _journal_begin(self, op: str, payload: dict, verify: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO journal(op, payload, verify, status, created)"
            " VALUES (?,?,?, 'pending', ?)",
            (op, json.dumps(payload), json.dumps(verify), time.time()),
        )
        self.db.commit()
        return cur.lastrowid

    def _journal_done(self, seq: int):
        self.db.execute("UPDATE journal SET status='done' WHERE seq=?", (seq,))
        self.db.commit()

    @_locked
    def recover(self) -> dict:
        """Reconcile dangling intents after an interrupted write."""
        reconciled = []
        for row in self.db.execute("SELECT * FROM journal WHERE status='pending'"):
            verify = json.loads(row["verify"])
            landed = self.db.execute(
                f"SELECT 1 FROM {verify['table']} WHERE id=?",  # noqa: S608 - table name is internal
                (verify["id"],),
            ).fetchone()
            new_status = "done" if landed else "discarded"
            self.db.execute("UPDATE journal SET status=? WHERE seq=?",
                            (new_status, row["seq"]))
            reconciled.append({"seq": row["seq"], "op": row["op"], "resolution": new_status})
        self.db.commit()
        return {"reconciled": reconciled}

    @_locked
    def journal(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT seq, op, status, created FROM journal ORDER BY seq")]

    # -- idempotency -------------------------------------------------------

    @_locked
    def idempotent(self, key: str | None, endpoint: str, fn):
        """Run fn once per idempotency key; replays return the first result."""
        if not key:
            status, body = fn()
            return status, body
        row = self.db.execute(
            "SELECT status_code, response FROM idempotency WHERE key=?", (key,)
        ).fetchone()
        if row:
            return row["status_code"], json.loads(row["response"])
        status, body = fn()
        try:
            self.db.execute(
                "INSERT INTO idempotency(key, endpoint, status_code, response, created)"
                " VALUES (?,?,?,?,?)",
                (key, endpoint, status, json.dumps(body), time.time()),
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            row = self.db.execute(
                "SELECT status_code, response FROM idempotency WHERE key=?", (key,)
            ).fetchone()
            return row["status_code"], json.loads(row["response"])
        return status, body

    # -- captures ----------------------------------------------------------

    @_locked
    def add_capture(self, name: str, records: list[dict]) -> dict:
        cid = uuid.uuid4().hex[:12]
        seq = self._journal_begin(
            "add_capture", {"id": cid, "name": name},
            {"table": "captures", "id": cid})
        self.db.execute("INSERT INTO captures VALUES (?,?,?,?)",
                        (cid, name, json.dumps(records), time.time()))
        self.db.commit()
        self._journal_done(seq)
        return {"id": cid, "name": name, "frames": len(records)}

    @_locked
    def list_captures(self) -> list[dict]:
        return [
            {"id": r["id"], "name": r["name"],
             "frames": len(json.loads(r["records"])), "created": r["created"]}
            for r in self.db.execute("SELECT * FROM captures ORDER BY created")
        ]

    @_locked
    def get_capture(self, cid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM captures WHERE id=?", (cid,)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "name": r["name"],
                "records": json.loads(r["records"]), "created": r["created"]}

    # -- branches ----------------------------------------------------------

    @_locked
    def create_branch(self, capture_id: str, name: str, protocol_version: str,
                      parent_id: str | None = None, ops: list | None = None) -> dict:
        bid = uuid.uuid4().hex[:12]
        ops = ops or []
        seq = self._journal_begin(
            "create_branch", {"id": bid, "capture_id": capture_id, "name": name},
            {"table": "branches", "id": bid})
        self.db.execute(
            "INSERT INTO branches VALUES (?,?,?,?,?,?,?)",
            (bid, capture_id, name, parent_id, protocol_version,
             json.dumps(ops), time.time()))
        self.db.commit()
        self._journal_done(seq)
        return self.get_branch(bid)

    @_locked
    def apply_op(self, branch_id: str, op: dict) -> dict | None:
        branch = self.get_branch(branch_id)
        if branch is None:
            return None
        ops = branch["ops"] + [op]
        seq = self._journal_begin(
            "apply_op", {"branch_id": branch_id, "op": op},
            {"table": "branches", "id": branch_id})
        self.db.execute("UPDATE branches SET ops=? WHERE id=?",
                        (json.dumps(ops), branch_id))
        self.db.execute("DELETE FROM runs WHERE branch_id=?", (branch_id,))
        self.db.commit()
        self._journal_done(seq)
        return self.get_branch(branch_id)

    @_locked
    def migrate_branch(self, branch_id: str, to_version: str) -> dict | None:
        branch = self.get_branch(branch_id)
        if branch is None:
            return None
        seq = self._journal_begin(
            "migrate_branch", {"branch_id": branch_id, "to": to_version},
            {"table": "branches", "id": branch_id})
        self.db.execute("UPDATE branches SET protocol_version=? WHERE id=?",
                        (to_version, branch_id))
        self.db.execute("DELETE FROM runs WHERE branch_id=?", (branch_id,))
        self.db.commit()
        self._journal_done(seq)
        return self.get_branch(branch_id)

    @_locked
    def get_branch(self, bid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM branches WHERE id=?", (bid,)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "capture_id": r["capture_id"], "name": r["name"],
                "parent_id": r["parent_id"], "protocol_version": r["protocol_version"],
                "ops": json.loads(r["ops"]), "created": r["created"]}

    @_locked
    def list_branches(self, capture_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM branches"
        args: tuple = ()
        if capture_id:
            sql += " WHERE capture_id=?"
            args = (capture_id,)
        return [{**self.get_branch(r["id"])} for r in self.db.execute(sql + " ORDER BY created", args)]

    # -- runs (cached analysis results) -------------------------------------

    @_locked
    def save_run(self, branch_id: str, ops_len: int, result: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
            (branch_id, ops_len, json.dumps(result), time.time()))
        self.db.commit()

    @_locked
    def get_run(self, branch_id: str, ops_len: int) -> dict | None:
        r = self.db.execute(
            "SELECT result FROM runs WHERE branch_id=? AND ops_len=?",
            (branch_id, ops_len)).fetchone()
        return json.loads(r["result"]) if r else None
