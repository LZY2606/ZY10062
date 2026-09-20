"""Journaled, crash-safe persistence.

Every state change is appended to ``journal.log`` (flush + fsync) *before*
the in-memory state is swapped in, so a failed write leaves the previous
state fully readable.  Snapshots are written to a temp file and atomically
renamed; the previous snapshot is kept as ``.bak``.  On load, a corrupt
snapshot falls back to the backup and a torn journal tail is ignored.

Idempotency: callers may attach an idempotency key to an operation.  The
first result is recorded in the journal; a duplicate key returns the
stored result without re-applying the mutation.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path


class StoreError(Exception):
    """Persistence failed; in-memory state was left unchanged."""


class DomainError(Exception):
    """Invalid request; carries an HTTP-ish status and a report payload."""

    def __init__(self, message, status=400, report=None):
        super().__init__(message)
        self.status = status
        self.report = report or {}


class Store:
    SNAPSHOT_EVERY = 10

    def __init__(self, path, applier):
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.dir / "journal.log"
        self.snapshot_path = self.dir / "snapshot.json"
        self.backup_path = self.dir / "snapshot.json.bak"
        self._applier = applier  # fn(state, op, payload) -> result
        self.state = self._empty()
        self._since_snapshot = 0
        self._load()

    @staticmethod
    def _empty():
        return {
            "seq": 0,
            "captures": {},
            "branches": {},
            "protocols": {},
            "idempotency": {},
        }

    def _load(self):
        snapshot = None
        for candidate in (self.snapshot_path, self.backup_path):
            try:
                snapshot = json.loads(candidate.read_text("utf-8"))
                break
            except (OSError, ValueError):
                continue
        if isinstance(snapshot, dict):
            for key, value in snapshot.items():
                if key in self.state:
                    self.state[key] = value
        if self.journal_path.exists():
            with self.journal_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        break  # torn tail from an interrupted write
                    if not isinstance(record, dict):
                        break
                    if record.get("seq", 0) <= self.state["seq"]:
                        continue
                    self._applier(self.state, record["op"], record["payload"])
                    self.state["seq"] = record["seq"]
                    if record.get("idem"):
                        self.state["idempotency"][record["idem"]] = {
                            "op": record["op"], "result": record["result"]}

    def execute(self, op, payload, idem=None):
        if idem:
            hit = self.state["idempotency"].get(idem)
            if hit is not None:
                result = copy.deepcopy(hit["result"])
                result["replayed"] = True
                return result
        candidate = copy.deepcopy(self.state)
        result = self._applier(candidate, op, payload)  # may raise DomainError
        candidate["seq"] = self.state["seq"] + 1
        record = {
            "seq": candidate["seq"],
            "op": op,
            "payload": payload,
            "idem": idem,
            "result": result,
        }
        if idem:
            candidate["idempotency"][idem] = {"op": op, "result": result}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            prior_size = self.journal_path.stat().st_size
        except OSError:
            prior_size = 0
        try:
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            # Roll the journal back so a reload never replays the failed op.
            try:
                with self.journal_path.open("r+", encoding="utf-8") as fh:
                    fh.truncate(prior_size)
            except OSError:
                pass
            raise StoreError("journal write failed; state unchanged: %s" % exc)
        self.state = candidate
        self._since_snapshot += 1
        if self._since_snapshot >= self.SNAPSHOT_EVERY:
            self.snapshot()
        return copy.deepcopy(result)

    def snapshot(self):
        tmp = self.dir / "snapshot.json.tmp"
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            if self.snapshot_path.exists():
                self.backup_path.write_bytes(self.snapshot_path.read_bytes())
            os.replace(tmp, self.snapshot_path)
        except OSError as exc:
            raise StoreError(
                "snapshot failed; previous snapshot preserved: %s" % exc)
        self._since_snapshot = 0
        self._truncate_journal()

    def _truncate_journal(self):
        if not self.journal_path.exists():
            return
        kept = []
        with self.journal_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(record, dict)
                        and record.get("seq", 0) > self.state["seq"]):
                    kept.append(line if line.endswith("\n") else line + "\n")
        tmp = self.dir / "journal.log.tmp"
        with tmp.open("w", encoding="utf-8") as fh:
            fh.writelines(kept)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.journal_path)
