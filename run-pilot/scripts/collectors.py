"""Bounded, read-only observations. Python 3.9 standard library only.

Public interfaces:
  process_identity(pid) -> {pid, start_time, boot_id, start_epoch}
  process_matches(identity) -> bool
  completion_fingerprint(path) -> fingerprint or None (missing file)
  collect(contract, unit, runtime, state_dir) -> field-wrapped snapshot

Launcher handshake: BEFORE spawning, save completion_fingerprint(report_path)
as runtime["completion_baseline"]. Its JSON schema is
{device: int, inode: int, size: int, mtime_ns: int, sha256: str}.
Use None for a missing report; other read failures must not be silently ignored.
Equal sha256 content is excluded even if the old file was touched or moved.
Set runtime["managed"] = True for launched workers, including reconciliation.
The worker's runtime log_path/exit_path override the unit's paths. Attached
workers (managed != True) establish an EOF baseline on their first observation.

No project code, validators, shell snippets, torch, remote installers, or
training commands are executed. SSH/fixture targets are explicitly unsupported
here; fixture snapshots are supplied directly to monitor.check_once instead.
"""

import base64
import calendar
import csv
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

from core import PilotError, field, now, redact


PROC = Path("/proc")
JSON_LIMIT = 1024 * 1024
TEXT_LIMIT = 128 * 1024
COMMAND_LIMIT = 256 * 1024
LOG_LIMIT = 256 * 1024
LINE_LIMIT = 16 * 1024
RESOURCE_NAMES = ("gpus", "ram_available_mb", "disk_free_mb", "shm_free_mb", "cpu_threads")
GPU_QUERY = (
    "nvidia-smi",
    "--query-gpu=uuid,index,name,memory.free,memory.total,compute_mode,utilization.gpu,temperature.gpu",
    "--format=csv,noheader,nounits",
)
GPU_APPS_QUERY = (
    "nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
    "--format=csv,noheader,nounits",
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
TEXT_STEP = re.compile(r"\b(step|epoch)\s*[:=#]?\s*\[?\s*(\d{1,16})(?:\s*/\s*(\d{1,16}))?", re.I)
TEXT_PHASE = re.compile(r"\bphase\s*[:=]\s*([A-Za-z0-9_.-]{1,96})", re.I)
TEXT_TOTAL = re.compile(r"\btotal(?:_steps|_epochs)?\s*[:=]\s*(\d{1,16})", re.I)
ERROR_PATTERNS = (
    ("cuda_oom", re.compile(
        r"\bcuda(?:\s+error)?\s*:?\s*out of memory\b|"
        r"\b(?:torch\.)?cuda\.OutOfMemoryError\b|\bCUDA_ERROR_OUT_OF_MEMORY\b", re.I)),
    ("host_oom", re.compile(
        r"\bMemoryError\b|\bstd::bad_alloc\b|\bcannot allocate memory\b|"
        r"\bhost out of memory\b|\boom[-_ ]kill(?:er)?\b|"
        r"\bout of memory:\s*killed process\b|CPUAllocator.*can't allocate memory", re.I)),
    ("nccl", re.compile(
        r"\bNCCL\s+(?:WARN|ERROR)\b|\bnccl(?:Error|UnhandledCudaError|SystemError|RemoteError)\b|"
        r"\bNCCL\s*:\s*(?:error|timeout)\b|\bDistBackendError\b", re.I)),
)


class _ObservationError(PilotError):
    pass


class _CommandFailure(_ObservationError):
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        super().__init__("probe exited %d: %s" % (returncode, redact(stderr)[:240]))


def _finite(value, minimum=0):
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= minimum
    )


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ObservationError("collection time budget exhausted")
    return remaining


def _command(argv, deadline, output_limit=COMMAND_LIMIT, timeout=3):
    """Fixed probes only; bound combined stdout/stderr while the child runs.

    communicate()/subprocess.run(capture_output=True) would buffer arbitrarily
    much output before a caller could check its size. Never use a shell.
    """
    end = min(deadline, time.monotonic() + timeout)
    _remaining(end)
    env = dict(os.environ, LC_ALL="C", LANG="C", TZ="UTC")
    process = subprocess.Popen(
        list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False, env=env, close_fds=True,
    )
    selector = selectors.DefaultSelector()
    chunks = {"stdout": [], "stderr": []}
    size = 0
    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            for key, _ in selector.select(_remaining(end)):
                data = os.read(key.fileobj.fileno(), min(16384, output_limit + 1 - size))
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                size += len(data)
                if size > output_limit:
                    raise _ObservationError("probe output exceeds byte limit")
                chunks[key.data].append(data)
        try:
            code = process.wait(timeout=_remaining(end))
        except subprocess.TimeoutExpired as error:
            raise _ObservationError("probe timed out") from error
        output = b"".join(chunks["stdout"]).decode("utf-8", "replace")
        if code:
            detail = b"".join(chunks["stderr"]).decode("utf-8", "replace")
            raise _CommandFailure(code, output, detail)
        return output
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            stream.close()
        if process.poll() is None:
            # This is our short-lived observation child, never the training PID.
            process.kill()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass


