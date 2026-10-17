import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run-pilot/scripts"))
from core import field, now
from policy import admission


def inputs():
    units = []
    for name in ("seed43", "seed44", "seed45"):
        units.append({
            "id": name, "argv": ["python", "train.py", "--seed", name[4:]],
            "devices": ["GPU-example"], "write_paths": ["/synthetic/results/" + name],
            "env": {"CUDA_VISIBLE_DEVICES": "GPU-example"},
            "independent_entry": True, "isolation_verified": True,
            "evidence": "existing --seed entry reviewed; no shared summaries",
            "resource_evidence": "formal baseline and validation coverage",
            "resources": {"gpu_mb": {"GPU-example": 4000}, "future_gpu_mb": 1000,
                          "ram_mb": 1000, "disk_mb": 500, "cpu_threads": 2},
        })
    doc = {
        "execution_id": "approved", "target": {"kind": "local"}, "scope_approved": True,
        "allowed_actions": ["observe", "launch"], "devices": ["GPU-example"],
        "units": units, "max_concurrency": 3,
        "reserves": {"gpu_mb": 2000, "ram_mb": 500, "disk_mb": 500, "cpu_threads": 1},
    }
    runtimes = {
        unit["id"]: {"execution_state": "ready", "monitoring": "active"}
        for unit in units
    }
    data = {
        "observed_at": now(), "reachable": True,
        "fields": {"resources": field({
            "gpus": field([{"uuid": "GPU-example", "memory_free_mb": 15000, "compute_mode": "Default"}]),
            "ram_available_mb": field(10000), "disk_free_mb": field(10000),
            "cpu_threads": field(16), "shm_free_mb": field(10000),
        })},
    }
    return doc, runtimes, data


class PolicyTests(unittest.TestCase):
    def test_first_formal_unit_does_not_need_smoke_or_prior_baseline(self):
        doc, states, data = inputs()
        doc["units"][0].pop("resource_evidence")
        doc["units"][0].pop("resources")
        result = admission(doc, doc["units"][0], states, data)
        self.assertTrue(result["allowed"], result)

    def test_new_task_waits_for_formal_observations_before_parallelism(self):
        doc, states, data = inputs()
        states["seed43"].update(execution_state="running", resource_observations=0)
        result = admission(doc, doc["units"][1], states, data)
        self.assertFalse(result["allowed"])
        states["seed43"]["resource_observations"] = 2
        self.assertTrue(admission(doc, doc["units"][1], states, data)["allowed"])

    def test_only_memory_division_is_not_sufficient(self):
        doc, states, data = inputs()
        states["seed43"].update(execution_state="running", resource_observations=3)
        doc["units"][1].pop("independent_entry")
        self.assertFalse(admission(doc, doc["units"][1], states, data)["allowed"])

    def test_shared_exclusive_output_is_not_overridden(self):
        doc, states, data = inputs()
        states["seed43"].update(execution_state="running", resource_observations=3)
        doc["units"][1]["write_paths"] = ["/synthetic/results/seed43/summary"]
        self.assertFalse(admission(doc, doc["units"][1], states, data)["allowed"])
        doc["units"][1]["write_paths"] = ["/synthetic/results/seed44"]
        doc["units"][0]["exclusive_project_lock"] = True
        self.assertFalse(admission(doc, doc["units"][1], states, data)["allowed"])

    def test_future_growth_and_unreflected_claims_are_budgeted(self):
        doc, states, data = inputs()
        states["seed43"].update(execution_state="running", resource_observations=3)
        data["fields"]["resources"]["value"]["gpus"]["value"][0]["memory_free_mb"] = 8000
        lease = {
            "execution": "approved", "unit": "seed43", "status": "confirmed",
            "resources": {"gpu_mb": {"GPU-example": 3000}},
        }
        result = admission(doc, doc["units"][1], states, data, [lease])
        self.assertFalse(result["allowed"])
        lease["reflected_in_sample"] = True
        self.assertTrue(admission(doc, doc["units"][1], states, data, [lease])["allowed"])

    def test_unknown_resources_never_turn_into_zero_demand_parallelism(self):
        doc, states, data = inputs()
        states["seed43"].update(execution_state="running", resource_observations=3)
        doc["units"][1].pop("resources")
        self.assertFalse(admission(doc, doc["units"][1], states, data)["allowed"])

    def test_external_usage_and_stale_telemetry_block(self):
        doc, states, data = inputs()
        resources = data["fields"]["resources"]["value"]
        resources["gpus"]["value"][0]["memory_free_mb"] = 1000
        self.assertFalse(admission(doc, doc["units"][0], states, data)["allowed"])
        resources["gpus"]["value"][0]["memory_free_mb"] = 15000
        resources["gpus"]["observed_at"] = now() - 10000
        self.assertFalse(admission(doc, doc["units"][0], states, data)["allowed"])

    def test_no_scope_no_unit_replay_no_dependency_bypass(self):
        doc, states, data = inputs()
        doc["scope_approved"] = False
        self.assertFalse(admission(doc, doc["units"][0], states, data)["allowed"])
        doc["scope_approved"] = True
        doc["units"][0]["depends_on"] = ["seed44"]
        self.assertFalse(admission(doc, doc["units"][0], states, data)["allowed"])
        doc["units"][0].pop("depends_on")
        states["seed43"]["execution_state"] = "completed"
        self.assertFalse(admission(doc, doc["units"][0], states, data)["allowed"])

    def test_drain_refuses_new_work_without_killing_running_work(self):
        doc, states, data = inputs()
        states["seed43"].update(execution_state="running", resource_observations=3, freeze_admission=True)
        result = admission(doc, doc["units"][1], states, data)
        self.assertFalse(result["allowed"])
        self.assertEqual(states["seed43"]["execution_state"], "running")

    def test_unknown_cross_client_claim_counts_as_active(self):
        doc, states, data = inputs()
        doc["max_concurrency"] = 1
        result = admission(doc, doc["units"][0], states, data, [{
            "execution": "another-client", "unit": "existing", "status": "unknown",
        }])
        self.assertFalse(result["allowed"])

    def test_no_reserved_resource_is_subtracted_twice(self):
        doc, states, data = inputs()
        states["seed43"].update(execution_state="running", resource_observations=3)
        data["fields"]["resources"]["value"]["gpus"]["value"][0]["memory_free_mb"] = 7000
        self.assertTrue(admission(doc, doc["units"][1], states, data)["allowed"])


if __name__ == "__main__":
    unittest.main()
