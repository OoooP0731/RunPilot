"""Private state, contracts, bounded I/O and process-safe locks for RunPilot."""

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

VERSION = "0.1.1"
SCHEMA_VERSION = 1
TERMINAL = {"completed", "incomplete", "failed", "cancelled", "finished_unverified"}
FIELD_STATES = {"ok", "unavailable", "not_expected_yet", "parse_error"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SECRET_FLAG = re.compile(
    r"(?i)(?:^|[-_])(password|passwd|secret|token|api[-_]?key|private[-_]?key)(?:$|=)"
)
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
SECRET_ENV = re.compile(
    r"(?i)(?:^|_)(password|passwd|secret|token|api_key|private_key|"
    r"access_key|credential|auth_key)(?:$|_)"
)


class PilotError(Exception):
    """A user-facing refusal or invalid input, not a task failure."""


class Busy(PilotError):
    pass


def now():
    return time.time()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def read_json(path, limit=1024 * 1024):
    with open(path, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PilotError("JSON exceeds the %s byte limit" % limit)
    return json.loads(data)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".%s.tmp" % os.getpid())
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def redact(value):
    """Defence in depth; callers should select evidence rather than dump logs."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SECRET_FLAG.search("-" + str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(
            r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)[^\s,;\"']+",
            r"\1[REDACTED]", value,
        )
        value = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", value)
        value = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[REDACTED PRIVATE KEY]", value, flags=re.S,
        )
    return value


def default_state_dir():
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base).expanduser() / "run-pilot" if base else Path.home() / ".local/state/run-pilot"


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise PilotError("%s must be 1-96 simple letters/digits/._- (no paths)" % label)
    return value


def absolute(value, label):
    if not isinstance(value, str) or not Path(value).is_absolute() or "\x00" in value:
        raise PilotError("%s must be an absolute path" % label)
    return value


def number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotError("%s must be numeric" % label)
    if not math.isfinite(value) or value < minimum:
        raise PilotError("%s must be finite and >= %s" % (label, minimum))
    return value


def validate_env(env):
    """Allow reviewed Python-task overrides, never persist obvious credentials."""
    if not isinstance(env, dict) or len(env) > 64:
        raise PilotError("unit.env must be an object with at most 64 variables")
    if not all(
        isinstance(key, str) and ENV_NAME.fullmatch(key) and not SECRET_ENV.search(key)
        and isinstance(value, str) and "\x00" not in value and len(value) <= 4096
        for key, value in env.items()
    ):
        raise PilotError("unit.env needs simple names and bounded string values; no secret variables")
    return env


def validate_contract(doc):
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        raise PilotError("Unsupported contract schema_version; expected 1")
    identifier(doc.get("execution_id"), "execution_id (private RunPilot handle)")
    absolute(doc.get("project"), "project")
    target = doc.get("target", {"kind": "local"})
    if not isinstance(target, dict):
        raise PilotError("target must be an object")
    if target.get("kind") not in {"local", "ssh", "fixture"}:
        raise PilotError("target.kind must be local, ssh or fixture")
    if target.get("kind") == "ssh":
        host = target.get("host", "")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,200}", host):
            raise PilotError("Use an existing simple SSH host alias (not options or a URL)")
    actions = doc.get("allowed_actions", ["observe"])
    if (not isinstance(actions, list) or not all(isinstance(a, str) for a in actions)
            or not set(actions) <= {"observe", "launch", "schedule", "retry"}):
        raise PilotError("Unknown action; stop/kill and configuration edits are not implemented")
    if any(a != "observe" for a in actions) and doc.get("scope_approved") is not True:
        raise PilotError("Launch/schedule/retry require scope_approved=true from explicit user approval")
    maximum = doc.get("max_concurrency", 1)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 128:
        raise PilotError("max_concurrency must be an integer between 1 and 128")
    number(doc.get("interval_seconds", 1200), "interval_seconds", 1)
    number(doc.get("collection_timeout_seconds", 15), "collection_timeout_seconds", 0.1)
    if doc.get("collection_timeout_seconds", 15) > 120:
        raise PilotError("collection_timeout_seconds must be <= 120")
    number(doc.get("freshness_seconds", 2400), "freshness_seconds", 1)
    if not isinstance(doc.get("reserves", {}), dict):
        raise PilotError("reserves must be an object")
    for key, value in doc.get("reserves", {}).items():
        if key not in {"gpu_mb", "ram_mb", "disk_mb", "shm_mb", "cpu_threads"}:
            raise PilotError("Unknown reserve: " + key)
        number(value, "reserves." + key)
    devices = doc.get("devices", [])
    if not isinstance(devices, list) or not all(isinstance(d, str) and d for d in devices):
        raise PilotError("devices must be an explicit list of stable GPU identifiers")
    if len(set(devices)) != len(devices):
        raise PilotError("Duplicate device identifier")
    if "resource_lock" in doc:
        absolute(doc["resource_lock"], "resource_lock")
    units = doc.get("units")
    if not isinstance(units, list) or not units or len(units) > 256:
        raise PilotError("units must be a nonempty list of at most 256 existing units")
    names = []
    for unit in units:
        if not isinstance(unit, dict):
            raise PilotError("Each unit must be an object")
        identifier(unit.get("id"), "unit.id")
        names.append(unit["id"])
        argv = unit.get("argv", [])
        if not isinstance(argv, list) or not all(isinstance(v, str) and "\x00" not in v for v in argv):
            raise PilotError("unit.argv must be a string array, never a generated shell expression")
        if any(SECRET_FLAG.search(arg) for arg in argv):
            raise PilotError("Secrets cannot be supplied in argv/contracts; use the prepared environment")
        if "cwd" in unit:
            absolute(unit["cwd"], "unit.cwd")
        for key in ("log_path", "exit_path"):
            if key in unit:
                absolute(unit[key], "unit." + key)
        if not isinstance(unit.get("write_paths", []), list):
            raise PilotError("write_paths must be an array")
        for path in unit.get("write_paths", []):
            absolute(path, "write_paths entry")
        if not isinstance(unit.get("depends_on", []), list):
            raise PilotError("unit.depends_on must be an array")
        if not all(isinstance(v, str) for v in unit.get("depends_on", [])):
            raise PilotError("unit.depends_on must contain unit identifiers")
        validate_env(unit.get("env", {}))
        if not isinstance(unit.get("resources", {}), dict):
            raise PilotError("unit.resources must be an object")
        for resource, value in unit.get("resources", {}).items():
            if resource not in {"gpu_mb", "ram_mb", "disk_mb", "shm_mb", "cpu_threads", "future_gpu_mb"}:
                raise PilotError("Unknown unit resource: " + resource)
            if resource == "gpu_mb":
                if not isinstance(value, dict) or not set(value) <= set(devices):
                    raise PilotError("gpu_mb must map approved device identifiers to MB estimates")
                for amount in value.values():
                    number(amount, "gpu_mb estimate")
            else:
                number(value, resource)
        unit_devices = unit.get("devices", [])
        if (not isinstance(unit_devices, list) or not all(isinstance(d, str) for d in unit_devices)
                or not set(unit_devices) <= set(devices)):
            raise PilotError("unit.devices must be a subset of approved devices")
        completion = unit.get("completion")
        if completion is not None:
            if not isinstance(completion, dict):
                raise PilotError("completion must be an object")
            if completion.get("kind") == "exit_code":
                if set(completion) != {"kind"}:
                    raise PilotError("exit_code completion accepts only kind; it is for supervised launches")
                continue
            if completion.get("kind") != "json":
                raise PilotError("completion.kind must be exit_code or json")
            absolute(completion.get("path"), "completion.path")
            required = completion.get("required_fields", [])
            if not isinstance(required, list) or not required or not all(isinstance(v, str) and v for v in required):
                raise PilotError("completion.required_fields must be explicit and nonempty")
            if "expected_groups" in completion:
                groups = completion["expected_groups"]
                if (not isinstance(groups, list) or not groups
                        or not all(isinstance(g, (str, int)) and not isinstance(g, bool) for g in groups)
                        or len(set(map(str, groups))) != len(groups)):
                    raise PilotError("expected_groups must be explicit, nonempty and unique")
            group_required = completion.get("group_required_fields", [])
            if not isinstance(group_required, list) or not all(isinstance(f, str) and f for f in group_required):
                raise PilotError("group_required_fields must list existing nonempty report field names")
            group_equals = completion.get("group_equals", {})
            if not isinstance(group_equals, dict) or not all(isinstance(k, str) and k for k in group_equals):
                raise PilotError("group_equals must map existing report fields to exact required values")
            if (group_required or group_equals) and "expected_groups" not in completion:
                raise PilotError("Per-group constraints need an explicit nonempty expected_groups contract")
    if len(set(names)) != len(names):
        raise PilotError("Duplicate unit id")
    graph = {unit["id"]: unit.get("depends_on", []) for unit in units}
    for unit, deps in graph.items():
        if not set(deps) <= set(names) or unit in deps:
            raise PilotError("Dependencies must refer to other existing units")
    visited, visiting = set(), set()

    def visit(unit):
        if unit in visiting:
            raise PilotError("Unit dependency cycle")
        if unit in visited:
            return
        visiting.add(unit)
        for dep in graph[unit]:
            visit(dep)
        visiting.remove(unit)
        visited.add(unit)

    for unit in names:
        visit(unit)
    notification = doc.get("notification", {"kind": "none"})
    if not isinstance(notification, dict) or not isinstance(doc.get("analysis", {}), dict):
        raise PilotError("notification and analysis must be objects")
    if notification.get("kind") not in {"none", "file", "desktop"}:
        raise PilotError("Notifications support none, file or macOS desktop only")
    if notification.get("kind") == "file":
        absolute(notification.get("path"), "notification.path")
    for section in ("notification", "analysis"):
        spec = doc.get(section, {})
        tries = spec.get("max_attempts", 3)
        if isinstance(tries, bool) or not isinstance(tries, int) or not 1 <= tries <= 10:
            raise PilotError(section + ".max_attempts must be an integer from 1 to 10")
    analysis = doc.get("analysis", {})
    if analysis.get("enabled"):
        if analysis.get("kind") != "spool":
            raise PilotError("Optional analysis supports a local, non-executing spool only")
        absolute(analysis.get("path"), "analysis.path")
        number(analysis.get("max_requests", 3), "analysis.max_requests")
    throughput = doc.get("throughput", {})
    if not isinstance(throughput, dict):
        raise PilotError("throughput must be an object")
    if throughput:
        number(throughput.get("baseline_total_rate"), "throughput.baseline_total_rate", 0.000001)
        number(throughput.get("min_gain_fraction", 0), "throughput.min_gain_fraction")
        if not isinstance(throughput.get("phase"), str) or not throughput["phase"]:
            raise PilotError("throughput.phase must name a comparable existing phase")
        window = throughput.get("window_samples", 3)
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 100:
            raise PilotError("throughput.window_samples must be an integer from 1 to 100")
    return doc


def initial_runtime():
    return {
        "execution_state": "ready", "access": "reachable", "monitoring": "active",
        "attempt": 0, "identity": None, "started_at": None, "finished_at": None,
        "record_written_at": now(), "snapshot": {}, "last_good": {},
        "log_cursor": {}, "retries_used": 0, "risk_active": [],
    }


@contextlib.contextmanager
def file_lock(path, blocking=False):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise Busy("Another RunPilot operation holds " + str(path)) from error
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, state_dir=None):
        self.root = Path(state_dir or default_state_dir()).expanduser().absolute()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise PilotError("The private state directory must not be a symlink")
        if self.root.stat().st_mode & 0o077:
            raise PilotError("State directory must be private (chmod 700): " + str(self.root))
        database = self.root / "state.sqlite3"
        if database.is_symlink():
            raise PilotError("State database must not be a symlink")
        fd = os.open(str(database), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(str(database), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, SCHEMA_VERSION}:
            raise PilotError("Unknown state version; retain state and use a compatible RunPilot version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS contracts (
                execution TEXT PRIMARY KEY, doc TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS units (
                execution TEXT NOT NULL, unit TEXT NOT NULL, doc TEXT NOT NULL,
                PRIMARY KEY(execution, unit));
            CREATE TABLE IF NOT EXISTS events (
                key TEXT PRIMARY KEY, execution TEXT NOT NULL, unit TEXT NOT NULL,
                kind TEXT NOT NULL, doc TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                key TEXT PRIMARY KEY, event_key TEXT NOT NULL, channel TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                claimed_at REAL, next_at REAL NOT NULL DEFAULT 0, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
                key TEXT PRIMARY KEY, execution TEXT NOT NULL, unit TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY, execution TEXT NOT NULL, started_at REAL NOT NULL,
                ended_at REAL NOT NULL, doc TEXT NOT NULL);
        """)
        self.db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def lock(self, execution):
        return file_lock(self.root / "locks" / (identifier(execution) + ".lock"))

    def save_contract(self, doc):
        validate_contract(doc)
        execution = doc["execution_id"]
        old = self.db.execute("SELECT doc FROM contracts WHERE execution=?", (execution,)).fetchone()
        if old:
            if canonical(doc) != old["doc"]:
                raise PilotError("An execution contract is immutable; use a new private handle for a new contract")
            return
        with self.transaction():
            self.db.execute("INSERT INTO contracts VALUES(?,?,?)", (execution, canonical(doc), now()))
            for unit in doc["units"]:
                self.save_unit(execution, unit["id"], initial_runtime())

    def contract(self, execution):
        row = self.db.execute("SELECT doc FROM contracts WHERE execution=?", (execution,)).fetchone()
        if not row:
            raise PilotError("Unknown execution: " + str(execution))
        return json.loads(row["doc"])

    def unit(self, execution, unit):
        row = self.db.execute("SELECT doc FROM units WHERE execution=? AND unit=?", (execution, unit)).fetchone()
        if not row:
            raise PilotError("Unknown unit: " + str(unit))
        return json.loads(row["doc"])

    def units(self, execution):
        return {
            row["unit"]: json.loads(row["doc"])
            for row in self.db.execute("SELECT unit,doc FROM units WHERE execution=?", (execution,))
        }

    def save_unit(self, execution, unit, doc):
        self.db.execute(
            "INSERT INTO units VALUES(?,?,?) ON CONFLICT(execution,unit) DO UPDATE SET doc=excluded.doc",
            (execution, unit, canonical(doc)),
        )

    def get_action(self, key):
        row = self.db.execute("SELECT * FROM actions WHERE key=?", (key,)).fetchone()
        return dict(row, doc=json.loads(row["doc"])) if row else None

    def action(self, key, execution, unit, kind, status, doc):
        self.db.execute(
            "INSERT INTO actions VALUES(?,?,?,?,?,?) ON CONFLICT(key) "
            "DO UPDATE SET status=excluded.status, doc=excluded.doc",
            (key, execution, unit, kind, status, canonical(redact(doc))),
        )

    def event(self, execution, unit, kind, payload, key, notify=True, analyze=False):
        """Call in the same transaction as the state transition."""
        doc = redact(dict(payload, observed_at=payload.get("observed_at", now())))
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
            (key, execution, unit, kind, canonical(doc), now()),
        )
        if not cursor.rowcount:
            return False
        for channel, enabled in (("notification", notify), ("analysis", analyze)):
            if enabled:
                self.db.execute(
                    "INSERT INTO outbox(key,event_key,channel,status,doc) VALUES(?,?,?,?,?)",
                    (key + ":" + channel, key, channel, "pending", "{}"),
                )
        return True

    def event_rows(self, execution, limit=1024):
        rows = [
            dict(row, doc=json.loads(row["doc"]))
            for row in self.db.execute(
                "SELECT * FROM events WHERE execution=? ORDER BY created_at DESC,key DESC LIMIT ?",
                (execution, limit),
            )
        ]
        return list(reversed(rows))

    def outbox_rows(self, execution, pending_only=False, limit=1024):
        where = " AND o.status IN ('pending','claimed')" if pending_only else ""
        return [
            dict(row, doc=json.loads(row["doc"]))
            for row in self.db.execute(
                "SELECT o.* FROM outbox o JOIN events e ON o.event_key=e.key "
                "WHERE e.execution=?" + where
                + " ORDER BY CASE WHEN o.status IN ('pending','claimed') THEN 0 ELSE 1 END,"
                " o.next_at ASC,e.created_at ASC,o.key LIMIT ?", (execution, limit)
            )
        ]

    def has_pending(self, execution):
        return self.db.execute(
            "SELECT 1 FROM outbox o JOIN events e ON o.event_key=e.key "
            "WHERE e.execution=? AND o.status IN ('pending','claimed') LIMIT 1", (execution,),
        ).fetchone() is not None


def field(value=None, status="ok", source="unknown", observed_at=None, **extra):
    if status not in FIELD_STATES:
        raise PilotError("Invalid observation field status")
    return dict(value=value, status=status, source=source,
                observed_at=now() if observed_at is None else observed_at, **extra)


def emit(value):
    print(json.dumps(redact(value), ensure_ascii=False, sort_keys=True, indent=2))
