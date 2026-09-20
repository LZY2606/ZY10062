"""CLI: serve the workbench, generate a sample capture, verify a bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="framebench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5200)
    parser.add_argument("--db", default="framebench.db")
    sub = parser.add_subparsers(dest="cmd")

    p_example = sub.add_parser("example", help="print a sample capture (JSONL)")
    p_example.add_argument("--protocol", default="1.0")

    p_verify = sub.add_parser("verify", help="verify an exported bundle offline")
    p_verify.add_argument("bundle")

    sub.add_parser("recover", help="reconcile interrupted writes and exit")

    args = parser.parse_args(argv)

    if args.cmd == "example":
        from .examples import sample_capture_jsonl
        sys.stdout.write(sample_capture_jsonl(args.protocol))
        return 0

    if args.cmd == "verify":
        from .export import verify_bundle
        result = verify_bundle(Path(args.bundle).read_bytes())
        print(result)
        return 0 if result["ok"] else 1

    if args.cmd == "recover":
        from .store import Store
        store = Store(args.db)
        report = store.recover()
        store.close()
        print(report)
        return 0

    from .server import serve
    serve(args.host, args.port, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
