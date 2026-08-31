#!/usr/bin/env python3
"""Resume an interrupted conformance run from current suite summaries."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from aggregate import ARTIFACTS, OUTPUT, ROOT, SUITES, passed


SUITE_TARGETS = {
    "config": "config-core",
    "config-runtime": "config-runtime-core",
    "dashboards": "dashboards-core",
    "standalone-components": "standalone-components-resume",
}


def suite_passed(name: str, artifacts: Path = ARTIFACTS) -> bool:
    path = artifacts / name / "summary.json"
    if not path.is_file():
        return False
    try:
        summary = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return passed(name, summary)[0]


def pending_targets(artifacts: Path = ARTIFACTS) -> list[str]:
    targets: list[str] = []
    for suite in SUITES:
        if suite_passed(suite, artifacts):
            continue
        target = SUITE_TARGETS.get(suite, suite)
        if target not in targets:
            targets.append(target)
    return targets


def aggregate_status(path: Path = OUTPUT) -> str:
    if not path.is_file():
        return "missing"
    try:
        value = json.loads(path.read_text()).get("status")
    except (OSError, json.JSONDecodeError):
        return "invalid"
    return str(value) if value is not None else "unknown"


def main() -> int:
    targets = pending_targets()
    print(
        f"Existing aggregate: {aggregate_status()}; "
        f"pending leaf suites: {len(targets)}",
        flush=True,
    )
    if targets:
        print("Resume order: " + ", ".join(targets), flush=True)
    else:
        print("Every suite summary already passes; rebuilding aggregate only.", flush=True)

    for target in targets:
        print(f"\n==> [conformance:resume] RUN {target}", flush=True)
        result = subprocess.run(
            ["make", "--no-print-directory", target], cwd=ROOT, check=False,
        )
        if result.returncode != 0:
            print(
                f"==> [conformance:resume] FAIL {target} "
                f"(exit {result.returncode})",
                file=sys.stderr,
                flush=True,
            )
            return result.returncode

    return subprocess.run(
        [sys.executable, "aggregate.py"], cwd=ROOT, check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
