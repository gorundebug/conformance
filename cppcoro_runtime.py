#!/usr/bin/env python3
"""Run the coroutine library's stock Docker tests without weakening its suite."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time


HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("DEPENDENCIES_DIR", HERE.parent)).expanduser().resolve()
ARTIFACTS = HERE / ".artifacts" / "cppcoro-runtime"
STAGE_TESTS = {
    "config": {"cppboostservicelib_config_loader_test"},
    "pools": {
        "cppboostservicelib_taskpool_test",
        "cppboostservicelib_other_pools_test",
        "cppcoroservicelib_coroutine_pool_test",
    },
    "operators": {
        "cppboostservicelib_operators_test",
        "cppboostservicelib_operators_topology_test",
        "cppboostservicelib_join_topology_test",
        "cppcoroservicelib_coroutine_selectors_test",
    },
    "serde": {"cppboostservicelib_serde_test"},
    "transports": {
        "cppboostservicelib_http_endpoints_test",
        "cppboostservicelib_kafka_endpoints_test",
        "cppboostservicelib_grpc_runtime_test",
        "cppboostservicelib_grpc_streaming_test",
        "cppcoroservicelib_coroutine_http_test",
        "cppcoroservicelib_coroutine_grpc_unary_test",
        "cppcoroservicelib_coroutine_grpc_streaming_test",
    },
    "telemetry": {
        "cppboostservicelib_telemetry_test",
        "cppboostservicelib_tracing_test",
        "cppboostservicelib_prometheus_test",
    },
    "lifecycle": {
        "cppboostservicelib_serviceapp_test",
        "cppcoroservicelib_coroutine_lifecycle_test",
        "cppcoroservicelib_coroutine_initialization_test",
    },
}


def passed_tests(log: Path) -> set[str]:
    content = log.read_text(errors="replace")
    if not re.search(r"100% tests passed, 0 tests failed out of \d+", content):
        raise RuntimeError(f"cppcoro CTest completion is missing: {log}")
    return set(re.findall(
        r"Test\s+#\d+:\s+([A-Za-z0-9_]+)\s+\.{3,}\s+Passed",
        content,
    ))


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
        covered = passed_tests(log) if result.returncode == 0 else set()
        missing_by_stage = {
            stage: sorted(tests - covered)
            for stage, tests in STAGE_TESTS.items()
            if tests - covered
        }
        runs.append({
            "build_type": build_type,
            "command": command,
            "exit_code": result.returncode,
            "duration_seconds": time.monotonic() - started,
            "log": str(log),
            "passed_tests": len(covered),
            "missing_by_stage": missing_by_stage,
        })
        if result.returncode or missing_by_stage:
            status = 1
            break
    summary = {
        "status": "fail" if status else "pass",
        "languages": ["cppcoro"],
        "scope": "complete runtime Docker test suite",
        "stage_tests": {
            stage: sorted(tests) for stage, tests in STAGE_TESTS.items()
        },
        "runs": runs,
    }
    (ARTIFACTS / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
