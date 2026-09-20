"""Branch overlay ops, deterministic re-execution and divergence.

A branch never mutates its capture: it is an ordered list of overlay ops
(mask_frame / retime / replace_payload) applied on top of the captured
records. Re-execution is a pure function of (capture, protocol version,
ops), so event numbering is stable across runs.
"""

from __future__ import annotations

from .analyzer import Record, analyze
from .capture import to_records
from .protocols import check_migration, get_protocol

OP_KINDS = ("mask_frame", "retime", "replace_payload")


def validate_op(op: dict) -> str | None:
    kind = op.get("op")
    if kind not in OP_KINDS:
        return f"unknown op {kind!r}; expected one of {OP_KINDS}"
    if not isinstance(op.get("record"), int):
        return "op.record must be an integer record id"
    if kind == "retime" and not isinstance(op.get("ts"), (int, float)):
        return "retime requires numeric 'ts'"
    if kind == "replace_payload":
        try:
            bytes.fromhex(op.get("data", ""))
        except ValueError:
            return "replace_payload requires hex 'data'"
    return None


def apply_ops(records: list[dict], ops: list[dict]) -> list[Record]:
    recs = to_records(records)
    by_id = {r.id: r for r in recs}
    for op in ops:
        rec = by_id.get(op["record"])
        if rec is None:
            continue
        if op["op"] == "mask_frame":
            rec.masked = True
        elif op["op"] == "retime":
            rec.ts = float(op["ts"])
        elif op["op"] == "replace_payload":
            rec.data = bytes.fromhex(op["data"])
    return recs


def execute(capture: dict, branch: dict) -> dict:
    spec = get_protocol(branch["protocol_version"])
    records = apply_ops(capture["records"], branch["ops"])
    result = analyze(records, spec)
    result["protocol_version"] = branch["protocol_version"]
    result["branch_id"] = branch["id"]
    return result


def divergence(run_a: dict, run_b: dict) -> dict:
    """First event index where two runs diverge, by event digest."""
    ea, eb = run_a["events"], run_b["events"]
    common = min(len(ea), len(eb))
    for i in range(common):
        if ea[i]["digest"] != eb[i]["digest"]:
            return {
                "diverges": True,
                "first_divergent_index": i,
                "event_a": ea[i],
                "event_b": eb[i],
                "events_a": len(ea),
                "events_b": len(eb),
            }
    if len(ea) != len(eb):
        longer, shorter = (ea, eb) if len(ea) > len(eb) else (eb, ea)
        return {
            "diverges": True,
            "first_divergent_index": common,
            "event_a": longer[common],
            "event_b": None,
            "events_a": len(ea),
            "events_b": len(eb),
        }
    return {"diverges": False, "events": len(ea)}


def migration_report(capture: dict, branch: dict, to_version: str) -> dict:
    """Compatibility check run only when the user explicitly migrates."""
    findings = check_migration(branch["protocol_version"], to_version)
    spec = get_protocol(to_version)
    records = apply_ops(capture["records"], branch["ops"])
    result = analyze(records, spec)
    rejected = [f for f in result["frames"] if f["status"] == "rejected"]
    errors = [e for e in result["events"] if e["severity"] in ("error", "fatal")]
    return {
        "from": branch["protocol_version"],
        "to": to_version,
        "static_findings": findings,
        "rejected_frames": len(rejected),
        "error_events": len(errors),
        "compatible": not findings and not rejected,
        "preview": result,
    }
