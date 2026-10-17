"""Pure, conservative admission policy. No commands, signals or model calls."""

import os

from core import TERMINAL, now


def _value(container, key, timestamp, freshness):
    item = container.get(key, {})
    age = timestamp - item.get("observed_at", 0)
    if item.get("status") != "ok" or not -30 <= age <= freshness:
        return None
    return item.get("value")


def _overlap(left, right):
    left, right = os.path.realpath(left), os.path.realpath(right)
    try:
        return os.path.commonpath([left, right]) in {left, right}
    except ValueError:
        return False


def admission(contract, unit, runtimes, snapshot, reservations=None):
    """Recheck under the resource-domain lock immediately before claiming a launch.

    Reservations cover starts not yet reflected in device samples, including
    unknown submissions. A known PID alone does not release a reservation.
    """
    reasons = []
    execution = contract["execution_id"]
    runtime = runtimes.get(unit["id"], {})
    timestamp = now()
    freshness = contract.get("freshness_seconds", 2400)
    if contract.get("scope_approved") is not True or "launch" not in contract.get("allowed_actions", []):
        reasons.append("scope/launch authorization is absent")
    if contract.get("target", {}).get("kind", "local") != "local":
        reasons.append("only local POSIX supervised launch is implemented")
    if runtime.get("execution_state") not in {"ready", "retry_pending"}:
        reasons.append("unit is not ready (terminal/running/ambiguous units are never replayed)")
    if runtime.get("monitoring") != "active":
        reasons.append("monitoring is paused/stopped")
    if not unit.get("argv"):
        reasons.append("no reviewed existing argv")
    for dependency in unit.get("depends_on", []):
        if runtimes.get(dependency, {}).get("execution_state") != "completed":
            reasons.append("dependency is not verified completed: " + dependency)
    if not snapshot.get("reachable", False):
        reasons.append("current resource observation is unreachable")
    if timestamp - snapshot.get("observed_at", 0) > freshness:
        reasons.append("snapshot is stale")
    active = [
        (name, current) for name, current in runtimes.items()
        if current.get("execution_state") in {"starting", "running", "verifying"}
    ]
    reservations = reservations or []
    foreign = [
        r for r in reservations if r.get("execution") != execution
        and r.get("status") in {"submitted", "confirmed", "unknown"}
    ]
    if len(active) + len(foreign) >= contract.get("max_concurrency", 1):
        reasons.append("approved concurrency limit reached (unknown claims count)")
    if any(current.get("freeze_admission") for current in runtimes.values()):
        reasons.append("new admission is frozen; drain existing work without stopping it")
    fields = snapshot.get("fields", {})
    resource_field = fields.get("resources", {})
    resources = resource_field.get("value") or {}
    if resource_field.get("status") != "ok":
        reasons.append("resource collection is unavailable")
    estimates = unit.get("resources", {})
    reserves = contract.get("reserves", {})
    unreflected = [
        r for r in reservations
        if r.get("status") in {"submitted", "confirmed", "unknown"}
        and not r.get("reflected_in_sample", False)
        and not (r.get("execution") == execution and r.get("unit") == unit["id"])
    ]
    for metric, estimate_key in (
        ("ram_available_mb", "ram_mb"), ("disk_free_mb", "disk_mb"),
        ("cpu_threads", "cpu_threads"),
    ):
        amount = _value(resources, metric, timestamp, freshness)
        if estimate_key not in reserves:
            reasons.append("explicit safety reserve is required: " + estimate_key)
        if amount is None:
            reasons.append("fresh host resource is unavailable: " + metric)
            continue
        pending = sum(r.get("resources", {}).get(estimate_key, 0) for r in unreflected)
        # RAM/disk are already free quantities; do not subtract current usage again.
        # CPU is a capacity, so current running thread budgets are accounted below.
        running_cpu = 0
        if estimate_key == "cpu_threads":
            running_cpu = sum(
                next((u.get("resources", {}).get("cpu_threads", 0)
                      for u in contract["units"] if u["id"] == name), 0)
                for name, _ in active
            )
            pending = sum(
                r.get("resources", {}).get(estimate_key, 0) for r in foreign
            )
        needed = estimates.get(estimate_key, 0) + reserves.get(estimate_key, 0) + pending + running_cpu
        if amount < needed:
            reasons.append("insufficient " + metric)
    if estimates.get("shm_mb", 0) or reserves.get("shm_mb", 0):
        free_shm = _value(resources, "shm_free_mb", timestamp, freshness)
        pending_shm = sum(r.get("resources", {}).get("shm_mb", 0) for r in unreflected)
        if free_shm is None or free_shm < estimates.get("shm_mb", 0) + reserves.get("shm_mb", 0) + pending_shm:
            reasons.append("shared-memory headroom is unknown/insufficient")
    devices = unit.get("devices", [])
    gpu_rows = _value(resources, "gpus", timestamp, freshness) if devices else []
    gpus = {g.get("uuid"): g for g in gpu_rows or []}
    if devices:
        visible = unit.get("env", {}).get("CUDA_VISIBLE_DEVICES", os.environ.get("CUDA_VISIBLE_DEVICES"))
        binding = []
        for item in visible.split(",") if visible else []:
            item = item.strip()
            binding.append(next(
                (g["uuid"] for g in gpu_rows or [] if str(g.get("index")) == item),
                item,
            ))
        if set(binding) != set(devices):
            reasons.append("actual CUDA_VISIBLE_DEVICES mapping is not proven equal to the approved unit devices")
    for device in devices:
        if device not in contract.get("devices", []) or device.startswith("MIG-"):
            reasons.append("device is outside the implemented/approved device mapping: " + device)
            continue
        gpu = gpus.get(device)
        if not gpu or gpu.get("memory_free_mb") is None:
            reasons.append("fresh per-device telemetry missing: " + device)
            continue
        mode = str(gpu.get("compute_mode", "")).lower().replace("_", " ")
        if mode in {"prohibited", "2"}:
            reasons.append("GPU compute mode prohibits execution")
        if mode in {"exclusive process", "exclusive thread", "exclusive", "1", "3"} and (
            active or foreign or gpu.get("process_count") != 0
        ):
            reasons.append("exclusive compute mode has no proven empty context slot")
        if (active or foreign) and mode not in {"default", "0"}:
            reasons.append("compute mode does not prove concurrent contexts are allowed")
        estimate = estimates.get("gpu_mb", {}).get(device)
        if estimate is None and (active or foreign):
            reasons.append("candidate GPU need is unknown: " + device)
            continue
        future = 0
        for name, _ in active:
            other = next(u for u in contract["units"] if u["id"] == name)
            if device in other.get("devices", []):
                growth = other.get("resources", {}).get("future_gpu_mb")
                if growth is None:
                    reasons.append("running unit future GPU growth is unknown: " + name)
                future += growth or 0
        pending = sum(r.get("resources", {}).get("gpu_mb", {}).get(device, 0) for r in unreflected)
        needed = (estimate or 0) + future + pending + reserves.get("gpu_mb", 0)
        if gpu["memory_free_mb"] < needed:
            reasons.append("insufficient per-device memory headroom: " + device)
    if active or foreign:
        participants = [unit] + [next(u for u in contract["units"] if u["id"] == name) for name, _ in active]
        for participant in participants:
            if not (
                participant.get("independent_entry") is True
                and participant.get("isolation_verified") is True
                and participant.get("evidence")
                and participant.get("write_paths")
            ):
                reasons.append("independent entry/write isolation evidence missing: " + participant["id"])
            if not participant.get("resource_evidence"):
                reasons.append("formal resource evidence missing: " + participant["id"])
            if not all(key in participant.get("resources", {}) for key in ("ram_mb", "disk_mb", "cpu_threads")):
                reasons.append("host demand estimate incomplete: " + participant["id"])
            if participant.get("exclusive_project_lock") or participant.get("unsafe_shared_writes"):
                reasons.append("project-level exclusive/shared write conflict")
        # Compare against all approved units, including not-yet-started units, so a
        # shared summary/checkpoint path cannot be silently accepted by ordering.
        for other in contract["units"]:
            if other["id"] == unit["id"]:
                continue
            if any(_overlap(a, b) for a in unit.get("write_paths", []) for b in other.get("write_paths", [])):
                reasons.append("overlapping write paths: " + other["id"])
        for reservation in foreign:
            if not reservation.get("isolation_verified") or not reservation.get("write_paths"):
                reasons.append("foreign claim lacks write isolation evidence")
            if any(_overlap(a, b) for a in unit.get("write_paths", []) for b in reservation.get("write_paths", [])):
                reasons.append("write conflict with shared resource-domain claim")
        if not contract.get("baseline_evidence"):
            for name, current in active:
                if current.get("resource_observations", 0) < 2:
                    reasons.append("new task needs multiple natural formal-run observations: " + name)
    return {"allowed": not reasons, "reasons": sorted(set(reasons))}
