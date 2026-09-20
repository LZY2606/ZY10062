"""Domain layer: captures, branches, migration, export."""
from __future__ import annotations

from . import engine
from .store import DomainError, Store

DIRECTIONS = ("c2s", "s2c")


def normalize_frame(raw):
    if not isinstance(raw, dict):
        raise DomainError("frame must be an object")
    try:
        fid = str(raw["id"])
        session = str(raw["session"])
        ts = float(raw["ts"])
        direction = str(raw["dir"])
        data_hex = str(raw["data"])
    except (KeyError, TypeError, ValueError):
        raise DomainError("frame needs id/session/ts/dir/data")
    if direction not in DIRECTIONS:
        raise DomainError("dir must be one of %s" % (DIRECTIONS,))
    if len(data_hex) % 2:
        raise DomainError("frame %s: odd-length hex data" % fid)
    try:
        bytes.fromhex(data_hex)
    except ValueError:
        raise DomainError("frame %s: data is not valid hex" % fid)
    return {"id": fid, "session": session, "ts": ts, "dir": direction,
            "data": data_hex.lower()}


def to_engine_frame(stored):
    return {
        "frame_id": stored["id"],
        "session": stored["session"],
        "ts": stored["ts"],
        "direction": stored["dir"],
        "data": bytes.fromhex(stored["data"]),
        "arrival": stored["arrival"],
    }


def apply_mutations(frames, mutations):
    """Return a mutated copy of stored frames; originals are untouched."""
    out = [dict(f) for f in frames]
    masked = set()
    for mutation in mutations:
        kind = mutation["type"]
        fid = mutation["frame_id"]
        if kind == "mask_frame":
            masked.add(fid)
        elif kind == "retime":
            for frame in out:
                if frame["id"] == fid:
                    frame["ts"] = float(mutation["ts"])
        elif kind == "replace_payload":
            patch = bytes.fromhex(mutation["data"])
            offset = int(mutation["offset"])
            for frame in out:
                if frame["id"] == fid:
                    data = bytearray.fromhex(frame["data"])
                    data[offset:offset + len(patch)] = patch
                    frame["data"] = data.hex()
    return [f for f in out if f["id"] not in masked]


