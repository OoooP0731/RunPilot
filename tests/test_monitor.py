import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run-pilot/scripts"))
from core import Busy, PilotError, Store, field, now
from monitor import _throughput_drain, apply_snapshot, check_once, dispatch


def contract(root, notification="none"):
    return {
        "schema_version": 1, "execution_id": "fixture-run", "project": str(root),
        "target": {"kind": "fixture"}, "allowed_actions": ["observe"],
        "units": [{"id": "seed43", "argv": []}],
        "reserves": {"ram_mb": 10, "disk_mb": 10},
        "notification": {"kind": notification, "path": str(root / "notifications"), "max_attempts": 3},
    }


def snapshot(alive=True, code=None, reachable=True):
    timestamp = now()
    return {
        "observed_at": timestamp, "reachable": reachable,
        "fields": {
            "process": field({"alive": alive, "identity_matches": True}, source="fixture"),
            "exit": field(
                {"execution_id": "fixture-run", "unit_id": "seed43", "attempt": 1,
                 "exit_code": code, "finished_at": timestamp - 10} if code is not None else None,
                status="ok" if code is not None else "not_expected_yet", source="fixture",
            ),
            "completion": field(status="not_expected_yet"),
            "progress": field({"phase": "train", "step": 10, "total": 100}),
            "errors": field([]),
            "resources": field({
                "ram_available_mb": field(1000), "disk_free_mb": field(1000),
                "cpu_threads": field(4), "shm_free_mb": field(1000),
                "gpus": field(status="unavailable"),
            }),
        },
    }


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runpilot-test-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state")
        self.doc = contract(self.root)
        self.store.save_contract(self.doc)
        self.running()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def running(self):
        runtime = self.store.unit("fixture-run", "seed43")
        runtime.update(execution_state="running", attempt=1,
                       identity={"pid": 99999999, "start_time": "not-real", "boot_id": "fake"})
        self.store.save_unit("fixture-run", "seed43", runtime)

    def check(self, data, **kwargs):
        return check_once(self.store, "fixture-run", fixture=data, **kwargs)

    def test_normal_progress_never_notifies_or_submits_models(self):
        for step in range(10, 20):
            data = snapshot()
            data["fields"]["progress"]["value"]["step"] = step
            result = self.check(data)
            self.assertEqual(result["model_submissions"], 0)
            self.assertEqual(result["events"], 0)
        self.assertEqual(self.store.event_rows("fixture-run"), [])

    def test_unreachable_preserves_execution_and_deduplicates(self):
        data = snapshot(reachable=False)
        self.check(data)
        self.check(data)
        current = self.store.unit("fixture-run", "seed43")
        self.assertEqual(current["execution_state"], "running")
        self.assertEqual(current["access"], "unreachable")
        self.assertEqual(len(self.store.event_rows("fixture-run")), 1)
        self.check(snapshot())
        self.assertEqual(len(self.store.event_rows("fixture-run")), 2)
        self.check(snapshot(reachable=False))
        self.assertEqual(len(self.store.event_rows("fixture-run")), 3)

    def test_missing_exit_file_is_unknown_not_success_or_failure(self):
        self.check(snapshot(alive=False))
        current = self.store.unit("fixture-run", "seed43")
        self.assertEqual(current["execution_state"], "finished_unverified")
        self.assertIsNone(current.get("exit_result"))
        self.assertIsNone(current.get("finished_at"))

    def test_wrong_attempt_exit_is_not_consumed(self):
        data = snapshot(alive=False, code=0)
        data["fields"]["exit"]["value"]["attempt"] = 0
        self.check(data)
        self.assertEqual(self.store.unit("fixture-run", "seed43")["execution_state"], "finished_unverified")

    def test_current_nonzero_exit_is_failure_without_fabricated_finish_time(self):
        data = snapshot(alive=False, code=2)
        finish = data["fields"]["exit"]["value"]["finished_at"]
        self.check(data)
        runtime = self.store.unit("fixture-run", "seed43")
        self.assertEqual(runtime["execution_state"], "failed")
        self.assertEqual(runtime["finished_at"], finish)
        self.assertGreater(runtime["record_written_at"], finish)

    def test_exit_zero_does_not_override_failed_completion(self):
        doc = copy.deepcopy(self.doc)
        doc["execution_id"] = "completion-run"
        doc["units"][0]["completion"] = {
            "kind": "json", "path": str(self.root / "report.json"), "required_fields": ["metric"],
        }
        self.store.save_contract(doc)
        runtime = self.store.unit("completion-run", "seed43")
        runtime.update(execution_state="running", identity={"pid": 999}, attempt=1)
        self.store.save_unit("completion-run", "seed43", runtime)
        data = snapshot(alive=False, code=0)
        data["fields"]["exit"]["value"]["execution_id"] = "completion-run"
        data["fields"]["completion"] = field({"passed": False, "belongs_to_current": False})
        check_once(self.store, "completion-run", fixture=data)
        self.assertEqual(self.store.unit("completion-run", "seed43")["execution_state"], "incomplete")

    def test_process_identity_changed_is_not_same_process(self):
        data = snapshot()
        data["fields"]["process"]["value"]["identity_matches"] = False
        self.check(data)
        self.assertEqual(self.store.unit("fixture-run", "seed43")["execution_state"], "finished_unverified")
        self.assertTrue(self.store.unit("fixture-run", "seed43")["freeze_admission"])

    def test_unavailable_metric_retains_last_good_not_zero(self):
        self.check(snapshot())
        data = snapshot()
        data["fields"]["resources"] = field(None, status="unavailable")
        self.check(data)
        runtime = self.store.unit("fixture-run", "seed43")
        last_good = runtime["last_good"]["resources"]["value"]["ram_available_mb"]["value"]
        self.assertEqual(last_good, 1000)
        self.assertIsNone(runtime["snapshot"]["fields"]["resources"]["value"])

    def test_future_file_missing_does_not_lose_process_progress(self):
        data = snapshot()
        data["fields"]["completion"] = field(None, status="not_expected_yet", source="future-report")
        self.check(data)
        runtime = self.store.unit("fixture-run", "seed43")
        self.assertEqual(runtime["execution_state"], "running")
        self.assertEqual(runtime["last_good"]["progress"]["value"]["step"], 10)

    def test_logged_caught_oom_is_not_training_failure(self):
        data = snapshot()
        data["fields"]["errors"] = field([{"kind": "cuda_oom", "evidence": "caught CUDA out of memory"}])
        self.check(data)
        self.assertEqual(self.store.unit("fixture-run", "seed43")["execution_state"], "running")
        self.assertEqual(self.store.event_rows("fixture-run"), [])

    def test_failed_cuda_oom_freezes_without_retry(self):
        data = snapshot(alive=False, code=1)
        data["fields"]["errors"] = field([{"kind": "cuda_oom"}])
        result = self.check(data)
        runtime = self.store.unit("fixture-run", "seed43")
        self.assertEqual(runtime["attempt"], 1)
        self.assertEqual(runtime["retries_used"], 0)
        self.assertTrue(runtime["drain"])
        self.assertEqual(result["actions"], 0)

    def test_state_and_events_rollback_together(self):
        original = self.store.unit("fixture-run", "seed43")
        with patch.object(self.store, "event", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                self.check(snapshot(reachable=False))
        self.assertEqual(self.store.unit("fixture-run", "seed43"), original)
        self.assertEqual(self.store.event_rows("fixture-run"), [])

    def test_overlapping_check_is_blocked(self):
        with self.store.lock("fixture-run"):
            with self.assertRaises(Busy):
                self.check(snapshot())

    def test_pause_does_not_change_execution(self):
        runtime = self.store.unit("fixture-run", "seed43")
        runtime["monitoring"] = "paused"
        self.store.save_unit("fixture-run", "seed43", runtime)
        result = self.check(snapshot(alive=False, code=1))
        self.assertEqual(result["collected_units"], 0)
        self.assertEqual(self.store.unit("fixture-run", "seed43")["execution_state"], "running")

    def test_normal_progress_loss_does_not_generate_stall_or_kill(self):
        for _ in range(5):
            data = snapshot()
            data["fields"]["progress"] = field(None, status="unavailable")
            self.check(data)
        self.assertEqual(self.store.unit("fixture-run", "seed43")["execution_state"], "running")
        self.assertEqual(self.store.event_rows("fixture-run"), [])

    def test_empty_groups_contract_is_rejected(self):
        doc = copy.deepcopy(self.doc)
        doc["execution_id"] = "bad-groups"
        doc["units"][0]["completion"] = {
            "kind": "json", "path": str(self.root / "report.json"),
            "required_fields": ["metric"], "expected_groups": [],
        }
        with self.assertRaises(PilotError):
            self.store.save_contract(doc)

    def test_permission_is_not_actual_device_observation(self):
        self.check(snapshot())
        self.assertNotIn("actual_devices", self.store.unit("fixture-run", "seed43"))
        data = snapshot()
        data["fields"]["process"]["value"]["actual_gpu_uuids"] = ["GPU-observed"]
        self.check(data)
        self.assertEqual(self.store.unit("fixture-run", "seed43")["actual_devices"], ["GPU-observed"])

    def test_unsupported_adapter_is_not_misreported_as_connection_failure(self):
        data = snapshot(reachable=False)
        data["access"] = "unsupported"
        self.check(data)
        self.assertEqual(self.store.unit("fixture-run", "seed43")["access"], "unsupported")

    def test_ambiguous_launch_stays_frozen_until_reconciled(self):
        runtime = self.store.unit("fixture-run", "seed43")
        runtime["launch_status"] = "unknown"
        self.store.save_unit("fixture-run", "seed43", runtime)
        self.check(snapshot())
        self.assertTrue(self.store.unit("fixture-run", "seed43")["freeze_admission"])

    def test_comparable_throughput_drain_does_not_interrupt_active_units(self):
        doc = copy.deepcopy(self.doc)
        doc["execution_id"] = "throughput"
        doc["units"].append({"id": "seed44", "argv": []})
        doc["throughput"] = {"baseline_total_rate": 10, "phase": "train", "window_samples": 2}
        self.store.save_contract(doc)
        signature = ["seed43:1", "seed44:1"]
        for unit in doc["units"]:
            runtime = self.store.unit("throughput", unit["id"])
            runtime.update(execution_state="running", attempt=1, concurrency_signature=signature)
            runtime["phase_rate"] = {"phase": "train", "work_kind": "step", "rate": 2, "observed_at": now()}
            self.store.save_unit("throughput", unit["id"], runtime)
        with self.store.transaction():
            _throughput_drain(self.store, doc)
            _throughput_drain(self.store, doc)
        for runtime in self.store.units("throughput").values():
            self.assertTrue(runtime["drain"])
            self.assertEqual(runtime["execution_state"], "running")

    def test_heterogeneous_phases_are_not_added_for_speed_claim(self):
        doc = copy.deepcopy(self.doc)
        doc["execution_id"] = "heterogeneous"
        doc["units"].append({"id": "seed44", "argv": []})
        doc["throughput"] = {"baseline_total_rate": 10, "phase": "train", "window_samples": 1}
        self.store.save_contract(doc)
        for unit, phase in zip(doc["units"], ["train", "validation"]):
            runtime = self.store.unit("heterogeneous", unit["id"])
            runtime.update(execution_state="running", attempt=1, concurrency_signature=["seed43:1", "seed44:1"])
            runtime["phase_rate"] = {"phase": phase, "work_kind": "step", "rate": 1, "observed_at": now()}
            self.store.save_unit("heterogeneous", unit["id"], runtime)
        with self.store.transaction():
            _throughput_drain(self.store, doc)
        self.assertFalse(any(r.get("drain") for r in self.store.units("heterogeneous").values()))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runpilot-outbox-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.doc = contract(self.root, "file")
        self.store.save_contract(self.doc)
        runtime = self.store.unit("fixture-run", "seed43")
        runtime.update(execution_state="running", attempt=1, identity={"pid": 111})
        self.store.save_unit("fixture-run", "seed43", runtime)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_outbox_survives_restart_and_terminal_collection_stops_only_after_delivery(self):
        target = Path(self.doc["notification"]["path"])
        target.write_text("not a directory")
        result = check_once(self.store, "fixture-run", fixture=snapshot(alive=False, code=0))
        self.assertTrue(result["needs_scheduler"])
        self.assertTrue(result["pending_delivery"])
        self.store.close()
        target.unlink()
        self.store = Store(self.root / "state")
        self.store.db.execute("UPDATE outbox SET next_at=0")
        result = check_once(self.store, "fixture-run", fixture=snapshot())
        self.assertEqual(result["collected_units"], 0)
        self.assertFalse(result["needs_scheduler"])
        self.assertEqual(len(list(target.glob("*.json"))), 1)
        self.assertEqual(self.store.outbox_rows("fixture-run")[0]["status"], "delivered")

    def test_file_delivery_lost_response_reconciles_at_retry_limit(self):
        with self.store.transaction():
            self.store.event("fixture-run", "seed43", "completed", {"token": "do-not-export"}, "stable")
        dispatch(self.store, self.doc)
        self.store.db.execute(
            "UPDATE outbox SET status='claimed',attempts=3,claimed_at=0,next_at=0"
        )
        dispatch(self.store, self.doc)
        self.assertEqual(self.store.outbox_rows("fixture-run")[0]["status"], "delivered")
        files = list(Path(self.doc["notification"]["path"]).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("do-not-export", files[0].read_text())

    def test_undeliverable_notification_exhausts_with_explicit_record(self):
        target = Path(self.doc["notification"]["path"])
        target.write_text("not a directory")
        with self.store.transaction():
            self.store.event("fixture-run", "seed43", "failed", {}, "stable")
        for _ in range(3):
            self.store.db.execute("UPDATE outbox SET next_at=0")
            dispatch(self.store, self.doc)
        item = self.store.outbox_rows("fixture-run")[0]
        self.assertEqual(item["status"], "exhausted")
        self.assertEqual(item["attempts"], 3)
        self.assertIn("error", item["doc"])
        self.assertEqual(len(self.store.event_rows("fixture-run")), 1)

    def test_analysis_spool_is_independent_from_broken_notification(self):
        doc = contract(self.root, "file")
        doc["execution_id"] = "analysis"
        doc["analysis"] = {
            "enabled": True, "kind": "spool", "path": str(self.root / "analysis"),
            "max_requests": 1,
        }
        self.store.save_contract(doc)
        Path(doc["notification"]["path"]).write_text("broken notification destination")
        with self.store.transaction():
            self.store.event("analysis", "seed43", "conflict", {}, "event-1", analyze=True)
            self.store.event("analysis", "seed43", "conflict", {}, "event-2", analyze=True)
        dispatch(self.store, doc)
        analysis = [r for r in self.store.outbox_rows("analysis") if r["channel"] == "analysis"]
        self.assertEqual({r["status"] for r in analysis}, {"delivered", "exhausted"})
        self.assertEqual(len(list((self.root / "analysis").glob("*.json"))), 1)

    def test_unknown_nonreconcilable_delivery_is_not_blindly_resubmitted(self):
        doc = contract(self.root, "desktop")
        doc["execution_id"] = "desktop"
        self.store.save_contract(doc)
        with self.store.transaction():
            self.store.event("desktop", "seed43", "failed", {}, "lost-desktop")
        self.store.db.execute("UPDATE outbox SET status='claimed',attempts=1,claimed_at=0")
        with patch("monitor._deliver", side_effect=AssertionError("must not resend")):
            dispatch(self.store, doc)
        self.assertEqual(self.store.outbox_rows("desktop")[0]["status"], "unknown")

    def test_large_delivered_history_cannot_starve_new_pending_event(self):
        with self.store.transaction():
            for index in range(1030):
                self.store.event("fixture-run", "seed43", "history", {}, "history-%04d" % index)
            self.store.db.execute("UPDATE outbox SET status='delivered'")
            self.store.event("fixture-run", "seed43", "failed", {}, "current-pending")
        self.assertTrue(self.store.has_pending("fixture-run"))
        dispatch(self.store, self.doc)
        self.assertFalse(self.store.has_pending("fixture-run"))
        current = self.store.db.execute(
            "SELECT status FROM outbox WHERE event_key='current-pending'"
        ).fetchone()[0]
        self.assertEqual(current, "delivered")


if __name__ == "__main__":
    unittest.main()
