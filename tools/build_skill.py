#!/usr/bin/env python3
"""Validate an installable skill and optionally build its deterministic archive.

Runtime scripts use only Python's standard library. This maintainer-side tool
parses YAML with existing PyYAML or Ruby/Psych; it installs neither.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "run-pilot"
EXCLUDE = {"__pycache__", "evals", ".DS_Store"}


def metadata(text):
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.S)
    if not match:
        raise ValueError("SKILL.md must start with YAML frontmatter")
    raw = match.group(1)
    try:
        import yaml
    except ImportError:
        ruby = shutil.which("ruby")
        if not ruby:
            raise ValueError("Metadata validation needs existing PyYAML or Ruby/Psych; nothing was installed")
        script = (
            "require 'yaml'; require 'json'; "
            "puts JSON.generate(YAML.safe_load(STDIN.read, permitted_classes: [], "
            "permitted_symbols: [], aliases: false))"
        )
        result = subprocess.run(
            [ruby, "-e", script], input=raw, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False,
        )
        if result.returncode:
            raise ValueError("Invalid YAML: " + result.stderr[:500])
        return json.loads(result.stdout), "Ruby/Psych safe_load"
    return yaml.safe_load(raw), "PyYAML safe_load"


def validate():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    info, parser = metadata(text)
    if not isinstance(info, dict):
        raise ValueError("Frontmatter must be an object")
    if info.get("name") != "run-pilot":
        raise ValueError("Skill name must match the directory: run-pilot")
    allowed = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
    if set(info) - allowed:
        raise ValueError("Unexpected frontmatter properties: " + str(set(info) - allowed))
    description = info.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("description must be a nonempty string of <=1024 characters")
    if "<" in description or ">" in description:
        raise ValueError("description cannot contain angle brackets")
    compatibility = info.get("compatibility", "")
    if not isinstance(compatibility, str) or len(compatibility) > 500:
        raise ValueError("Invalid compatibility metadata")
    license_name = info.get("license")
    if license_name != "MIT":
        raise ValueError("The distributable skill must declare its MIT license")
    if (SKILL / "LICENSE").read_bytes() != (ROOT / "LICENSE").read_bytes():
        raise ValueError("The packaged skill license must match the repository license")
    if len(text.splitlines()) > 500:
        raise ValueError("Disclose references instead of a >500 line main skill")
    links = []
    for path in sorted(SKILL.rglob("*.md")):
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in link or link.startswith("#"):
                continue
            target = (path.parent / link.split("#")[0]).resolve()
            if SKILL.resolve() not in target.parents and target != SKILL.resolve():
                raise ValueError("Installed skill depends on a path outside itself: " + link)
            if not target.is_file():
                raise ValueError("Missing local reference in %s: %s" % (path.name, link))
            links.append(str(target.relative_to(SKILL)))
    files = []
    for path in sorted(SKILL.rglob("*")):
        relative = path.relative_to(SKILL)
        if any(part in EXCLUDE for part in relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Skill archive must not contain symlinks: " + str(relative))
        if path.is_file():
            if path.suffix == ".py":
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            files.append(path)
    return files, {"name": info["name"], "yaml_parser": parser,
                   "main_lines": len(text.splitlines()), "local_reference_count": len(links),
                   "packaged_file_count": len(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--output", default=str(ROOT / "dist/run-pilot.skill"))
    args = parser.parse_args()
    try:
        files, result = validate()
        if not args.check_only:
            output = Path(args.output).absolute()
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="runpilot-package-") as directory:
                archive = Path(directory) / "run-pilot.skill"
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    for path in files:
                        info = zipfile.ZipInfo("run-pilot/" + path.relative_to(SKILL).as_posix())
                        info.date_time = (2026, 10, 17, 0, 0, 0)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o100644 << 16
                        bundle.writestr(info, path.read_bytes())
                with zipfile.ZipFile(archive) as bundle:
                    if bundle.testzip() is not None:
                        raise ValueError("Archive verification failed")
                shutil.copyfile(archive, output)
            checksum = hashlib.sha256(output.read_bytes()).hexdigest()
            output.with_suffix(output.suffix + ".sha256").write_text(
                checksum + "  " + output.name + "\n", encoding="ascii",
            )
            result.update(archive=str(output), sha256=checksum)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"validation_error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
