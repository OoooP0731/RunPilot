#!/usr/bin/env python3
"""One deterministic, bounded monitoring pass. No model client is imported."""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

from core import (
    Busy, FIELD_STATES, PilotError, Store, TERMINAL, canonical, digest, emit,
    field, now, read_json, redact, write_json,
)


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fields"), dict):
        raise PilotError("A snapshot must contain fields")
    if not isinstance(snapshot.get("reachable"), bool):
        raise PilotError("snapshot.reachable must be boolean")
    for name, item in snapshot["fields"].items():
        if not isinstance(item, dict) or item.get("status") not in FIELD_STATES:
            raise PilotError("Invalid field status: " + name)
    return snapshot


def _event_key(contract, unit, runtime, suffix):
    return "%s:%s:%s:%s" % (contract["execution_id"], unit["id"], runtime["attempt"], suffix)


def _emit_event(store, contract, unit, runtime, kind, evidence, suffix=None, analyze=False):
    enabled = contract.get("analysis", {}).get("enabled", False) and analyze
    return int(store.event(
        contract["execution_id"], unit["id"], kind,
        {"evidence": evidence, "execution_state": runtime["execution_state"],
         "actual_devices": runtime.get("actual_devices"), "observed_at": now(),
         "actual_devices_status": runtime.get("actual_devices_status", "unavailable"),
         "actual_devices_observed_at": runtime.get("actual_devices_observed_at"),
         "action": "observe; no task configuration change or process signal"},
        _event_key(contract, unit, runtime, suffix or kind),
        notify=contract.get("notification", {}).get("kind", "none") != "none",
        analyze=enabled,
    ))


def _fresh(item, timestamp, limit):
    return (
        item.get("status") == "ok"
        and -30 <= timestamp - item.get("observed_at", 0) <= limit
    )


