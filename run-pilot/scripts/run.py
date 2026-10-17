#!/usr/bin/env python3
"""Local RunPilot launch/observe controls. Never signal training or install a scheduler."""

import argparse
import copy
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from core import (
    Busy, PilotError, Store, TERMINAL, absolute, canonical, default_state_dir,
    digest, emit, field, file_lock, identifier, now, read_json, write_json,
)
import policy
from supervisor import private_directory, private_file

STARTUP_WAIT_SECONDS = 2.0
_SUPERVISORS = []
HELD = {"submitted", "confirmed", "unknown"}


def collect(contract, unit, runtime, state_dir):
    # Lazy import keeps init/status/monitor controls useful without an installed
    # platform collector. Never substitute synthetic telemetry in production.
    try:
        from collectors import collect as collector
    except ImportError as error:
        raise PilotError("Local collector is unavailable; launch is not permitted") from error
    return collector(contract, unit, runtime, state_dir)


def _identity(pid):
    try:
        from collectors import process_identity
    except ImportError as error:
        raise PilotError("Local process identity collector is unavailable") from error
    return process_identity(pid)


def _matches(expected):
    from collectors import process_matches
    return process_matches(expected)


def _full_identity(identity):
    return (
        isinstance(identity, dict) and isinstance(identity.get("pid"), int)
        and not isinstance(identity["pid"], bool) and identity["pid"] > 0
        and bool(identity.get("start_time")) and bool(identity.get("boot_id"))
    )


def _same_identity(expected, observed):
    return _full_identity(expected) and _full_identity(observed) and all(
        expected[key] == observed[key] for key in ("pid", "start_time", "boot_id")
    )


def _confirmed_gone(identity):
    """Tri-state: True=confirmed gone, False=same identity, None=coverage loss."""
    if not _full_identity(identity):
        return None
    try:
        observed = _identity(identity["pid"])
    except ProcessLookupError:
        return True
    except (OSError, ValueError, PilotError):
        return None
    return not _same_identity(identity, observed) if _full_identity(observed) else None


def completion_baseline(unit):
    """The same stat fingerprint consumed by the completion collector."""
    completion = unit.get("completion")
    if not completion:
        return None
    try:
        from collectors import completion_fingerprint
        return completion_fingerprint(completion["path"])
    except (OSError, PilotError, ImportError) as error:
        raise PilotError("Cannot establish pre-attempt completion baseline: " + str(error)) from error


def resource_lock_path(contract):
    # Never derive the resource domain from --state-dir. Different control
    # clients must contend on the same lock AND its sibling SQLite registry.
    path = Path(contract.get("resource_lock", default_state_dir() / "resources.lock"))
    absolute(str(path), "resource_lock")
    if path.is_symlink():
        raise PilotError("The shared resource lock must not be a symlink")
    return path.resolve()


def registry_path(contract):
    lock = resource_lock_path(contract)
    return lock.with_name(lock.name + ".sqlite3")


class _Registry:
    """Authoritative claims, accessed only while holding the resource lock."""

    def __init__(self, contract):
        path = registry_path(contract)
        private_directory(path.parent)
        with private_file(path):
            pass
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.db.close()
            raise PilotError("Unknown resource registry version; keep existing claims")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS claims (execution TEXT NOT NULL, unit TEXT NOT NULL, "
            "attempt INTEGER NOT NULL, status TEXT NOT NULL, doc TEXT NOT NULL, "
            "PRIMARY KEY(execution,unit,attempt))"
        )
        self.db.execute("PRAGMA user_version=1")

    def close(self):
        self.db.close()

    def rows(self):
        rows = self.db.execute(
            "SELECT * FROM claims ORDER BY execution,unit,attempt LIMIT 4097"
        ).fetchall()
        if len(rows) > 4096:
            raise PilotError("Resource registry inspection limit reached; review retained claims before new admission")
        return [dict(row, doc=json.loads(row["doc"])) for row in rows]

    def save(self, claim, insert=False):
        query = (
            "INSERT INTO claims VALUES(?,?,?,?,?)" if insert else
            "UPDATE claims SET status=?,doc=? WHERE execution=? AND unit=? AND attempt=?"
        )
        values = (claim["execution"], claim["unit"], claim["attempt"],
                  claim["status"], canonical(claim["doc"]))
        self.db.execute(query, values if insert else (values[3], values[4], *values[:3]))


