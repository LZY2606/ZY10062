import http.client
import json
import threading

import pytest

from framebench.examples import sample_capture_jsonl
from framebench.server import make_handler
from framebench.store import Store
from http.server import ThreadingHTTPServer


@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path / "api.db")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, store
    srv.shutdown()
    store.close()


def call(srv, method, path, body=None, idem=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port)
    headers = {"Content-Type": "application/json"}
    if idem:
        headers["Idempotency-Key"] = idem
    conn.request(method, path, json.dumps(body) if body is not None else None,
                 headers)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    try:
        return res.status, json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return res.status, data


def upload(srv, idem="up-1"):
    return call(srv, "POST", "/api/captures",
                {"name": "sess", "data": sample_capture_jsonl()}, idem=idem)


def test_upload_analyze_branch_flow(server):
    srv, _ = server
    status, out = upload(srv)
    assert status == 201
    cap_id, trunk = out["capture"]["id"], out["trunk"]

    status, out = call(srv, "GET", f"/api/branches/{trunk['id']}")
    assert status == 200
    run = out["run"]
    assert any(e["kind"] == "handshake_complete" for e in run["events"])
    statuses = {f["status"] for f in run["frames"]}
    assert statuses >= {"accepted", "rejected", "recovering"}

    # apply an overlay op: mask the corrupted frame
    status, out = call(srv, "POST", f"/api/branches/{trunk['id']}/ops",
                       {"op": "mask_frame", "record": 6}, idem="op-1")
    assert status == 200
    assert any(e["kind"] == "frame_masked" for e in out["run"]["events"])
    # original capture untouched
    _, cap = call(srv, "GET", f"/api/captures/{cap_id}")
    assert len(cap["records"]) == 10


def test_idempotent_upload_replays_first_response(server):
    srv, store = server
    s1, b1 = upload(srv, idem="same-key")
    s2, b2 = upload(srv, idem="same-key")
    assert (s1, b1) == (s2, b2)
    assert len(store.list_captures()) == 1


def test_branch_diff_endpoint(server):
    srv, _ = server
    _, out = upload(srv)
    cap_id, trunk = out["capture"]["id"], out["trunk"]
    _, fork = call(srv, "POST", f"/api/captures/{cap_id}/branches",
                   {"name": "masked", "parent_id": trunk["id"],
                    "ops": [{"op": "mask_frame", "record": 0}]}, idem="br-1")
    status, d = call(srv, "GET",
                     f"/api/branches/{trunk['id']}/diff/{fork['branch']['id']}")
    assert status == 200 and d["diverges"] is True
    assert d["first_divergent_index"] == 0


def test_migration_endpoint_requires_confirm(server):
    srv, _ = server
    _, out = upload(srv)
    trunk = out["trunk"]
    _, dry = call(srv, "POST", f"/api/branches/{trunk['id']}/migrate",
                  {"to": "2.0"}, idem="mig-1")
    assert dry["report"]["compatible"] is False
    _, b = call(srv, "GET", f"/api/branches/{trunk['id']}")
    assert b["branch"]["protocol_version"] == "1.0"  # unchanged without confirm


def test_export_bundle_and_recover_endpoint(server):
    srv, _ = server
    _, out = upload(srv)
    trunk = out["trunk"]
    status, blob = call(srv, "GET", f"/api/branches/{trunk['id']}/export")
    assert status == 200
    from framebench.export import verify_bundle
    assert verify_bundle(blob)["ok"] is True
    status, report = call(srv, "POST", "/api/recover", {}, idem="rec-1")
    assert status == 200 and report["reconciled"] == []


def test_bad_capture_rejected(server):
    srv, _ = server
    status, out = call(srv, "POST", "/api/captures",
                       {"name": "bad", "data": "not jsonl"}, idem="bad-1")
    assert status == 400
    status, out = call(srv, "POST", "/api/captures",
                       {"name": "empty", "data": ""}, idem="bad-2")
    assert status == 400