def apply_snapshot(store, contract, unit, runtime, snapshot):
    """Update a runtime and enqueue events inside the caller's transaction."""
    validate_snapshot(snapshot)
    timestamp = snapshot.get("observed_at", now())
    fields = snapshot["fields"]
    old_state = runtime["execution_state"]
    events = 0
    risks = set()
    freshness = contract.get("freshness_seconds", 2400)
    for name, item in fields.items():
        if item.get("status") == "ok":
            runtime.setdefault("last_good", {})[name] = item
    runtime["snapshot"] = snapshot
    runtime["observed_at"] = timestamp
    if not snapshot["reachable"]:
        runtime["access"] = "unsupported" if snapshot.get("access") == "unsupported" else "unreachable"
        risks.add(runtime["access"])
        # No execution-state inference is possible during a transport failure.
    else:
        runtime["access"] = "reachable"
        runtime["log_cursor"] = snapshot.get("log_cursor", runtime.get("log_cursor", {}))
        process = fields.get("process", {})
        exit_field = fields.get("exit", {})
        completion = fields.get("completion", {})
        pvalue = process.get("value") or {}
        evalue = exit_field.get("value") or {}
        cvalue = completion.get("value") or {}
        process_known = _fresh(process, timestamp, freshness)
        exit_known = _fresh(exit_field, timestamp, freshness)
        if process_known:
            actual = pvalue.get("actual_gpu_uuids")
            runtime["actual_devices_status"] = "ok" if actual is not None else "unavailable"
            if actual is not None:
                runtime["actual_devices"] = actual
                runtime["actual_devices_observed_at"] = process["observed_at"]
        if exit_known:
            exit_known = (
                evalue.get("execution_id") == contract["execution_id"]
                and evalue.get("unit_id") == unit["id"]
                and evalue.get("attempt") == runtime["attempt"]
                and isinstance(evalue.get("exit_code"), int)
                and not isinstance(evalue.get("exit_code"), bool)
            )
            if not exit_known:
                risks.add("conflicting_exit_attribution")
        alive = process_known and pvalue.get("alive") is True
        identity_conflict = (
            process_known and pvalue.get("identity_matches") is False
            and pvalue.get("alive") is True and runtime.get("identity") is not None
        )
        if identity_conflict:
            runtime["execution_state"] = "finished_unverified"
            risks.add("process_identity_changed")
        elif alive and exit_known:
            # A valid exit record and a still-live same identity conflict: retain
            # both facts rather than acting on the more convenient one.
            runtime["execution_state"] = "needs_action"
            risks.add("conflicting_live_and_exit")
        elif alive:
            runtime["execution_state"] = "running"
        elif exit_known or (process_known and pvalue.get("alive") is False and runtime.get("identity")):
            runtime["exit_result"] = evalue if exit_known else None
            if exit_known:
                # Never replace a real finish time by this pass's discovery time.
                runtime["finished_at"] = evalue.get("finished_at")
            if exit_known and evalue["exit_code"] != 0:
                runtime["execution_state"] = "failed"
            elif (
                (unit.get("completion") or {}).get("kind") == "exit_code"
                and runtime.get("managed") is True and exit_known
                and evalue["exit_code"] == 0
            ):
                runtime["execution_state"] = "completed"
            elif (
                (unit.get("completion") or {}).get("kind") == "json"
                and _fresh(completion, timestamp, freshness) and cvalue.get("passed") is True
                and cvalue.get("belongs_to_current") is True
            ):
                runtime["execution_state"] = "completed"
            elif (unit.get("completion") or {}).get("kind") == "json" and (
                completion.get("status") in {"parse_error", "not_expected_yet"}
                or cvalue.get("passed") is False
            ):
                runtime["execution_state"] = "incomplete"
            else:
                runtime["execution_state"] = "finished_unverified"
        elif runtime.get("identity") and not process_known and not exit_known:
            risks.add("process_coverage_unavailable")
        if runtime.get("launch_status") == "unknown" and runtime["execution_state"] not in TERMINAL:
            risks.add("ambiguous_launch")
        errors = fields.get("errors", {})
        if errors.get("status") == "ok" and errors.get("value"):
            runtime["current_error_evidence"] = errors["value"]
        resource_field = fields.get("resources", {})
        resources = resource_field.get("value") or {}
        if resource_field.get("status") == "ok":
            coverage_ok = True
            for metric, reserve in (
                ("ram_available_mb", "ram_mb"), ("disk_free_mb", "disk_mb"),
                ("shm_free_mb", "shm_mb"),
            ):
                if reserve not in contract.get("reserves", {}):
                    continue
                item = resources.get(metric, {})
                if not _fresh(item, timestamp, freshness):
                    risks.add("coverage:" + metric)
                    coverage_ok = False
                elif item.get("value") is None or item["value"] < contract["reserves"][reserve]:
                    risks.add("low:" + metric)
                    coverage_ok = False
            if unit.get("devices"):
                gpu_field = resources.get("gpus", {})
                rows = gpu_field.get("value") or []
                actual = {row.get("uuid"): row for row in rows}
                if not _fresh(gpu_field, timestamp, freshness):
                    risks.add("coverage:gpus")
                    coverage_ok = False
                for device in unit["devices"]:
                    free = actual.get(device, {}).get("memory_free_mb")
                    if free is None:
                        risks.add("coverage:" + device)
                        coverage_ok = False
                    elif free < contract.get("reserves", {}).get("gpu_mb", 0):
                        risks.add("low:" + device)
                        coverage_ok = False
            if coverage_ok:
                runtime["resource_observations"] = runtime.get("resource_observations", 0) + 1
        elif runtime.get("execution_state") not in TERMINAL:
            risks.add("coverage:resources")
        progress = fields.get("progress", {})
        if _fresh(progress, timestamp, freshness) and isinstance(progress.get("value"), dict):
            current = progress["value"]
            previous = runtime.get("previous_progress")
            runtime["previous_progress"] = {"value": current, "at": timestamp}
            if previous and current.get("phase") == previous["value"].get("phase"):
                delta = (current.get("step") or 0) - (previous["value"].get("step") or 0)
                elapsed = timestamp - previous["at"]
                if delta > 0 and elapsed > 0:
                    runtime["phase_rate"] = {
                        "phase": current.get("phase"), "rate": delta / elapsed,
                        "work_kind": current.get("work_kind", "step"), "observed_at": timestamp,
                    }
                    total = current.get("total")
                    runtime["eta_seconds"] = (
                        (total - current["step"]) / (delta / elapsed)
                        if isinstance(total, (int, float)) and total >= current["step"] else None
                    )
                elif delta < 0:
                    runtime.pop("phase_rate", None)
                    runtime["eta_seconds"] = None
            else:
                runtime.pop("phase_rate", None)
                runtime["eta_seconds"] = None
        if runtime["execution_state"] == "failed":
            kinds = {
                item.get("kind") if isinstance(item, dict) else str(item)
                for item in runtime.get("current_error_evidence", [])
            }
            if "cuda_oom" in kinds:
                risks.add("cuda_oom_freeze")
    old_risks = set(runtime.get("risk_active", []))
    episodes = runtime.setdefault("risk_episodes", {})
    for risk in sorted(risks - old_risks):
        episodes[risk] = episodes.get(risk, 0) + 1
        events += _emit_event(
            store, contract, unit, runtime, "risk", {"risk": risk},
            "risk:%s:%s" % (risk, episodes[risk]),
            analyze=risk.startswith("conflicting") or risk == "process_identity_changed",
        )
    for risk in sorted(old_risks - risks):
        events += _emit_event(
            store, contract, unit, runtime, "recovered", {"risk": risk},
            "recovered:%s:%s" % (risk, episodes.get(risk, 1)),
        )
    runtime["risk_active"] = sorted(risks)
    # OOM/throughput freezes remain until a separately reviewed contract; a
    # transient network/telemetry freeze clears only with fresh observations.
    runtime["freeze_admission"] = (
        bool(risks) or runtime.get("drain", False)
        or runtime["execution_state"] in {"needs_action", "failed", "incomplete", "finished_unverified"}
    )
    if "cuda_oom_freeze" in risks:
        runtime["drain"] = True
    if runtime["execution_state"] in TERMINAL and old_state != runtime["execution_state"]:
        events += _emit_event(
            store, contract, unit, runtime, runtime["execution_state"],
            {"exit_result": runtime.get("exit_result"), "completion": fields.get("completion"),
             "errors": runtime.get("current_error_evidence", []),
             "finished_at": runtime.get("finished_at")},
            analyze=runtime["execution_state"] == "failed" and not runtime.get("current_error_evidence"),
        )
    runtime["record_written_at"] = now()
    return events, int(old_state != runtime["execution_state"])