def _open_regular(path):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _ObservationError("observation source is not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _read_bytes(path, limit=TEXT_LIMIT):
    with _open_regular(path) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _ObservationError("observation source exceeds byte limit")
    return data


def _read_text(path, limit=TEXT_LIMIT):
    return _read_bytes(path, limit).decode("utf-8", "strict")


def _fingerprint(info, data):
    return {
        "device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
        "mtime_ns": info.st_mtime_ns, "sha256": hashlib.sha256(data).hexdigest(),
    }


def _report_bytes(path):
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > JSON_LIMIT:
            raise _ObservationError("JSON report exceeds byte limit")
        data = stream.read(JSON_LIMIT + 1)
        after = os.fstat(stream.fileno())
    if len(data) > JSON_LIMIT:
        raise _ObservationError("JSON report exceeds byte limit")
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise _ObservationError("report changed while being read; retry observation")
    return data, after, _fingerprint(after, data)


def completion_fingerprint(path):
    """Read at most 1 MiB; return the launch-baseline schema documented above.

    Returns None ONLY for a missing file. Permission, non-regular-file, changing
    report and size-limit errors propagate (OSError/PilotError).
    """
    try:
        return _report_bytes(path)[2]
    except FileNotFoundError:
        return None


def _linux_identity(pid):
    text = _read_text(PROC / str(pid) / "stat", 16384)
    # comm is parenthesized, but may itself contain whitespace and ')' chars.
    closing = text.rfind(")")
    if closing < 0 or text.partition(" ")[0] != str(pid):
        raise ValueError("invalid /proc process stat")
    values = text[closing + 1:].split()  # field 3 (state) onward
    if len(values) < 20:
        raise ValueError("incomplete /proc process stat")
    if values[0] in {"Z", "X", "x"}:
        raise ProcessLookupError(errno.ESRCH, "process is a zombie or dead")
    ticks = int(values[19])  # field 22, not a whitespace split of comm
    if ticks < 0:
        raise ValueError("invalid process start ticks")
    match = re.search(r"^btime\s+(\d+)$", _read_text(PROC / "stat"), re.M)
    if not match:
        raise ValueError("boot time is unavailable")
    boot_epoch = int(match.group(1))
    try:
        boot_id = _read_text(PROC / "sys/kernel/random/boot_id", 256).strip()
    except FileNotFoundError:
        boot_id = "btime:%d" % boot_epoch
    if not boot_id:
        raise ValueError("empty boot identity")
    frequency = os.sysconf("SC_CLK_TCK")
    if frequency <= 0:
        raise ValueError("invalid system clock tick frequency")
    return {
        "pid": pid, "start_time": str(ticks), "boot_id": boot_id,
        "start_epoch": boot_epoch + ticks / float(frequency),
    }


def _mac_identity(pid, deadline):
    try:
        text = _command(
            ("/bin/ps", "-p", str(pid), "-o", "pid=", "-o", "lstart=", "-o", "stat="),
            deadline, output_limit=4096,
        )
    except _CommandFailure as error:
        # macOS ps uses status 1 for an empty selection. A permission error,
        # diagnostic, nonempty output, other status or timeout is NOT death.
        if error.returncode == 1 and not error.stdout.strip() and not error.stderr.strip():
            raise ProcessLookupError(errno.ESRCH, "process does not exist") from error
        raise
    pieces = text.split()
    if not pieces:
        raise ProcessLookupError(errno.ESRCH, "process does not exist")
    if len(pieces) != 7 or pieces[0] != str(pid):
        raise ValueError("invalid ps identity output")
    if pieces[6].startswith(("Z", "X")):
        raise ProcessLookupError(errno.ESRCH, "process is a zombie or dead")
    start = " ".join(pieces[1:6])
    epoch = calendar.timegm(time.strptime(start, "%a %b %d %H:%M:%S %Y"))
    boot = _command(("/usr/sbin/sysctl", "-n", "kern.boottime"), deadline, output_limit=4096)
    match = re.search(r"sec\s*=\s*(\d+)\s*,\s*usec\s*=\s*(\d+)", boot)
    if not match:
        raise ValueError("invalid macOS boot identity")
    return {
        "pid": pid, "start_time": start,
        "boot_id": "btime:%s:%s" % match.groups(), "start_epoch": epoch,
    }


def _process_identity(pid, deadline):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    _remaining(deadline)
    try:
        if sys.platform.startswith("linux"):
            return _linux_identity(pid)
        if sys.platform == "darwin":
            return _mac_identity(pid, deadline)
    except FileNotFoundError as error:
        # Only the process stat disappearing means death. Missing host boot
        # metadata is a coverage problem, not evidence of process termination.
        if str(error.filename) == str(PROC / str(pid) / "stat"):
            raise ProcessLookupError(errno.ESRCH, "process does not exist") from error
        raise
    raise _ObservationError("process identity is implemented only on Linux/macOS")


def process_identity(pid):
    """Return a live process's identity, or raise OSError/ValueError/PilotError.

    Zombies are not live. macOS ps start times have one-second precision.
    No kill(0), name matching, training execution, or project import is used.
    """
    return _process_identity(pid, time.monotonic() + 6)


def _identities_equal(expected, current):
    return (
        isinstance(expected, dict)
        and isinstance(expected.get("pid"), int) and not isinstance(expected["pid"], bool)
        and bool(expected.get("start_time")) and bool(expected.get("boot_id"))
        and all(expected.get(key) == current.get(key) for key in ("pid", "start_time", "boot_id"))
    )


def process_matches(identity):
    """Fail closed on death, PID reuse, reboot, malformed identity or coverage loss."""
    if not isinstance(identity, dict):
        return False
    try:
        return _identities_equal(identity, process_identity(identity.get("pid")))
    except (OSError, ValueError, PilotError):
        return False


def _failure(error, source, timestamp, status="unavailable"):
    return field(None, status=status, source=str(source), observed_at=timestamp,
                 detail=redact(str(error))[:300])


def _process_field(runtime, timestamp, deadline):
    expected = runtime.get("identity")
    if not expected:
        return field(None, status="not_expected_yet", source="process identity", observed_at=timestamp)
    try:
        current = _process_identity(expected.get("pid"), deadline)
    except ProcessLookupError:
        return field(
            {"alive": False, "identity_matches": False, "identity": None,
             "actual_gpu_uuids": None}, source="process identity", observed_at=timestamp,
        )
    except (OSError, ValueError, PilotError, AttributeError) as error:
        return _failure(error, "process identity", timestamp)
    return field(
        {"alive": True, "identity_matches": _identities_equal(expected, current),
         "identity": current, "actual_gpu_uuids": None},
        source="process identity", observed_at=timestamp,
    )


def _path(unit, runtime, key):
    path = runtime[key] if key in runtime else unit.get(key)
    if path is not None and (not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path):
        raise ValueError(key + " must be an absolute path")
    return path


def _json(data):
    def reject(value):
        raise ValueError("non-finite JSON number: " + value)
    def bounded_int(value):
        # Python 3.9 builds do not all limit giant integer conversions.
        if len(value) > 32:
            raise ValueError("JSON integer exceeds observation precision limit")
        return int(value)
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            reject(value)
        return result
    return json.loads(data, parse_constant=reject, parse_float=finite_float, parse_int=bounded_int)


def _attribution_matches(doc, contract, unit, runtime, required=False):
    expected = {"execution_id": contract.get("execution_id"), "unit_id": unit.get("id"),
                "attempt": runtime.get("attempt")}
    for key, value in expected.items():
        if key not in doc:
            if required:
                return False
            continue
        if type(doc[key]) is not type(value) or doc[key] != value:
            return False
    return True


def _exit_field(contract, unit, runtime, timestamp, deadline):
    source = "exit record"
    try:
        _remaining(deadline)
        path = _path(unit, runtime, "exit_path")
        if not path:
            return field(None, status="not_expected_yet", source=source, observed_at=timestamp)
        source = path
        data, info, _ = _report_bytes(path)
        doc = _json(data)
        if not isinstance(doc, dict):
            raise ValueError("exit record must be a JSON object")
        attempt = runtime.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("no current execution attempt for exit attribution")
        if not _attribution_matches(doc, contract, unit, runtime, required=True):
            return _failure("exit record does not match execution/unit/attempt", source, timestamp)
        if not isinstance(doc.get("exit_code"), int) or isinstance(doc["exit_code"], bool):
            raise ValueError("exit_code must be an actual integer exit result")
        if not _finite(doc.get("finished_at"), minimum=0.000001):
            raise ValueError("finished_at must be a real finite epoch timestamp")
        if _finite(runtime.get("started_at")) and doc["finished_at"] < runtime["started_at"]:
            raise ValueError("exit record predates this attempt")
        return field(
            {key: doc[key] for key in ("execution_id", "unit_id", "attempt", "exit_code", "finished_at")},
            source=source, observed_at=timestamp, source_updated_at=info.st_mtime,
        )
    except FileNotFoundError:
        return field(None, status="not_expected_yet", source=source, observed_at=timestamp)
    except (ValueError, UnicodeError, RecursionError) as error:
        return _failure(error, source, timestamp, "parse_error")
    except (OSError, PilotError) as error:
        return _failure(error, source, timestamp)


def _lookup(doc, name):
    if not isinstance(name, str) or not name or any(not p for p in name.split(".")):
        raise ValueError("report field mappings must be nonempty dotted names")
    if name in doc:
        return doc[name]
    value = doc
    for piece in name.split("."):
        if not isinstance(value, dict) or piece not in value:
            return None
        value = value[piece]
    return value


def _nonempty(value):
    return (value is not None and value != [] and value != {}
            and (not isinstance(value, str) or bool(value.strip())))


def _bounded_value(value, depth=0, count=None):
    """Bound exact JSON comparisons; booleans are not interchangeable with 1."""
    count = [0] if count is None else count
    count[0] += 1
    if depth > 8 or count[0] > 256:
        raise ValueError("group comparison exceeds nesting/item limits")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("group comparison requires finite JSON values")
        return
    if isinstance(value, str):
        if len(value) > 2048:
            raise ValueError("group comparison string exceeds limit")
        return
    if isinstance(value, list) and len(value) <= 64:
        for item in value:
            _bounded_value(item, depth + 1, count)
        return
    if isinstance(value, dict) and len(value) <= 64 and all(
        isinstance(key, str) and len(key) <= 128 for key in value
    ):
        for item in value.values():
            _bounded_value(item, depth + 1, count)
        return
    raise ValueError("group comparison must be a small JSON value")


def _group_constraints(spec):
    required, equals = spec.get("group_required_fields", []), spec.get("group_equals", {})
    if "group_required_fields" in spec and (
        not isinstance(required, list) or not 1 <= len(required) <= 64
        or not all(isinstance(key, str) and 0 < len(key) <= 128 for key in required)
    ):
        raise ValueError("group_required_fields must be a nonempty list of at most 64 field names")
    if "group_equals" in spec and (
        not isinstance(equals, dict) or not 1 <= len(equals) <= 64
        or not all(isinstance(key, str) and 0 < len(key) <= 128 for key in equals)
    ):
        raise ValueError("group_equals must be a nonempty mapping of at most 64 exact JSON values")
    for key in list(required) + list(equals):
        _lookup({}, key)  # validate the static dotted mapping without executing it
    for value in equals.values():
        _bounded_value(value)
    if (required or equals) and "expected_groups" not in spec:
        raise ValueError("per-group constraints require an explicit nonempty expected_groups")
    return required, equals


def _group_names(value, id_field):
    if isinstance(value, dict) and len(value) <= 256:
        return [str(key) for key in value]
    if not isinstance(value, list) or len(value) > 256:
        return None
    result = []
    for item in value:
        if isinstance(item, dict):
            item = _lookup(item, id_field)
        if isinstance(item, bool) or not isinstance(item, (str, int)) or item == "":
            return None
        result.append(str(item))
    return result


def _check_group_results(value, expected, spec, required, equals):
    if not required and not equals:
        return []
    if isinstance(value, dict):
        records = value
    else:
        id_field = spec.get("group_id_field", "id")
        records = {
            str(_lookup(item, id_field)): item for item in value if isinstance(item, dict)
        }
    reasons = []
    for name in expected:
        record = records.get(name)
        if not isinstance(record, dict):
            reasons.append("group %s has no object for required per-group results" % name)
            continue
        for key in required:
            if not _nonempty(_lookup(record, key)):
                reasons.append("group %s missing/empty required field: %s" % (name, key))
        for key, expected_value in equals.items():
            actual_value = _lookup(record, key)
            try:
                _bounded_value(actual_value)
                same = json.dumps(actual_value, sort_keys=True, allow_nan=False) == json.dumps(
                    expected_value, sort_keys=True, allow_nan=False,
                )
            except (ValueError, TypeError):
                same = False
            # Missing is not the JSON null value explicitly required by equals.
            if actual_value is None and _lookup(record, key) is None:
                current = record
                for part in key.split("."):
                    if not isinstance(current, dict) or part not in current:
                        same = False
                        break
                    current = current[part]
            if not same:
                reasons.append("group %s exact result mismatch: %s" % (name, key))
        if len(reasons) >= 32:
            break
    return reasons[:32]


def _completion_field(contract, unit, runtime, timestamp, deadline):
    """Consume an existing report; optional group constraints are declarative.

    Example mapping additions (never scripts):
      expected_groups: ["a", "b"], groups_field: "oof_groups",
      group_id_field: "id", group_required_fields: ["metric", "subject_count"],
      group_equals: {"subject_count": 128, "status": "completed"}
    oof_groups may be [{id: "a", ...}, {id: "b", ...}] or {"a": {...},
    "b": {...}}. Scalar group-name lists cannot satisfy per-group constraints.
    Dotted field names are supported. Exact comparisons distinguish bool/number,
    and bound field counts, group counts, nesting, string length and value size.
    """
    source = "existing completion report"
    try:
        _remaining(deadline)
        spec = unit.get("completion")
        if not spec:
            return field(None, status="not_expected_yet", source=source, observed_at=timestamp)
        if not isinstance(spec, dict) or spec.get("kind") != "json":
            raise ValueError("only existing JSON completion reports are supported")
        required = spec.get("required_fields")
        if not isinstance(required, list) or not 1 <= len(required) <= 128 or not all(
            isinstance(key, str) and 0 < len(key) <= 256 for key in required
        ):
            raise ValueError("required_fields must be explicit and nonempty")
        group_required, group_equals = _group_constraints(spec)
        source = spec.get("path")
        if not isinstance(source, str) or not os.path.isabs(source):
            raise ValueError("completion report path must be absolute")
        data, info, fingerprint = _report_bytes(source)
        doc = _json(data)
        if not isinstance(doc, dict):
            raise ValueError("completion report must be a JSON object")
        reasons, ownership = [], []
        started = runtime.get("started_at")
        if not _finite(started) or not started or info.st_mtime < started:
            ownership.append("report is not fresh relative to runtime.started_at")
        baseline = runtime.get("completion_baseline")
        if baseline is not None:
            if not isinstance(baseline, dict) or set(baseline) != set(fingerprint):
                ownership.append("unsupported launch baseline fingerprint schema")
            elif baseline["sha256"] == fingerprint["sha256"]:
                ownership.append("report content is unchanged from before launch (touch/move is not new evidence)")
        if not _attribution_matches(doc, contract, unit, runtime):
            ownership.append("report execution/unit/attempt attribution conflicts")
        for key in required:
            if not _nonempty(_lookup(doc, key)):
                reasons.append("missing/empty required field: " + key)
        if _lookup(doc, spec.get("success_field", "status")) != spec.get("success_value", "completed"):
            reasons.append("report success field does not match the mapped success value")
        if "expected_groups" in spec:
            expected = _group_names(spec["expected_groups"], "id")
            group_values = _lookup(doc, spec.get("groups_field", "groups"))
            actual = _group_names(
                group_values, spec.get("group_id_field", "id"),
            )
            if not expected or len(set(expected)) != len(expected):
                raise ValueError("expected_groups must be explicit, unique and nonempty")
            if not actual or len(set(actual)) != len(actual) or set(actual) != set(expected):
                reasons.append("report groups do not equal the nonempty expected group set")
            else:
                reasons.extend(_check_group_results(
                    group_values, expected, spec, group_required, group_equals,
                ))
        belongs = not ownership
        return field(
            {"passed": not reasons and belongs, "belongs_to_current": belongs,
             "reasons": ownership + reasons, "fingerprint": fingerprint},
            source=source, observed_at=timestamp, source_updated_at=info.st_mtime,
        )
    except FileNotFoundError:
        return field(None, status="not_expected_yet", source=source, observed_at=timestamp)
    except (ValueError, UnicodeError, RecursionError, TypeError) as error:
        return _failure(error, source, timestamp, "parse_error")
    except (OSError, PilotError) as error:
        return _failure(error, source, timestamp)


def _cursor_base(contract, unit, runtime, path):
    return {
        "execution_id": contract.get("execution_id"), "unit_id": unit.get("id"),
        "attempt": runtime.get("attempt"), "path": path, "device": None, "inode": None,
        "offset": 0, "partial": "", "partial_encoding": "base64", "generation": 0,
        "discard_partial": False,
    }


def _progress(line, doc):
    if isinstance(doc, dict):
        key = "step" if "step" in doc else "epoch" if "epoch" in doc else None
        if key and _finite(doc[key]):
            total = doc.get("total", doc.get("total_epochs" if key == "epoch" else "total_steps"))
            if total is not None and (not _finite(total) or total < doc[key]):
                raise ValueError("invalid progress total")
            phase = doc.get("phase")
            if phase is not None and (not isinstance(phase, str) or not phase or len(phase) > 128):
                raise ValueError("invalid progress phase")
            return {"step": doc[key], "total": total, "phase": phase, "work_kind": key}
        if key:
            raise ValueError("invalid progress step")
    match = TEXT_STEP.search(line)
    if not match:
        return None
    kind, step, total = match.groups()
    total_match, phase_match = TEXT_TOTAL.search(line), TEXT_PHASE.search(line)
    if total is None and total_match:
        total = total_match.group(1)
    step, total = int(step), int(total) if total is not None else None
    if total is not None and step > total:
        raise ValueError("invalid text progress total")
    return {"step": step, "total": total, "phase": phase_match.group(1) if phase_match else None,
            "work_kind": kind.lower()}


def _log_fields(contract, unit, runtime, timestamp, deadline):
    cursor = dict(runtime.get("log_cursor") or {})
    source = "current-attempt log"
    try:
        _remaining(deadline)
        path = _path(unit, runtime, "log_path")
        attempt = runtime.get("attempt")
        if not path or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            missing = field(None, status="not_expected_yet", source=source, observed_at=timestamp)
            return missing, dict(missing), dict(missing), cursor
        source = path
        base = _cursor_base(contract, unit, runtime, path)
        same_scope = all(cursor.get(k) == base[k] for k in ("execution_id", "unit_id", "attempt", "path"))
        if not same_scope:
            cursor = base
        with _open_regular(path) as stream:
            info = os.fstat(stream.fileno())
            baseline = not same_scope and runtime.get("managed") is not True
            rotated = same_scope and cursor.get("inode") is not None and (
                cursor.get("inode") != info.st_ino or cursor.get("device") != info.st_dev
            )
            offset = cursor.get("offset", 0)
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("invalid log cursor offset")
            truncated = not rotated and info.st_size < offset
            if not baseline and not rotated and not truncated and offset and cursor.get("anchor"):
                stream.seek(max(0, offset - 64))
                anchor = stream.read(min(offset, 64))
                truncated = hashlib.sha256(anchor).hexdigest() != cursor["anchor"]
            if baseline:
                offset = info.st_size
                stream.seek(max(0, offset - 1))
                last = stream.read(1) if offset else b""
                cursor.update(partial="", discard_partial=bool(last and last not in (b"\n", b"\r")))
            elif rotated or truncated or not same_scope or cursor.get("inode") is None:
                offset = 0
                cursor.update(partial="", discard_partial=False)
                cursor["generation"] = cursor.get("generation", 0) + int(rotated or truncated)
            stream.seek(offset)
            data = stream.read(min(LOG_LIMIT, max(0, info.st_size - offset)))
            offset += len(data)
            stream.seek(max(0, offset - 64))
            anchor = stream.read(min(offset, 64))
        partial = cursor.get("partial", "")
        if not isinstance(partial, str) or len(partial) > LINE_LIMIT * 2:
            raise ValueError("invalid log cursor partial")
        previous = base64.b64decode(partial, validate=True) if partial else b""
        if len(previous) > LINE_LIMIT:
            raise ValueError("log cursor partial exceeds line limit")
        combined = previous + data
        pieces = re.split(b"[\r\n]", combined)
        tail = pieces.pop()
        discard = cursor.get("discard_partial", False)
        if discard:
            if pieces:
                pieces.pop(0)
                discard = False
            else:
                tail = b""
        progress, errors, excerpts = None, [], []
        parse_errors, oversized, lines = 0, 0, 0
        for raw in pieces:
            _remaining(deadline)
            if len(raw) > LINE_LIMIT:
                oversized += 1
                continue
            line = ANSI.sub("", raw.decode("utf-8", "replace"))
            line = "".join(c for c in line if c >= " " or c == "\t").strip()
            if not line:
                continue
            doc = None
            if line.startswith("{"):
                try:
                    doc = _json(line)
                except (ValueError, RecursionError):
                    parse_errors += 1
                if isinstance(doc, dict) and not _attribution_matches(doc, contract, unit, runtime):
                    continue
            lines += 1
            excerpts.append(redact(line)[:240])
            excerpts = excerpts[-8:]
            try:
                parsed = _progress(line, doc)
                if parsed is not None:
                    progress = parsed
            except ValueError:
                parse_errors += 1
            for kind, pattern in ERROR_PATTERNS:
                if pattern.search(line) and len(errors) < 32:
                    errors.append({"kind": kind, "evidence": redact(line)[:240],
                                   "attempt": attempt, "generation": cursor.get("generation", 0)})
        if len(tail) > LINE_LIMIT:
            tail, discard = b"", True
            oversized += 1
        cursor.update(
            device=info.st_dev, inode=info.st_ino, offset=offset,
            partial=base64.b64encode(tail).decode("ascii"), partial_encoding="base64",
            discard_partial=discard, anchor=hashlib.sha256(anchor).hexdigest(),
        )
        log = field(
            {"bytes_read": len(data), "size": info.st_size, "offset": offset,
             "baselined": baseline, "rotated": rotated, "truncated": truncated,
             "lines_read": lines, "partial_bytes": len(tail), "has_more": offset < info.st_size,
             "discarded_long_lines": oversized, "excerpt": excerpts},
            source=source, observed_at=timestamp, source_updated_at=info.st_mtime,
        )
        progress_field = field(
            progress, status="ok" if progress is not None else "parse_error" if parse_errors else
            "unavailable" if lines else "not_expected_yet",
            source=source, observed_at=timestamp, parse_errors=parse_errors,
        )
        return log, progress_field, field(errors, source=source, observed_at=timestamp), cursor
    except FileNotFoundError:
        # Remember that the file was absent. Its subsequent creation is NEW
        # data, not a historical attachment log to baseline at that later EOF.
        empty = _cursor_base(contract, unit, runtime, source)
        missing = field(None, status="not_expected_yet", source=source, observed_at=timestamp)
        return missing, dict(missing), dict(missing), empty
    except (ValueError, UnicodeError) as error:
        failed = _failure(error, source, timestamp, "parse_error")
        return failed, dict(failed), dict(failed), dict(runtime.get("log_cursor") or {})
    except (OSError, PilotError) as error:
        failed = _failure(error, source, timestamp)
        return failed, dict(failed), dict(failed), dict(runtime.get("log_cursor") or {})


def _cpu_set(value):
    intervals = []
    for part in value.strip().split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError("invalid CPU set")
        start, end = int(match[1]), int(match[2] or match[1])
        if end < start:
            raise ValueError("invalid CPU interval")
        intervals.append((start, end))
    total, previous = 0, -1
    for start, end in sorted(intervals):
        if start <= previous:
            raise ValueError("overlapping CPU intervals")
        total += end - start + 1
        previous = end
    return total


def _mount_unescape(text):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), text)


