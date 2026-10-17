"""Read-only collector regression tests: synthetic files/probes and dummy PIDs."""

import ast
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run-pilot/scripts"))

import collectors
import inspect_project
from core import PilotError, field
from monitor import validate_snapshot


def resources(*args):
    return field({
        "gpus": field([], source="synthetic"), "ram_available_mb": field(4096),
        "disk_free_mb": field(8192), "shm_free_mb": field(256), "cpu_threads": field(4),
    }, source="synthetic")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runpilot-collectors-")
        self.root = Path(self.temp.name)
        self.log = self.root / "run.log"
        self.exit = self.root / "exit.json"
        self.report = self.root / "report.json"
        self.doc = {
            "execution_id": "test-run", "project": str(self.root), "target": {"kind": "local"},
            "devices": [], "collection_timeout_seconds": 5,
        }
        self.unit = {"id": "fold1", "log_path": str(self.log), "exit_path": str(self.exit)}
        self.runtime = {"attempt": 1, "started_at": time.time() - 10, "managed": True, "identity": None}
        self.resource_patch = patch.object(collectors, "_collect_resources", side_effect=resources)
        self.resource_patch.start()
        self.addCleanup(self.resource_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def collect(self, remember=True):
        before = copy.deepcopy(self.runtime)
        snapshot = collectors.collect(self.doc, self.unit, self.runtime, self.root / "MUST-NOT-CREATE")
        self.assertEqual(self.runtime, before, "collect must not mutate input runtime/state")
        self.assertFalse((self.root / "MUST-NOT-CREATE").exists())
        validate_snapshot(snapshot)
        self.assertEqual(set(snapshot["fields"]), {
            "process", "exit", "progress", "errors", "completion", "resources", "log",
        })
        if remember:
            self.runtime["log_cursor"] = snapshot["log_cursor"]
        return snapshot["fields"]

    def write_json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def exit_doc(self):
        return {"execution_id": "test-run", "unit_id": "fold1", "attempt": 1,
                "exit_code": 0, "finished_at": time.time() - 1}

    def completion(self, groups=False):
        self.unit["completion"] = {
            "kind": "json", "path": str(self.report), "required_fields": ["status", "metric"],
            "success_field": "status", "success_value": "completed",
        }
        if groups:
            self.unit["completion"].update(expected_groups=["a", "b"], groups_field="groups")
        doc = {"status": "completed", "metric": 0, "groups": ["a", "b"]}
        self.write_json(self.report, doc)
        return doc

    def test_missing_fields_do_not_short_circuit_resources(self):
        fields = self.collect()
        for key in ("process", "exit", "log", "progress", "errors", "completion"):
            self.assertEqual(fields[key]["status"], "not_expected_yet", key)
        self.assertEqual(fields["resources"]["status"], "ok")

    def test_exit_requires_exact_attribution_and_real_result(self):
        correct = self.exit_doc()
        for key, bad in (("execution_id", "other"), ("unit_id", "other"), ("attempt", 0), ("attempt", True)):
            doc = dict(correct, **{key: bad})
            self.write_json(self.exit, doc)
            item = self.collect()["exit"]
            self.assertNotEqual(item["status"], "ok")
            self.assertIsNone(item["value"])
        for key, bad in (("exit_code", True), ("exit_code", "0"), ("finished_at", None),
                         ("finished_at", False), ("finished_at", float("nan"))):
            self.write_json(self.exit, dict(correct, **{key: bad}))
            self.assertEqual(self.collect()["exit"]["status"], "parse_error")
        self.write_json(self.exit, correct)
        item = self.collect()["exit"]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["value"], correct)
        self.assertNotEqual(item["value"]["finished_at"], item["observed_at"])

    def test_missing_exit_key_or_nonobject_is_not_consumed(self):
        for doc in ([], 0, {}, {"execution_id": "test-run"}):
            self.write_json(self.exit, doc)
            self.assertNotEqual(self.collect()["exit"]["status"], "ok")

    def test_huge_json_integers_and_nonfinite_exponents_are_rejected(self):
        self.exit.write_text('{"exit_code":' + "9" * 1000 + "}")
        self.assertEqual(self.collect()["exit"]["status"], "parse_error")
        self.completion()
        self.report.write_text('{"status":"completed","metric":1e999}')
        self.assertEqual(self.collect()["completion"]["status"], "parse_error")

    def test_exit_predating_attempt_is_invalid(self):
        self.write_json(self.exit, dict(self.exit_doc(), finished_at=self.runtime["started_at"] - 1))
        self.assertEqual(self.collect()["exit"]["status"], "parse_error")

    def test_runtime_log_and_exit_paths_override_unit(self):
        self.log.write_text("CUDA out of memory\nstep 999/999\n")
        self.write_json(self.exit, dict(self.exit_doc(), exit_code=99))
        current = self.root / "current.log"
        current_exit = self.root / "current-exit.json"
        current.write_text("step 2/10\n")
        self.write_json(current_exit, self.exit_doc())
        self.runtime.update(log_path=str(current), exit_path=str(current_exit))
        fields = self.collect()
        self.assertEqual(fields["progress"]["value"]["step"], 2)
        self.assertEqual(fields["errors"]["value"], [])
        self.assertEqual(fields["exit"]["value"]["exit_code"], 0)

    def test_completion_fresh_report_without_optional_groups(self):
        self.completion()
        item = self.collect()["completion"]
        self.assertEqual(item["status"], "ok")
        self.assertTrue(item["value"]["passed"])
        self.assertTrue(item["value"]["belongs_to_current"])

    def test_completion_empty_required_mapping_is_not_vacuously_valid(self):
        self.completion()
        self.unit["completion"]["required_fields"] = []
        self.assertEqual(self.collect()["completion"]["status"], "parse_error")

    def test_completion_checks_groups_as_nonempty_exact_set(self):
        doc = self.completion(groups=True)
        for groups in ([], ["a"], ["a", "a", "b"], ["a", "b", "c"], None):
            self.write_json(self.report, dict(doc, groups=groups))
            self.assertFalse(self.collect()["completion"]["value"]["passed"])
        self.write_json(self.report, dict(doc, groups=[{"id": "b"}, {"id": "a"}]))
        self.assertTrue(self.collect()["completion"]["value"]["passed"])
        self.unit["completion"]["expected_groups"] = []
        self.assertEqual(self.collect()["completion"]["status"], "parse_error")

    def test_completion_missing_values_status_and_identity(self):
        doc = self.completion()
        for key, value in (("metric", None), ("metric", []), ("status", "failed")):
            self.write_json(self.report, dict(doc, **{key: value}))
            self.assertFalse(self.collect()["completion"]["value"]["passed"])
        for key, value in (("attempt", 2), ("attempt", True), ("unit_id", "old"), ("execution_id", "old")):
            self.write_json(self.report, dict(doc, **{key: value}))
            value = self.collect()["completion"]["value"]
            self.assertFalse(value["belongs_to_current"])
            self.assertFalse(value["passed"])

    def test_completion_old_timestamp_and_unknown_start_are_excluded(self):
        self.completion()
        old = self.runtime["started_at"] - 20
        os.utime(self.report, (old, old))
        self.assertFalse(self.collect()["completion"]["value"]["belongs_to_current"])
        self.runtime["started_at"] = None
        self.assertFalse(self.collect()["completion"]["value"]["belongs_to_current"])

    def test_launch_baseline_fingerprint_and_content_hash(self):
        doc = self.completion()
        baseline = collectors.completion_fingerprint(self.report)
        self.assertEqual(set(baseline), {"device", "inode", "size", "mtime_ns", "sha256"})
        self.runtime["completion_baseline"] = baseline
        self.assertFalse(self.collect()["completion"]["value"]["belongs_to_current"])
        self.write_json(self.report, dict(doc, metric=1))  # same length, same inode
        os.utime(self.report, ns=(baseline["mtime_ns"], baseline["mtime_ns"]))
        value = self.collect()["completion"]["value"]
        self.assertTrue(value["passed"], "hash must distinguish a same-size same-mtime replacement")
        self.assertNotEqual(value["fingerprint"]["sha256"], baseline["sha256"])
        self.assertIsNone(collectors.completion_fingerprint(self.root / "missing.json"))

    def test_completion_touch_or_copy_old_content_does_not_become_current(self):
        self.completion()
        self.runtime["completion_baseline"] = collectors.completion_fingerprint(self.report)
        before = self.runtime["completion_baseline"]
        os.utime(self.report, ns=(before["mtime_ns"] + 1000000000, before["mtime_ns"] + 1000000000))
        self.assertFalse(self.collect()["completion"]["value"]["belongs_to_current"])
        data = self.report.read_bytes()
        self.report.unlink()
        self.report.write_bytes(data)
        value = self.collect()["completion"]["value"]
        self.assertFalse(value["belongs_to_current"])
        self.assertFalse(value["passed"])

    def test_per_group_required_and_exact_result_constraints(self):
        doc = self.completion(groups=True)
        self.unit["completion"].update(
            group_required_fields=["metric", "subject_count"],
            group_equals={"subject_count": 128, "status": "completed"},
        )
        correct = [
            {"id": "a", "metric": 0, "subject_count": 128, "status": "completed"},
            {"id": "b", "metric": 0.5, "subject_count": 128, "status": "completed"},
        ]
        self.write_json(self.report, dict(doc, groups=correct))
        self.assertTrue(self.collect()["completion"]["value"]["passed"])
        for key, wrong in (("subject_count", None), ("subject_count", 127), ("subject_count", True),
                           ("metric", []), ("status", "failed")):
            groups = copy.deepcopy(correct)
            groups[1][key] = wrong
            self.write_json(self.report, dict(doc, groups=groups))
            value = self.collect()["completion"]["value"]
            self.assertTrue(value["belongs_to_current"])
            self.assertFalse(value["passed"], (key, wrong))
        groups = copy.deepcopy(correct)
        del groups[1]["subject_count"]
        self.write_json(self.report, dict(doc, groups=groups))
        self.assertFalse(self.collect()["completion"]["value"]["passed"])
        self.write_json(self.report, dict(doc, groups=["a", "b"]))
        self.assertFalse(self.collect()["completion"]["value"]["passed"])
        self.write_json(self.report, dict(doc, groups={r["id"]: r for r in correct}))
        self.assertTrue(self.collect()["completion"]["value"]["passed"])

    def test_group_constraint_configuration_is_bounded_and_never_ignored(self):
        self.completion(groups=True)
        for key, value in (
            ("group_required_fields", []), ("group_equals", {}), ("group_equals", "script.py"),
            ("group_equals", {"subject_count": list(range(1000))}),
        ):
            self.unit["completion"].pop("group_equals", None)
            self.unit["completion"].pop("group_required_fields", None)
            self.unit["completion"][key] = value
            self.assertEqual(self.collect()["completion"]["status"], "parse_error", (key, value))
        self.unit["completion"]["group_equals"] = {"subject_count": 128}
        self.unit["completion"].pop("expected_groups")
        self.assertEqual(self.collect()["completion"]["status"], "parse_error")

    def test_json_oversize_and_nonregular_sources_are_bounded(self):
        self.completion()
        self.report.write_bytes(b" " * (collectors.JSON_LIMIT + 1))
        self.assertNotEqual(self.collect()["completion"]["status"], "ok")
        with self.assertRaises(PilotError):
            collectors.completion_fingerprint(self.report)
        self.runtime["exit_path"] = str(self.root)
        self.assertEqual(self.collect()["exit"]["status"], "unavailable")
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo"
            os.mkfifo(fifo)
            self.runtime["log_path"] = str(fifo)
            before = time.monotonic()
            self.assertEqual(self.collect()["log"]["status"], "unavailable")
            self.assertLess(time.monotonic() - before, 1)

    def test_dotted_report_mappings(self):
        self.completion()
        self.unit["completion"].update(required_fields=["result.metric"], success_field="result.state")
        self.write_json(self.report, {"result": {"metric": 0, "state": "completed"}})
        self.assertTrue(self.collect()["completion"]["value"]["passed"])

    def test_managed_log_reads_from_zero_once(self):
        self.log.write_text('{"phase":"train","step":1,"total":20}\nCUDA out of memory\n')
        fields = self.collect()
        self.assertEqual(fields["progress"]["value"], {
            "phase": "train", "step": 1, "total": 20, "work_kind": "step",
        })
        self.assertEqual([e["kind"] for e in fields["errors"]["value"]], ["cuda_oom"])
        second = self.collect()
        self.assertEqual(second["errors"]["value"], [])
        self.assertEqual(second["progress"]["status"], "not_expected_yet")
        self.assertEqual(second["log"]["value"]["bytes_read"], 0)

    def test_attachment_baselines_historical_errors_and_partial_line(self):
        self.runtime["managed"] = False
        self.log.write_text("CUDA out of memory\nstep 99/100\nold CUDA out of")
        fields = self.collect()
        self.assertTrue(fields["log"]["value"]["baselined"])
        self.assertEqual(fields["errors"]["value"], [])
        self.assertIsNone(fields["progress"]["value"])
        with self.log.open("a") as stream:
            stream.write(" memory\nstep 2/10\n")
        fields = self.collect()
        self.assertEqual(fields["errors"]["value"], [])
        self.assertEqual(fields["progress"]["value"]["step"], 2)

    def test_attachment_absent_log_creation_is_new_data(self):
        self.runtime["managed"] = False
        self.collect()
        self.log.write_text("step 3/10\n")
        self.assertEqual(self.collect()["progress"]["value"]["step"], 3)

    def test_partial_utf8_and_ansi_and_cr_are_incremental(self):
        data = '\x1b[32m{"phase":"训练","step":4,"total":10}\x1b[0m'.encode()
        split = data.index("训".encode()) + 1
        self.log.write_bytes(data[:split])
        self.assertIsNone(self.collect()["progress"]["value"])
        self.assertTrue(self.runtime["log_cursor"]["partial"])
        with self.log.open("ab") as stream:
            stream.write(data[split:] + b"\rstep 5/10 phase=train\rstep 6")
        self.assertEqual(self.collect()["progress"]["value"]["step"], 5)
        with self.log.open("ab") as stream:
            stream.write(b"/10 phase=train\n")
        self.assertEqual(self.collect()["progress"]["value"]["step"], 6)

    def test_rotation_does_not_join_old_partial_with_new_file(self):
        self.log.write_text("CUDA out of")
        self.collect()
        self.log.rename(self.root / "run.log.1")
        self.log.write_text(" memory\nstep 2/10\n")
        fields = self.collect()
        self.assertTrue(fields["log"]["value"]["rotated"])
        self.assertEqual(fields["errors"]["value"], [])
        self.assertEqual(fields["progress"]["value"]["step"], 2)

    def test_truncation_and_regrown_copytruncate_are_detected(self):
        self.log.write_text("step 100/999\n" + "old text\n" * 20)
        self.collect()
        self.log.write_text("step 1/999\n")
        fields = self.collect()
        self.assertTrue(fields["log"]["value"]["truncated"])
        self.assertEqual(fields["progress"]["value"]["step"], 1)
        self.log.write_text("new content\n" * 30 + "step 2/999\n")
        fields = self.collect()
        self.assertTrue(fields["log"]["value"]["truncated"], "anchor detects truncate/regrow past old offset")
        self.assertEqual(fields["progress"]["value"]["step"], 2)

    def test_attempt_change_resets_managed_but_rebaselines_attached(self):
        self.log.write_text("step 4/10\n")
        self.collect()
        self.runtime["attempt"] = 2
        self.assertEqual(self.collect()["progress"]["value"]["step"], 4)
        self.runtime.update(attempt=3, managed=False)
        self.assertIsNone(self.collect()["progress"]["value"])

    def test_error_kinds_do_not_conflate_host_nccl_cuda_or_killed(self):
        self.log.write_text(
            "CUDA out of memory\nMemoryError: no host RAM\nNCCL WARN unhandled system error\n"
            "Killed\nNCCL version 2.0\n"
        )
        kinds = [e["kind"] for e in self.collect()["errors"]["value"]]
        self.assertEqual(kinds, ["cuda_oom", "host_oom", "nccl"])

    def test_explicit_wrong_attempt_json_log_is_ignored(self):
        self.log.write_text(
            '{"attempt":0,"step":999,"message":"CUDA out of memory"}\n'
            '{"execution_id":"other","step":777,"message":"MemoryError"}\nstep 1/10\n'
        )
        fields = self.collect()
        self.assertEqual(fields["errors"]["value"], [])
        self.assertEqual(fields["progress"]["value"]["step"], 1)

    def test_error_evidence_is_redacted(self):
        self.log.write_text("CUDA out of memory token=private_value\n")
        fields = self.collect()
        self.assertNotIn("private_value", json.dumps(fields))

    def test_malformed_progress_and_oversize_partial_do_not_guess(self):
        self.log.write_text('{"step":true,"total":5}\n')
        self.assertEqual(self.collect()["progress"]["status"], "parse_error")
        with self.log.open("a") as stream:
            stream.write("x" * (collectors.LINE_LIMIT + 1))
        self.collect()
        self.assertEqual(self.runtime["log_cursor"]["partial"], "")
        with self.log.open("a") as stream:
            stream.write("CUDA out of memory\nstep 1/5\n")
        fields = self.collect()
        self.assertEqual(fields["errors"]["value"], [])
        self.assertEqual(fields["progress"]["value"]["step"], 1)

    def test_large_log_uses_incremental_byte_budget(self):
        self.log.write_bytes(b"step 1/5\n" * 60)
        with patch.object(collectors, "LOG_LIMIT", 64):
            fields = self.collect()
            self.assertLessEqual(fields["log"]["value"]["bytes_read"], 64)
            self.assertTrue(fields["log"]["value"]["has_more"])
            offset = self.runtime["log_cursor"]["offset"]
            self.collect()
            self.assertEqual(self.runtime["log_cursor"]["offset"], offset + 64)

    def test_unreachable_transport_does_not_probe_local_or_network(self):
        self.doc["target"] = {"kind": "ssh", "host": "synthetic.invalid"}
        self.log.write_text("step 999/999\n")
        with patch.object(collectors, "_command", side_effect=AssertionError("no commands allowed")):
            snapshot = collectors.collect(self.doc, self.unit, self.runtime, self.root)
        self.assertFalse(snapshot["reachable"])
        self.assertEqual(snapshot["access"], "unsupported")
        self.assertTrue(all(item["status"] == "unavailable" for item in snapshot["fields"].values()))


