#!/usr/bin/env python3
"""End-to-end throughput benchmark for the generated Temporal graph boundary."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import dependency_environment
import tooling_lock


HERE = Path(__file__).resolve().parent
ROOT = Path(
    os.environ.get("DEPENDENCIES_DIR", HERE.parent / ".dependencies")
).expanduser().resolve()
ARTIFACTS = HERE / ".artifacts" / "durable"
SCHEDULE_ID = "example-automation-schedule"
CALLS_RE = re.compile(r"(?:^|\n)calls:\s*(\d+)")


@dataclass(frozen=True)
class Language:
    name: str
    example: Path
    runtime: Path
    override_target: str

    @property
    def project(self) -> str:
        return f"servicelib-durable-benchmark-{self.name}"


LANGUAGES = {
    item.name: item
    for item in (
        Language("go", ROOT / "goexample", ROOT / "servicelib", "/app/config/overrides.yaml"),
        Language(
            "python", ROOT / "pyexample", ROOT / "pyservicelib",
            "/workspace/config/docker_overrides.yaml",
        ),
        Language(
            "typescript", ROOT / "tsexample", ROOT / "tsservicelib",
            "/app/config/docker_overrides.yaml",
        ),
    )
}


def environment(language: Language, cores: int) -> dict[str, str]:
    env = os.environ.copy()
    env["DURABLE_BENCHMARK_CORES"] = str(cores)
    if language.name == "go":
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    else:
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    return env


def run(
    command: list[str], *, cwd: Path, env: dict[str, str],
    capture: bool = False, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("-", " ".join(command), flush=True)
    return subprocess.run(
        command, cwd=cwd, env=env, check=check, text=True,
        capture_output=capture,
    )


def prepare(language: Language) -> Path:
    directory = ARTIFACTS / language.name
    directory.mkdir(parents=True, exist_ok=True)
    overrides = directory / "automationservice.overrides.yaml"
    overrides.write_text(
        """dataConnectors:
  temporal:
    address: temporal:7233
endpoints:
  durableJob:
    enabled: true
  localSchedule:
    enabled: false
  temporalSchedule:
    enabled: true
    schedule: "* * * * *"
    overlapPolicy: Allow
services:
  automationService:
    defaultGrpcTimeout: 0
    environment: ""
    grpcHost: 0.0.0.0
    grpcPort: 9204
    httpHost: 0.0.0.0
    httpPort: 9094
