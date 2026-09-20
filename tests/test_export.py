import io
import json
import zipfile

from framebench.branch import execute
from framebench.examples import sample_records
from framebench.export import build_bundle, verify_bundle


def make():
    capture = {"name": "t", "records": sample_records()}
    branch = {"id": "b1", "name": "trunk", "protocol_version": "1.0",
              "ops": [{"op": "mask_frame", "record": 6}]}
    run = execute(capture, branch)
    return capture, branch, run


def test_bundle_roundtrip_verifies_offline():
    capture, branch, run = make()
    blob = build_bundle(capture, branch, run)
    result = verify_bundle(blob)
    assert result["ok"] is True
    assert result["events"] == len(run["events"])


def test_tampered_bundle_fails():
    capture, branch, run = make()
    blob = build_bundle(capture, branch, run)
    src = zipfile.ZipFile(io.BytesIO(blob))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "capture.json":
                doc = json.loads(data)
                doc["records"][0]["data"] = "00"  # corrupt the capture
                data = json.dumps(doc).encode()
            dst.writestr(name, data)
    assert verify_bundle(out.getvalue())["ok"] is False