def _unit(contract, unit_id):
    identifier(unit_id, "unit id")
    for unit in contract["units"]:
        if unit["id"] == unit_id:
            return unit
    raise PilotError("Unknown unit: " + unit_id)


def _output_refusals(unit):
    """Do not turn a fresh private attempt into permission to overwrite research."""
    reasons = []
    evidence = unit.get("existing_outputs_evidence")
    reviewed = (
        unit.get("allow_existing_outputs") is True
        and isinstance(evidence, str) and bool(evidence.strip())
    )
    for name in unit.get("write_paths", []):
        path = Path(name)
        try:
            if not path.exists():
                if path.is_symlink():
                    reasons.append("Cannot establish output safety for dangling symlink: " + name)
                continue
            if path.is_dir():
                with os.scandir(str(path)) as entries:
                    nonempty = next(entries, None) is not None
            elif path.is_file():
                nonempty = path.stat().st_size > 0
            else:
                reasons.append("Declared output is not a regular file/directory: " + name)
                continue
            if nonempty and not reviewed:
                reasons.append(
                    "Existing nonempty output requires allow_existing_outputs=true and "
                    "existing_outputs_evidence for the reviewed original behavior: " + name
                )
        except OSError as error:
            reasons.append("Cannot inspect declared output safely: %s: %s" % (name, error))
    return reasons


def _reservation(contract, unit):
    estimates = unit.get("resources", {})
    devices = unit.get("devices", [])
    reviewed = bool(
        unit.get("independent_entry") is True and unit.get("isolation_verified") is True
        and unit.get("evidence") and unit.get("write_paths") and unit.get("resource_evidence")
        and all(k in estimates for k in ("ram_mb", "disk_mb", "cpu_threads"))
        and all(d in estimates.get("gpu_mb", {}) for d in devices)
        and (not devices or "future_gpu_mb" in estimates)
        and not unit.get("exclusive_project_lock") and not unit.get("unsafe_shared_writes")
    )
    return {
        "execution": contract["execution_id"], "unit": unit["id"], "status": "submitted",
        "resources": {
            "gpu_mb": {
                d: value + (estimates.get("future_gpu_mb", 0) if d in devices else 0)
                for d, value in estimates.get("gpu_mb", {}).items()
            },
            "ram_mb": estimates.get("ram_mb", 0), "disk_mb": estimates.get("disk_mb", 0),
            "cpu_threads": estimates.get("cpu_threads", 0),
        },
        "write_paths": unit.get("write_paths", []), "isolation_verified": reviewed,
        # A returned PID, or simply taking another sample, is NOT proof that
        # the reserved demand is reflected. v0.1 keeps this conservative.
        "reflected_in_sample": False,
    }


def _reservations(registry):
    return [
        dict(claim["doc"]["reservation"], status=claim["status"])
        for claim in registry.rows() if claim["status"] in HELD
    ]


def _attributed(record, claim):
    return isinstance(record, dict) and all(
        record.get(key) == value for key, value in (
            ("schema_version", 1), ("execution_id", claim["execution"]),
            ("unit_id", claim["unit"]), ("attempt", claim["attempt"]),
            ("claim_id", claim["doc"]["runtime"]["claim_id"]),
        )
    )


