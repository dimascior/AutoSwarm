"""Process-level concurrent recovery tests for call identity.

Replaces the thread-only concurrency demonstration with independent
launcher processes. Each test verifies that concurrent or sequential
recovery of a stale call directory produces exactly one execution
and no duplicate runners.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from autoswarm import engine, platform
from autoswarm.records import Records, read_json, write_json

if __package__:
    from .autoswarm_test_support import cleanup_temporary
else:
    from autoswarm_test_support import cleanup_temporary

FIXTURE = Path(__file__).parent / "fixtures/provider.py"
LAUNCHER = Path(__file__).parent / "fixtures/process_launcher.py"


def _make_records(root):
    records = Records(root / "state")
    write_json(records.home / "config.json", {
        "providers": {"codex": {
            "command": [sys.executable, str(FIXTURE), "codex"]
        }}
    })
    return records


class ProcessRecoveryTests(unittest.TestCase):
    """Two independent OS processes race on the same stale call id."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.records = _make_records(self.root)
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

    def _launcher_env(self):
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parent.parent / "src")
        env["PYTHONPATH"] = src + (
            os.pathsep + env["PYTHONPATH"]
            if env.get("PYTHONPATH") else "")
        return env

    def _launch_process(self, call_id, label, crash_point=None):
        """Spawn an independent process that calls start_call."""
        args = [
            sys.executable, str(LAUNCHER),
            "--home", str(self.records.home),
            "--workspace", str(self.root),
            "--call-id", call_id,
            "--label", label,
            "--output", str(self.root / ("%s.result.json" % label)),
        ]
        if crash_point:
            args.extend(["--crash-at", crash_point])
        return subprocess.Popen(
            args, env=self._launcher_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_two_processes_race_on_stale_claim(self):
        """Two OS processes simultaneously reclaim the same bare
        call directory. Exactly one spawns a runner; the other adopts."""
        call_id = "call_" + "a" * 32
        self.ids.append(call_id)
        self.records.call(call_id).mkdir(parents=True)

        p1 = self._launch_process(call_id, "P1")
        p2 = self._launch_process(call_id, "P2")
        p1.wait(timeout=30)
        p2.wait(timeout=30)

        r1 = read_json(self.root / "P1.result.json")
        r2 = read_json(self.root / "P2.result.json")
        self.assertIsNotNone(r1, "P1 produced no result")
        self.assertIsNotNone(r2, "P2 produced no result")
        self.assertEqual(r1["call_id"], call_id)
        self.assertEqual(r2["call_id"], call_id)
        task = read_json(self.records.call(call_id) / "task.json")
        self.assertIsNotNone(task)
        self.assertEqual(task["prompt"], "process-race")
        result = engine.wait(self.records, [call_id])[0]
        self.assertEqual(result["outcome"], "completed")

    def test_crash_before_task_persistence(self):
        """Process A creates the directory then crashes before writing
        task.json. Process B reclaims the bare directory and succeeds."""
        call_id = "call_" + "b" * 32
        self.ids.append(call_id)
        self.records.call(call_id).mkdir(parents=True)

        crashed = self._launch_process(
            call_id, "crash-before-task", crash_point="before_task")
        crashed.wait(timeout=10)
        self.assertNotEqual(crashed.returncode, 0,
                            "crash-before-task should have exited non-zero")

        survivor = self._launch_process(call_id, "survivor-task")
        survivor.wait(timeout=30)
        r = read_json(self.root / "survivor-task.result.json")
        self.assertIsNotNone(r)
        self.assertEqual(r["call_id"], call_id)
        result = engine.wait(self.records, [call_id])[0]
        self.assertEqual(result["outcome"], "completed")

    def test_crash_after_task_before_spawn(self):
        """Process A writes task.json then crashes before spawning.
        Process B adopts the existing task (idempotent call_id)."""
        call_id = "call_" + "c" * 32
        self.ids.append(call_id)
        task = {"call_id": call_id, "provider": "codex",
                "prompt": "process-race",
                "workspace": str(self.root),
                "parent": None, "native_session_id": None,
                "model": None, "provider_args": [],
                "previous_call": None, "return_mode": "native",
                "created_at": "2026-01-01T00:00:00Z",
                "effort": None, "delegate_approvals": None,
                "message_only": False,
                "command": [sys.executable, str(FIXTURE), "codex"],
                "agent_id": call_id,
                "controller_id": None, "communication": None}
        d = self.records.call(call_id)
        d.mkdir(parents=True)
        write_json(d / "task.json", task)
        agent_dir = self.records.home / "agents" / call_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        write_json(agent_dir / "agent.json",
                   {"agent_id": call_id, "idle": False})

        adopter = self._launch_process(call_id, "adopter")
        adopter.wait(timeout=30)
        r = read_json(self.root / "adopter.result.json")
        self.assertIsNotNone(r)
        self.assertEqual(r["call_id"], call_id)

    def test_no_duplicate_runners_across_processes(self):
        """After two processes race, exactly one handle.json and one
        runner complete — never two runners for the same call."""
        call_id = "call_" + "d" * 32
        self.ids.append(call_id)
        self.records.call(call_id).mkdir(parents=True)

        procs = []
        for i in range(3):
            procs.append(self._launch_process(
                call_id, "R%d" % i))
        for p in procs:
            p.wait(timeout=30)

        results = []
        for i in range(3):
            r = read_json(self.root / ("R%d.result.json" % i))
            self.assertIsNotNone(r, "R%d produced no result" % i)
            results.append(r)
        calls = [t for _, t in self.records.calls()
                 if t["call_id"] == call_id]
        self.assertEqual(len(calls), 1,
                         "More than one task.json for the same call_id")
        result = engine.wait(self.records, [call_id])[0]
        self.assertEqual(result["outcome"], "completed")


class ManifestProcessRecoveryTests(unittest.TestCase):
    """Process-level recovery with manifest identity."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.records = _make_records(self.root)
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

    def _make_manifest(self, call_id, **overrides):
        import hashlib
        manifest = {
            "schema_version": "v1",
            "kind": "ExecutionManifest",
            "sequence_id": "sq_" + "0" * 32,
            "attempt_id": "at_" + "0" * 32,
            "attempt_contract_hash": "sha256:" + "a" * 64,
            "call_id": call_id,
            "projection_address": "sha256:" + "b" * 64,
            "role": "builder",
            "capability": "research",
            "provider_assignment": {
                "provider": "codex",
                "selected_by": "agent_factory"
            },
            "constraints": {
                "finalization_class": "READ_ONLY",
                "evidence_requirements": [
                    "COMMAND_GOVERNANCE", "EXECUTION_LIFECYCLE"]
            },
        }
        manifest.update(overrides)
        return json.dumps(manifest, sort_keys=True).encode("utf-8")

    def test_manifest_crash_recovery_same_manifest(self):
        """Process A creates directory and manifest, crashes before task.
        Process B with same manifest recovers and completes the claim."""
        call_id = "call_" + "e" * 32
        self.ids.append(call_id)
        manifest_bytes = self._make_manifest(call_id)
        d = self.records.call(call_id)
        d.mkdir(parents=True)
        d.joinpath("manifest.json").write_bytes(manifest_bytes)
        sha = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        d.joinpath("manifest.sha256").write_text(sha, encoding="utf-8")

        info = engine.start_call(
            self.records, "codex", "manifest-recovery", self.root,
            call_id=call_id, manifest_bytes=manifest_bytes)
        self.assertEqual(info["call_id"], call_id)
        self.assertTrue((d / "task.json").is_file())
        result = engine.wait(self.records, [call_id])[0]
        self.assertEqual(result["outcome"], "completed")

    def test_manifest_crash_recovery_different_manifest(self):
        """Process A's accepted manifest survives; Process B carrying a
        different manifest is refused."""
        call_id = "call_" + "f0" * 16
        self.ids.append(call_id)
        original = self._make_manifest(call_id,
            constraints={"finalization_class": "READ_ONLY",
                         "evidence_requirements": ["EXECUTION_LIFECYCLE"]})
        d = self.records.call(call_id)
        d.mkdir(parents=True)
        d.joinpath("manifest.json").write_bytes(original)
        sha = "sha256:" + hashlib.sha256(original).hexdigest()
        d.joinpath("manifest.sha256").write_text(sha, encoding="utf-8")

        different = self._make_manifest(call_id,
            constraints={"finalization_class": "SOURCE_MODIFYING",
                         "evidence_requirements": ["EXECUTION_LIFECYCLE"]})
        from autoswarm.records import SwarmError
        with self.assertRaises(SwarmError) as ctx:
            engine.start_call(
                self.records, "codex", "manifest-recovery", self.root,
                call_id=call_id, manifest_bytes=different)
        self.assertEqual(ctx.exception.code, "execution_identity_conflict")
        self.assertFalse((d / "task.json").exists())


if __name__ == "__main__":
    unittest.main()
