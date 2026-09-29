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
BACKENDS = ("epoll", "uring")
BUILD_TYPES = ("Debug", "Release")
STAGE_TESTS = {
    "io": {"coro_resolver_tests", "coro_event_engine_tests", "coro_callback_transport_tests"},
    "config": {"cppcoroservicelib_config_loader_test"},
    "pools": {
        "cppcoroservicelib_taskpool_test",
        "cppcoroservicelib_other_pools_test",
        "cppcoroservicelib_coroutine_pool_test",
        "cppcoroservicelib_coroutine_mutex_test",
        "cppcoroservicelib_coroutine_task_executor_test",
    },
    "operators": {
        "cppcoroservicelib_operators_test",
        "cppcoroservicelib_operators_topology_test",
        "cppcoroservicelib_join_topology_test",
        "cppcoroservicelib_coroutine_selectors_test",
        "cppcoroservicelib_coroutine_join_storage_test",
        "cppcoroservicelib_substream_test",
        "cppcoroservicelib_direct_caller_queue_test",
    },
    "serde": {"cppcoroservicelib_serde_test"},
    "transports": {
        "cppcoroservicelib_http_endpoints_test",
        "cppcoroservicelib_kafka_endpoints_test",
        "cppcoroservicelib_grpc_runtime_test",
        "cppcoroservicelib_grpc_streaming_test",
        "cppcoroservicelib_coroutine_http_test",
        "cppcoroservicelib_coroutine_grpc_unary_test",
        "cppcoroservicelib_coroutine_grpc_streaming_test",
    },
    "telemetry": {
        "cppcoroservicelib_telemetry_test",
        "cppcoroservicelib_tracing_test",
        "cppcoroservicelib_prometheus_test",
    },
    "lifecycle": {
        "cppcoroservicelib_serviceapp_test",
        "cppcoroservicelib_coroutine_lifecycle_test",
        "cppcoroservicelib_coroutine_initialization_test",
    },
}


def required_stage_tests(backend: str) -> dict[str, set[str]]:
    if backend not in BACKENDS:
        raise ValueError(f"Unknown Coro backend: {backend}")
    stages = {stage: set(tests) for stage, tests in STAGE_TESTS.items()}
    stages["io"].add(f"{backend}_backend_tests")
    return stages


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
    for name in ("CPPCORO_BUILD_TARGET", "CPPCORO_TEST_REGEX"):
        if environment.get(name):
            raise RuntimeError(f"Full runtime gate does not allow filtering with {name}")
    environment["CPPCOROSERVICELIB_ENABLE_OTEL"] = "True"
    environment["CPPCOROSERVICELIB_CONAN_IMAGE"] = "cppcoroservicelib-conan-build"
    runs = []
    status = 0
    for backend, build_type in (
        (backend, build_type) for backend in BACKENDS for build_type in BUILD_TYPES
    ):
        environment["CPP_CORO_IO_BACKEND"] = backend
        required = required_stage_tests(backend)
        command = ["bash", str(driver), build_type]
        log = ARTIFACTS / f"{backend}-{build_type.lower()}.log"
        started = time.monotonic()
        print(f"cppcoro {backend} {build_type}: {log}", flush=True)
        with log.open("w") as output:
            result = subprocess.run(
                command, cwd=framework, env=environment,
                stdout=output, stderr=subprocess.STDOUT, check=False,
            )
        covered = passed_tests(log) if result.returncode == 0 else set()
        missing_by_stage = {
            stage: sorted(tests - covered)
            for stage, tests in required.items()
            if tests - covered
        }
        runs.append({
            "io_backend": backend,
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
        "io_backends": list(BACKENDS),
        "build_types": list(BUILD_TYPES),
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
