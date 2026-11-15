"""Manifest verification and claim-time enforcement tests.

Proves the execution boundary: valid manifests are accepted and
persisted, invalid/conflicting manifests are refused before spawn.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autoswarm import engine, platform
from autoswarm.autobot_boundary.verify import (
    verify_manifest, verify_manifest_identity)
from autoswarm.autobot_boundary.claim import write_claim_record
from autoswarm.records import Records, SwarmError, read_json, write_json

if __package__:
    from .autoswarm_test_support import cleanup_temporary
else:
    from autoswarm_test_support import cleanup_temporary


FIXTURE = Path(__file__).parent / "fixtures/provider.py"


def _sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _make_manifest(call_id, provider="codex", role="builder",
                   finalization_class="READ_ONLY", **overrides):
    manifest = {
        "schema_version": "v1",
        "kind": "ExecutionManifest",
        "sequence_id": "sq_" + "0" * 32,
        "attempt_id": "at_" + "0" * 32,
        "attempt_contract_hash": "sha256:" + "a" * 64,
        "call_id": call_id,
        "projection_address": "sha256:" + "b" * 64,
        "role": role,
        "capability": "research",
        "provider_assignment": {
            "provider": provider,
            "selected_by": "agent_factory"
        },
        "constraints": {
            "finalization_class": finalization_class,
            "evidence_requirements": [
                "COMMAND_GOVERNANCE", "EXECUTION_LIFECYCLE"]
        },
    }
    manifest.update(overrides)
    return json.dumps(manifest, sort_keys=True).encode("utf-8")


class ManifestVerificationTests(unittest.TestCase):
    """verify_manifest catches invalid manifests before spawn."""

    def test_valid_manifest_produces_no_issues(self):
        call_id = "call_" + "a" * 32
        data = _make_manifest(call_id)
        issues = verify_manifest(data, call_id, "codex")
        self.assertEqual(issues, [])

    def test_call_id_mismatch_detected(self):
        data = _make_manifest("call_" + "a" * 32)
        issues = verify_manifest(data, "call_" + "b" * 32, "codex")
        self.assertTrue(any("call_id mismatch" in i for i in issues))

    def test_provider_mismatch_detected(self):
        call_id = "call_" + "c" * 32
        data = _make_manifest(call_id, provider="codex")
        issues = verify_manifest(data, call_id, "claude-code")
        self.assertTrue(any("provider mismatch" in i for i in issues))

    def test_malformed_json_rejected(self):
        issues = verify_manifest(b"not json", "call_" + "d" * 32, "codex")
        self.assertTrue(any("not valid JSON" in i for i in issues))

    def test_wrong_schema_version_detected(self):
        data = _make_manifest("call_" + "e" * 32)
        manifest = json.loads(data)
        manifest["schema_version"] = "v999"
        data = json.dumps(manifest).encode()
        issues = verify_manifest(data, "call_" + "e" * 32, "codex")
        self.assertTrue(any("schema_version" in i for i in issues))

    def test_missing_required_field_detected(self):
        call_id = "call_" + "f" * 32
        manifest = json.loads(_make_manifest(call_id))
        del manifest["projection_address"]
        data = json.dumps(manifest).encode()
        issues = verify_manifest(data, call_id, "codex")
        self.assertTrue(any("projection_address" in i for i in issues))


class ManifestIdentityTests(unittest.TestCase):
    """Idempotency: same manifest adopts, different manifest refuses."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_no_existing_manifest_returns_none(self):
        result = verify_manifest_identity(self.dir, b"any bytes")
        self.assertIsNone(result)

    def test_same_manifest_returns_none(self):
        data = b'{"test": "manifest"}'
        (self.dir / "manifest.sha256").write_text(
            _sha256(data), encoding="utf-8")
        result = verify_manifest_identity(self.dir, data)
        self.assertIsNone(result)

    def test_different_manifest_returns_conflict(self):
        original = b'{"version": 1}'
        changed = b'{"version": 2}'
        (self.dir / "manifest.sha256").write_text(
            _sha256(original), encoding="utf-8")
        result = verify_manifest_identity(self.dir, changed)
        self.assertIsNotNone(result)
        self.assertIn("execution_identity_conflict", result)


