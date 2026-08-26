#!/usr/bin/env python3
"""Run one conformance command with an unmistakable console result."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / ".artifacts"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: run_suite.py SUITE COMMAND [ARG ...]", file=sys.stderr)
        return 2
    suite = sys.argv[1]
    command = sys.argv[2:]
    summary = ARTIFACTS / suite / "summary.json"
    # Never let aggregate.py report an older successful artifact after the
    # current invocation failed before its suite could write a new summary.
    summary.unlink(missing_ok=True)
    started = time.monotonic()
    print(f"\n==> [conformance:{suite}] START", flush=True)
    result = subprocess.run(command, check=False)
    elapsed = time.monotonic() - started
    status = "PASS" if result.returncode == 0 else "FAIL"
    target = sys.stdout if result.returncode == 0 else sys.stderr
    print(
        f"==> [conformance:{suite}] {status} ({elapsed:.1f}s, exit {result.returncode})",
        file=target,
        flush=True,
    )
    if result.returncode != 0:
        diagnostic = {
            "code": "SG_VERIFICATION_COMMAND_FAILED",
            "severity": "error",
            "stage": "verification",
            "message": f"Conformance suite '{suite}' failed",
            "object": {"kind": "conformanceSuite", "name": suite},
            "details": {"exitCode": result.returncode},
        }
        print(
            f"    [{diagnostic['code']}] {diagnostic['message']}",
            file=sys.stderr,
            flush=True,
        )
        # A suite may already have written a richer, domain-specific failure
        # summary. Keep it intact so the exact language/assertion survives the
        # wrapper. Only synthesize a generic summary when the command failed
        # before producing one.
        if not summary.is_file():
            summary.parent.mkdir(parents=True, exist_ok=True)
            summary.write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "operation": "verify",
                        "status": "fail",
                        "error": f"suite command exited with code {result.returncode}",
                        "command": command,
                        "diagnostics": [diagnostic],
                        "artifacts": [],
                        "summary": {"errors": 1, "warnings": 0},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
