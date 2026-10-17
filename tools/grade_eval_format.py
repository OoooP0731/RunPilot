#!/usr/bin/env python3
"""Objective file/JSON checks for offline decision evals; not semantic grading."""

import argparse
import json
from pathlib import Path
import sys

REQUIRED = {"authorized_actions", "execution_state", "actual_gpu", "monitor_mode", "unknowns", "next_steps"}


def reject_constant(value):
    raise ValueError("Non-finite JSON value: " + value)


def check(run):
    outputs = run / "outputs"
    details = []
    decision = None
    try:
        raw = (outputs / "decision.json").read_text(encoding="utf-8")
        decision = json.loads(raw, parse_constant=reject_constant)
        valid = isinstance(decision, dict) and REQUIRED <= decision.keys()
        details.append("JSON keys present: " + str(sorted(decision) if isinstance(decision, dict) else type(decision)))
    except (OSError, ValueError) as error:
        valid = False
        details.append("JSON error: " + str(error))
    try:
        markdown = (outputs / "decision.md").read_text(encoding="utf-8")
        valid = valid and bool(markdown.strip())
        details.append("decision.md characters: %d" % len(markdown))
    except OSError as error:
        valid = False
        details.append("Markdown error: " + str(error))
    result = {
        "format_passed": bool(valid), "evidence": "; ".join(details),
        "case1_actual_gpu_is_null": (
            decision.get("actual_gpu") is None if isinstance(decision, dict)
            and run.parents[1].name.startswith("eval-1-") else None
        ),
        "semantic_checks_not_performed": True,
    }
    (run / "format_grading.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iteration", type=Path)
    args = parser.parse_args()
    results = {str(run.relative_to(args.iteration)): check(run)
               for run in sorted(args.iteration.glob("eval-*/*/run-*"))}
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(r["format_passed"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
