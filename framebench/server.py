"""HTTP API + static web UI (stdlib only)."""
from __future__ import annotations

import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .app import Workbench
from .samplegen import demo_frames
from .store import DomainError, StoreError

WEB_DIR = Path(__file__).parent / "web"

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "framebench/0.1"

    def log_message(self, *args):
        pass

    @property
    def workbench(self):
        return self.server.workbench

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path):
        name, ctype = STATIC[path]
        try:
            data = (WEB_DIR / name).read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise DomainError("request body is not valid JSON", 400)

    def _execute(self, op, payload):
        idem = self.headers.get("Idempotency-Key")
        try:
            result = self.workbench.store.execute(op, payload, idem=idem)
        except DomainError as exc:
            body = {"error": str(exc)}
            body.update(exc.report)
            self._send_json(exc.status, body)
            return
        except StoreError as exc:
            self._send_json(500, {"error": str(exc), "state_preserved": True})
            return
        self._send_json(200, result)

    def _query(self, fn, *args):
        try:
            self._send_json(200, fn(*args))
        except DomainError as exc:
            body = {"error": str(exc)}
            body.update(exc.report)
            self._send_json(exc.status, body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in STATIC:
            self._send_static(path)
            return
        wb = self.workbench
        parts = [p for p in path.split("/") if p]
        if parts == ["api", "protocols"]:
            self._query(wb.list_protocols)
        elif parts == ["api", "example"]:
            self._query(lambda: {"frames": demo_frames()})
        elif parts == ["api", "captures"]:
            self._query(wb.list_captures)
        elif len(parts) == 3 and parts[:2] == ["api", "captures"]:
            self._query(wb.capture_detail, parts[2])
        elif (len(parts) == 4 and parts[:2] == ["api", "captures"]
              and parts[3] == "export"):
            self._query(wb.export_bundle, parts[2])
        elif len(parts) == 3 and parts[:2] == ["api", "branches"]:
            self._query(wb.branch_detail, parts[2])
        elif (len(parts) == 4 and parts[:2] == ["api", "branches"]
              and parts[3] == "diff"):
            self._query(wb.diff_branch, parts[2])
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        try:
            body = self._read_body()
        except DomainError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        if parts == ["api", "captures"]:
            self._execute("create_capture", {
                "capture_id": "cap-" + uuid.uuid4().hex[:12],
                "name": body.get("name"),
                "protocol": body.get("protocol"),
            })
        elif (len(parts) == 4 and parts[:2] == ["api", "captures"]
              and parts[3] == "frames"):
            self._execute("add_frames", {
                "capture_id": parts[2],
                "frames": body.get("frames", []),
            })
        elif (len(parts) == 4 and parts[:2] == ["api", "captures"]
              and parts[3] == "branches"):
            self._execute("create_branch", {
                "branch_id": "br-" + uuid.uuid4().hex[:12],
                "capture_id": parts[2],
                "name": body.get("name"),
                "anchor_event": body.get("anchor_event"),
            })
        elif (len(parts) == 4 and parts[:2] == ["api", "branches"]
              and parts[3] == "mutations"):
            self._execute("add_mutation", {
                "branch_id": parts[2],
                "mutation": body.get("mutation"),
            })
        elif (len(parts) == 4 and parts[:2] == ["api", "branches"]
              and parts[3] == "migrate"):
            self._execute("migrate_branch", {
                "branch_id": parts[2],
                "version": body.get("version"),
                "force": bool(body.get("force")),
            })
        elif parts == ["api", "protocols"]:
            self._execute("register_protocol", {"doc": body.get("doc")})
        else:
            self._send_json(404, {"error": "not found"})


def serve(host, port, data_dir):
    workbench = Workbench(data_dir)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.workbench = workbench
    print("framebench listening on http://%s:%d (data dir: %s)"
          % (host, port, data_dir))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        workbench.store.snapshot()
        httpd.server_close()
