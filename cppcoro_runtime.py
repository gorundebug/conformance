#!/usr/bin/env python3
"""Run the coroutine library's stock Docker tests without weakening its suite."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time


HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("DEPENDENCIES_DIR", HERE.parent)).expanduser().resolve()
ARTIFACTS = HERE / ".artifacts" / "cppcoro-runtime"


def main() -> int:
    framework = ROOT / "cppcoroservicelib"
    driver = framework / "scripts" / "test-conan.sh"
    if not driver.is_file():
        raise RuntimeError(f"Missing coroutine runtime test driver: {driver}")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["CPPBOOSTSERVICELIB_ENABLE_OTEL"] = "True"
    environment["CPPBOOSTSERVICELIB_CONAN_IMAGE"] = "cppcoroservicelib-conan-build"
    runs = []
    status = 0
    for build_type in ("Debug", "Release"):
        command = ["bash", str(driver), build_type]
        log = ARTIFACTS / f"{build_type.lower()}.log"
        started = time.monotonic()
        print(f"cppcoro {build_type}: {log}", flush=True)
        with log.open("w") as output:
            result = subprocess.run(
                command, cwd=framework, env=environment,
                stdout=output, stderr=subprocess.STDOUT, check=False,
            )
        runs.append({
            "build_type": build_type,
            "command": command,
            "exit_code": result.returncode,
            "duration_seconds": time.monotonic() - started,
            "log": str(log),
        })
        if result.returncode:
            status = 1
            break
    summary = {
        "status": "fail" if status else "pass",
        "languages": ["cppcoro"],
        "scope": "complete runtime Docker test suite",
        "runs": runs,
    }
    (ARTIFACTS / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