def validate_mutation(mutation, frames):
    if not isinstance(mutation, dict) or "type" not in mutation:
        raise DomainError("mutation needs a type")
    kind = mutation["type"]
    fid = mutation.get("frame_id")
    by_id = {f["id"]: f for f in frames}
    if fid not in by_id:
        raise DomainError("unknown frame %r" % fid, 404)
    if kind == "mask_frame":
        return {"type": kind, "frame_id": fid}
    if kind == "retime":
        try:
            ts = float(mutation["ts"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("retime needs a numeric ts")
        return {"type": kind, "frame_id": fid, "ts": ts}
    if kind == "replace_payload":
        try:
            offset = int(mutation["offset"])
            patch = bytes.fromhex(str(mutation["data"]))
        except (KeyError, TypeError, ValueError):
            raise DomainError("replace_payload needs offset and hex data")
        size = len(bytes.fromhex(by_id[fid]["data"]))
        if offset < 0 or not patch or offset + len(patch) > size:
            raise DomainError(
                "replace_payload range [%d, %d) outside frame of %d bytes"
                % (offset, offset + len(patch), size))
        return {"type": kind, "frame_id": fid, "offset": offset,
                "data": patch.hex()}
    raise DomainError("unknown mutation type %r" % kind)


class Workbench:
    def __init__(self, data_dir):
        self.store = Store(data_dir, self._apply)
        for doc in (engine.base_protocol(1), engine.base_protocol(2)):
            key = engine.protocol_key(doc)
            if key not in self.store.state["protocols"]:
                self.store.execute("register_protocol", {"doc": doc})
        self._cache = {}

    def _apply(self, state, op, payload):
        handler = getattr(self, "_op_" + op, None)
        if handler is None:
            raise DomainError("unknown op %r" % op, 400)
        return handler(state, payload)

    def _op_register_protocol(self, state, payload):
        doc = payload["doc"]
        for field in ("name", "version", "msg_types", "transitions"):
            if field not in doc:
                raise DomainError("protocol doc missing %r" % field)
        key = engine.protocol_key(doc)
        if key in state["protocols"]:
            raise DomainError("protocol %s already registered" % key, 409)
        state["protocols"][key] = doc
        return {"protocol": key}

    def _op_create_capture(self, state, payload):
        cid = str(payload["capture_id"])
        if cid in state["captures"]:
            raise DomainError("capture %s already exists" % cid, 409)
        proto = str(payload["protocol"])
        if proto not in state["protocols"]:
            raise DomainError("unknown protocol %s" % proto, 404)
        state["captures"][cid] = {
            "id": cid,
            "name": str(payload.get("name") or cid),
            "protocol": proto,
            "frames": [],
            "next_arrival": 0,
        }
        return {"capture_id": cid, "protocol": proto}

    def _op_add_frames(self, state, payload):
        cap = state["captures"].get(str(payload["capture_id"]))
        if cap is None:
            raise DomainError("unknown capture", 404)
        accepted = []
        for raw in payload["frames"]:
            frame = normalize_frame(raw)
            frame["arrival"] = cap["next_arrival"]
            cap["next_arrival"] += 1
            cap["frames"].append(frame)
            accepted.append(frame["id"])
        return {"capture_id": cap["id"], "accepted": len(accepted),
                "frame_ids": accepted}

    def _op_create_branch(self, state, payload):
        cap = state["captures"].get(str(payload["capture_id"]))
        if cap is None:
            raise DomainError("unknown capture", 404)
        bid = str(payload["branch_id"])
        if bid in state["branches"]:
            raise DomainError("branch %s already exists" % bid, 409)
        state["branches"][bid] = {
            "id": bid,
            "capture_id": cap["id"],
            "name": str(payload.get("name") or bid),
            "anchor_event": payload.get("anchor_event"),
            "protocol": cap["protocol"],
            "mutations": [],
        }
        return {"branch_id": bid, "protocol": cap["protocol"]}

    def _op_add_mutation(self, state, payload):
        branch = state["branches"].get(str(payload["branch_id"]))
        if branch is None:
            raise DomainError("unknown branch", 404)
        cap = state["captures"][branch["capture_id"]]
        mutation = validate_mutation(payload["mutation"], cap["frames"])
        branch["mutations"].append(mutation)
        return {"branch_id": branch["id"],
                "mutation_count": len(branch["mutations"])}

    def _op_migrate_branch(self, state, payload):
        branch = state["branches"].get(str(payload["branch_id"]))
        if branch is None:
            raise DomainError("unknown branch", 404)
        target = str(payload["version"])
        if target not in state["protocols"]:
            raise DomainError("unknown protocol %s" % target, 404)
        report = self._compat_report(state, branch, target)
        if not report["compatible"] and not payload.get("force"):
            raise DomainError("migration is not compatible", 409, report)
        branch["protocol"] = target
        return {"branch_id": branch["id"], "protocol": target,
                "report": report}

    # ------------------------------------------------------------ queries
    def _protocol(self, state, key):
        doc = state["protocols"].get(key)
        if doc is None:
            raise DomainError("unknown protocol %s" % key, 404)
        return doc

    def _analyze(self, cache_key, frames, doc):
        hit = self._cache.get(cache_key)
        if hit is not None:
            return hit
        result = engine.analyze([to_engine_frame(f) for f in frames], doc)
        self._cache[cache_key] = result
        return result

    def analyze_capture(self, cap):
        state = self.store.state
        doc = self._protocol(state, cap["protocol"])
        key = ("cap", cap["id"], cap["next_arrival"], cap["protocol"])
        return self._analyze(key, cap["frames"], doc)

    def analyze_branch(self, branch):
        state = self.store.state
        cap = state["captures"][branch["capture_id"]]
        doc = self._protocol(state, branch["protocol"])
        frames = apply_mutations(cap["frames"], branch["mutations"])
        key = ("br", branch["id"], cap["next_arrival"],
               len(branch["mutations"]), branch["protocol"])
        return self._analyze(key, frames, doc)

    def list_protocols(self):
        return sorted(self.store.state["protocols"].values(),
                      key=lambda d: (d["name"], d["version"]))

    def list_captures(self):
        out = []
        for cap in self.store.state["captures"].values():
            analysis = self.analyze_capture(cap)
            statuses = {}
            for sess in analysis["sessions"].values():
                statuses[sess["status"]] = statuses.get(sess["status"], 0) + 1
            out.append({
                "id": cap["id"],
                "name": cap["name"],
                "protocol": cap["protocol"],
                "frames": len(cap["frames"]),
                "events": len(analysis["events"]),
                "digest": analysis["digest"],
                "statuses": statuses,
            })
        return out

    def capture_detail(self, cid):
        cap = self.store.state["captures"].get(cid)
        if cap is None:
            raise DomainError("unknown capture", 404)
        analysis = self.analyze_capture(cap)
        return {
            "capture": {k: cap[k] for k in ("id", "name", "protocol")},
            "frames": cap["frames"],
            "events": analysis["events"],
            "sessions": analysis["sessions"],
            "digest": analysis["digest"],
            "branches": [b for b in self.store.state["branches"].values()
                         if b["capture_id"] == cid],
        }

    def branch_detail(self, bid):
        branch = self.store.state["branches"].get(bid)
        if branch is None:
            raise DomainError("unknown branch", 404)
        analysis = self.analyze_branch(branch)
        return {
            "branch": branch,
            "events": analysis["events"],
            "sessions": analysis["sessions"],
            "digest": analysis["digest"],
        }

    def diff_branch(self, bid):
        branch = self.store.state["branches"].get(bid)
        if branch is None:
            raise DomainError("unknown branch", 404)
        cap = self.store.state["captures"][branch["capture_id"]]
        base = self.analyze_capture(cap)
        altered = self.analyze_branch(branch)
        divergence = engine.first_divergence(base["events"], altered["events"])
        return {
            "branch_id": bid,
            "identical": divergence is None,
            "diverges_at": divergence,
            "base_digest": base["digest"],
            "branch_digest": altered["digest"],
        }

    def _compat_report(self, state, branch, target):
        cap = state["captures"][branch["capture_id"]]
        frames = apply_mutations(cap["frames"], branch["mutations"])
        old = engine.analyze([to_engine_frame(f) for f in frames],
                             self._protocol(state, branch["protocol"]))
        new = engine.analyze([to_engine_frame(f) for f in frames],
                             self._protocol(state, target))
        divergence = engine.first_divergence(old["events"], new["events"])
        return {
            "from": branch["protocol"],
            "to": target,
            "compatible": old["digest"] == new["digest"],
            "old_digest": old["digest"],
            "new_digest": new["digest"],
            "first_divergence": divergence,
        }

    # ------------------------------------------------------------- export
    def export_bundle(self, cid):
        state = self.store.state
        cap = state["captures"].get(cid)
        if cap is None:
            raise DomainError("unknown capture", 404)
        analysis = self.analyze_capture(cap)
        branches = []
        for branch in state["branches"].values():
            if branch["capture_id"] != cid:
                continue
            b_analysis = self.analyze_branch(branch)
            branches.append({
                "id": branch["id"],
                "name": branch["name"],
                "anchor_event": branch["anchor_event"],
                "protocol": branch["protocol"],
                "protocol_doc": self._protocol(state, branch["protocol"]),
                "mutations": branch["mutations"],
                "events": b_analysis["events"],
                "sessions": b_analysis["sessions"],
                "digest": b_analysis["digest"],
            })
        return {
            "format": "framebench-export/1",
            "capture": {"id": cap["id"], "name": cap["name"],
                        "protocol": cap["protocol"]},
            "protocol": self._protocol(state, cap["protocol"]),
            "frames": cap["frames"],
            "events": analysis["events"],
            "sessions": analysis["sessions"],
            "digest": analysis["digest"],
            "branches": branches,
        }


def verify_bundle(bundle):
    """Offline replay: re-analyze the bundle and verify every digest."""
    if bundle.get("format") != "framebench-export/1":
        return {"ok": False, "error": "unsupported bundle format"}
    checks = []

    def check(label, frames, doc, expected_digest, expected_events):
        analysis = engine.analyze([to_engine_frame(f) for f in frames], doc)
        checks.append({
            "label": label,
            "digest_ok": analysis["digest"] == expected_digest,
            "events_ok": analysis["events"] == expected_events,
            "expected": expected_digest,
            "actual": analysis["digest"],
        })

    check("capture:" + bundle["capture"]["id"], bundle["frames"],
          bundle["protocol"], bundle["digest"], bundle["events"])
    for branch in bundle.get("branches", []):
        frames = apply_mutations(bundle["frames"], branch["mutations"])
        check("branch:" + branch["id"], frames, branch["protocol_doc"],
              branch["digest"], branch["events"])
    ok = all(c["digest_ok"] and c["events_ok"] for c in checks)
    return {"ok": ok, "checks": checks}
