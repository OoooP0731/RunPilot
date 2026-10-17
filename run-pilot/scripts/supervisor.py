#!/usr/bin/env python3
"""One local attempt: execute reviewed argv once, wait, atomically record its exit.

No retry, signal forwarding, training cancellation, shell expansion or scheduler.
The request/manifest are private control records, never project-provided commands.
"""

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys

from core import (
    Busy, PilotError, absolute, file_lock, identifier, now, read_json, write_json,
)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise PilotError("Control directory must be an owned private directory: " + str(path))
    return path


def private_file(path, exclusive=False):
    """Open control/log files without following a final symlink."""
    flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_EXCL if exclusive else os.O_APPEND
    fd = os.open(str(path), flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        os.close(fd)
        raise PilotError("Control file must be an owned private file: " + str(path))
    return os.fdopen(fd, "ab", buffering=0)


def _request(path):
    path = Path(absolute(str(path), "request path"))
    private_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise PilotError("Supervisor request must be an owned private regular file")
    request = read_json(path)
    if request.get("schema_version") != 1:
        raise PilotError("Unknown supervisor request schema")
    identifier(request.get("execution_id"), "execution_id")
    identifier(request.get("unit_id"), "unit_id")
    identifier(request.get("claim_id"), "claim_id")
    attempt = request.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise PilotError("Invalid attempt")
    argv = request.get("argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and "\x00" not in item for item in argv
    ):
        raise PilotError("Supervisor requires unchanged structured argv")
    absolute(request.get("cwd"), "cwd")
    env = request.get("env", {})
    if not isinstance(env, dict) or not set(env) <= {
        "CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
    } or not all(isinstance(v, str) and "\x00" not in v for v in env.values()):
        raise PilotError("Invalid reviewed environment overrides")
    for key, filename in (("manifest_path", "manifest.json"), ("exit_path", "exit.json"),
                          ("log_path", "output.log")):
        if request.get(key) != str(path.parent / filename):
            raise PilotError("Attempt evidence must stay in its dedicated private directory")
    return request, path.parent


def supervise(request_path):
    request, directory = _request(request_path)
    from collectors import process_identity
    with file_lock(directory / "supervisor.lock"):
        manifest_path, exit_path = Path(request["manifest_path"]), Path(request["exit_path"])
        # Even a stale "spawning" marker is an ambiguous prior spawn, NOT a lease
        # permitting replay. The parent will reconcile; v0.1 never retries it.
        if manifest_path.exists() or exit_path.exists():
            return 0
        record = {
            "schema_version": 1,
            "execution_id": request["execution_id"], "unit_id": request["unit_id"],
            "attempt": request["attempt"], "claim_id": request["claim_id"],
            "argv": request["argv"], "cwd": request["cwd"], "env": request.get("env", {}),
            "assigned_devices": request.get("assigned_devices", []),
            "identity": None, "supervisor_identity": process_identity(os.getpid()),
            "phase": "spawning", "child_started": None, "started_at": None,
            "record_written_at": now(),
        }
        write_json(manifest_path, record)
        child = None
        try:
            with private_file(request["log_path"]) as log:
                started_at = now()
                child = subprocess.Popen(
                    request["argv"], cwd=request["cwd"],
                    env=dict(os.environ, **request.get("env", {})),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    shell=False, close_fds=True,
                )
                record.update(phase="running", child_started=True, started_at=started_at)
                try:
                    record["identity"] = process_identity(child.pid)
                except (OSError, ValueError, PilotError):
                    # Losing identity coverage must not stop waiting for the
                    # child or turn the supervisor's exit into a training exit.
                    record["identity"] = None
                try:
                    write_json(manifest_path, dict(record, record_written_at=now()))
                except OSError:
                    pass  # Still wait and try to preserve the authoritative exit.
                code = child.wait()
                finished_at = now()
        except (OSError, ValueError, PilotError) as error:
            if child is not None:
                # A post-spawn bookkeeping error cannot justify abandoning or
                # restarting the child. wait() is the only exit-code authority.
                code = child.wait()
                finished_at = now()
            else:
                write_json(manifest_path, dict(
                    record, phase="spawn_failed", child_started=False,
                    error=str(error)[:500], record_written_at=now(),
                ))
                return 0
        result = dict(
            record, phase="exited", child_started=True, exit_code=code,
            finished_at=finished_at, record_written_at=now(),
        )
        write_json(exit_path, result)
        write_json(manifest_path, result)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        return supervise(args.request)
    except Busy:
        return 0  # Another supervisor owns the attempt; never create a second.
    except (PilotError, OSError, ValueError, ImportError) as error:
        print("RunPilot supervisor: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
