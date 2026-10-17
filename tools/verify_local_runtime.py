#!/usr/bin/env python3
"""Exercise the real local CLI with small CPU-only Python workers, no mocks.

This is RunPilot's own implementation test, not a new task in a user's
project. All worker/state/output files live in one temporary directory.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "run-pilot/scripts"


def command(script, arguments):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script)] + arguments,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=20, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def verify():
    with tempfile.TemporaryDirectory(prefix="runpilot-cpu-e2e-") as temporary:
        root = Path(temporary).resolve()
        report = root / "report.json"
        worker = root / "synthetic_cpu_worker.py"
        worker.write_text(
            "import json,sys,time\n"
            "print(json.dumps({'phase':'cpu-fixture','step':1,'total':1}),flush=True)\n"
            "time.sleep(0.25)\n"
            "with open(sys.argv[1],'w') as f:\n"
            " json.dump({'execution_id':'cpu-integration','unit_id':'fixture','attempt':1,"
            "'status':'completed','metric':0.75,'groups':["
            "{'id':'fold1','metric':0.7,'subject_count':128},"
            "{'id':'fold2','metric':0.8,'subject_count':128}]},f)\n",
            encoding="utf-8",
        )
        inspected = command("inspect_project.py", ["--project", str(root)])
        if report.exists():
            raise RuntimeError("Static inspection executed the worker")
        contract = {
            "schema_version": 1, "execution_id": "cpu-integration", "project": str(root),
            "target": {"kind": "local"}, "scope_approved": True,
            "allowed_actions": ["observe", "launch"], "max_concurrency": 1, "devices": [],
            "resource_lock": str(root / "resource-domain/resources.lock"),
            "reserves": {"ram_mb": 0, "disk_mb": 0, "cpu_threads": 0},
            "units": [{
                "id": "fixture", "argv": [sys.executable, str(worker), str(report)],
                "write_paths": [str(report)], "devices": [],
                "resources": {"ram_mb": 1, "disk_mb": 1, "cpu_threads": 1},
                "completion": {
                    "kind": "json", "path": str(report),
                    "required_fields": ["status", "metric"], "expected_groups": ["fold1", "fold2"],
                    "group_required_fields": ["metric", "subject_count"],
                    "group_equals": {"subject_count": 128},
                },
            }],
            "notification": {"kind": "file", "path": str(root / "notifications")},
            "analysis": {"enabled": False},
        }
        path = root / "contract.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        state = root / "state"
        common = ["--state-dir", str(state), "--execution", "cpu-integration"]
        initialized = command("run.py", ["init", "--contract", str(path), "--state-dir", str(state)])
        dry = command("run.py", ["launch"] + common + ["--unit", "fixture", "--dry-run"])
        if not dry.get("allowed") or report.exists() or (state / "attempts").exists():
            raise RuntimeError("Dry run did not remain non-executing")
        launched = command("run.py", ["launch"] + common + ["--unit", "fixture"])
        deadline = time.monotonic() + 15
        checked, status = None, None
        while time.monotonic() < deadline:
            checked = command("monitor.py", ["check-once"] + common)
            status = command("monitor.py", ["status"] + common)
            if status["units"]["fixture"]["execution_state"] == "completed":
                break
            time.sleep(0.1)
        runtime = status["units"]["fixture"]
        if runtime["execution_state"] != "completed":
            raise RuntimeError("Fixture did not verify completed: " + json.dumps(status))
        if runtime["exit_result"]["exit_code"] != 0 or checked["model_submissions"] != 0:
            raise RuntimeError("Incorrect exit/model accounting")
        if checked["needs_scheduler"] or any(item["status"] != "delivered" for item in status["outbox"]):
            raise RuntimeError("Completion outbox did not finish")
        export = root / "export.py"
        output = root / "output.csv"
        export.write_text(
            "import sys,time\n"
            "print('{\"phase\":\"export\",\"current\":1,\"total\":1}',flush=True)\n"
            "time.sleep(0.25)\n"
            "with open(sys.argv[1],'w') as f: f.write('name,value\\nexample,1\\n')\n",
            encoding="utf-8",
        )
        generic_inspection = command("inspect_project.py", ["--project", str(root)])
        if not any(item["path"] == "export.py" for item in generic_inspection["candidates"]):
            raise RuntimeError("Ordinary Python entry was not identified")
        generic = {
            **contract, "execution_id": "generic-integration",
            "units": [{
                "id": "export", "argv": [sys.executable, str(export), str(output)],
                "write_paths": [str(output)], "devices": [],
                "resources": {"ram_mb": 1, "disk_mb": 1, "cpu_threads": 1},
                "completion": {"kind": "exit_code"},
            }],
            "notification": {"kind": "file", "path": str(root / "generic-notifications")},
        }
        generic_path = root / "generic-contract.json"
        generic_path.write_text(json.dumps(generic), encoding="utf-8")
        generic_args = ["--state-dir", str(state), "--execution", "generic-integration"]
        command("run.py", ["init", "--contract", str(generic_path), "--state-dir", str(state)])
        generic_dry = command("run.py", ["launch"] + generic_args + ["--unit", "export", "--dry-run"])
        if not generic_dry.get("allowed") or generic_dry.get("task_started") is not False:
            raise RuntimeError("Generic dry-run started a task")
        command("run.py", ["launch"] + generic_args + ["--unit", "export"])
        deadline = time.monotonic() + 15
        generic_check, generic_status = None, None
        while time.monotonic() < deadline:
            generic_check = command("monitor.py", ["check-once"] + generic_args)
            generic_status = command("monitor.py", ["status"] + generic_args)
            if generic_status["units"]["export"]["execution_state"] == "completed":
                break
            time.sleep(0.1)
        generic_runtime = generic_status["units"]["export"]
        if (generic_runtime["execution_state"] != "completed"
                or generic_runtime["exit_result"]["exit_code"] != 0
                or output.read_text(encoding="utf-8") != "name,value\nexample,1\n"
                or generic_check["needs_scheduler"]
                or any(item["status"] != "delivered" for item in generic_status["outbox"])
                or generic_check["model_submissions"]):
            raise RuntimeError("Generic Python CLI loop did not complete and deliver")
        def delivered_kinds(directory):
            return sorted(
                json.loads(path.read_text(encoding="utf-8"))["kind"]
                for path in directory.glob("*.json")
            )
        if ("completed" not in delivered_kinds(root / "notifications")
                or "completed" not in delivered_kinds(root / "generic-notifications")):
            raise RuntimeError("A completed event notification was not delivered")
        return {
            "passed": True, "mocked_probes": False, "python": sys.version.split()[0],
            "static_inspection_executed_project": False,
            "dry_run_started_task": dry["task_started"],
            "dry_run_started_training": dry["training_started"],
            "launch_status": launched["status"], "execution_state": runtime["execution_state"],
            "attempt": runtime["attempt"], "exit_code": runtime["exit_result"]["exit_code"],
            "model_submissions": checked["model_submissions"],
            "notifications": len(list((root / "notifications").glob("*.json"))),
            "notification_kinds": delivered_kinds(root / "notifications"),
            "needs_scheduler": checked["needs_scheduler"],
            "actual_gpu": runtime.get("actual_devices"),
            "generic_script": {
                "kind": "Python CSV export without PyTorch or a JSON report",
                "execution_state": generic_runtime["execution_state"],
                "exit_code": generic_runtime["exit_result"]["exit_code"],
                "notifications": len(list((root / "generic-notifications").glob("*.json"))),
                "notification_kinds": delivered_kinds(root / "generic-notifications"),
                "model_submissions": generic_check["model_submissions"],
            },
            "scope": "temporary synthetic CPU workers only; no GPU/SSH/services",
            "temporary_files_removed_after_check": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "run-pilot-workspace/validation/local-cpu-e2e.json")
    args = parser.parse_args()
    try:
        result = verify()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
