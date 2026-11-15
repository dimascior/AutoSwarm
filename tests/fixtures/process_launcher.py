"""Standalone launcher for process-level recovery tests.

Invoked as an independent OS process to test concurrent call recovery.
Writes the start_call result (or error) to --output as JSON.
"""
import argparse
import json
import os
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--home", required=True)
parser.add_argument("--workspace", required=True)
parser.add_argument("--call-id", required=True)
parser.add_argument("--label", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--crash-at", default=None,
                    choices=["before_task", "after_task"])
args = parser.parse_args()

from autoswarm import engine
from autoswarm.records import Records, write_json

records = Records(os.path.join(args.home))

if args.crash_at == "before_task":
    d = records.call(args.call_id)
    d.mkdir(parents=True, exist_ok=True)
    sys.exit(1)

if args.crash_at == "after_task":
    d = records.call(args.call_id)
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "task.json", {
        "call_id": args.call_id,
        "provider": "codex",
        "prompt": "process-race",
        "workspace": args.workspace,
    })
    sys.exit(1)

try:
    info = engine.start_call(
        records, "codex", "process-race", args.workspace,
        call_id=args.call_id)
    with open(args.output, "w") as fh:
        json.dump({"call_id": info["call_id"],
                    "state": info["state"],
                    "label": args.label}, fh)
except Exception as error:
    with open(args.output, "w") as fh:
        json.dump({"error": str(error),
                    "label": args.label}, fh)
    sys.exit(2)
