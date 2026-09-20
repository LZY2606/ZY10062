"""Deterministic capture analyzer.

Reassembles fragments per session key, verifies checksums, drives the
handshake state machine and emits an ordered event stream. Every event
carries byte ranges pointing back into the original capture records.

Rules that may never be broken:
  * bytes that are not present in the capture are never synthesized;
    gaps are reported, not filled;
  * recoverable problems (duplicates, reordering, time rollback, partial
    UTF-8) are warnings; terminal problems (bad checksum, malformed
    frame, gap, conflicting retransmission) are errors that reject the
    affected frame without aborting the whole analysis;
  * the same input always yields the same event ids.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field

from .protocols import FLAG_FRAG

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "fatal": 3}

# Frame statuses surfaced as separate columns in the UI.
STATUS_PENDING = "pending"        # fragment held, awaiting completion
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_RECOVERING = "recovering"  # duplicate / out-of-order / repaired


@dataclass
class Record:
    """One captured frame after overlay ops have been applied."""

    id: int
    ts: float
    dir: str
    data: bytes
    masked: bool = False


@dataclass
class FrameResult:
    record_id: int
    status: str
    parsed: dict = field(default_factory=dict)
    reason: str = ""


def _event(kind: str, severity: str, detail: dict, byte_ranges: list[dict]) -> dict:
    return {
        "kind": kind,
        "severity": severity,
        "detail": detail,
        "byte_ranges": byte_ranges,
    }


def _finalize_events(events: list[dict]) -> list[dict]:
    for idx, ev in enumerate(events, start=1):
        ev["id"] = f"evt-{idx:04d}"
        payload = json.dumps(
            {k: ev[k] for k in ("kind", "severity", "detail", "byte_ranges")},
            sort_keys=True,
        ).encode()
        ev["digest"] = hashlib.sha256(payload).hexdigest()[:16]
    return events


def _crc_ok(spec: dict, data: bytes) -> bool:
    trailer = spec["checksum"]["offset_from_end"]
    body, expect = data[:-trailer], data[-trailer:]
    got = zlib.crc32(body) & 0xFFFFFFFF
    return got == int.from_bytes(expect, "big")


def _parse_header(spec: dict, data: bytes) -> dict | None:
    if len(data) < spec["header_len"] + spec["checksum"]["offset_from_end"]:
        return None
    f = spec["fields"]
    magic = data[f["magic"][0] : f["magic"][0] + f["magic"][1]].hex()
    if magic != spec["magic"]:
        return None
    plen_off, plen_len = f["plen"]
    plen = int.from_bytes(data[plen_off : plen_off + plen_len], "big")
    expect_total = spec["header_len"] + plen + spec["checksum"]["offset_from_end"]
    if len(data) != expect_total:
        return None
    out = {"plen": plen}
    for name, (off, ln) in f.items():
        if name in ("magic", "plen"):
            continue
        out[name] = int.from_bytes(data[off : off + ln], "big")
    out["payload"] = data[spec["header_len"] : spec["header_len"] + plen]
    return out


def _utf8_check(payload: bytes) -> tuple[str, int, bool]:
    """Decode payload; report (text, valid_prefix_len, truncated)."""
    try:
        return payload.decode("utf-8"), len(payload), False
    except UnicodeDecodeError as exc:
        valid = payload[: exc.start]
        return valid.decode("utf-8", "replace"), exc.start, True


def analyze(records: list[Record], spec: dict) -> dict:
    events: list[dict] = []
    frames: list[FrameResult] = []
    prev_ts: float | None = None

    seen: dict[tuple, bytes] = {}          # (session, dir, seq) -> raw bytes
    next_seq: dict[tuple, int] = {}        # (session, dir) -> expected seq
    frag_buf: dict[tuple, list[Record]] = {}  # (session, dir) -> held fragments
    frag_status: dict[int, str] = {}       # record_id -> pending/rejected
    states: dict[int, str] = {}            # session -> state
    transitions = spec["transitions"]

    def rng(rec: Record, start: int = 0, end: int | None = None) -> list[dict]:
        return [{"record": rec.id, "start": start, "end": len(rec.data) if end is None else end}]

    def state_of(session: int) -> str:
        return states.get(session, "INIT")

    def deliver_message(recs: list[Record], session: int, direction: str, payload: bytes):
        byte_ranges = [r for rec in recs for r in rng(rec)]
        if not payload:
            events.append(_event("empty_message", "error",
                                 {"session": session, "dir": direction},
                                 byte_ranges))
            return
        mtype = payload[0]
        mname = spec["msg_types"].get(mtype)
        if mname is None:
            events.append(_event("unknown_message", "error",
                                 {"session": session, "dir": direction,
                                  "type_code": mtype}, byte_ranges))
            return
        if mname in spec["text_msg_types"] and len(payload) > 1:
            text, valid_len, truncated = _utf8_check(payload[1:])
            if truncated:
                events.append(_event(
                    "utf8_truncated", "warning",
                    {"session": session, "dir": direction,
                     "valid_prefix_bytes": valid_len,
                     "declared_text_prefix": text},
                    byte_ranges))
        cur = state_of(session)
        match = next(
            (t for t in transitions
             if t["msg"] == mname
             and t["from"] in ("*", cur)
             and t["dir"] in ("*", direction)),
            None,
        )
        if match is None:
            events.append(_event(
                "unexpected_message", "warning",
                {"session": session, "dir": direction, "msg": mname,
                 "state": cur, "note": "message ignored, state unchanged"},
                byte_ranges))
            return
        new_state = match["to"]
        states[session] = new_state
        events.append(_event(
            "state_transition", "info",
            {"session": session, "dir": direction, "msg": mname,
             "from": cur, "to": new_state},
            byte_ranges))
        if new_state == "ESTABLISHED" and cur != "ESTABLISHED":
            events.append(_event("handshake_complete", "info",
                                 {"session": session}, byte_ranges))
        if new_state == "CLOSED":
            events.append(_event("session_closed", "info",
                                 {"session": session, "via": mname}, byte_ranges))

    for rec in records:
        if rec.masked:
            frames.append(FrameResult(rec.id, STATUS_REJECTED, reason="masked by branch op"))
            events.append(_event("frame_masked", "info",
                                 {"record": rec.id}, rng(rec)))
            continue
        if prev_ts is not None and rec.ts < prev_ts:
            events.append(_event("time_rollback", "warning",
                                 {"record": rec.id, "ts": rec.ts,
                                  "previous_ts": prev_ts}, rng(rec)))
        prev_ts = rec.ts

        parsed = _parse_header(spec, rec.data)
        if parsed is None:
            frames.append(FrameResult(rec.id, STATUS_REJECTED, reason="malformed frame"))
            events.append(_event("malformed_frame", "error",
                                 {"record": rec.id, "length": len(rec.data)},
                                 rng(rec)))
            continue
        if not _crc_ok(spec, rec.data):
            frames.append(FrameResult(rec.id, STATUS_REJECTED,
                                      parsed={k: v for k, v in parsed.items() if k != "payload"},
                                      reason="checksum mismatch"))
            events.append(_event("checksum_mismatch", "error",
                                 {"record": rec.id, "session": parsed["session"],
                                  "seq": parsed["seq"]}, rng(rec)))
            continue

        session, direction, seq = parsed["session"], rec.dir, parsed["seq"]
        key = (session, direction, seq)
        order_key = (session, direction)

        if key in seen:
            status = STATUS_RECOVERING
            if seen[key] == rec.data:
                kind, sev, reason = "duplicate_frame", "warning", "exact duplicate ignored"
            else:
                kind, sev, reason = "conflicting_frame", "error", "same seq, different bytes; frame rejected"
                status = STATUS_REJECTED
            frames.append(FrameResult(rec.id, status,
                                      parsed={k: v for k, v in parsed.items() if k != "payload"},
                                      reason=reason))
            events.append(_event(kind, sev,
                                 {"record": rec.id, "session": session,
                                  "dir": direction, "seq": seq}, rng(rec)))
            continue
        seen[key] = rec.data

        expected = next_seq.get(order_key, seq)
        if seq > expected and order_key in next_seq:
            frames.append(FrameResult(rec.id, STATUS_RECOVERING,
                                      parsed={k: v for k, v in parsed.items() if k != "payload"},
                                      reason=f"gap: expected seq {expected}"))
            events.append(_event("gap_detected", "error",
                                 {"record": rec.id, "session": session,
                                  "dir": direction, "expected_seq": expected,
                                  "got_seq": seq,
                                  "missing": seq - expected},
                                 rng(rec)))
        elif seq < expected:
            frames.append(FrameResult(rec.id, STATUS_RECOVERING,
                                      parsed={k: v for k, v in parsed.items() if k != "payload"},
                                      reason="out-of-order delivery"))
            events.append(_event("out_of_order", "warning",
                                 {"record": rec.id, "session": session,
                                  "dir": direction, "seq": seq,
                                  "expected_seq": expected}, rng(rec)))
        else:
            frames.append(FrameResult(rec.id, STATUS_ACCEPTED,
                                      parsed={k: v for k, v in parsed.items() if k != "payload"}))
        next_seq[order_key] = max(expected, seq) + 1

        if parsed["flags"] & FLAG_FRAG:
            frag_buf.setdefault(order_key, []).append(rec)
            frag_status[rec.id] = STATUS_PENDING
            events.append(_event("fragment_held", "info",
                                 {"record": rec.id, "session": session,
                                  "dir": direction, "seq": seq,
                                  "fragments_held": len(frag_buf[order_key])},
                                 rng(rec)))
            continue

        held = frag_buf.pop(order_key, [])
        if held:
            chain = held + [rec]
            for r in held:
                frag_status[r.id] = STATUS_ACCEPTED
            payload = b"".join(
                _parse_header(spec, r.data)["payload"] for r in chain)
            events.append(_event("reassembled", "info",
                                 {"session": session, "dir": direction,
                                  "fragments": [r.id for r in chain],
                                  "payload_bytes": len(payload)},
                                 [x for r in chain for x in rng(r)]))
            deliver_message(chain, session, direction, payload)
        else:
            deliver_message([rec], session, direction, parsed["payload"])

    # Fragments still held at end of capture: never completed.
    for order_key, held in frag_buf.items():
        for rec in held:
            frag_status[rec.id] = STATUS_REJECTED
        events.append(_event("fragment_abandoned", "error",
                             {"session": order_key[0], "dir": order_key[1],
                              "fragments": [r.id for r in held],
                              "note": "capture ended before final fragment; "
                                      "no bytes synthesized"},
                             [x for r in held for x in rng(r)]))

    for fr in frames:
        if fr.record_id in frag_status:
            fr.status = frag_status[fr.record_id]

    return {
        "events": _finalize_events(events),
        "frames": [
            {"record": f.record_id, "status": f.status,
             "reason": f.reason, "parsed": f.parsed}
            for f in frames
        ],
        "sessions": {str(s): st for s, st in states.items()},
    }
