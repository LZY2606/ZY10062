"""HTTP API + static web UI (stdlib only)."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import branch as branch_mod
from . import export as export_mod
from .capture import parse_capture
from .protocols import default_version, list_protocols
from .store import Store

WEB_DIR = Path(__file__).parent / "web"


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # quiet
            pass

        # -- helpers -------------------------------------------------------
        def _json(self, status: int, body: dict | list):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length) if length else b""

        def _idem_key(self) -> str | None:
            return self.headers.get("Idempotency-Key")

        def _err(self, status: int, msg: str):
            self._json(status, {"error": msg})

        def _run_for(self, b: dict) -> dict:
            cached = store.get_run(b["id"], len(b["ops"]))
            if cached:
                return cached
            capture = store.get_capture(b["capture_id"])
            run = branch_mod.execute(capture, b)
            store.save_run(b["id"], len(b["ops"]), run)
            return run

        # -- routing -------------------------------------------------------
        def do_GET(self):
            path = self.path.split("?")[0]
            m: re.Match | None
            if path == "/api/protocols":
                return self._json(200, {"protocols": list_protocols(),
                                        "default": default_version()})
            if path == "/api/sample":
                from .examples import sample_capture_jsonl
                data = sample_capture_jsonl().encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/captures":
                return self._json(200, {"captures": store.list_captures()})
            if m := re.fullmatch(r"/api/captures/([\w-]+)", path):
                cap = store.get_capture(m[1])
                return self._json(200, cap) if cap else self._err(404, "capture not found")
            if m := re.fullmatch(r"/api/captures/([\w-]+)/branches", path):
                return self._json(200, {"branches": store.list_branches(m[1])})
            if m := re.fullmatch(r"/api/branches/([\w-]+)", path):
                b = store.get_branch(m[1])
                if not b:
                    return self._err(404, "branch not found")
                return self._json(200, {"branch": b, "run": self._run_for(b)})
            if m := re.fullmatch(r"/api/branches/([\w-]+)/diff/([\w-]+)", path):
                a, b = store.get_branch(m[1]), store.get_branch(m[2])
                if not a or not b:
                    return self._err(404, "branch not found")
                return self._json(200, branch_mod.divergence(
                    self._run_for(a), self._run_for(b)))
            if m := re.fullmatch(r"/api/branches/([\w-]+)/export", path):
                b = store.get_branch(m[1])
                if not b:
                    return self._err(404, "branch not found")
                blob = export_mod.build_bundle(
                    store.get_capture(b["capture_id"]), b, self._run_for(b))
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="branch-{b["id"]}.zip"')
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            if path == "/api/journal":
                return self._json(200, {"journal": store.journal()})
            return self._static(path)

        def do_POST(self):
            path = self.path.split("?")[0]
            m: re.Match | None
            try:
                if path == "/api/captures":
                    return self._post_capture()
                if m := re.fullmatch(r"/api/captures/([\w-]+)/branches", path):
                    return self._post_branch(m[1])
                if m := re.fullmatch(r"/api/branches/([\w-]+)/ops", path):
                    return self._post_op(m[1])
                if m := re.fullmatch(r"/api/branches/([\w-]+)/migrate", path):
                    return self._post_migrate(m[1])
                if path == "/api/recover":
                    return self._json(200, store.recover())
                return self._err(404, "not found")
            except ValueError as exc:
                return self._err(400, str(exc))

        # -- POST handlers -------------------------------------------------
        def _post_capture(self):
            payload = json.loads(self._body() or b"{}")
            name = payload.get("name") or "capture"
            text = payload.get("data", "")

            def work():
                records = parse_capture(text)
                cap = store.add_capture(name, records)
                version = payload.get("protocol") or default_version()
                trunk = store.create_branch(cap["id"], "trunk", version)
                return 201, {"capture": cap, "trunk": trunk}

            status, body = store.idempotent(self._idem_key(), "POST /api/captures", work)
            return self._json(status, body)

        def _post_branch(self, capture_id: str):
            if not store.get_capture(capture_id):
                return self._err(404, "capture not found")
            payload = json.loads(self._body() or b"{}")

            def work():
                b = store.create_branch(
                    capture_id,
                    payload.get("name") or "branch",
                    payload.get("protocol_version") or default_version(),
                    parent_id=payload.get("parent_id"),
                    ops=payload.get("ops") or [])
                return 201, {"branch": b}

            status, body = store.idempotent(
                self._idem_key(), f"POST /api/captures/{capture_id}/branches", work)
            return self._json(status, body)

        def _post_op(self, branch_id: str):
            payload = json.loads(self._body() or b"{}")
            if err := branch_mod.validate_op(payload):
                return self._err(400, err)

            def work():
                b = store.apply_op(branch_id, payload)
                if b is None:
                    return 404, {"error": "branch not found"}
                return 200, {"branch": b, "run": self._run_for(b)}

            status, body = store.idempotent(
                self._idem_key(), f"POST /api/branches/{branch_id}/ops", work)
            return self._json(status, body)

        def _post_migrate(self, branch_id: str):
            b = store.get_branch(branch_id)
            if not b:
                return self._err(404, "branch not found")
            payload = json.loads(self._body() or b"{}")
            to_version = payload.get("to")
            capture = store.get_capture(b["capture_id"])
            report = branch_mod.migration_report(capture, b, to_version)
            if payload.get("confirm") and report["compatible"]:
                b = store.migrate_branch(branch_id, to_version)
                return self._json(200, {"branch": b, "report": report})
            return self._json(200, {"branch": b, "report": report,
                                    "migrated": False})

        # -- static --------------------------------------------------------
        def _static(self, path: str):
            if path in ("/", ""):
                path = "/index.html"
            target = (WEB_DIR / path.lstrip("/")).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
                return self._err(404, "not found")
            ctype = {".html": "text/html", ".js": "text/javascript",
                     ".css": "text/css"}.get(target.suffix, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(host: str, port: int, db_path: str):
    store = Store(db_path)
    report = store.recover()
    if report["reconciled"]:
        print(f"recovered {len(report['reconciled'])} interrupted write(s): "
              f"{report['reconciled']}")
    server = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"framebench listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