def _cgroup_locations(pid):
    memberships = []
    for line in _read_text(PROC / str(pid) / "cgroup").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[2].startswith("/") or ".." in parts[2].split("/"):
            raise ValueError("unsupported cgroup membership")
        memberships.append((set(parts[1].split(",")), parts[2], parts[0] == "0" and not parts[1]))
    locations = []
    for line in _read_text(PROC / "self/mountinfo").splitlines():
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        if not separator or len(left) < 5 or len(right) < 3 or right[0] not in {"cgroup", "cgroup2"}:
            continue
        root, mount = _mount_unescape(left[3]), Path(_mount_unescape(left[4]))
        controllers = set(right[2].split(","))
        for names, membership, unified in memberships:
            if unified != (right[0] == "cgroup2"):
                continue
            kinds = {"cpu", "memory", "cpuset"} if unified else names & controllers & {"cpu", "memory", "cpuset"}
            if not kinds:
                continue
            if root == "/":
                relative = membership.lstrip("/")
            elif membership == root or membership.startswith(root.rstrip("/") + "/"):
                relative = membership[len(root):].lstrip("/")
            elif unified and membership == "/":
                relative = ""  # cgroup namespace rooted at this mounted group
            else:
                continue
            locations.append((mount / relative, mount, unified, kinds))
    wanted = set()
    for names, _, unified in memberships:
        wanted.update({"cpu", "memory", "cpuset"} if unified else names & {"cpu", "memory", "cpuset"})
    covered = set().union(*(kinds for _, _, _, kinds in locations))
    if not wanted <= covered:
        raise _ObservationError("cgroup budgets are not visible from this mount namespace")
    return locations


