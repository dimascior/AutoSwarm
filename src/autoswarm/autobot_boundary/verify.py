"""Verify an ExecutionManifest at AutoSwarm claim time.

AutoSwarm refuses launch if the manifest fails validation. On retry
with the same call_id, a different manifest hash is refused as an
execution identity conflict.
"""

import hashlib
import json
import re
from pathlib import Path

from ..records import SwarmError

_SCHEMA_PATH = (Path(__file__).resolve().parent / "schemas"
                / "execution_manifest.schema.json")
_SCHEMA_CACHE = []


def _load_schema():
    if not _SCHEMA_CACHE:
        with open(str(_SCHEMA_PATH), "r", encoding="utf-8") as fh:
            _SCHEMA_CACHE.append(json.load(fh))
    return _SCHEMA_CACHE[0]


def _sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _check_pattern(value, pattern, name):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        return "%s: does not match %s" % (name, pattern)
    return None


def verify_manifest(manifest_bytes, call_id, provider):
    """Validate the manifest against the schema and the invocation.

    Returns a list of issues (empty when valid). Does not raise —
    the caller decides whether to refuse."""
    issues = []
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, ValueError) as error:
        return ["manifest is not valid JSON: %s" % error]

    if not isinstance(manifest, dict):
        return ["manifest is not a JSON object"]

    if manifest.get("schema_version") != "v1":
        issues.append("schema_version: expected v1")
    if manifest.get("kind") != "ExecutionManifest":
        issues.append("kind: expected ExecutionManifest")

    for field, pattern in [
        ("call_id", r"call_[a-f0-9]{32}"),
        ("sequence_id", r"sq_[0-9a-f]{32}"),
        ("attempt_id", r"at_[0-9a-f]{32}"),
    ]:
        issue = _check_pattern(manifest.get(field), pattern, field)
        if issue:
            issues.append(issue)

    if manifest.get("call_id") != call_id:
        issues.append(
            "call_id mismatch: manifest %r != invocation %r"
            % (manifest.get("call_id"), call_id))

    pa = manifest.get("provider_assignment")
    if isinstance(pa, dict):
        if pa.get("provider") != provider:
            issues.append(
                "provider mismatch: manifest %r != invocation %r"
                % (pa.get("provider"), provider))
    else:
        issues.append("provider_assignment: not an object")

    if not isinstance(manifest.get("projection_address"), str):
        issues.append("projection_address: missing or not a string")
    if not isinstance(manifest.get("role"), str):
        issues.append("role: missing or not a string")

    return issues


def verify_manifest_identity(directory, manifest_bytes):
    """On retry: verify the persisted manifest matches the new one.

    Returns None if consistent, or an error message if they conflict."""
    existing_sha_path = directory / "manifest.sha256"
    if not existing_sha_path.is_file():
        return None
    existing_sha = existing_sha_path.read_text(encoding="utf-8").strip()
    new_sha = _sha256(manifest_bytes)
    if existing_sha != new_sha:
        return ("execution_identity_conflict: existing manifest %s"
                " != new manifest %s" % (existing_sha, new_sha))
    return None
