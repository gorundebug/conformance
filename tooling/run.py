#!/usr/bin/env python3
"""Validate the profiling and benchmark toolkits embedded in conformance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / ".artifacts" / "tooling" / "summary.json"


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    commands = [
        [sys.executable, "-m", "unittest", "test_paths", "-v"],
        [sys.executable, "-m", "unittest", "discover", "-s",
         "profiling/examples", "-p", "test_*.py", "-v"],
        [sys.executable, "-m", "unittest", "discover", "-s",
         "benchmarks/examples", "-p", "test_*.py", "-v"],
    ]
    for command in commands:
        run(command)
    summary = {
        "status": "pass",
        "toolkits": ["profiling", "benchmarks"],
        "commands": commands,
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
