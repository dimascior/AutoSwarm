"""Write the ExecutionClaimRecord at call claim time.

This is AutoSwarm's durable statement of what it accepted before
starting the provider. It carries the manifest identity, the prompt
identity, and the claim instant. The existing task.json, handle.json
and result.json remain intact as producer originals.
"""

import hashlib
import json

from ..records import now as _now, write_json


def _sha256_str(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_claim_record(directory, call_id, manifest_sha256, prompt):
    """Write the claim record to the call directory.

    Called after manifest verification passes and before the provider
    process starts. The record is the canonical obj-11 producer
    evidence of what AutoSwarm accepted."""
    record = {
        "schema_version": "v1",
        "kind": "ExecutionClaimRecord",
        "call_id": call_id,
        "manifest_sha256": manifest_sha256,
        "prompt_sha256": _sha256_str(prompt),
        "claimed_utc": _now(),
        "claim_state": "CLAIMED",
    }
    write_json(directory / "claim.json", record)
    return record