def _timestamp(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _refresh_claim(registry, claim):
    """Read supervisor evidence even for terminal/released local runtimes.

    Only an attributed real exit or definite no-spawn releases managed budget.
    A missing primary PID alone does not prove that all workers have exited.
    """
    doc = claim["doc"]
    runtime = doc["runtime"]
    if doc.get("release_reason") == "true_exit" and doc.get("exit_result"):
        # Received/waited-for exit evidence is durable in the registry as well
        # as on disk. Removing the old attempt directory cannot resurrect it.
        claim["status"] = "released"
        return claim
    if not runtime.get("managed"):
        identity = runtime.get("identity")
        if identity and claim["status"] in HELD:
            gone = _confirmed_gone(identity)
            if gone is True:
                claim["status"] = "unknown"
                doc["release_reason"] = "confirmed_identity_vanished"
            elif gone is None:
                claim["status"] = "unknown"
        registry.save(claim)
        return claim
    try:
        exit_path = Path(runtime["exit_path"])
        manifest_path = Path(runtime["manifest_path"])
        exit_result = read_json(exit_path) if exit_path.exists() else None
        manifest = read_json(manifest_path) if manifest_path.exists() else None
        for record in (exit_result, manifest):
            if record is not None and (
                not _attributed(record, claim)
                or record.get("argv") != doc["argv"] or record.get("cwd") != doc["cwd"]
            ):
                raise PilotError("Supervisor evidence has conflicting attempt attribution")
        if exit_result is not None:
            code, finish, start = (
                exit_result.get("exit_code"), exit_result.get("finished_at"), exit_result.get("started_at")
            )
            if (not isinstance(code, int) or isinstance(code, bool)
                    or exit_result.get("child_started") is not True
                    or not _timestamp(start) or not _timestamp(finish)
                    or not start <= finish <= now() + 30):
                raise PilotError("Supervisor exit does not contain a real attributed exit/time")
            doc["exit_result"] = exit_result
            doc["manifest"] = exit_result
            doc["release_reason"] = "true_exit"
            claim["status"] = "released"
        elif manifest is not None and manifest.get("phase") == "spawn_failed":
            if manifest.get("child_started") is not False:
                raise PilotError("Conflicting no-spawn evidence")
            doc["manifest"] = manifest
            doc["release_reason"] = "never_spawned"
            claim["status"] = "released"
        elif manifest is not None and manifest.get("child_started") is True:
            doc["manifest"] = manifest
            identity = manifest.get("identity")
            if _full_identity(identity):
                claim["status"] = "confirmed"
                gone = _confirmed_gone(identity)
                if gone is True:
                    claim["status"] = "unknown"
                    doc["release_reason"] = "confirmed_identity_vanished"
                elif gone is None:
                    claim["status"] = "unknown"
            elif claim["status"] in HELD:
                claim["status"] = "unknown"
        elif claim["status"] in HELD:
            claim["status"] = "unknown"
    except (OSError, ValueError, KeyError, TypeError, PilotError) as error:
        # Corrupt/missing evidence is not evidence that the command never ran.
        claim["status"] = "unknown"
        doc["evidence_error"] = str(error)[:500]
    registry.save(claim)
    return claim


def _persist(store, contract, unit, runtime, launch_status, detail=None):
    execution = contract["execution_id"]
    runtime["launch_status"] = launch_status
    runtime["record_written_at"] = now()
    kind = "launch" if runtime.get("managed") else "attach"
    key = "%s:%s:%s:%s" % (execution, unit["id"], runtime["attempt"], kind)
    with store.transaction():
        store.save_unit(execution, unit["id"], runtime)
        store.action(key, execution, unit["id"], kind, launch_status, {
            "attempt": runtime["attempt"], "claim_id": runtime.get("claim_id"),
            "manifest_path": runtime.get("manifest_path"), "identity": runtime.get("identity"),
            "detail": detail, "no_auto_retry": True,
        })
        store.event(execution, unit["id"], kind + "_" + launch_status,
                    {"attempt": runtime["attempt"], "detail": detail},
                    key + ":" + launch_status, notify=False)


def _synchronize(store, contract, unit, claim, observe=False):
    """Mirror authoritative attempt metadata without overwriting monitor controls."""
    execution = contract["execution_id"]
    runtime = store.unit(execution, unit["id"])
    saved = claim["doc"]["runtime"]
    if runtime["attempt"] > claim["attempt"]:
        raise PilotError("Local attempt conflicts with authoritative registry; no replay")
    for key in ("managed", "attempt", "identity", "supervisor_identity", "started_at",
                "log_path", "exit_path", "manifest_path", "request_path", "claim_id",
                "assigned_devices", "completion_baseline", "attached_at"):
        if key in saved:
            runtime[key] = copy.deepcopy(saved[key])
    manifest = claim["doc"].get("manifest", {})
    for key in ("identity", "supervisor_identity", "started_at"):
        if manifest.get(key) is not None:
            runtime[key] = manifest[key]
    release = claim["doc"].get("release_reason")
    if release == "never_spawned":
        runtime["execution_state"] = "needs_action"
        runtime["freeze_admission"] = True
        launch_status = "failed"
    elif release in {"true_exit", "confirmed_identity_vanished"}:
        launch_status = "confirmed"
    else:
        launch_status = claim["status"]
        if runtime["execution_state"] == "ready":
            runtime["execution_state"] = "starting"
        if launch_status == "unknown":
            runtime["freeze_admission"] = True
    result = claim["doc"].get("exit_result")
    if observe or result or release == "confirmed_identity_vanished":
        from monitor import apply_snapshot, validate_snapshot
        snapshot = {"observed_at": now(), "reachable": True, "fields": {}}
        if observe:
            try:
                observed = collect(contract, unit, runtime, store.root)
                validate_snapshot(observed)
                snapshot = observed
            except (OSError, ValueError, PilotError) as error:
                runtime["reconcile_error"] = str(error)[:500]
        if result:
            snapshot["fields"]["exit"] = field(result, source="supervisor.wait", observed_at=now())
            # The waited-for child is gone. A reused PID is not a live target.
            snapshot["fields"]["process"] = field(
                {"alive": False, "identity_matches": True, "identity": result.get("identity")},
                source="supervisor.wait", observed_at=now(),
            )
        elif release == "confirmed_identity_vanished":
            snapshot["fields"]["process"] = field(
                {"alive": False, "identity_matches": False, "identity": runtime.get("identity")},
                source="confirmed-identity-disappearance", observed_at=now(),
            )
        with store.transaction():
            apply_snapshot(store, contract, unit, runtime, snapshot)
            store.save_unit(execution, unit["id"], runtime)
    elif manifest.get("identity") or (not runtime.get("managed") and launch_status == "confirmed"):
        runtime["execution_state"] = "running"
    _persist(store, contract, unit, runtime, launch_status,
             claim["doc"].get("evidence_error") or release)
    return {"status": launch_status, "execution_id": execution, "unit_id": unit["id"],
            "attempt": runtime["attempt"], "execution_state": runtime["execution_state"],
            "replayed": False, "registry_status": claim["status"]}


def _reap_supervisors():
    _SUPERVISORS[:] = [child for child in _SUPERVISORS if child.poll() is None]


def _refused(reasons, **extra):
    return dict(status="refused", allowed=False, reasons=reasons, **extra)


def launch_unit(store, contract, unit_id, dry_run=False):
    """Admit and submit ONE original local unit, never a retry/resume.

    Lock order is execution -> shared resource domain -> short DB transactions.
    Starting/action intent and the authoritative claim are durable before Popen.
    """
    execution = identifier(contract.get("execution_id"), "execution_id")
    with store.lock(execution):
        if canonical(store.contract(execution)) != canonical(contract):
            raise PilotError("Launch must use the stored immutable contract")
        unit = _unit(contract, unit_id)
        if contract.get("target", {}).get("kind", "local") != "local":
            return _refused(["Remote/fixture launch is unsupported; only local supervised launch is implemented"])
        lock = resource_lock_path(contract)
        private_directory(lock.parent)
        with file_lock(lock):
            registry = _Registry(contract)
            try:
                claims = [_refresh_claim(registry, c) for c in registry.rows()]
                same = [c for c in claims if c["execution"] == execution]
                if any(c["doc"]["contract_digest"] != digest(contract) for c in same):
                    return _refused(["Shared resource registry has a different contract for this execution"])
                existing = [c for c in same if c["unit"] == unit_id]
                if existing:
                    claim = existing[-1]
                    if dry_run:
                        return _refused(["Attempt already claimed; no retry/resume or duplicate spawn"])
                    result = _synchronize(store, contract, unit, claim)
                    result["already_claimed"] = True
                    if claim["status"] == "released":
                        return _refused([
                            "v0.1 refuses retry/resume/replay, including best-weights-only recovery"
                        ], **{k: v for k, v in result.items() if k != "status"})
                    return result
                runtime = store.unit(execution, unit_id)
                if runtime["attempt"] or runtime["execution_state"] != "ready":
                    return _refused(["v0.1 refuses retry/resume or replay of non-ready/previously attempted work"])
                if not Path(unit.get("cwd", contract["project"])).is_dir():
                    return _refused(["Reviewed cwd must already exist; RunPilot does not create project directories"])
                output_reasons = _output_refusals(unit)
                if output_reasons:
                    return _refused(output_reasons)
                runtimes = store.units(execution)
                for claim in same:
                    if claim["status"] in HELD:
                        state = runtimes[claim["unit"]]
                        state["execution_state"] = "running" if claim["status"] == "confirmed" else "starting"
                # All telemetry, reservations and authorization are rechecked
                # INSIDE the shared lock, not a pre-lock cached admission.
                snapshot = collect(contract, unit, runtime, store.root)
                decision = policy.admission(contract, unit, runtimes, snapshot, _reservations(registry))
                if not decision["allowed"]:
                    return _refused(decision["reasons"])
                if dry_run:
                    return {"status": "dry-run", "allowed": True, "execution_id": execution,
                            "unit_id": unit_id, "argv": unit["argv"], "cwd": unit.get("cwd", contract["project"]),
                            "assigned_devices": unit.get("devices", []), "training_started": False}
                baseline = completion_baseline(unit)
                attempt = runtime["attempt"] + 1
                directory = store.root / "attempts"
                for part in (execution, unit_id):
                    directory = private_directory(directory) / part
                directory = private_directory(directory) / ("attempt-%d" % attempt)
                directory.mkdir(mode=0o700, exist_ok=False)
                paths = {key: str(directory / name) for key, name in (
                    ("request_path", "request.json"), ("manifest_path", "manifest.json"),
                    ("exit_path", "exit.json"), ("log_path", "output.log"),
                )}
                for path in (paths["log_path"], str(directory / "supervisor.log")):
                    with private_file(path, exclusive=True):
                        pass
                runtime.update(
                    paths, execution_state="starting", attempt=attempt, managed=True,
                    identity=None, supervisor_identity=None, claim_id=uuid.uuid4().hex,
                    completion_baseline=baseline, submitted_at=now(), started_at=None,
                    finished_at=None, assigned_devices=unit.get("devices", []), launch_status="submitted",
                )
                request = dict(
                    paths, schema_version=1, execution_id=execution, unit_id=unit_id,
                    attempt=attempt, claim_id=runtime["claim_id"], argv=unit["argv"],
                    cwd=unit.get("cwd", contract["project"]), env=unit.get("env", {}),
                    assigned_devices=unit.get("devices", []),
                )
                write_json(paths["request_path"], request)
                claim = {
                    "execution": execution, "unit": unit_id, "attempt": attempt, "status": "submitted",
                    "doc": {"contract_digest": digest(contract), "state_dir": str(store.root),
                            "runtime": copy.deepcopy(runtime), "reservation": _reservation(contract, unit),
                            "argv": unit["argv"], "cwd": request["cwd"]},
                }
                # Registry first: a crash between the two commits leaves a claim
                # other clients can discover, never an invisible spawned task.
                registry.save(claim, insert=True)
                _persist(store, contract, unit, runtime, "submitted", decision)
                deadline = time.monotonic() + STARTUP_WAIT_SECONDS
                try:
                    with private_file(directory / "supervisor.log") as output:
                        child = subprocess.Popen(
                            [sys.executable, str(Path(__file__).with_name("supervisor.py").resolve()),
                             "--request", paths["request_path"]],
                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                            shell=False, start_new_session=True, close_fds=True,
                        )
                    _SUPERVISORS.append(child)
                    while time.monotonic() < deadline:
                        if Path(paths["exit_path"]).exists():
                            break
                        if Path(paths["manifest_path"]).exists():
                            manifest = read_json(paths["manifest_path"])
                            if (manifest.get("child_started") is False
                                    or _full_identity(manifest.get("identity"))):
                                break
                        if child.poll() is not None:
                            break
                        time.sleep(0.02)
                except Exception as error:
                    # Includes a lost Popen response AFTER the OS accepted it.
                    # Never infer "not started" from this client-side failure.
                    claim["doc"]["evidence_error"] = "Submission response unknown: " + str(error)[:400]
                claim = _refresh_claim(registry, claim)
                # A short-lived child can disappear between the running
                # manifest read and identity revalidation. Give its real wait
                # record the remainder of the bounded handshake, not a replay.
                if claim["status"] == "unknown" and claim["doc"].get("manifest", {}).get("child_started"):
                    while time.monotonic() < deadline:
                        if Path(paths["exit_path"]).exists():
                            claim = _refresh_claim(registry, claim)
                            break
                        time.sleep(0.02)
                _reap_supervisors()
                return _synchronize(store, contract, unit, claim)
            finally:
                registry.close()


def reconcile(store, execution, unit_id=None):
    """Explicit, read-only-to-training reconciliation; does not schedule work."""
    contract = store.contract(execution)
    selected = [unit_id] if unit_id is not None else [u["id"] for u in contract["units"]]
    for name in selected:
        _unit(contract, name)
    results = {}
    with store.lock(execution):
        lock = resource_lock_path(contract)
        private_directory(lock.parent)
        with file_lock(lock):
            registry = _Registry(contract)
            try:
                for claim in registry.rows():
                    claim = _refresh_claim(registry, claim)
                    if claim["execution"] != execution or claim["unit"] not in selected:
                        continue
                    if claim["doc"]["contract_digest"] != digest(contract):
                        raise PilotError("Contract conflicts with authoritative registry; no replay")
                    unit = _unit(contract, claim["unit"])
                    active = store.unit(execution, unit["id"])["monitoring"] == "active"
                    results[unit["id"]] = _synchronize(store, contract, unit, claim, observe=active)
                held = _reservations(registry)
            finally:
                registry.close()
    _reap_supervisors()
    return {"status": "reconciled", "execution_id": execution, "units": results,
            "reservations": held, "training_started": False, "model_submissions": 0}


def attach(store, execution, unit_id, pid):
    """Bind an existing full process identity. Never restart or reparent it."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise PilotError("PID must be a positive integer")
    contract = store.contract(execution)
    unit = _unit(contract, unit_id)
    if contract.get("target", {}).get("kind", "local") != "local":
        raise PilotError("Attach requires a local process; remote launch/attach is unsupported")
    if "observe" not in contract.get("allowed_actions", ["observe"]):
        raise PilotError("Observation is not authorized")
    with store.lock(execution):
        lock = resource_lock_path(contract)
        private_directory(lock.parent)
        with file_lock(lock):
            registry = _Registry(contract)
            try:
                runtime = store.unit(execution, unit_id)
                identity = _identity(pid)
                if not _full_identity(identity) or not _matches(identity):
                    raise PilotError("Cannot confirm the existing process's full identity")
                execution_claims = [c for c in registry.rows() if c["execution"] == execution]
                if any(c["doc"]["contract_digest"] != digest(contract) for c in execution_claims):
                    raise PilotError("Contract conflicts with authoritative resource registry")
                same = [c for c in execution_claims if c["unit"] == unit_id]
                if same:
                    saved = same[-1]["doc"]["runtime"]
                    if saved.get("managed") or not _same_identity(saved.get("identity"), identity):
                        raise PilotError("Unit already has a different/managed claim; attach cannot replace it")
                    _synchronize(store, contract, unit, same[-1])
                    return {"status": "attached", "identity": identity, "already_attached": True,
                            "training_started": False}
                if runtime["attempt"] or runtime["execution_state"] != "ready":
                    raise PilotError("Attach cannot replace an existing attempt")
                runtime.update(
                    execution_state="running", attempt=1, identity=identity, managed=False,
                    attached_at=now(), started_at=identity.get("start_epoch"),
                    completion_baseline=completion_baseline(unit), claim_id=uuid.uuid4().hex,
                    assigned_devices=unit.get("devices", []),
                )
                claim = {
                    "execution": execution, "unit": unit_id, "attempt": 1, "status": "confirmed",
                    "doc": {"contract_digest": digest(contract), "state_dir": str(store.root),
                            "runtime": copy.deepcopy(runtime), "reservation": _reservation(contract, unit)},
                }
                registry.save(claim, insert=True)
                with store.transaction():
                    store.save_unit(execution, unit_id, runtime)
                    store.action("%s:%s:1:attach" % (execution, unit_id), execution, unit_id,
                                 "attach", "confirmed", {"identity": identity, "managed": False})
                    store.event(execution, unit_id, "attached", {"identity": identity},
                                "%s:%s:1:attached" % (execution, unit_id), notify=False)
            finally:
                registry.close()
    return {"status": "attached", "identity": identity, "training_started": False, "managed": False}


def set_monitoring(store, execution, monitoring, unit_id=None):
    if monitoring not in {"active", "paused", "stopped"}:
        raise PilotError("Unknown monitoring mode")
    contract = store.contract(execution)
    units = [_unit(contract, unit_id)] if unit_id is not None else contract["units"]
    with store.lock(execution):
        with store.transaction():
            for unit in units:
                runtime = store.unit(execution, unit["id"])
                runtime["monitoring"] = monitoring
                runtime["record_written_at"] = now()
                store.save_unit(execution, unit["id"], runtime)
    return {"status": "monitoring_" + monitoring, "monitoring": monitoring,
            "units": [unit["id"] for unit in units], "training_stopped": False,
            "note": "Monitoring controls never stop/restart/resume training; v0.1 has no training retry/resume"}


def status(store, execution):
    store.contract(execution)
    return {
        "execution_id": execution, "units": store.units(execution),
        "actions": [
            dict(row, doc=json.loads(row["doc"]))
            for row in store.db.execute(
                "SELECT * FROM actions WHERE execution=? ORDER BY key", (execution,)
            )
        ],
        "model_submissions": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="Private local state; does not change the shared resource lock")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Save an immutable contract; does not launch")
    init.add_argument("--contract", required=True)
    for name in ("status", "attach", "launch", "reconcile",
                 "pause-monitor", "resume-monitor", "stop-monitor"):
        child = commands.add_parser(name)
        child.add_argument("--execution", required=True)
        if name in {"attach", "launch"}:
            child.add_argument("--unit", required=True)
        elif name != "status":
            child.add_argument("--unit", help="Omit to select all units in this execution")
        if name == "attach":
            child.add_argument("--pid", required=True, type=int)
        if name == "launch":
            child.add_argument("--dry-run", action="store_true")
    for child in commands.choices.values():
        child.add_argument("--state-dir", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    store = None
    try:
        store = Store(args.state_dir)
        if args.command == "init":
            doc = read_json(args.contract)
            execution = identifier(doc.get("execution_id"), "execution_id")
            with store.lock(execution):
                store.save_contract(doc)
            result = {"status": "initialized", "execution_id": execution,
                      "training_started": False, "scheduler_installed": False}
        elif args.command == "status":
            result = status(store, args.execution)
        elif args.command == "launch":
            result = launch_unit(store, store.contract(args.execution), args.unit, args.dry_run)
        elif args.command == "attach":
            result = attach(store, args.execution, args.unit, args.pid)
        elif args.command == "reconcile":
            result = reconcile(store, args.execution, args.unit)
        else:
            mode = {"pause-monitor": "paused", "resume-monitor": "active",
                    "stop-monitor": "stopped"}[args.command]
            result = set_monitoring(store, args.execution, mode, args.unit)
        emit(result)
        return 2 if result.get("status") == "refused" else 0
    except Busy as error:
        emit({"status": "busy", "detail": str(error), "model_submissions": 0})
        return 0
    except (PilotError, OSError, ValueError, sqlite3.Error) as error:
        emit({"program_error": str(error), "training_failure": False})
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