def _optional_text(path):
    try:
        return _read_text(path, 8192).strip()
    except FileNotFoundError:
        return None


def _cgroup_budget(pid, deadline):
    cpu, ram = [], []
    errors = {}
    try:
        locations = _cgroup_locations(pid)
    except (OSError, ValueError, PilotError) as error:
        return cpu, ram, {"cpu": str(error), "ram": str(error)}
    for directory, mount, unified, kinds in locations:
        for _ in range(64):
            try:
                _remaining(deadline)
                if "cpu" in kinds:
                    quota = _optional_text(directory / ("cpu.max" if unified else "cpu.cfs_quota_us"))
                    if quota is not None:
                        if unified:
                            amount, period = quota.split()
                        else:
                            amount, period = quota, _optional_text(directory / "cpu.cfs_period_us")
                        if amount not in {"max", "-1"}:
                            amount, period = int(amount), int(period)
                            if amount < 0 or period <= 0:
                                raise ValueError("invalid cgroup CPU quota")
                            cpu.append(amount / float(period))
                if "cpuset" in kinds:
                    cpus = _optional_text(directory / ("cpuset.cpus.effective" if unified else "cpuset.cpus"))
                    if cpus:
                        cpu.append(_cpu_set(cpus))
            except (OSError, ValueError, TypeError, PilotError) as error:
                errors["cpu"] = str(error)
            try:
                _remaining(deadline)
                if "memory" in kinds:
                    maximum = _optional_text(directory / ("memory.max" if unified else "memory.limit_in_bytes"))
                    if maximum is not None and maximum != "max":
                        used = _optional_text(directory / ("memory.current" if unified else "memory.usage_in_bytes"))
                        if int(maximum) < 0 or used is None or int(used) < 0:
                            raise ValueError("invalid/unavailable cgroup memory budget")
                        ram.append(max(0, int(maximum) - int(used)) / (1024.0 ** 2))
            except (OSError, ValueError, TypeError, PilotError) as error:
                errors["ram"] = str(error)
            if directory == mount:
                break
            if mount not in directory.parents:
                errors.update(cpu="unresolved cgroup hierarchy", ram="unresolved cgroup hierarchy")
                break
            directory = directory.parent
        else:
            errors.update(cpu="cgroup depth exceeds limit", ram="cgroup depth exceeds limit")
    return cpu, ram, errors


