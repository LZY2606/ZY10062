"""CLI: serve the workbench, replay an export bundle, or print examples."""
from __future__ import annotations

import argparse
import json
import sys

from .app import verify_bundle
from .samplegen import demo_frames
from .server import serve


def main(argv=None):
    parser = argparse.ArgumentParser(prog="framebench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5200)
    parser.add_argument("--data", default="framebench-data",
                        help="persistence directory (journal + snapshots)")
    sub = parser.add_subparsers(dest="cmd")
    replay = sub.add_parser("replay", help="verify an exported bundle offline")
    replay.add_argument("bundle", help="path to a framebench export JSON")
    sub.add_parser("gen-example", help="print an example frames JSON document")
    args = parser.parse_args(argv)

    if args.cmd == "replay":
        with open(args.bundle, "r", encoding="utf-8") as fh:
            bundle = json.load(fh)
        report = verify_bundle(bundle)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("ok") else 1
    if args.cmd == "gen-example":
        print(json.dumps({"frames": demo_frames()}, ensure_ascii=False,
                         indent=1))
        return 0
    serve(args.host, args.port, args.data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