"""
    )
    overlay = directory / "compose.yml"
    overlay.write_text(
        "services:\n"
        "  automationservice:\n"
        "    cpus: ${DURABLE_BENCHMARK_CORES}\n"
        "    environment:\n"
        "      SERVICELIB_NOOP_METRICS: \"1\"\n"
        "      SERVICELIB_NOOP_TRACING: \"1\"\n"
        "    volumes:\n"
        f"      - {overrides}:{language.override_target}:ro\n"
    )
    return overlay


def compose(language: Language, overlay: Path, *arguments: str) -> list[str]:
    command = [
        "docker", "compose", "--project-name", language.project,
        "--project-directory", str(language.example),
        "--file", str(language.example / "docker-compose.yml"),
    ]
    command += [
        part
        for runtime in sorted(language.example.glob("docker-compose.*-runtime.generated.yml"))
        for part in ("--file", str(runtime))
    ]
    return [*command, "--file", str(overlay), *arguments]


def build(language: Language, overlay: Path, env: dict[str, str]) -> None:
    if language.name == "go":
        dependency_environment.run_dependency_command(
            ["make", "-C", "automationservice", "docker-build", f"PROJECT_DIR={language.example}"],
            cwd=language.example, env=env,
        )
    else:
        dependency_environment.run_dependency_command(
            compose(language, overlay, "build", "automationservice"),
            cwd=language.example, env=env,
        )


def status() -> dict[str, object]:
    with urllib.request.urlopen("http://localhost:9094/status/data", timeout=3) as response:
        return json.loads(response.read())


def wait_ready(timeout: float = 90) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return status()
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last = error
            time.sleep(0.5)
    raise RuntimeError(f"Automation Service did not become ready: {last}")


def edge_calls(value: dict[str, object], source: str, target: str) -> int:
    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise RuntimeError("status graph has no nodes/edges")
    ids = {
        str(node.get("label", "")).split("(", 1)[0]: node.get("id")
        for node in nodes if isinstance(node, dict)
    }
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if edge.get("from") == ids.get(source) and edge.get("to") == ids.get(target):
            match = CALLS_RE.search(str(edge.get("label", "")))
            return int(match.group(1)) if match else 0
    raise RuntimeError(f"status graph has no {source!r}->{target!r} edge")


def cli(
    language: Language, overlay: Path, env: dict[str, str], *arguments: str,
) -> None:
    run(
        compose(
            language, overlay, "run", "--rm", "--no-deps",
            "--entrypoint", "temporal", "temporal-create-namespace",
            *arguments, "--address", "temporal:7233", "--namespace", "default",
        ),
        cwd=language.example, env=env,
    )


def backfill(
    language: Language, overlay: Path, env: dict[str, str],
    start: datetime, jobs: int,
) -> None:
    # Temporal includes firings exactly on both backfill boundaries. Start one
    # second after a minute boundary so the following `jobs` minute marks are
    # selected exactly once.
    start += timedelta(seconds=1)
    end = start + timedelta(minutes=jobs)
    cli(
        language, overlay, env,
        "schedule", "backfill", "--schedule-id", SCHEDULE_ID,
        "--start-time", start.isoformat().replace("+00:00", "Z"),
        "--end-time", end.isoformat().replace("+00:00", "Z"),
        "--overlap-policy", "AllowAll",
    )


def wait_completed(baseline: int, jobs: int, timeout: float) -> tuple[int, float]:
    started = time.monotonic()
    deadline = started + timeout
    current = baseline
    while time.monotonic() < deadline:
        current = edge_calls(
            wait_ready(timeout=5), "Consume Durable Job", "Process Durable Job"
        )
        if current - baseline >= jobs:
            elapsed = time.monotonic() - started
            time.sleep(0.25)
            settled = edge_calls(
                wait_ready(timeout=5), "Consume Durable Job", "Process Durable Job"
            )
            return settled - baseline, elapsed
        time.sleep(0.05)
    raise RuntimeError(
        f"only {current - baseline}/{jobs} durable jobs completed in {timeout}s"
    )


def benchmark_language(
    language: Language, *, cores: int, jobs: int, warmup_jobs: int,
    runs: int, skip_build: bool, timeout: float,
) -> dict[str, object]:
    overlay = prepare(language)
    env = environment(language, cores)
    if not skip_build:
        build(language, overlay, env)
    down = compose(language, overlay, "down", "--volumes", "--remove-orphans")
    run(down, cwd=language.example, env=env, check=False)
    try:
        run(
            compose(
                language, overlay, "up", "--detach",
                "temporal-postgresql", "temporal-schema", "temporal",
                "temporal-create-namespace", "temporal-ui", "automationservice",
            ),
            cwd=language.example, env=env,
        )
        wait_ready()
        cli(
            language, overlay, env,
            "schedule", "toggle", "--schedule-id", SCHEDULE_ID, "--pause",
        )
        time.sleep(0.5)
        baseline = edge_calls(
            wait_ready(), "Consume Durable Job", "Process Durable Job"
        )
        epoch = (
            datetime.now(timezone.utc).replace(second=0, microsecond=0)
            - timedelta(days=30)
        )
        if warmup_jobs > 0:
            backfill(language, overlay, env, epoch, warmup_jobs)
            completed, _ = wait_completed(baseline, warmup_jobs, timeout)
            baseline += completed
        attempts: list[dict[str, float | int]] = []
        for index in range(runs):
            start = epoch + timedelta(days=index + 1)
            submitted_at = time.monotonic()
            backfill(language, overlay, env, start, jobs)
            completed, drain_seconds = wait_completed(baseline, jobs, timeout)
            elapsed = time.monotonic() - submitted_at
            attempts.append({
                "run": index + 1,
                "jobs": completed,
                "seconds": elapsed,
                "drainSeconds": drain_seconds,
                "jobsPerSecond": completed / elapsed,
            })
            baseline += completed
        best = max(attempts, key=lambda item: float(item["jobsPerSecond"]))
        return {
            "language": language.name,
            "cores": cores,
            "requestedJobs": jobs,
            "warmupJobs": warmup_jobs,
            "runs": attempts,
            "best": best,
        }
    finally:
        run(down, cwd=language.example, env=env, check=False)


def write_report(results: list[dict[str, object]]) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "results.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n"
    )
    lines = [
        "# DurableCall benchmark", "",
        "Temporal Schedule backfill → endpoint Activity → graph → DurableCall Activity → result.",
        "The best complete run is reported, matching the normal benchmark policy.", "",
        "| Language | Cores | Jobs | Runs | Jobs/s | Seconds |", "|---|---:|---:|---:|---:|---:|",
    ]
    for result in sorted(results, key=lambda item: -float(item["best"]["jobsPerSecond"])):  # type: ignore[index]
        best = result["best"]  # type: ignore[assignment]
        lines.append(
            f"| {result['language']} | {result['cores']} | {best['jobs']} | "  # type: ignore[index]
            f"{len(result['runs'])} | {float(best['jobsPerSecond']):.2f} | "  # type: ignore[arg-type,index]
            f"{float(best['seconds']):.3f} |"  # type: ignore[arg-type,index]
        )
    (ARTIFACTS / "results.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", action="append", choices=sorted(LANGUAGES))
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--jobs", type=int, default=1_000)
    parser.add_argument("--warmup-jobs", type=int, default=50)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    for name, value in (
        ("cores", args.cores), ("jobs", args.jobs),
        ("runs", args.runs), ("timeout", args.timeout),
    ):
        if value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.warmup_jobs < 0:
        parser.error("--warmup-jobs must be non-negative")
    tooling_lock.acquire()
    results = [
        benchmark_language(
            LANGUAGES[name], cores=args.cores, jobs=args.jobs,
            warmup_jobs=args.warmup_jobs, runs=args.runs,
            skip_build=args.skip_build, timeout=args.timeout,
        )
        for name in (args.language or list(LANGUAGES))
    ]
    write_report(results)
    print(f"DurableCall benchmark passed: {', '.join(args.language or LANGUAGES)}")
    print(f"Results: {ARTIFACTS / 'results.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