def _ram_available(deadline):
    _remaining(deadline)
    if sys.platform.startswith("linux"):
        match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", _read_text(PROC / "meminfo"), re.M)
        if not match:
            raise ValueError("MemAvailable is unavailable")
        return int(match[1]) / 1024.0
    if sys.platform == "darwin":
        text = _command(("/usr/bin/vm_stat",), deadline, output_limit=16384)
        page = re.search(r"page size of (\d+) bytes", text)
        values = dict(re.findall(r"^(Pages (?:free|inactive|speculative)):\s+(\d+)\.", text, re.M))
        if not page or "Pages free" not in values:
            raise ValueError("invalid vm_stat memory counters")
        return sum(int(v) for v in values.values()) * int(page[1]) / (1024.0 ** 2)
    raise _ObservationError("RAM observation is implemented only on Linux/macOS")


def _disk_available(contract, unit, deadline):
    paths = unit.get("write_paths") or [unit.get("cwd", contract["project"])]
    measurements = []
    for path in paths:
        _remaining(deadline)
        current = Path(path)
        for _ in range(64):
            try:
                info = os.statvfs(str(current))
                break
            except FileNotFoundError:
                if current == current.parent:
                    raise
                current = current.parent
        else:
            raise _ObservationError("output path depth exceeds limit")
        measurements.append({
            "path": str(path), "observed_path": str(current),
            "free_mb": info.f_bavail * info.f_frsize / (1024.0 ** 2),
            "inodes_available": info.f_favail,
        })
    return min(item["free_mb"] for item in measurements), measurements