def _delivery_path(spec, key):
    return Path(spec["path"]) / (digest(key) + ".json")


def _payload(store, item):
    row = store.db.execute("SELECT * FROM events WHERE key=?", (item["event_key"],)).fetchone()
    return redact({
        "event_key": row["key"], "execution_id": row["execution"], "unit_id": row["unit"],
        "kind": row["kind"], "event": json.loads(row["doc"]),
        "analysis_is_advisory": True,
    })


def _deliver(spec, item, payload):
    kind = spec.get("kind", "none")
    if kind in {"file", "spool"}:
        path = _delivery_path(spec, item["key"])
        if path.exists():
            if read_json(path) != payload:
                raise PilotError("Stable delivery path contains conflicting content")
        else:
            write_json(path, payload)
        return {"path": str(path), "reconciliable": True}
    if kind == "desktop":
        if sys.platform != "darwin":
            raise PilotError("desktop notifications are implemented only on macOS")
        script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script, "RunPilot",
             "%s / %s: %s" % (payload["execution_id"], payload["unit_id"], payload["kind"])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        if result.returncode:
            raise PilotError("desktop adapter returned %d" % result.returncode)
        return {"reconciliable": False, "note": "Desktop acceptance is not proof the user saw it"}
    raise PilotError("No authorized delivery adapter")


