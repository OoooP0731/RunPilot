#!/usr/bin/env python3
"""Bounded static project inspection; never import or execute project code."""

import argparse
import ast
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from core import PilotError, SECRET_FLAG, absolute, emit, redact


MAX_FILES = 160
MAX_ENTRIES = 1200
MAX_DEPTH = 4
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_SECONDS = 3
EXCLUDED = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "virtualenv",
    "__pycache__", "node_modules", "site-packages", "data", "dataset", "datasets",
    "weights", "weight", "checkpoints", "checkpoint", "models", "artifacts",
    "logs", "outputs", "results", ".cache", "wandb", "dist", "build",
}
TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}
PROTECTED = re.compile(
    r"(?:batch(?:_size)?|learning_rate|lr|epochs?|max_steps|seed|precision|"
    r"gradient_accumulation_steps|num_workers|world_size)", re.I,
)


def _command(value):
    if value is None:
        return None
    if not isinstance(value, list) or not value or len(value) > 256 or not all(
        isinstance(item, str) and "\x00" not in item for item in value
    ) or not value[0]:
        raise PilotError("command must be a nonempty JSON argv array of at most 256 strings")
    if sum(len(item.encode("utf-8")) for item in value) > 16384:
        raise PilotError("command exceeds 16384 bytes")
    result, secret_next = [], False
    for item in value:
        if secret_next:
            result.append("[REDACTED]")
            secret_next = False
        elif SECRET_FLAG.search(item):
            result.append(item.split("=", 1)[0] + "=[REDACTED]" if "=" in item else item)
            secret_next = "=" not in item
        else:
            result.append(redact(item))
    return result