def _visible_rows(rows, visible, label):
    if visible is None or (label == "NVIDIA_VISIBLE_DEVICES" and visible == "all"):
        return rows
    if visible.strip() in {"", "-1", "none", "void"}:
        return []
    selected = []
    for token in visible.split(","):
        token = token.strip()
        if token.startswith("MIG-"):
            raise _ObservationError("MIG telemetry/mapping is not implemented")
        matches = [
            row for row in rows
            if str(row["index"]) == token or row["uuid"] == token
            or (token.startswith("GPU-") and row["uuid"].startswith(token))
        ]
        if len(matches) != 1 or matches[0] in selected:
            raise _ObservationError("unresolved/ambiguous " + label + " device mapping")
        selected.append(matches[0])
    return selected


def _gpu_number(text):
    if text.strip().lower() in {"n/a", "[n/a]", "not supported", "[not supported]"}:
        return None
    value = float(text)
    if not _finite(value):
        raise ValueError("invalid GPU telemetry number")
    return value


def _gpu_resources(contract, unit, runtime, process_field, timestamp, deadline):
    """Return (GPU field, actual PID UUIDs or None, actual-mapping detail).

    Device assignment/visibility is NOT evidence the worker used a device.
    Only positive PID->UUID evidence is reported. No matching row remains None:
    MPS/MIG/PID namespaces may hide a worker, so absence is not proof of CPU-only
    execution. Never substitute the contract's permitted devices.
    """
    source = "nvidia-smi fixed GPU/compute-app queries"
    pvalue = process_field.get("value") or {}
    pid = (pvalue.get("identity") or {}).get("pid") if (
        process_field.get("status") == "ok" and pvalue.get("identity_matches") is True
    ) else None
    approved = contract.get("devices", [])
    if not approved and pid is None:
        return field([], source="contract (no GPUs requested)", observed_at=timestamp), None, "not sampled"
    try:
        text = _command(GPU_QUERY, deadline)
        rows = []
        for values in csv.reader(io.StringIO(text), skipinitialspace=True):
            if not values:
                continue
            if len(values) != 8 or not values[0].strip().startswith("GPU-"):
                raise ValueError("unexpected GPU query schema (MIG is unsupported)")
            uuid, index, name, free, total, mode, utilization, temperature = (v.strip() for v in values)
            row = {
                "uuid": uuid, "index": int(index), "name": name,
                "memory_free_mb": _gpu_number(free), "memory_total_mb": _gpu_number(total),
                "compute_mode": mode, "utilization_percent": _gpu_number(utilization),
                "temperature_c": _gpu_number(temperature), "process_count": None,
                "target_memory_mb": None,
            }
            if row["index"] < 0 or (
                row["memory_free_mb"] is not None and row["memory_total_mb"] is not None
                and row["memory_free_mb"] > row["memory_total_mb"]
            ):
                raise ValueError("inconsistent GPU counters")
            rows.append(row)
        if len({r["uuid"] for r in rows}) != len(rows) or len({r["index"] for r in rows}) != len(rows):
            raise ValueError("duplicate GPU identity/index")
        mapping_detail, actual = "compute-app sample unavailable", None
        try:
            apps = _command(GPU_APPS_QUERY, deadline)
            known = {row["uuid"] for row in rows}
            processes = {uuid: {} for uuid in known}
            for values in csv.reader(io.StringIO(apps), skipinitialspace=True):
                if not values:
                    continue
                if len(values) != 3 or values[0].strip() not in known:
                    raise ValueError("unresolved compute-app GPU UUID (MIG/PID mapping unsupported)")
                uuid, app_pid, memory = (v.strip() for v in values)
                app_pid = int(app_pid)
                if app_pid <= 0:
                    raise ValueError("invalid compute-app PID")
                processes[uuid][app_pid] = _gpu_number(memory)
            for row in rows:
                row["process_count"] = len(processes[row["uuid"]])
                row["target_memory_mb"] = processes[row["uuid"]].get(pid)
            if pid is not None and rows:
                # Do not attach a probe taken across PID death/reuse to a worker.
                if _identities_equal(runtime.get("identity"), _process_identity(pid, deadline)):
                    observed = sorted(uuid for uuid, apps_by_pid in processes.items() if pid in apps_by_pid)
                    if observed:
                        actual = observed
                        mapping_detail = "positive observed live target PID -> GPU UUID mapping"
                    else:
                        mapping_detail = "no positive target PID mapping; CPU-only use is not established"
        except (OSError, ValueError, PilotError) as error:
            mapping_detail = redact(str(error))[:300]
        visible = rows
        visible = _visible_rows(visible, os.environ.get("NVIDIA_VISIBLE_DEVICES"), "NVIDIA_VISIBLE_DEVICES")
        effective_cuda = unit.get("env", {}).get("CUDA_VISIBLE_DEVICES", os.environ.get("CUDA_VISIBLE_DEVICES"))
        if pid is not None and sys.platform.startswith("linux"):
            try:
                environment = _read_bytes(PROC / str(pid) / "environ")
                for key in ("NVIDIA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
                    prefix = key.encode("ascii") + b"="
                    values = [part[len(prefix):].decode("utf-8") for part in environment.split(b"\0")
                              if part.startswith(prefix)]
                    if values:
                        if key == "CUDA_VISIBLE_DEVICES":
                            effective_cuda = values[-1]
                        else:
                            visible = _visible_rows(visible, values[-1], key)
            except (OSError, ValueError, PilotError) as error:
                return _failure(error, "target GPU visibility", timestamp), actual, mapping_detail
        # Use the actual worker environment, or the reviewed override/inherited
        # launch environment. Never expand NVIDIA/container allocation.
        visible = _visible_rows(visible, effective_cuda, "CUDA_VISIBLE_DEVICES")
        if approved:
            visible = [row for row in visible if row["uuid"] in approved]
        return field(visible, source=source, observed_at=timestamp,
                     process_mapping_detail=mapping_detail), actual, mapping_detail
    except (ValueError, UnicodeError, csv.Error) as error:
        return _failure(error, source, timestamp, "parse_error"), None, str(error)
    except (OSError, PilotError) as error:
        return _failure(error, source, timestamp), None, str(error)


def _collect_resources(contract, unit, runtime, process_field, timestamp, deadline):
    result = {}
    pvalue = process_field.get("value") or {}
    pid = (pvalue.get("identity") or {}).get("pid") if pvalue.get("identity_matches") else None
    pid = pid or "self"
    cpu_limits, ram_limits, errors = [], [], {}
    namespace_error = None
    if sys.platform.startswith("linux"):
        if pid != "self":
            try:
                if os.stat(PROC / str(pid) / "ns/mnt").st_ino != os.stat(PROC / "self/ns/mnt").st_ino:
                    raise _ObservationError("target mount namespace differs; filesystem/cgroup mapping unsupported")
            except (OSError, PilotError) as error:
                namespace_error = str(error)
        if namespace_error:
            errors = {"cpu": namespace_error, "ram": namespace_error}
        else:
            cpu_limits, ram_limits, errors = _cgroup_budget(pid, deadline)
    for key in ("cpu_threads", "ram_available_mb"):
        source = "CPU count/affinity/cgroup" if key == "cpu_threads" else "available RAM/cgroup"
        try:
            _remaining(deadline)
            if key == "cpu_threads":
                if "cpu" in errors:
                    raise _ObservationError(errors["cpu"])
                count = os.cpu_count()
                if count is None:
                    raise _ObservationError("CPU count unavailable")
                if hasattr(os, "sched_getaffinity"):
                    count = min(count, len(os.sched_getaffinity(0 if pid == "self" else pid)))
                amount = min([float(count)] + cpu_limits)
            else:
                if "ram" in errors:
                    raise _ObservationError(errors["ram"])
                amount = min([_ram_available(deadline)] + ram_limits)
            result[key] = field(amount, source=source, observed_at=timestamp)
        except (OSError, ValueError, PilotError) as error:
            result[key] = _failure(error, source, timestamp)
    try:
        if namespace_error:
            raise _ObservationError(namespace_error)
        amount, measurements = _disk_available(contract, unit, deadline)
        result["disk_free_mb"] = field(amount, source="output filesystem statvfs", observed_at=timestamp,
                                       measurements=measurements)
    except (OSError, ValueError, PilotError) as error:
        result["disk_free_mb"] = _failure(error, "output filesystem statvfs", timestamp)
    try:
        _remaining(deadline)
        if namespace_error:
            raise _ObservationError(namespace_error)
        info = os.statvfs("/dev/shm")
        result["shm_free_mb"] = field(
            info.f_bavail * info.f_frsize / (1024.0 ** 2), source="/dev/shm statvfs", observed_at=timestamp,
        )
    except (OSError, PilotError) as error:
        result["shm_free_mb"] = _failure(error, "/dev/shm statvfs", timestamp)
    result["gpus"], actual, detail = _gpu_resources(
        contract, unit, runtime, process_field, timestamp, deadline,
    )
    if process_field.get("status") == "ok" and pvalue.get("identity_matches"):
        pvalue["actual_gpu_uuids"] = actual
        pvalue["gpu_mapping"] = field(
            actual, status="ok" if actual is not None else "unavailable",
            source="nvidia-smi compute-app target PID mapping", observed_at=timestamp,
            detail=detail,
        )
    return field(result, source="independent read-only resource probes", observed_at=timestamp)


def collect(contract, unit, runtime, state_dir):
    """Observe once without writing state, changing training, or launching work.

    state_dir is accepted for the shared interface but is never created/read.
    One monotonic collection_timeout_seconds budget is shared by every probe,
    including process identity, GPU queries and identity revalidation. Regular
    file reads are size-bounded; OS filesystem calls cannot interrupt a hung
    filesystem/kernel. Unsupported transports never fall back to local paths.
    """
    timestamp = now()
    budget = contract.get("collection_timeout_seconds", 15)
    if not _finite(budget, 0.001):
        raise PilotError("collection_timeout_seconds must be positive and finite")
    deadline = time.monotonic() + min(budget, 120)
    kind = contract.get("target", {}).get("kind", "local")
    if kind != "local":
        detail = (
            "SSH observation is unsupported; no connection/install/launch attempted"
            if kind == "ssh" else "fixture/unsupported target: supply snapshots to monitor, not live collectors"
        )
        fields = {
            key: _failure(detail, kind, timestamp)
            for key in ("process", "exit", "progress", "errors", "completion", "log")
        }
        fields["resources"] = field(
            {key: _failure(detail, kind, timestamp) for key in RESOURCE_NAMES},
            status="unavailable", source=kind, observed_at=timestamp, detail=detail,
        )
        return {"observed_at": timestamp, "reachable": False, "access": "unsupported",
                "fields": fields, "log_cursor": dict(runtime.get("log_cursor") or {})}
    fields = {"process": _process_field(runtime, timestamp, deadline)}
    fields["exit"] = _exit_field(contract, unit, runtime, timestamp, deadline)
    fields["log"], fields["progress"], fields["errors"], cursor = _log_fields(
        contract, unit, runtime, timestamp, deadline,
    )
    fields["completion"] = _completion_field(contract, unit, runtime, timestamp, deadline)
    fields["resources"] = _collect_resources(contract, unit, runtime, fields["process"], timestamp, deadline)
    return {"observed_at": timestamp, "reachable": True, "fields": fields, "log_cursor": cursor}