class ClaimRecordTests(unittest.TestCase):
    """The claim record is written before provider execution."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_claim_record_written(self):
        record = write_claim_record(
            self.dir, "call_" + "a" * 32,
            "sha256:" + "x" * 64, "test prompt")
        self.assertEqual(record["kind"], "ExecutionClaimRecord")
        self.assertEqual(record["call_id"], "call_" + "a" * 32)
        self.assertEqual(record["manifest_sha256"], "sha256:" + "x" * 64)
        self.assertTrue(record["prompt_sha256"].startswith("sha256:"))
        self.assertEqual(record["claim_state"], "CLAIMED")
        persisted = read_json(self.dir / "claim.json")
        self.assertEqual(persisted["call_id"], record["call_id"])


class EngineManifestIntegrationTests(unittest.TestCase):
    """The engine validates and persists manifests during start_call."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.records = Records(self.root / "state")
        write_json(self.records.home / "config.json", {
            "providers": {"codex": {
                "command": [sys.executable, str(FIXTURE), "codex"]
            }}
        })
        self.ids = []

    def tearDown(self):
        for call_id in self.ids:
            try:
                engine.cancel(self.records, call_id)
            except Exception:
                pass
        deadline = time.monotonic() + 10
        while any(
            platform.running(read_json(self.records.call(v) / "handle.json"))
            for v in self.ids
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        cleanup_temporary(self.temporary, deadline)

    def test_valid_manifest_persisted_and_claimed(self):
        """A valid manifest is persisted as manifest.json with its
        SHA-256, and claim.json is written before the provider starts."""
        call_id = "call_" + "a" * 32
        self.ids.append(call_id)
        manifest_bytes = _make_manifest(call_id)
        info = engine.start_call(
            self.records, "codex", "manifest test", self.root,
            call_id=call_id, manifest_bytes=manifest_bytes)
        self.assertEqual(info["call_id"], call_id)
        d = self.records.call(call_id)
        self.assertTrue((d / "manifest.json").is_file())
        self.assertTrue((d / "manifest.sha256").is_file())
        self.assertTrue((d / "claim.json").is_file())
        self.assertEqual(
            (d / "manifest.json").read_bytes(), manifest_bytes)
        self.assertEqual(
            (d / "manifest.sha256").read_text(encoding="utf-8").strip(),
            _sha256(manifest_bytes))
        claim = read_json(d / "claim.json")
        self.assertEqual(claim["manifest_sha256"], _sha256(manifest_bytes))

    def test_same_manifest_retry_adopts(self):
        """Retrying with the same call_id and same manifest adopts."""
        call_id = "call_" + "b" * 32
        self.ids.append(call_id)
        manifest_bytes = _make_manifest(call_id)
        info1 = engine.start_call(
            self.records, "codex", "retry test", self.root,
            call_id=call_id, manifest_bytes=manifest_bytes)
        info2 = engine.start_call(
            self.records, "codex", "retry test", self.root,
            call_id=call_id, manifest_bytes=manifest_bytes)
        self.assertEqual(info1["call_id"], info2["call_id"])

    def test_different_manifest_retry_refused(self):
        """Retrying with the same call_id but different manifest raises
        execution_identity_conflict."""
        call_id = "call_" + "c" * 32
        self.ids.append(call_id)
        m1 = _make_manifest(call_id, finalization_class="READ_ONLY")
        engine.start_call(
            self.records, "codex", "conflict test", self.root,
            call_id=call_id, manifest_bytes=m1)
        m2 = _make_manifest(call_id, finalization_class="SOURCE_MODIFYING")
        with self.assertRaises(SwarmError) as ctx:
            engine.start_call(
                self.records, "codex", "conflict test", self.root,
                call_id=call_id, manifest_bytes=m2)
        self.assertEqual(ctx.exception.code, "execution_identity_conflict")

    def test_manifest_call_id_mismatch_refused(self):
        """A manifest whose call_id doesn't match the CLI call_id is
        refused before spawn."""
        call_id = "call_" + "d" * 32
        self.ids.append(call_id)
        wrong_manifest = _make_manifest("call_" + "e" * 32)
        with self.assertRaises(SwarmError) as ctx:
            engine.start_call(
                self.records, "codex", "mismatch test", self.root,
                call_id=call_id, manifest_bytes=wrong_manifest)
        self.assertEqual(ctx.exception.code, "manifest_invalid")

    def test_manifest_provider_mismatch_refused(self):
        """A manifest naming a different provider is refused."""
        call_id = "call_" + "f" * 32
        self.ids.append(call_id)
        wrong_manifest = _make_manifest(call_id, provider="claude-code")
        with self.assertRaises(SwarmError) as ctx:
            engine.start_call(
                self.records, "codex", "provider test", self.root,
                call_id=call_id, manifest_bytes=wrong_manifest)
        self.assertEqual(ctx.exception.code, "manifest_invalid")

    def test_native_call_without_manifest_still_works(self):
        """Non-Autobot calls (no manifest) still work normally."""
        call_id = "call_" + "1" * 32
        self.ids.append(call_id)
        info = engine.start_call(
            self.records, "codex", "native call", self.root,
            call_id=call_id)
        self.assertEqual(info["call_id"], call_id)
        d = self.records.call(call_id)
        self.assertFalse((d / "manifest.json").exists())
        self.assertFalse((d / "claim.json").exists())


if __name__ == "__main__":
    unittest.main()