def dispatch(store, contract, batch=16):
    """Claim briefly, deliver outside locks, preserve failures across restarts."""
    stats = {"delivered": 0, "exhausted": 0, "unknown": 0, "errors": 0, "analysis_spooled": 0}
    execution = contract["execution_id"]
    for item in store.outbox_rows(execution, pending_only=True):
        if sum(stats.values()) >= batch:
            break
        spec = contract.get(item["channel"], {"kind": "none"})
        if item["status"] in {"delivered", "exhausted", "unknown"}:
            continue
        if item["status"] == "claimed":
            if now() - (item["claimed_at"] or 0) < 120:
                continue
            # Only the stable file/spool adapter can reconcile a lost response.
            if spec.get("kind") not in {"file", "spool"}:
                with store.transaction():
                    store.db.execute(
                        "UPDATE outbox SET status='unknown',doc=? WHERE key=? AND status='claimed'",
                        (canonical({"error": "delivery response lost; cannot prove never sent; no blind resend"}), item["key"]),
                    )
                stats["unknown"] += 1
                continue
        if item["next_at"] > now():
            continue
        payload = _payload(store, item)
        with store.transaction():
            fresh = store.db.execute("SELECT * FROM outbox WHERE key=?", (item["key"],)).fetchone()
            if not fresh or fresh["status"] != item["status"] or fresh["attempts"] != item["attempts"]:
                continue
            if item["channel"] == "analysis":
                count = store.db.execute(
                    "SELECT COUNT(*) FROM outbox o JOIN events e ON o.event_key=e.key "
                    "WHERE e.execution=? AND o.channel='analysis' AND o.status IN ('delivered','claimed','unknown')",
                    (execution,),
                ).fetchone()[0]
                # A stale file claim belongs to this existing request, not a new one.
                if item["status"] != "claimed" and count >= spec.get("max_requests", 3):
                    store.db.execute("UPDATE outbox SET status='exhausted',doc=? WHERE key=?",
                                     (canonical({"error": "analysis request budget exhausted"}), item["key"]))
                    stats["exhausted"] += 1
                    continue
            # A previously claimed file may already exist, even at the retry
            # limit: reconcile it before deciding its outcome.
            can_reconcile = (
                item["status"] == "claimed" and spec.get("kind") in {"file", "spool"}
                and _delivery_path(spec, item["key"]).exists()
            )
            if fresh["attempts"] >= spec.get("max_attempts", 3) and not can_reconcile:
                store.db.execute("UPDATE outbox SET status='exhausted',doc=? WHERE key=?",
                                 (canonical({"error": "delivery budget exhausted"}), item["key"]))
                stats["exhausted"] += 1
                continue
            store.db.execute(
                "UPDATE outbox SET status='claimed',attempts=attempts+?,claimed_at=?,doc=? WHERE key=?",
                (0 if can_reconcile else 1, now(), canonical({"submission_intent": True}), item["key"]),
            )
        try:
            result = _deliver(spec, item, payload)
        except subprocess.TimeoutExpired:
            status, result = "unknown", {"error": "adapter timed out; delivery may have occurred; no blind resend"}
            stats["unknown"] += 1
        except (OSError, ValueError, PilotError) as error:
            status, result = "pending", {"error": redact(str(error))[:500]}
            stats["errors"] += 1
        else:
            status = "delivered"
            stats["delivered"] += 1
            stats["analysis_spooled"] += int(item["channel"] == "analysis")
        with store.transaction():
            attempts = store.db.execute("SELECT attempts FROM outbox WHERE key=?", (item["key"],)).fetchone()[0]
            if status == "pending" and attempts >= spec.get("max_attempts", 3):
                status = "exhausted"
                stats["exhausted"] += 1
            store.db.execute(
                "UPDATE outbox SET status=?,doc=?,next_at=? WHERE key=?",
                (status, canonical(result), now() + min(3600, 30 * 2 ** attempts), item["key"]),
            )
    return stats