class ProcessTests(unittest.TestCase):
    def test_linux_comm_with_parentheses_and_zombies(self):
        with tempfile.TemporaryDirectory(prefix="runpilot-proc-") as temporary:
            root = Path(temporary)
            (root / "123").mkdir()
            (root / "sys/kernel/random").mkdir(parents=True)
            (root / "sys/kernel/random/boot_id").write_text("synthetic-boot\n")
            (root / "stat").write_text("cpu 0 0 0\nbtime 1000\n")
            tail = ["S"] + ["0"] * 18 + ["500"] + ["0"] * 5
            target = root / "123/stat"
            target.write_text("123 (worker name ) with ) parentheses) " + " ".join(tail))
            with patch.object(collectors, "PROC", root), patch.object(collectors.sys, "platform", "linux"), \
                    patch.object(collectors.os, "sysconf", return_value=100):
                identity = collectors.process_identity(123)
                self.assertEqual(identity, {"pid": 123, "start_time": "500", "boot_id": "synthetic-boot",
                                            "start_epoch": 1005.0})
                self.assertTrue(collectors.process_matches(identity))
                self.assertFalse(collectors.process_matches(dict(identity, start_time="501")))
                self.assertFalse(collectors.process_matches(dict(identity, boot_id="old-boot")))
                tail[0] = "Z"
                target.write_text("123 (zombie)) " + " ".join(tail))
                with self.assertRaises(ProcessLookupError):
                    collectors.process_identity(123)
                self.assertFalse(collectors.process_matches(identity))

    def test_mac_ps_identity_and_zombie(self):
        def fake(argv, deadline, **kwargs):
            return ("123 Fri Oct 16 00:00:00 2026 S+\n" if argv[0] == "/bin/ps"
                    else "{ sec = 1000, usec = 123 } Fri Oct 16\n")
        with patch.object(collectors.sys, "platform", "darwin"), patch.object(collectors, "_command", side_effect=fake):
            identity = collectors.process_identity(123)
            self.assertEqual(identity["start_time"], "Fri Oct 16 00:00:00 2026")
            self.assertEqual(identity["boot_id"], "btime:1000:123")
            self.assertTrue(collectors.process_matches(identity))
        with patch.object(collectors.sys, "platform", "darwin"), patch.object(
            collectors, "_command", return_value="123 Fri Oct 16 00:00:00 2026 Z\n",
        ):
            with self.assertRaises(ProcessLookupError):
                collectors.process_identity(123)

    def test_mac_ps_empty_exit_one_only_is_disappearance(self):
        with patch.object(collectors.sys, "platform", "darwin"):
            with patch.object(collectors, "_command", side_effect=collectors._CommandFailure(1, "", "")):
                with self.assertRaises(ProcessLookupError):
                    collectors.process_identity(123)
                process = collectors._process_field({"identity": {"pid": 123}}, time.time(), time.monotonic() + 1)
                self.assertEqual(process["status"], "ok")
                self.assertFalse(process["value"]["alive"])
            for error in (
                collectors._CommandFailure(1, "", "permission denied"),
                collectors._CommandFailure(2, "", ""),
                collectors._CommandFailure(1, "unexpected output", ""),
                collectors._ObservationError("timeout"),
            ):
                with patch.object(collectors, "_command", side_effect=error):
                    process = collectors._process_field(
                        {"identity": {"pid": 123}}, time.time(), time.monotonic() + 1,
                    )
                    self.assertEqual(process["status"], "unavailable")
                    self.assertIsNone(process["value"])

    def test_invalid_and_partial_identity_fail_closed(self):
        for value in (None, {}, {"pid": True}, {"pid": 1}, {"pid": -1}):
            self.assertFalse(collectors.process_matches(value))
        for pid in (0, -1, True, "123"):
            with self.assertRaises(ValueError):
                collectors.process_identity(pid)

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "POSIX identities only")
    def test_current_local_dummy_process_identity(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            identity = collectors.process_identity(child.pid)
            self.assertTrue(collectors.process_matches(identity))
            self.assertFalse(collectors.process_matches(dict(identity, start_time="definitely-not-current")))
            self.assertTrue(identity["boot_id"])
            self.assertGreater(identity["start_epoch"], 0)
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertFalse(collectors.process_matches(identity))

    def test_probe_bounds_output_and_elapsed_time(self):
        before = time.monotonic()
        with self.assertRaises(PilotError):
            collectors._command(
                [sys.executable, "-c", "import os,time; os.write(1,b'x'*65536); time.sleep(5)"],
                time.monotonic() + 2, output_limit=1024,
            )
        self.assertLess(time.monotonic() - before, 2)
        before = time.monotonic()
        with self.assertRaises(PilotError):
            collectors._command([sys.executable, "-c", "import time; time.sleep(5)"],
                                time.monotonic() + 0.1)
        self.assertLess(time.monotonic() - before, 1)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runpilot-resources-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.identity = {"pid": 123, "start_time": "100", "boot_id": "boot", "start_epoch": 1}
        self.runtime = {"identity": self.identity, "attempt": 1}
        self.process = field({"alive": True, "identity_matches": True, "identity": self.identity,
                              "actual_gpu_uuids": None})
        self.doc = {"project": str(self.root), "devices": ["GPU-aaa", "GPU-bbb"]}
        self.unit = {"id": "fold1", "devices": ["GPU-bbb"], "env": {"CUDA_VISIBLE_DEVICES": "1"}}
        self.gpus = "GPU-aaa, 0, Fixture A, 4000, 8000, Default, 0, 30\n" \
                    "GPU-bbb, 1, Fixture B, 5000, 9000, Exclusive_Process, 10, 40\n"

    def gpu(self, apps="GPU-bbb, 123, 64\n", process=True):
        def fake(argv, deadline, **kwargs):
            return self.gpus if tuple(argv) == collectors.GPU_QUERY else apps
        with patch.dict(os.environ, {}, clear=True), patch.object(collectors, "_command", side_effect=fake), \
                patch.object(collectors, "_process_identity", return_value=self.identity), \
                patch.object(collectors, "_read_bytes", return_value=b""):
            return collectors._gpu_resources(self.doc, self.unit, self.runtime, self.process if process else field(),
                                             time.time(), time.monotonic() + 5)

    def test_gpu_preserves_uuid_index_mode_and_process_count(self):
        item, actual, detail = self.gpu()
        self.assertEqual(item["status"], "ok")
        row = item["value"][0]
        self.assertEqual(row["uuid"], "GPU-bbb")
        self.assertEqual(row["index"], 1)
        self.assertEqual(row["compute_mode"], "Exclusive_Process")
        self.assertEqual(row["process_count"], 1)
        self.assertEqual(row["target_memory_mb"], 64)
        self.assertEqual(actual, ["GPU-bbb"])

    def test_assignment_is_not_proof_of_actual_use(self):
        item, actual, detail = self.gpu(apps="GPU-aaa, 123, 64\n")
        self.assertEqual(actual, ["GPU-aaa"])
        self.assertEqual(item["value"][0]["uuid"], "GPU-bbb")
        self.assertEqual(item["value"][0]["process_count"], 0)
        self.process["value"]["identity_matches"] = False
        _, actual, _ = self.gpu()
        self.assertIsNone(actual)
        self.process["value"]["identity_matches"] = True
        _, actual, _ = self.gpu(apps="")
        self.assertIsNone(actual, "no positive mapping must not become an observed empty/CPU-only list")

    def test_failed_app_query_does_not_invent_zero_processes_or_actual_devices(self):
        def fake(argv, deadline, **kwargs):
            if tuple(argv) == collectors.GPU_QUERY:
                return self.gpus
            raise collectors._ObservationError("synthetic unavailable compute-app query")
        with patch.dict(os.environ, {}, clear=True), patch.object(collectors, "_command", side_effect=fake), \
                patch.object(collectors, "_read_bytes", return_value=b""):
            item, actual, detail = collectors._gpu_resources(
                self.doc, self.unit, self.runtime, self.process, time.time(), time.monotonic() + 5,
            )
        self.assertEqual(item["status"], "ok")
        self.assertIsNone(item["value"][0]["process_count"])
        self.assertIsNone(actual)

    def test_visibility_prefix_and_container_budget(self):
        self.unit["env"] = {"CUDA_VISIBLE_DEVICES": "GPU-bbb"}
        with patch.dict(os.environ, {"NVIDIA_VISIBLE_DEVICES": "GPU-aaa"}, clear=True), \
                patch.object(collectors, "_command", side_effect=[self.gpus, ""]):
            item, _, _ = collectors._gpu_resources(self.doc, self.unit, {}, field(), time.time(), time.monotonic() + 5)
        self.assertEqual(item["status"], "unavailable")
        rows = [{"uuid": "GPU-abc123", "index": 0}, {"uuid": "GPU-def456", "index": 1}]
        self.assertEqual(collectors._visible_rows(rows, "GPU-abc", "CUDA_VISIBLE_DEVICES"), rows[:1])
        self.assertEqual(collectors._visible_rows(rows, "-1", "CUDA_VISIBLE_DEVICES"), [])
        with self.assertRaises(PilotError):
            collectors._visible_rows(rows, "MIG-fixture", "CUDA_VISIBLE_DEVICES")

    def test_unknown_gpu_memory_not_zero(self):
        self.gpus = self.gpus.replace("5000", "[N/A]")
        item, _, _ = self.gpu()
        self.assertIsNone(item["value"][0]["memory_free_mb"])

    def test_cgroup_v2_ancestor_cpu_memory_and_cpuset_limits(self):
        proc, mount = self.root / "proc", self.root / "cgroup"
        (proc / "self").mkdir(parents=True)
        (mount / "parent/worker").mkdir(parents=True)
        (proc / "self/cgroup").write_text("0::/parent/worker\n")
        (proc / "self/mountinfo").write_text("1 0 0:1 / %s rw - cgroup2 cgroup rw\n" % mount)
        (mount / "cpu.max").write_text("max 100000")
        (mount / "memory.max").write_text("max")
        (mount / "parent/cpu.max").write_text("150000 100000")
        (mount / "parent/memory.max").write_text(str(1024 * 1024 * 100))
        (mount / "parent/memory.current").write_text(str(1024 * 1024 * 40))
        (mount / "parent/worker/cpu.max").write_text("200000 100000")
        (mount / "parent/worker/cpuset.cpus.effective").write_text("0-3,6")
        (mount / "parent/worker/memory.max").write_text(str(1024 * 1024 * 200))
        (mount / "parent/worker/memory.current").write_text(str(1024 * 1024 * 10))
        with patch.object(collectors, "PROC", proc):
            cpu, ram, errors = collectors._cgroup_budget("self", time.monotonic() + 2)
        self.assertFalse(errors)
        self.assertEqual(min(cpu), 1.5)
        self.assertEqual(min(ram), 60)
        self.assertIn(5, cpu)

    def test_cgroup_v1_cpu_and_memory(self):
        proc, cpu, memory = self.root / "proc", self.root / "cpu", self.root / "memory"
        (proc / "self").mkdir(parents=True)
        (cpu / "worker").mkdir(parents=True)
        (memory / "worker").mkdir(parents=True)
        (proc / "self/cgroup").write_text("2:cpu,cpuacct:/worker\n3:memory:/worker\n")
        (proc / "self/mountinfo").write_text(
            "1 0 0:1 / %s rw - cgroup cgroup rw,cpu,cpuacct\n"
            "2 0 0:2 / %s rw - cgroup cgroup rw,memory\n" % (cpu, memory)
        )
        (cpu / "worker/cpu.cfs_quota_us").write_text("50000")
        (cpu / "worker/cpu.cfs_period_us").write_text("100000")
        (memory / "worker/memory.limit_in_bytes").write_text(str(20 * 1024 * 1024))
        (memory / "worker/memory.usage_in_bytes").write_text(str(8 * 1024 * 1024))
        with patch.object(collectors, "PROC", proc):
            quotas, ram, errors = collectors._cgroup_budget("self", time.monotonic() + 2)
        self.assertFalse(errors)
        self.assertEqual(min(quotas), 0.5)
        self.assertEqual(min(ram), 12)

    def test_partially_invisible_cgroup_controllers_do_not_widen_host_budget(self):
        proc, cpu = self.root / "proc", self.root / "cpu"
        (proc / "self").mkdir(parents=True)
        cpu.mkdir()
        (proc / "self/cgroup").write_text("2:cpu:/worker\n3:memory:/worker\n")
        (proc / "self/mountinfo").write_text("1 0 0:1 / %s rw - cgroup cgroup rw,cpu\n" % cpu)
        with patch.object(collectors, "PROC", proc):
            _, _, errors = collectors._cgroup_budget("self", time.monotonic() + 2)
        self.assertIn("ram", errors)
        self.assertIn("cpu", errors)

    def test_host_resources_use_affinity_and_do_not_mask_probe_failure(self):
        with patch.object(collectors.sys, "platform", "linux"), \
                patch.object(collectors, "_cgroup_budget", return_value=([1.5], [64], {})), \
                patch.object(collectors, "_ram_available", return_value=4000), \
                patch.object(collectors.os, "cpu_count", return_value=64), \
                patch.object(collectors.os, "sched_getaffinity", return_value={0, 1}, create=True), \
                patch.object(collectors, "_gpu_resources", return_value=(field([], status="unavailable"), None, "unknown")):
            result = collectors._collect_resources(
                self.doc, self.unit, {}, field(), time.time(), time.monotonic() + 2,
            )["value"]
        self.assertEqual(set(result), set(collectors.RESOURCE_NAMES))
        self.assertEqual(result["cpu_threads"]["value"], 1.5)
        self.assertEqual(result["ram_available_mb"]["value"], 64)
        self.assertEqual(result["gpus"]["status"], "unavailable")
        self.assertEqual(result["disk_free_mb"]["status"], "ok")

    def test_disk_uses_output_parent_without_creating_it(self):
        output = self.root / "future/output"
        unit = dict(self.unit, write_paths=[str(output)])
        amount, values = collectors._disk_available(self.doc, unit, time.monotonic() + 2)
        self.assertGreaterEqual(amount, 0)
        self.assertEqual(values[0]["observed_path"], str(self.root))
        self.assertFalse(output.exists())

    def test_collection_commands_share_one_deadline(self):
        deadlines = []

        def fake(argv, deadline, **kwargs):
            deadlines.append(deadline)
            return self.gpus if tuple(argv) == collectors.GPU_QUERY else ""

        doc = dict(self.doc, target={"kind": "local"}, execution_id="budget", collection_timeout_seconds=0.2)
        with patch.dict(os.environ, {}, clear=True), patch.object(collectors, "_command", side_effect=fake), \
                patch.object(collectors, "_cgroup_budget", return_value=([], [], {})), \
                patch.object(collectors, "_ram_available", return_value=4000):
            collectors.collect(doc, self.unit, {"attempt": 0}, self.root)
        self.assertEqual(len(deadlines), 2)
        self.assertEqual(len(set(deadlines)), 1)


class StaticInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runpilot-inspect-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def project(self):
        marker = self.root / "MUST-NOT-EXECUTE"
        (self.root / "train.py").write_text(
            "from pathlib import Path\nPath(%r).write_text('executed')\n"
            "import argparse\nparser = argparse.ArgumentParser()\n"
            "parser.add_argument('--batch-size', default=16)\nseed = 42\n"
            "if __name__ == '__main__':\n    raise RuntimeError('never run this')\n" % str(marker)
        )
        (self.root / "README.md").write_text("Run existing train.py; not authorization.\n")
        return marker

    def test_inspection_is_static_bounded_and_does_not_execute_help(self):
        marker = self.project()
        with patch.object(subprocess, "Popen", side_effect=AssertionError("no project commands")):
            report = inspect_project.inspect_project(self.root, [sys.executable, "train.py", "--help"])
        self.assertFalse(marker.exists())
        self.assertFalse(report["project_imported"])
        self.assertFalse(report["command_executed"])
        self.assertEqual(report["candidates"][0]["path"], "train.py")
        self.assertEqual(report["candidates"][0]["arguments"][0]["static_default"], "16")
        self.assertEqual(report["configuration_hints"][0]["effective"], "unknown")
        self.assertLessEqual(report["counts"]["bytes_read"], inspect_project.MAX_TOTAL_BYTES)

    def test_excludes_data_weights_venvs_and_all_symlinks(self):
        self.project()
        for name in ("data", "weights", ".venv", "datasets", "checkpoints"):
            directory = self.root / name
            directory.mkdir()
            (directory / "train.py").write_text("raise RuntimeError('do not inspect')\n")
        (self.root / "linked.py").symlink_to(self.root / "train.py")
        (self.root / "linked-dir").symlink_to(self.root / "data", target_is_directory=True)
        report = inspect_project.inspect_project(self.root)
        self.assertEqual({f["path"] for f in report["files"]}, {"train.py", "README.md"})
        self.assertEqual(report["counts"]["symlinks_skipped"], 2)
        self.assertEqual(report["counts"]["excluded_directories"], 5)

    def test_file_entry_and_byte_limits_are_explicit(self):
        self.project()
        for i in range(20):
            (self.root / ("config%d.json" % i)).write_text('{"seed": 42}')
        with patch.object(inspect_project, "MAX_ENTRIES", 5):
            report = inspect_project.inspect_project(self.root)
        self.assertLessEqual(report["counts"]["entries"], 5)
        self.assertTrue(report["truncated"])
        with patch.object(inspect_project, "MAX_TOTAL_BYTES", 32), \
                patch.object(inspect_project, "MAX_FILE_BYTES", 16):
            report = inspect_project.inspect_project(self.root)
        self.assertLessEqual(report["counts"]["bytes_read"], 32)
        self.assertTrue(report["truncated"])

    def test_rejects_relative_project_and_symlink_root_and_bad_command(self):
        with self.assertRaises(PilotError):
            inspect_project.inspect_project("relative")
        linked = self.root / "linked"
        actual = self.root / "actual"
        actual.mkdir()
        linked.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(PilotError):
            inspect_project.inspect_project(linked)
        for command in ("python train.py", {}, [], ["python", 3], ["python", "\0"]):
            with self.assertRaises(PilotError):
                inspect_project.inspect_project(self.root, command)

    def test_command_metacharacters_recorded_not_executed_and_secrets_redacted(self):
        marker = self.project()
        command = ["python3", "train.py", "--name", "$(touch NOT_ALLOWED)", "--token", "secret-value"]
        report = inspect_project.inspect_project(self.root, command)
        self.assertFalse(marker.exists())
        self.assertEqual(report["provided_command"][3], command[3])
        self.assertNotIn("secret-value", json.dumps(report))

    def test_cli_outputs_json_without_running_project(self):
        marker = self.project()
        result = subprocess.run(
            [sys.executable, str(Path(inspect_project.__file__)), "--project", str(self.root),
             "--command-json", json.dumps(["python3", "train.py", "--help"])],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(marker.exists())
        self.assertTrue(json.loads(result.stdout)["read_only"])
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(inspect_project.main(["--project", str(self.root), "--command-json", "{}"]), 2)
            self.assertEqual(inspect_project.main(["--project", str(self.root), "--command-json", "null"]), 2)

    def test_new_sources_parse_as_python39(self):
        paths = [Path(collectors.__file__), Path(inspect_project.__file__), Path(__file__)]
        for path in paths:
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
