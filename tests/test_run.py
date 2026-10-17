"""Local CPU-only launcher integration tests; no training, GPU, SSH or scheduler."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "run-pilot" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from core import Busy, Store, field, now


def contract(root, execution="cpu-run", units=None):
    return {
        "schema_version": 1, "execution_id": execution, "project": str(root),
        "target": {"kind": "local"}, "scope_approved": True,
        "allowed_actions": ["observe", "launch"], "devices": [],
        "max_concurrency": 1,
        "resource_lock": str(root / "resource-domain" / "resources.lock"),
        "reserves": {"ram_mb": 0, "disk_mb": 0, "cpu_threads": 0},
        "units": units or [{
            "id": "dummy", "argv": [sys.executable, "-c", "import sys; sys.exit(7)"],
            "devices": [], "resources": {"ram_mb": 1, "disk_mb": 1, "cpu_threads": 1},
        }],
        "notification": {"kind": "none"}, "analysis": {"enabled": False},
    }


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runpilot-launch-")
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.doc = contract(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *args, expected=0):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "run.py"), *args,
             "--state-dir", str(self.state_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=15, check=False,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def initialize(self):
        path = self.root / "contract.json"
        path.write_text(json.dumps(self.doc), encoding="utf-8")
        return self.cli("init", "--contract", str(path))

    def store(self, doc=None, state_dir=None):
        store = Store(state_dir or self.state_dir)
        self.addCleanup(store.close)
        store.save_contract(doc or self.doc)
        return store

    def wait_exit(self, runtime):
        deadline = time.monotonic() + 10
        path = Path(runtime["exit_path"])
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertTrue(path.exists(), "No true child exit: " + str(runtime))
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def resources(*args):
        """CPU test capacity, not a fabricated production resource observation."""
        return {
            "observed_at": now(), "reachable": True, "fields": {"resources": field({
                "ram_available_mb": field(10000), "disk_free_mb": field(10000),
                "cpu_threads": field(16), "shm_free_mb": field(10000),
                "gpus": field([], source="CPU-only fixture"),
            })},
        }

    def observations(self, *args):
        from collectors import collect
        snapshot = collect(*args)
        snapshot["fields"]["resources"] = self.resources()["fields"]["resources"]
        return snapshot

    def test_monitor_reconciles_late_receipt_without_duplicate_submission(self):
        from run import launch_unit
        from monitor import check_once
        self.doc["units"][0]["argv"] = [
            sys.executable, "-c", "import time; time.sleep(0.15)",
        ]
        store = self.store()
        with patch("run.collect", side_effect=self.resources), patch("run.STARTUP_WAIT_SECONDS", 0):
            launch_unit(store, self.doc, "dummy")
        self.wait_exit(store.unit("cpu-run", "dummy"))
        result = check_once(store, "cpu-run")
        runtime = store.unit("cpu-run", "dummy")
        self.assertEqual(runtime["attempt"], 1)
        self.assertEqual(runtime["exit_result"]["exit_code"], 0)
        self.assertEqual(runtime["execution_state"], "finished_unverified")
        self.assertEqual(result["model_submissions"], 0)
        self.assertEqual(len(list((self.state_dir / "attempts/cpu-run/dummy").glob("attempt-*"))), 1)

    def test_generic_python_export_is_completed_by_supervised_zero_exit(self):
        from run import launch_unit, reconcile
        worker = self.root / "export.py"
        output = self.root / "export.csv"
        worker.write_text(
            "import sys,time\n"
            "time.sleep(0.2)\n"
            "open(sys.argv[1], 'w').write('name,value\\nexample,1\\n')\n",
            encoding="utf-8",
        )
        self.doc["units"][0].update(
            argv=[sys.executable, str(worker), str(output)],
            write_paths=[str(output)], completion={"kind": "exit_code"},
        )
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            launched = launch_unit(store, self.doc, "dummy")
        self.assertIn(launched["status"], {"submitted", "confirmed"})
        runtime = store.unit("cpu-run", "dummy")
        self.assertIsNone(runtime["completion_baseline"])
        self.assertEqual(self.wait_exit(runtime)["exit_code"], 0)
        reconcile(store, "cpu-run")
        self.assertEqual(store.unit("cpu-run", "dummy")["execution_state"], "completed")
        self.assertEqual(output.read_text(encoding="utf-8"), "name,value\nexample,1\n")

    def test_missing_primary_without_exit_holds_authoritative_reservation(self):
        import run
        from core import write_json
        directory = self.root / "synthetic-attempt"
        directory.mkdir()
        runtime = {
            "managed": True, "claim_id": "synthetic",
            "identity": {"pid": 9999999, "start_time": "old", "boot_id": "synthetic"},
            "exit_path": str(directory / "exit.json"),
            "manifest_path": str(directory / "manifest.json"),
        }
        record = {
            "schema_version": 1, "execution_id": "cpu-run", "unit_id": "dummy", "attempt": 1,
            "claim_id": "synthetic", "argv": self.doc["units"][0]["argv"], "cwd": self.doc["project"],
            "child_started": True, "phase": "running", "identity": runtime["identity"],
        }
        write_json(runtime["manifest_path"], record)
        registry = run._Registry(self.doc)
        self.addCleanup(registry.close)
        claim = {"execution": "cpu-run", "unit": "dummy", "attempt": 1, "status": "confirmed",
                 "doc": {"runtime": runtime, "argv": record["argv"], "cwd": record["cwd"]}}
        registry.save(claim, insert=True)
        with patch("run._confirmed_gone", return_value=True):
            result = run._refresh_claim(registry, claim)
        self.assertEqual(result["status"], "unknown")

    def test_init_is_immutable_and_status_never_starts_work(self):
        self.assertEqual(self.initialize()["status"], "initialized")
        self.assertEqual(self.initialize()["status"], "initialized")
        status = self.cli("status", "--execution", "cpu-run")
        self.assertEqual(status["units"]["dummy"]["attempt"], 0)
        self.assertEqual(status["units"]["dummy"]["execution_state"], "ready")
        self.assertFalse((self.state_dir / "attempts").exists())
        self.doc["units"][0]["argv"][-1] = "raise SystemExit(0)"
        path = self.root / "different.json"
        path.write_text(json.dumps(self.doc), encoding="utf-8")
        self.assertIn("immutable", self.cli("init", "--contract", str(path), expected=2)["program_error"])

    def test_supervisor_captures_actual_child_exit_and_finish_time(self):
        from run import launch_unit, reconcile
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            result = launch_unit(store, self.doc, "dummy")
        self.assertIn(result["status"], {"submitted", "confirmed"})
        runtime = store.unit("cpu-run", "dummy")
        self.assertTrue(runtime["managed"])
        self.assertEqual(runtime["attempt"], 1)
        exit_result = self.wait_exit(runtime)
        self.assertEqual(exit_result["exit_code"], 7)
        self.assertEqual(exit_result["execution_id"], "cpu-run")
        self.assertEqual(exit_result["unit_id"], "dummy")
        self.assertEqual(exit_result["attempt"], 1)
        finish = exit_result["finished_at"]
        time.sleep(0.05)
        reconcile(store, "cpu-run")
        runtime = store.unit("cpu-run", "dummy")
        self.assertEqual(runtime["execution_state"], "failed")
        self.assertEqual(runtime["finished_at"], finish)
        self.assertGreater(runtime["record_written_at"], finish)
        self.assertEqual(runtime["retries_used"], 0)
        self.assertEqual(runtime["assigned_devices"], [])
        self.assertIsNone(runtime.get("actual_devices"))
        self.assertEqual(Path(runtime["log_path"]).stat().st_mode & 0o777, 0o600)

    def test_dry_run_uses_real_policy_but_has_no_attempt_or_submission(self):
        from run import launch_unit, reconcile
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            result = launch_unit(store, self.doc, "dummy", dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertFalse(result["training_started"])
        self.assertEqual(store.unit("cpu-run", "dummy")["attempt"], 0)
        self.assertFalse((store.root / "attempts").exists())
        self.assertEqual(reconcile(store, "cpu-run")["reservations"], [])

    def test_missing_explicit_reserve_refuses_before_spawning(self):
        from run import launch_unit
        self.doc["reserves"].pop("ram_mb")
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            result = launch_unit(store, self.doc, "dummy")
        self.assertEqual(result["status"], "refused")
        self.assertTrue(any("reserve" in reason for reason in result["reasons"]))
        self.assertEqual(store.unit("cpu-run", "dummy")["attempt"], 0)

    def test_nonempty_outputs_require_explicit_behavior_authorization(self):
        from run import launch_unit
        output = self.root / "existing-results"
        output.mkdir()
        sentinel = output / "best-weights"
        sentinel.write_text("valuable existing weights", encoding="utf-8")
        self.doc["units"][0]["write_paths"] = [str(output)]
        first = self.store()
        with patch("run.collect", side_effect=self.resources):
            result = launch_unit(first, self.doc, "dummy")
        self.assertEqual(result["status"], "refused")
        self.assertTrue(any("nonempty output" in reason for reason in result["reasons"]))
        self.assertFalse((first.root / "attempts").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "valuable existing weights")
        self.doc["execution_id"] = "explicit-existing-output"
        self.doc["units"][0]["allow_existing_outputs"] = True
        second = self.store(self.doc, self.root / "second-state")
        with patch("run.collect", side_effect=self.resources):
            self.assertEqual(launch_unit(second, self.doc, "dummy", dry_run=True)["status"], "refused")
        self.doc["execution_id"] = "reviewed-existing-output"
        self.doc["units"][0]["existing_outputs_evidence"] = (
            "Reviewed original command writes a distinct summary and does not replace weights."
        )
        third = self.store(self.doc, self.root / "third-state")
        with patch("run.collect", side_effect=self.resources):
            self.assertEqual(launch_unit(third, self.doc, "dummy", dry_run=True)["status"], "dry-run")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "valuable existing weights")

    def test_project_output_directories_are_not_automatically_created(self):
        from run import launch_unit
        output = self.root / "absent" / "project-output"
        self.doc["units"][0]["write_paths"] = [str(output)]
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            self.assertEqual(launch_unit(store, self.doc, "dummy", dry_run=True)["status"], "dry-run")
        self.assertFalse(output.exists())
        self.doc["units"][0]["cwd"] = str(self.root / "absent-cwd")
        self.doc["execution_id"] = "missing-cwd"
        other = self.store(self.doc, self.root / "missing-cwd-state")
        with patch("run.collect", side_effect=self.resources):
            self.assertEqual(launch_unit(other, self.doc, "dummy")["status"], "refused")
        self.assertFalse((self.root / "absent-cwd").exists())

    def test_unknown_missing_manifest_holds_shared_budget_and_concurrency(self):
        import run
        from run import launch_unit, reconcile
        first = self.store()
        other_doc = contract(self.root, execution="other-client")
        other = self.store(other_doc, self.root / "other-state")
        original = subprocess.Popen
        attempts = []

        def missing_response(argv, **kwargs):
            if len(argv) > 1 and argv[1] == str(SCRIPTS / "supervisor.py"):
                attempts.append(argv)
                raise OSError("submission acknowledgement lost; outcome unknown")
            return original(argv, **kwargs)

        with patch("run.collect", side_effect=self.resources), patch("run.subprocess.Popen", side_effect=missing_response):
            result = launch_unit(first, self.doc, "dummy")
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(launch_unit(first, self.doc, "dummy")["status"], "unknown")
            blocked = launch_unit(other, other_doc, "dummy")
        self.assertEqual(blocked["status"], "refused")
        self.assertTrue(any("concurrency limit" in reason for reason in blocked["reasons"]))
        self.assertEqual(len(attempts), 1)
        runtime = first.unit("cpu-run", "dummy")
        self.assertFalse(Path(runtime["manifest_path"]).exists())
        held = reconcile(first, "cpu-run")["reservations"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0], {
            "execution": "cpu-run", "unit": "dummy", "status": "unknown",
            "resources": {"gpu_mb": {}, "ram_mb": 1, "disk_mb": 1, "cpu_threads": 1},
            "write_paths": [], "isolation_verified": False, "reflected_in_sample": False,
        })
        self.assertEqual(runtime["attempt"], 1)
        self.assertEqual(runtime["retries_used"], 0)

    def test_argv_metacharacters_spaces_unicode_and_cwd_remain_literal(self):
        from run import launch_unit, reconcile
        project = self.root / "项目 space's directory"
        project.mkdir()
        marker = self.root / "MUST-NOT-EXIST"
        args = ["two words", "中文", "$(touch %s)" % marker, "; touch %s" % marker, "*", ">output"]
        source = (
            "import json,os,sys; "
            "print(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),"
            "'omp':os.environ.get('OMP_NUM_THREADS'),"
            "'mode':os.environ.get('JOB_MODE')},ensure_ascii=False))"
        )
        self.doc["project"] = str(project)
        self.doc["units"][0].update(argv=[sys.executable, "-c", source] + args,
                                    env={"OMP_NUM_THREADS": "2", "JOB_MODE": "export"})
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            launch_unit(store, self.doc, "dummy")
        runtime = store.unit("cpu-run", "dummy")
        self.assertEqual(self.wait_exit(runtime)["exit_code"], 0)
        log = json.loads(Path(runtime["log_path"]).read_text(encoding="utf-8"))
        self.assertEqual(log["argv"], args)
        self.assertEqual(Path(log["cwd"]).resolve(), project.resolve())
        self.assertEqual(log["omp"], "2")
        self.assertEqual(log["mode"], "export")
        self.assertFalse(marker.exists())
        request = json.loads(Path(runtime["request_path"]).read_text(encoding="utf-8"))
        manifest = json.loads(Path(runtime["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(request["argv"], self.doc["units"][0]["argv"])
        self.assertEqual(manifest["argv"], request["argv"])
        reconcile(store, "cpu-run")
        self.assertEqual(store.unit("cpu-run", "dummy")["execution_state"], "finished_unverified")

    def test_duplicate_launch_across_state_directories_never_replays(self):
        from run import launch_unit, reconcile
        marker = self.root / "one-invocation"
        self.doc["units"][0]["argv"] = [
            sys.executable, "-c",
            "import sys,time; open(sys.argv[1],'a').write('one\\n'); time.sleep(.5)",
            str(marker),
        ]
        first = self.store()
        second = self.store(self.doc, self.root / "another-state")
        with patch("run.collect", side_effect=self.resources):
            self.assertIn(launch_unit(first, self.doc, "dummy")["status"], {"submitted", "confirmed"})
            duplicate = launch_unit(second, self.doc, "dummy")
        self.assertTrue(duplicate["already_claimed"])
        runtime = first.unit("cpu-run", "dummy")
        self.wait_exit(runtime)
        self.assertEqual(marker.read_text(encoding="utf-8"), "one\n")
        reconcile(second, "cpu-run")
        refused = launch_unit(second, self.doc, "dummy")
        self.assertEqual(refused["status"], "refused")
        self.assertIn("best-weights", refused["reasons"][0])
        self.assertEqual(second.unit("cpu-run", "dummy")["attempt"], 1)
        self.assertEqual(second.unit("cpu-run", "dummy")["retries_used"], 0)

    def test_attach_and_monitor_controls_preserve_existing_process(self):
        from collectors import process_identity
        from monitor import check_once
        from run import attach, set_monitoring
        self.doc.update(scope_approved=False, allowed_actions=["observe"])
        self.doc["units"][0]["argv"] = []
        store = self.store()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(child.wait, timeout=10)
        before = process_identity(child.pid)
        result = attach(store, "cpu-run", "dummy", child.pid)
        self.assertEqual(result["identity"], before)
        self.assertFalse(result["managed"])
        self.assertTrue(attach(store, "cpu-run", "dummy", child.pid)["already_attached"])
        checked = check_once(store, "cpu-run")
        self.assertEqual(checked["model_submissions"], 0)
        self.assertEqual(store.unit("cpu-run", "dummy")["execution_state"], "running")
        set_monitoring(store, "cpu-run", "paused", "dummy")
        self.assertIsNone(child.poll())
        self.cli("stop-monitor", "--execution", "cpu-run")
        self.assertIsNone(child.poll())
        self.assertEqual(store.unit("cpu-run", "dummy")["execution_state"], "running")
        self.assertEqual(store.unit("cpu-run", "dummy")["monitoring"], "stopped")
        self.cli("resume-monitor", "--execution", "cpu-run", "--unit", "dummy")
        self.assertEqual(store.unit("cpu-run", "dummy")["monitoring"], "active")
        self.assertEqual(process_identity(child.pid), before)
        self.assertEqual(child.wait(timeout=10), 0)

    def test_response_lost_after_real_spawn_is_reconciled_without_resubmission(self):
        from run import launch_unit, reconcile
        marker = self.root / "started-once"
        self.doc["units"][0]["argv"] = [
            sys.executable, "-c", "import sys; open(sys.argv[1],'a').write('once\\n')", str(marker),
        ]
        store = self.store()
        original = subprocess.Popen
        accepted = []

        def lose_response(argv, **kwargs):
            if len(argv) > 1 and argv[1] == str(SCRIPTS / "supervisor.py"):
                # Delay only the supervisor's acknowledgement, never alter the
                # reviewed dummy argv. The real accepted process still runs.
                delayed = [
                    sys.executable, "-c",
                    "import os,sys,time; time.sleep(.3); "
                    "os.execv(sys.executable,[sys.executable]+sys.argv[1:])",
                ] + argv[1:]
                child = original(delayed, **kwargs)
                accepted.append(child)
                raise OSError("OS accepted submission, client lost response")
            return original(argv, **kwargs)

        with patch("run.collect", side_effect=self.resources), patch("run.subprocess.Popen", side_effect=lose_response):
            result = launch_unit(store, self.doc, "dummy")
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(launch_unit(store, self.doc, "dummy")["status"], "unknown")
        self.assertEqual(len(accepted), 1)
        self.addCleanup(accepted[0].wait, timeout=10)
        runtime = store.unit("cpu-run", "dummy")
        self.assertEqual(self.wait_exit(runtime)["exit_code"], 0)
        result = reconcile(store, "cpu-run")
        self.assertEqual(result["units"]["dummy"]["status"], "confirmed")
        self.assertEqual(result["reservations"], [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "once\n")
        self.assertEqual(store.unit("cpu-run", "dummy")["attempt"], 1)
        self.assertEqual(launch_unit(store, self.doc, "dummy")["status"], "refused")
        self.assertEqual(accepted[0].wait(timeout=10), 0)

    def test_completion_baseline_matches_collector_and_old_success_is_not_reused(self):
        from collectors import completion_fingerprint
        from run import launch_unit, reconcile
        report = self.root / "old-report.json"
        report.write_text('{"status":"completed","metric":1}', encoding="utf-8")
        baseline = completion_fingerprint(str(report))
        self.doc["units"][0].update(
            argv=[sys.executable, "-c", "pass"],
            completion={"kind": "json", "path": str(report), "required_fields": ["status", "metric"]},
        )
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            launch_unit(store, self.doc, "dummy")
        runtime = store.unit("cpu-run", "dummy")
        self.assertEqual(runtime["completion_baseline"], baseline)
        self.assertEqual(set(baseline), {"device", "inode", "size", "mtime_ns", "sha256"})
        self.wait_exit(runtime)
        reconcile(store, "cpu-run")
        self.assertEqual(store.unit("cpu-run", "dummy")["execution_state"], "incomplete")
        self.assertEqual(completion_fingerprint(str(report)), baseline)

    def test_fresh_current_completion_is_verified_after_true_zero_exit(self):
        from run import launch_unit, reconcile
        report = self.root / "new-report.json"
        self.doc["units"][0].update(
            argv=[sys.executable, "-c",
                  "import json,sys; json.dump({'status':'completed','metric':1,"
                  "'execution_id':'cpu-run','unit_id':'dummy','attempt':1},open(sys.argv[1],'w'))",
                  str(report)],
            write_paths=[str(report)],
            completion={"kind": "json", "path": str(report), "required_fields": ["status", "metric"]},
        )
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            launch_unit(store, self.doc, "dummy")
        runtime = store.unit("cpu-run", "dummy")
        self.assertIsNone(runtime["completion_baseline"])
        self.assertEqual(self.wait_exit(runtime)["exit_code"], 0)
        reconcile(store, "cpu-run")
        current = store.unit("cpu-run", "dummy")
        self.assertEqual(current["execution_state"], "completed")
        self.assertTrue(current["snapshot"]["fields"]["completion"]["value"]["belongs_to_current"])

    def test_stopping_monitoring_a_managed_attempt_does_not_stop_training(self):
        from collectors import process_matches
        from run import launch_unit, reconcile
        marker = self.root / "natural-finish"
        self.doc["units"][0]["argv"] = [
            sys.executable, "-c", "import sys,time; time.sleep(.7); open(sys.argv[1],'w').write('finished')",
            str(marker),
        ]
        store = self.store()
        with patch("run.collect", side_effect=self.resources):
            launch_unit(store, self.doc, "dummy")
        runtime = store.unit("cpu-run", "dummy")
        self.assertTrue(runtime["managed"])
        self.cli("stop-monitor", "--execution", "cpu-run", "--unit", "dummy")
        self.assertTrue(process_matches(runtime["identity"]))
        self.assertEqual(store.unit("cpu-run", "dummy")["monitoring"], "stopped")
        result = self.wait_exit(runtime)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "finished")
        reconcile(store, "cpu-run")
        self.assertEqual(store.unit("cpu-run", "dummy")["monitoring"], "stopped")
        self.assertEqual(store.unit("cpu-run", "dummy")["finished_at"], result["finished_at"])

    def test_second_unit_waits_for_formal_observations_and_isolation(self):
        from run import launch_unit, reconcile
        units = []
        for name in ("a", "b", "c"):
            units.append({
                "id": name, "argv": [sys.executable, "-c", "import time; time.sleep(1.2)"],
                "devices": [], "resources": {"ram_mb": 1, "disk_mb": 1, "cpu_threads": 1},
                "write_paths": [str(self.root / "results" / name)],
                "independent_entry": True, "isolation_verified": True,
                "evidence": "Reviewed independent CPU dummy entry and disjoint writes",
                "resource_evidence": "Known bounded CPU dummy capacity, no future phases",
            })
        self.doc["units"] = units
        self.doc["max_concurrency"] = 2
        store = self.store()
        with patch("run.collect", side_effect=self.observations):
            self.assertEqual(launch_unit(store, self.doc, "a")["status"], "confirmed")
            denied = launch_unit(store, self.doc, "b")
            self.assertEqual(denied["status"], "refused")
            self.assertTrue(any("formal-run observations" in r for r in denied["reasons"]))
            reconcile(store, "cpu-run", "a")
            reconcile(store, "cpu-run", "a")
            self.assertGreaterEqual(store.unit("cpu-run", "a")["resource_observations"], 2)
            self.assertEqual(launch_unit(store, self.doc, "b")["status"], "confirmed")
            self.assertEqual(launch_unit(store, self.doc, "c")["status"], "refused")
        self.wait_exit(store.unit("cpu-run", "a"))
        self.wait_exit(store.unit("cpu-run", "b"))
        self.assertFalse((self.root / "results").exists())

    def test_default_resource_domain_is_shared_across_state_directories(self):
        from run import launch_unit, resource_lock_path
        self.doc.pop("resource_lock")
        other_doc = contract(self.root, execution="default-other")
        other_doc.pop("resource_lock")
        first = self.store()
        other = self.store(other_doc, self.root / "other-state")
        original = subprocess.Popen

        def lost(argv, **kwargs):
            if len(argv) > 1 and argv[1] == str(SCRIPTS / "supervisor.py"):
                raise OSError("unknown submission")
            return original(argv, **kwargs)

        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg-state")}), \
                patch("run.collect", side_effect=self.resources), \
                patch("run.subprocess.Popen", side_effect=lost):
            self.assertEqual(resource_lock_path(self.doc), resource_lock_path(other_doc))
            self.assertTrue(resource_lock_path(self.doc).is_absolute())
            self.assertEqual(launch_unit(first, self.doc, "dummy")["status"], "unknown")
            denied = launch_unit(other, other_doc, "dummy")
            self.assertEqual(denied["status"], "refused")
            self.assertTrue(any("concurrency" in reason for reason in denied["reasons"]))

    def test_simultaneous_clients_admit_at_most_one_unit(self):
        from run import launch_unit
        barrier = threading.Barrier(2)
        docs = [contract(self.root, execution="race-" + name) for name in ("a", "b")]
        for doc in docs:
            doc["units"][0]["argv"] = [sys.executable, "-c", "import time; time.sleep(.6)"]

        def client(index):
            doc = docs[index]
            store = Store(self.root / ("client-%d" % index))
            try:
                store.save_contract(doc)
                barrier.wait(timeout=10)
                try:
                    return launch_unit(store, doc, "dummy")
                except Busy:
                    return {"status": "busy"}
            finally:
                store.close()

        with patch("run.collect", side_effect=self.resources), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(client, (0, 1)))
        self.assertEqual(sum(r["status"] in {"submitted", "confirmed", "unknown"} for r in results), 1)
        attempts = []
        for index, doc in enumerate(docs):
            store = Store(self.root / ("client-%d" % index))
            self.addCleanup(store.close)
            runtime = store.unit(doc["execution_id"], "dummy")
            if runtime["attempt"]:
                attempts.append(runtime)
        self.assertEqual(len(attempts), 1)
        self.wait_exit(attempts[0])


if __name__ == "__main__":
    unittest.main()
