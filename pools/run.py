#!/usr/bin/env python3
"""Differential TaskPool/PriorityTaskPool/DelayPool conformance gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_source_cache
import cpp_userver
import typescript_toolchain


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "pools" / "summary.json"

GO = ROOT / "servicelib"
CANONICAL = ROOT / "cppservicelib"
BOOST = ROOT / "cppboostservicelib"
TYPESCRIPT = ROOT / "tsservicelib"


REQUIRED_SOURCE_CASES = {
    GO / "runtime/pool/taskpool_behavior_test.go": (
        "TestTaskPool_ExecutorMetrics",
        "TestTaskPool_UsesConstructorConfigWhenRuntimePoolIsMissing",
        "TestTaskPool_CancelMovesToFront",
    ),
    GO / "runtime/pool/prioritytaskpool_test.go": (
        "TestPriorityTaskPool_ExecutorMetrics",
        "TestPriorityTaskPool_UsesConstructorConfigWhenRuntimePoolIsMissing",
        "TestPriorityTaskPool_PriorityOrdering",
        "TestPriorityTaskPool_CancelPromotion",
    ),
    GO / "runtime/pool/delaypool_behavior_test.go": (
        "TestDelayPool_ContextCancelRunsImmediately",
        "TestDelayPool_TimerCompletionUnregistersLateContextAfterFunc",
    ),
    CANONICAL / "tests/taskpool_test.cpp": (
        "LifecycleFifoAndMetrics",
        "CancelledContextIsRejectedAndTaskFailureIsIsolated",
        "DeadlineMovesQueuedTaskToFront",
        "EarlierDeadlinePrecedesLaterExplicitCancellation",
        "ExternalCancellationMovesQueuedTaskToFront",
        "RejectsExpiredDeadline",
    ),
    CANONICAL / "tests/other_pools_test.cpp": (
        "PriorityFifoAndDeadlinePromotion",
        "ExplicitCancellationPromotesOnlyOnce",
        "ExternalCancellationPromotesQueuedTask",
        "StopDeadlineReportsButStillDrainsAcceptedTask",
        "CancelledTimerCoroutineRetiresPromptly",
        "RejectsCancelledContextAndDetectsSelfStop",
        "ExternalCancellationExpeditesAndIsVisibleToCallback",
    ),
    BOOST / "tests/taskpool_test.cpp": (
        "lifecycleFifoAndMetrics",
        "cancellationAndFailureIsolation",
        "deadlineMovesQueuedTaskToFront",
        "laterCancellationPrecedesEarlierDeadline",
        "externalCancellationMovesQueuedTaskToFront",
        "rejectsExpiredDeadline",
        "hotResizeUsesLatestRuntimeConfig",
        "lifecycleCancellationDrainsAndRejectsNewTasks",
        "concurrentStopJoinsTheSameDrain",
        "stopDeadlineReportsButStillDrains",
        "selfStopIsRejectedWithoutBreakingThePool",
    ),
    BOOST / "tests/other_pools_test.cpp": (
        "PriorityFifoAndDeadlinePromotion",
        "ExplicitCancellationPromotesOnlyOnce",
        "ExternalCancellationPromotesQueuedTask",
        "HotResizeUsesLatestRuntimeConfig",
        "LifecycleCancellationDrainsAndRejectsNewTasks",
        "ConcurrentStopJoinsTheSameDrain",
        "StopDeadlineReportsButStillDrains",
        "SelfStopIsRejectedWithoutBreakingThePool",
        "DeadlineAndCancellationExecuteExactlyOnce",
        "TimerCompletionUnregistersCancellationCallbacks",
        "StopDeadlineReportsButStillDrainsAcceptedTask",
        "CancelledTimerCoroutineRetiresPromptly",
        "RejectsCancelledContextAndDetectsSelfStop",
        "ExternalCancellationExpeditesAndIsVisibleToCallback",
    ),
    TYPESCRIPT / "test/task-pool.test.ts": (
        "task pool bounds concurrent asynchronous work and drains on stop",
        "priority pool uses canonical lower-number-first ordering",
        "priority pool preserves FIFO order for equal priorities",
        "task pool hot resize increases concurrency without dropping queued work",
        "cancelled queued work is promoted and executes with cancelled context",
    ),
    TYPESCRIPT / "test/delay-pool.test.ts": (
        "delay pool executes once at the earlier context deadline",
        "delay cancellation executes accepted callback immediately and exactly once",
        "delay pool rejects new work after stop",
        "delay pool accepts work before lazy lifecycle start",
        "delay pool records the canonical queue, execution and cancellation metrics",
    ),
}


def execute(
    name: str,
    command: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    print(f"[pools] START {name}", file=sys.stderr, flush=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=1,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        lines.append(line)
        print(line, end="", file=sys.stderr, flush=True)
    return_code = process.wait()
    output = "".join(lines)
    duration = round(time.monotonic() - started, 3)
    result: dict[str, object] = {
        "name": name,
        "command": command,
        "exit_code": return_code,
        "duration_seconds": duration,
        "output_tail": output[-8000:],
    }
    if return_code != 0:
        raise RuntimeError(f"{name} failed:\n{output}")
    print(f"[pools] PASS  {name} ({duration:.1f}s)", file=sys.stderr, flush=True)
    return result


def verify_source_matrix() -> dict[str, object]:
    files: dict[str, object] = {}
    errors: list[str] = []
    total = 0
    for path, cases in REQUIRED_SOURCE_CASES.items():
        relative = str(path.relative_to(ROOT))
        if not path.is_file():
            errors.append(f"missing source: {relative}")
            continue
        source = path.read_text()
        missing = [case for case in cases if case not in source]
        total += len(cases)
        files[relative] = {
            "required_cases": len(cases),
            "missing_cases": missing,
        }
        errors.extend(f"{relative}: missing {case}" for case in missing)
    if errors:
        raise RuntimeError("pool source matrix failed:\n" + "\n".join(errors))
    return {"files": files, "required_case_markers": total}


def boost_framework_build_script() -> str:
    return (
        "cmake --fresh -S . -B build/docker -G Ninja "
        "-DCMAKE_BUILD_TYPE=Debug "
        "-DCMAKE_INSTALL_PREFIX=/workspace/build/docker-install "
        "-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON "
        "-DCPPBOOSTSERVICELIB_ENABLE_KAFKA=ON "
        f"{cpp_source_cache.cmake_args(BOOST)}&& "
        "cmake --build build/docker --parallel && "
        "ctest --test-dir build/docker --output-on-failure && "
        "cmake --install build/docker && "
        "cmake --fresh -S tests/consumer -B build/consumer -G Ninja "
        "-DCMAKE_PREFIX_PATH=/workspace/build/docker-install && "
        "cmake --build build/consumer --parallel && "
        "./build/consumer/cppboostservicelib_consumer"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse the current canonical and Boost Docker build trees",
    )
    args = parser.parse_args()

    missing = [str(path) for path in (GO, CANONICAL, BOOST, TYPESCRIPT) if not path.is_dir()]
    if missing:
        raise RuntimeError("missing conformance input: " + ", ".join(missing))

    source_matrix = verify_source_matrix()
    runs: list[dict[str, object]] = []
    runs.append(
        execute(
            "go-pools",
            [
                "go",
                "test",
                "./runtime/pool",
                "-run",
                "Test(TaskPool|PriorityTaskPool|DelayPool)_",
                "-count=1",
            ],
            GO,
            env={**os.environ, "GOWORK": "off"},
        )
    )
    if not args.skip_build:
        runs.append(
            execute(
                "typescript-pools-dependencies",
                typescript_toolchain.install_command(),
                TYPESCRIPT,
                env=typescript_toolchain.environment(),
            )
        )
        runs.append(
            execute(
                "typescript-pools-build",
                typescript_toolchain.tsc_command(
                    "tsconfig.test.json", force=True
                ),
                TYPESCRIPT,
            )
        )
        runs.append(
            execute(
                "typescript-pools-runtime-assets",
                typescript_toolchain.copy_runtime_assets_command(),
                TYPESCRIPT,
            )
        )
    runs.append(
        execute(
            "typescript-pools",
            [
                "node", "--test", "--enable-source-maps",
                "dist-test/test/task-pool.test.js",
                "dist-test/test/delay-pool.test.js",
            ],
            TYPESCRIPT,
        )
    )

    canonical_script = (
        cpp_userver.configure_script() + " && "
        "cmake --build --preset docker --parallel --target "
        "servicelib_taskpool_test servicelib_other_pools_test && "
        "./build/servicelib_taskpool_test && "
        "./build/servicelib_other_pools_test"
    )
    if args.skip_build:
        canonical_script = (
            "./build/servicelib_taskpool_test && "
            "./build/servicelib_other_pools_test"
        )
    canonical_command = [
        "docker",
        "compose",
        "-f",
        "docker-compose.cmake.yml",
        "run",
    ]
    if not args.skip_build:
        canonical_command.append("--build")
    canonical_command.extend(
        ["--rm", "test", "/bin/bash", "-lc", canonical_script]
    )
    runs.append(execute("canonical-cpp-pools", canonical_command, CANONICAL))

    if not args.skip_build:
        runs.append(execute(
            "boost-build-image",
            ["docker", "build", "-f", "Dockerfile.cmake", "-t",
             "cppboostservicelib-build", "."],
            BOOST,
        ))
        runs.append(execute(
            "boost-source-cache",
            cpp_source_cache.prepare_command(BOOST),
            BOOST,
        ))
        runs.append(execute(
            "boost-framework-build-and-tests",
            [
                "docker", "run", "--rm",
                "-e", "CCACHE_DIR=/ccache",
                "-e", "CCACHE_BASEDIR=/workspace",
                "-e", "CCACHE_COMPILERCHECK=content",
                "-v", "cppboostservicelib-ccache:/ccache",
                "-v", cpp_source_cache.source_mount(BOOST),
                "-v", f"{BOOST}:/workspace", "-w", "/workspace",
                *cpp_source_cache.build_volume_mount_args(
                    BOOST, "cppboostservicelib-pools"
                ),
                "cppboostservicelib-build:latest", "/bin/bash", "-lc",
                boost_framework_build_script(),
            ],
            BOOST,
        ))
    runs.append(
        execute(
            "boost-cpp-pools",
            [
                "docker",
                "run",
                "--rm",
                "-v",
                cpp_source_cache.source_mount(BOOST),
                "-v",
                f"{BOOST}:/workspace",
                *cpp_source_cache.build_volume_mount_args(
                    BOOST, "cppboostservicelib-pools"
                ),
                "-w",
                "/workspace",
                "cppboostservicelib-build:latest",
                "ctest",
                "--test-dir",
                "build/docker",
                "--output-on-failure",
                "-R",
                "cppboostservicelib_(taskpool|other_pools)_test",
            ],
            BOOST,
        )
    )

    summary = {
        "status": "pass",
        "languages": ["go", "canonical-cpp", "cppboost", "typescript"],
        "source_matrix": source_matrix,
        "runs": runs,
        "unrestricted_build_parallelism": True,
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - gate must emit one diagnostic
        print(f"Pool conformance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