def _read(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PilotError("not a regular static source")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(limit)
        return raw.decode("utf-8", "replace"), len(raw), info.st_size > len(raw)
    finally:
        if fd is not None:
            os.close(fd)


def _python_evidence(text, relative):
    evidence, hints, flags = [], [], []
    try:
        tree = ast.parse(text, filename=relative)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return evidence, hints, flags, "unparsed/truncated Python; no execution attempted"
    for count, node in enumerate(ast.walk(tree)):
        if count >= 8000:
            return evidence, hints, flags, "AST node limit reached"
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            test = node.test
            if (
                isinstance(test.left, ast.Name) and test.left.id == "__name__"
                and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            ):
                evidence.append({"kind": "main_guard", "line": node.lineno})
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            names = [
                arg.value for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("-")
            ][:8]
            if names and len(flags) < 64:
                flag = {"names": names, "line": node.lineno}
                default = next((kw.value for kw in node.keywords if kw.arg == "default"), None)
                if isinstance(default, ast.Constant) and isinstance(default.value, (str, int, float, bool, type(None))):
                    flag["static_default"] = redact(str(default.value))[:160]
                flags.append(flag)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and len(hints) < 32:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if isinstance(target, ast.Name) and PROTECTED.fullmatch(target.id) and isinstance(value, ast.Constant):
                    if isinstance(value.value, (str, int, float, bool, type(None))):
                        hints.append({"name": target.id, "static_literal": redact(str(value.value))[:160],
                                      "source": relative, "line": node.lineno, "effective": "unknown"})
    return evidence, hints, flags, None


def inspect_project(project, command=None):
    """Return static candidates/evidence, never a synthesized launch command.

    Limits bound visited entries (not just matching files), depth, elapsed time,
    bytes per file/overall, AST nodes and output evidence. Symlinks and common
    dataset/weight/virtual-environment trees are not followed. Limits are
    conservative coverage bounds, not a sandbox against hostile filesystems.
    """
    project = absolute(str(project), "project")
    root = Path(project)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PilotError("project must be an existing nonsymlink directory")
    provided = _command(command)
    start = time.monotonic()
    files, candidates, hints, notes = [], [], [], []
    counts = {"entries": 0, "files_read": 0, "bytes_read": 0, "excluded_directories": 0,
              "symlinks_skipped": 0}
    truncated = False
    pending = [(root, 0)]
    while pending:
        if (
            time.monotonic() - start >= MAX_SECONDS or counts["entries"] >= MAX_ENTRIES
            or counts["files_read"] >= MAX_FILES or counts["bytes_read"] >= MAX_TOTAL_BYTES
        ):
            truncated = True
            break
        directory, depth = pending.pop()
        try:
            with os.scandir(str(directory)) as entries:
                for entry in entries:
                    if (
                        time.monotonic() - start >= MAX_SECONDS or counts["entries"] >= MAX_ENTRIES
                        or counts["files_read"] >= MAX_FILES or counts["bytes_read"] >= MAX_TOTAL_BYTES
                    ):
                        truncated = True
                        break
                    counts["entries"] += 1
                    if entry.is_symlink():
                        counts["symlinks_skipped"] += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in EXCLUDED:
                            counts["excluded_directories"] += 1
                        elif depth < MAX_DEPTH:
                            pending.append((Path(entry.path), depth + 1))
                        else:
                            truncated = True
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if path.suffix.lower() not in TEXT_SUFFIXES and entry.name not in {
                        "requirements.txt", "Dockerfile", "Makefile",
                    }:
                        continue
                    relative = str(path.relative_to(root))
                    limit = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - counts["bytes_read"])
                    try:
                        text, size, clipped = _read(path, limit)
                    except (OSError, PilotError) as error:
                        notes.append({"path": relative, "detail": redact(str(error))[:200]})
                        continue
                    counts["files_read"] += 1
                    counts["bytes_read"] += size
                    files.append({"path": relative, "bytes_read": size, "truncated": clipped})
                    truncated = truncated or clipped
                    if path.suffix.lower() == ".py":
                        evidence, file_hints, flags, problem = _python_evidence(text, relative)
                        hints.extend(file_hints[:max(0, 128 - len(hints))])
                        if evidence or path.stem.lower() in {"train", "main", "run", "fit", "launch"}:
                            candidates.append({
                                "path": relative, "kind": "python", "evidence": evidence,
                                "arguments": flags, "executable_verified": False,
                                "note": "static candidate only; not permission or proof of an independent unit",
                            })
                        if problem:
                            notes.append({"path": relative, "detail": problem})
                    elif path.suffix.lower() == ".sh":
                        candidates.append({
                            "path": relative, "kind": "shell_script", "executable_verified": False,
                            "note": "review the complete existing wrapper before any authorized execution",
                        })
        except OSError as error:
            notes.append({"path": str(directory.relative_to(root)), "detail": redact(str(error))[:200]})
    return {
        "project": str(root), "read_only": True, "provided_command": provided,
        "command_executed": False, "project_imported": False,
        "candidates": candidates, "files": files, "configuration_hints": hints,
        "notes": notes[:64], "counts": counts, "truncated": truncated,
        "limits": {"files": MAX_FILES, "entries": MAX_ENTRIES, "depth": MAX_DEPTH,
                   "file_bytes": MAX_FILE_BYTES, "total_bytes": MAX_TOTAL_BYTES, "seconds": MAX_SECONDS},
        "unknowns": [
            "Actual effective configuration and project environment were not executed or imported.",
            "Candidate names/main guards do not prove launch safety, unit independence or resume support.",
            "No train --help, dependency installation, GPU probe, network access or project write occurred.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Absolute existing project directory")
    parser.add_argument("--command-json", help="Existing command as a JSON argv array; recorded, never executed")
    args = parser.parse_args(argv)
    try:
        if args.command_json and len(args.command_json) > 32768:
            raise PilotError("--command-json exceeds input limit")
        command = json.loads(args.command_json) if args.command_json is not None else None
        if args.command_json is not None and command is None:
            raise PilotError("--command-json must be an argv array, not null")
        emit(inspect_project(args.project, command))
        return 0
    except (OSError, ValueError, PilotError, RecursionError) as error:
        emit({"program_error": str(error), "read_only": True, "command_executed": False})
        return 2


if __name__ == "__main__":
    sys.exit(main())
