"""Export bundles: everything needed to replay and verify offline.

A bundle is a zip with the capture, the pinned protocol spec, the branch
overlay ops, the expected event digests and a manifest with SHA-256 sums.
`python3 -m framebench verify bundle.zip` re-runs the analysis offline and
compares event digests.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

from .branch import execute
from .protocols import get_protocol


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_bundle(capture: dict, branch: dict, run: dict) -> bytes:
    spec = get_protocol(branch["protocol_version"])
    files = {
        "capture.json": json.dumps(
            {"name": capture["name"], "records": capture["records"]},
            indent=1).encode(),
        "protocol.json": json.dumps(spec, indent=1, sort_keys=True).encode(),
        "branch.json": json.dumps(
            {"name": branch["name"],
             "protocol_version": branch["protocol_version"],
             "ops": branch["ops"]}, indent=1).encode(),
        "events.json": json.dumps(run["events"], indent=1).encode(),
    }
    manifest = {name: _sha(data) for name, data in files.items()}
    files["manifest.json"] = json.dumps(manifest, indent=1, sort_keys=True).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def verify_bundle(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        files = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(files["manifest.json"])
    bad = [n for n, digest in manifest.items()
           if n in files and _sha(files[n]) != digest]
    if bad:
        return {"ok": False, "reason": f"checksum mismatch in bundle: {bad}"}
    capture = json.loads(files["capture.json"])
    branch = json.loads(files["branch.json"])
    expected = json.loads(files["events.json"])
    run = execute(
        {"records": capture["records"]},
        {"id": "offline", "protocol_version": branch["protocol_version"],
         "ops": branch["ops"]},
    )
    got = [e["digest"] for e in run["events"]]
    want = [e["digest"] for e in expected]
    return {
        "ok": got == want,
        "events": len(got),
        "expected_events": len(want),
        "first_mismatch": next(
            (i for i, (a, b) in enumerate(zip(got, want)) if a != b), None),
    }