def _throughput_drain(store, contract):
    """Use only an explicitly comparable baseline, never add unlike Epoch/s."""
    runtimes = store.units(contract["execution_id"])
    active_pairs = [(name, r) for name, r in runtimes.items() if r["execution_state"] == "running"]
    signature = sorted("%s:%s" % (name, r["attempt"]) for name, r in active_pairs)
    if any(r.get("concurrency_signature") != signature for _, r in active_pairs):
        for name, runtime in active_pairs:
            runtime["concurrency_signature"] = signature
            runtime.pop("phase_rate", None)
            runtime.pop("previous_progress", None)
            runtime["eta_seconds"] = None
            runtime["low_throughput_checks"] = 0
            store.save_unit(contract["execution_id"], name, runtime)
        return  # Relearn speed after concurrency changes; no stale serial ETA.
    spec = contract.get("throughput", {})
    baseline = spec.get("baseline_total_rate")
    if not isinstance(baseline, (int, float)) or baseline <= 0 or not spec.get("phase"):
        return
    active = [r for _, r in active_pairs]
    if len(active) < 2:
        return
    rates = [r.get("phase_rate", {}) for r in active]
    if any(
        r.get("phase") != spec["phase"] or r.get("work_kind") != spec.get("work_kind", "step")
        or now() - r.get("observed_at", 0) > contract.get("freshness_seconds", 2400)
        for r in rates
    ):
        return
    total = sum(r["rate"] for r in rates)
    low = total < baseline * (1 + spec.get("min_gain_fraction", 0))
    for name, runtime in active_pairs:
        runtime["low_throughput_checks"] = runtime.get("low_throughput_checks", 0) + 1 if low else 0
        store.save_unit(contract["execution_id"], name, runtime)
    if not low or min(r["low_throughput_checks"] for r in active) < spec.get("window_samples", 3):
        return
    events = 0
    for unit in contract["units"]:
        runtime = runtimes[unit["id"]]
        runtime["drain"] = True
        runtime["freeze_admission"] = True
        events += _emit_event(store, contract, unit, runtime, "throughput_drain",
                              {"total_rate": total, "baseline": baseline, "phase": spec["phase"]})
        store.save_unit(contract["execution_id"], unit["id"], runtime)
    return events


