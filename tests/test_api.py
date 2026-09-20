"""HTTP API end-to-end: idempotency, branching, export, failure paths."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from framebench import samplegen
from framebench.app import Workbench
from framebench.server import Handler


@pytest.fixture()
def server(tmp_path):
    workbench = Workbench(tmp_path / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.workbench = workbench
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1], workbench
    httpd.shutdown()
    httpd.server_close()


def call(base, method, path, body=None, idem=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Idempotency-Key": idem} if idem else {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def make_capture(base):
    status, cap = call(base, "POST", "/api/captures",
                       {"name": "api-test", "protocol": "handshake@1"},
                       idem="cap-1")
    assert status == 200
    status, res = call(base, "POST",
                       "/api/captures/%s/frames" % cap["capture_id"],
                       {"frames": samplegen.demo_frames()}, idem="frames-1")
    assert status == 200 and res["accepted"] == 9
    return cap["capture_id"]


def test_full_flow(server):
    base, _ = server
    cid = make_capture(base)

    status, detail = call(base, "GET", "/api/captures/" + cid)
    assert status == 200
    statuses = {s["status"] for s in detail["sessions"].values()}
    assert "accepted" in statuses and "recovering" in statuses
    ids = [e["id"] for e in detail["events"]]
    assert ids == ["e%04d" % i for i in range(len(ids))]

    # idempotent replay of the same frame upload returns the first result
    status, again = call(base, "POST", "/api/captures/%s/frames" % cid,
                         {"frames": samplegen.demo_frames()}, idem="frames-1")
    assert status == 200 and again["replayed"] is True
    status, detail2 = call(base, "GET", "/api/captures/" + cid)
    assert len(detail2["frames"]) == 9  # not applied twice

    # branch + mutation + diff
    status, br = call(base, "POST", "/api/captures/%s/branches" % cid,
                      {"name": "what-if", "anchor_event": "e0000"},
                      idem="br-1")
    bid = br["branch_id"]
    assert br["protocol"] == "handshake@1"
    status, _ = call(base, "POST", "/api/branches/%s/mutations" % bid,
                     {"mutation": {"type": "mask_frame", "frame_id": "f4"}},
                     idem="mut-1")
    status, diff = call(base, "GET", "/api/branches/%s/diff" % bid)
    assert status == 200 and not diff["identical"]
    assert diff["diverges_at"]["event_id"].startswith("e")

    # migration to v2 is incompatible -> 409 with a report; force succeeds
    status, report = call(base, "POST", "/api/branches/%s/migrate" % bid,
                          {"version": "handshake@2"}, idem="mig-1")
    assert status == 409 and report["compatible"] is False
    status, done = call(base, "POST", "/api/branches/%s/migrate" % bid,
                        {"version": "handshake@2", "force": True},
                        idem="mig-2")
    assert status == 200 and done["protocol"] == "handshake@2"

    # export bundle verifies offline
    status, bundle = call(base, "GET", "/api/captures/%s/export" % cid)
    assert status == 200
    from framebench.app import verify_bundle
    assert verify_bundle(bundle)["ok"]


def test_static_pages(server):
    base, _ = server
    for path, marker in (("/", b"framebench"), ("/app.js", b"api("),
                         ("/style.css", b"--bg")):
        with urllib.request.urlopen(base + path) as resp:
            assert resp.status == 200
            assert marker in resp.read()


def test_invalid_frame_rejected_and_state_untouched(server):
    base, _ = server
    status, cap = call(base, "POST", "/api/captures",
                       {"name": "bad", "protocol": "handshake@1"})
    cid = cap["capture_id"]
    status, err = call(base, "POST", "/api/captures/%s/frames" % cid,
                       {"frames": [{"id": "x", "session": "s", "ts": 1,
                                    "dir": "c2s", "data": "zz"}]})
    assert status == 400
    status, detail = call(base, "GET", "/api/captures/" + cid)
    assert detail["frames"] == []


def test_unknown_capture_404(server):
    base, _ = server
    status, _ = call(base, "GET", "/api/captures/nope")
    assert status == 404


def test_persistence_across_restart(server, tmp_path):
    base, _ = server
    cid = make_capture(base)
    # a second Workbench over the same directory sees the same state
    reloaded = Workbench(tmp_path / "data")
    captures = reloaded.list_captures()
    assert any(c["id"] == cid for c in captures)