def check_once(store, execution, fixture=None, deliver=True):
    started_at = now()
    contract = store.contract(execution)
    if fixture is not None and contract.get("target", {}).get("kind") != "fixture":
        raise PilotError("Synthetic snapshots are accepted only for fixture contracts")
    counts = {"events": 0, "state_changes": 0, "collected_units": 0, "program_errors": [],
              "actions": 0, "model_submissions": 0}
    if fixture is None and contract.get("target", {}).get("kind", "local") == "local":
        # A late supervisor receipt can arrive after the launch caller lost its
        # response. Reconcile metadata before interpreting missing identity or
        # start time; this path never resubmits a task command.
        from run import reconcile
        for unit_id, runtime in store.units(execution).items():
            if (runtime.get("managed") and runtime["execution_state"] not in TERMINAL
                    and (runtime.get("launch_status") == "unknown" or runtime["execution_state"] == "starting")):
                try:
                    reconcile(store, execution, unit_id)
                except (Busy, PilotError, OSError, ValueError) as error:
                    counts["program_errors"].append({"unit": unit_id, "reconciliation_error": str(error)[:500]})
    with store.lock(execution):
        snapshots = {}
        for unit in contract["units"]:
            runtime = store.unit(execution, unit["id"])
            if runtime["monitoring"] != "active" or runtime["execution_state"] in TERMINAL:
                continue
            # Ready units have no process to observe. Resource admission collects
            # a fresh snapshot at launch; do not invent "finished" for no PID.
            if not runtime.get("identity") and runtime["execution_state"] == "ready" and fixture is None:
                continue
            try:
                if fixture is not None:
                    snapshots[unit["id"]] = copy.deepcopy(
                        fixture.get("units", {}).get(unit["id"], fixture)
                    )
                else:
                    from collectors import collect
                    snapshots[unit["id"]] = collect(contract, unit, runtime, store.root)
                validate_snapshot(snapshots[unit["id"]])
            except (OSError, ValueError, PilotError, KeyError) as error:
                counts["program_errors"].append({"unit": unit["id"], "error": str(error)[:500]})
        with store.transaction():
            for unit in contract["units"]:
                snapshot = snapshots.get(unit["id"])
                if snapshot is None:
                    continue
                runtime = store.unit(execution, unit["id"])
                events, changes = apply_snapshot(store, contract, unit, runtime, snapshot)
                store.save_unit(execution, unit["id"], runtime)
                counts["events"] += events
                counts["state_changes"] += changes
                counts["collected_units"] += 1
            counts["events"] += _throughput_drain(store, contract) or 0
            store.db.execute("INSERT INTO checks(execution,started_at,ended_at,doc) VALUES(?,?,?,?)",
                             (execution, started_at, now(), canonical(counts)))
    # Slow external I/O must not hold the collection or SQLite state lock.
    deliveries = dispatch(store, contract) if deliver else {}
    if (
        fixture is None and "schedule" in contract.get("allowed_actions", [])
        and contract.get("scope_approved") is True
    ):
        from run import launch_unit
        for unit in contract["units"]:
            runtime = store.unit(execution, unit["id"])
            if runtime["execution_state"] == "ready" and runtime["monitoring"] == "active":
                try:
                    result = launch_unit(store, contract, unit["id"])
                    counts["actions"] += int(result.get("status") in {"confirmed", "submitted", "unknown", "running"})
                    counts["admission"] = result
                except (PilotError, OSError) as error:
                    counts["admission"] = {"allowed": False, "reason": str(error)}
                break  # Gradual admission: at most ONE candidate per pass.
    units = store.units(execution)
    active = any(r["monitoring"] == "active" and r["execution_state"] not in TERMINAL for r in units.values())
    pending = store.has_pending(execution)
    counts.update(
        deliveries=deliveries, needs_scheduler=active or pending, pending_delivery=pending,
        observed_at=now(), mode="fixed-program", collection_complete=not counts["program_errors"],
    )
    # Preserve actual action/delivery counts as well as the transactional record.
    with store.transaction():
        store.db.execute("UPDATE checks SET ended_at=?,doc=? WHERE id=("
                         "SELECT MAX(id) FROM checks WHERE execution=? AND started_at=?)",
                         (now(), canonical(counts), execution, started_at))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check-once", "status", "events"):
        child = sub.add_parser(name)
        child.add_argument("--state-dir")
        child.add_argument("--execution", required=True)
        if name == "check-once":
            child.add_argument("--fixture", help="Synthetic snapshot; fixture contracts only")
            child.add_argument("--no-deliver", action="store_true")
    args = parser.parse_args(argv)
    store = None
    try:
        store = Store(args.state_dir)
        store.contract(args.execution)
        if args.command == "check-once":
            result = check_once(
                store, args.execution, read_json(args.fixture) if args.fixture else None,
                deliver=not args.no_deliver,
            )
            emit(result)
            return 1 if result["program_errors"] else 0
        if args.command == "status":
            emit({"units": store.units(args.execution), "outbox": store.outbox_rows(args.execution)})
        else:
            emit(store.event_rows(args.execution))
        return 0
    except Busy as error:
        emit({"status": "busy", "detail": str(error), "model_submissions": 0})
        return 0
    except (PilotError, OSError, ValueError) as error:
        emit({"program_error": str(error), "task_failure": False, "training_failure": False})
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
