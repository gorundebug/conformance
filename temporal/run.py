#!/usr/bin/env python3
"""Temporal Schedule, queued endpoint and DurableCall conformance."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


CONFORMANCE = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "temporal"
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
        return f"servicelib-temporal-conformance-{self.name}"


LANGUAGES = {
    language.name: language
    for language in (
        Language(
            "go", ROOT / "goexample", ROOT / "servicelib",
            "/app/config/overrides.yaml",
        ),
        Language(
            "python", ROOT / "pyexample", ROOT / "pyservicelib",
            "/workspace/automationservice/config/docker_overrides.yaml",
        ),
        Language(
            "typescript", ROOT / "tsexample", ROOT / "tsservicelib",
            "/workspace/automationservice/config/docker_overrides.yaml",
        ),
    )
}


def environment(language: Language) -> dict[str, str]:
    env = os.environ.copy()
    if language.name == "go":
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    else:
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    return env


def run(
    command: list[str], *, cwd: Path, env: dict[str, str],
    capture: bool = False, check: bool = True, announce: bool = True,
) -> subprocess.CompletedProcess[str]:
    if announce:
        print("-", " ".join(command), flush=True)
    return subprocess.run(
        command, cwd=cwd, env=env, check=check, text=True,
        capture_output=capture,
    )


def prepare_files(language: Language) -> tuple[Path, Path]:
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
    enabled: true
    schedule: "* * * * *"
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
    compose = directory / "compose.yml"
    compose.write_text(
        "services:\n"
        "  automationservice:\n"
        "    volumes:\n"
        f"      - {overrides}:{language.override_target}:ro\n"
    )
    return overrides, compose


def compose_command(language: Language, overlay: Path, *args: str) -> list[str]:
    command = [
        "docker", "compose", "--project-name", language.project,
        "--project-directory", str(language.example),
        "--file", str(language.example / "docker-compose.yml"),
    ]
    for runtime_overlay in sorted(
        language.example.glob("docker-compose.*-runtime.generated.yml")
    ):
        command.extend(["--file", str(runtime_overlay)])
    command.extend(["--file", str(overlay)])
    return [*command, *args]


def build(language: Language, overlay: Path, env: dict[str, str]) -> None:
    if language.name == "go":
        run(
            [
                "make", "-C", "automationservice", "docker-build",
                f"PROJECT_DIR={language.example}",
            ],
            cwd=language.example, env=env,
        )
        return
    run(
        compose_command(language, overlay, "build", "automationservice"),
        cwd=language.example, env=env,
    )


def diagnostics(language: Language, overlay: Path, env: dict[str, str]) -> str:
    result = run(
        compose_command(
            language, overlay, "logs", "--no-color", "--tail", "160",
            "automationservice", "temporal",
        ),
        cwd=language.example, env=env, capture=True, check=False,
        announce=False,
    )
    return (result.stdout + result.stderr).strip() or "logs unavailable"


def wait_status(
    language: Language, overlay: Path, env: dict[str, str], timeout: float = 90,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                "http://localhost:9094/status/data", timeout=2
            ) as response:
                if response.status == 200:
                    value = json.loads(response.read())
                    if isinstance(value, dict):
                        return value
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(
        f"automationservice did not become ready: {last_error}\n"
        + diagnostics(language, overlay, env)
    )


def edge_calls(status: dict[str, object], source: str, target: str) -> int:
    nodes = status.get("nodes")
    edges = status.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise RuntimeError("/status/data has no nodes/edges arrays")
    ids: dict[str, object] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        label = node.get("label")
        if isinstance(label, str):
            ids[label.split("(", 1)[0]] = node.get("id")
    if source not in ids or target not in ids:
        raise RuntimeError(f"status graph has no {source!r}->{target!r} nodes")
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if edge.get("from") == ids[source] and edge.get("to") == ids[target]:
            label = edge.get("label", "")
            match = CALLS_RE.search(label if isinstance(label, str) else "")
            return int(match.group(1)) if match else 0
    raise RuntimeError(f"status graph has no {source!r}->{target!r} edge")


def trigger_schedule(
    language: Language, overlay: Path, env: dict[str, str], count: int,
) -> None:
    for _ in range(count):
        run(
            compose_command(
                language, overlay, "run", "--rm", "--no-deps",
                "--entrypoint", "temporal", "temporal-create-namespace",
                "schedule", "trigger", "--address", "temporal:7233",
                "--namespace", "default", "--schedule-id", SCHEDULE_ID,
                "--overlap-policy", "AllowAll",
            ),
            cwd=language.example, env=env,
        )


def workflow_list(
    language: Language, overlay: Path, env: dict[str, str],
) -> str:
    result = run(
        compose_command(
            language, overlay, "run", "--rm", "--no-deps",
            "--entrypoint", "temporal", "temporal-create-namespace",
            "workflow", "list", "--address", "temporal:7233",
            "--namespace", "default",
        ),
        cwd=language.example, env=env, capture=True,
    )
    return result.stdout


def wait_graph(
    language: Language, overlay: Path, env: dict[str, str], jobs: int,
    timeout: float = 120,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = wait_status(language, overlay, env, timeout=5)
        if (
            edge_calls(last, "Temporal Schedule", "Make Temporal Job") >= jobs
            and edge_calls(last, "Consume Durable Job", "Process Durable Job")
            >= jobs
            and edge_calls(last, "Process Durable Job", "Consume Durable Job")
            >= jobs
        ):
            return last
        time.sleep(0.5)
    raise RuntimeError(
        f"only partial Temporal graph execution after {jobs} queued jobs\n"
        + diagnostics(language, overlay, env)
    )


def wait_local_cron(
    language: Language, overlay: Path, env: dict[str, str], timeout: float = 75,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = wait_status(language, overlay, env, timeout=5)
        if edge_calls(last, "Local Schedule", "Make Local Job") >= 1:
            return last
        time.sleep(0.5)
    raise RuntimeError(
        "local cron did not activate its configured input within one minute\n"
        + diagnostics(language, overlay, env)
    )


def exercise(language: Language, *, skip_build: bool, jobs: int) -> dict[str, object]:
    _, overlay = prepare_files(language)
    env = environment(language)
    if not skip_build:
        build(language, overlay, env)
    down = compose_command(
        language, overlay, "down", "--volumes", "--remove-orphans",
    )
    run(down, cwd=language.example, env=env, check=False)
    try:
        run(
            compose_command(
                language, overlay, "up", "--detach",
                "temporal-postgresql", "temporal-schema", "temporal",
                "temporal-create-namespace", "temporal-ui", "automationservice",
            ),
            cwd=language.example, env=env,
        )
        wait_status(language, overlay, env)

        # Schedule state belongs to Temporal, not to the Worker process. Stop
        # admission, enqueue more jobs than the configured two Activity slots,
        # then prove the same graph consumes the durable backlog after restart.
        run(
            compose_command(language, overlay, "stop", "automationservice"),
            cwd=language.example, env=env,
        )
        trigger_schedule(language, overlay, env, jobs)
        run(
            compose_command(
                language, overlay, "up", "--detach", "--no-deps",
                "automationservice",
            ),
            cwd=language.example, env=env,
        )
        status = wait_graph(language, overlay, env, jobs)
        status = wait_local_cron(language, overlay, env)
        workflows = workflow_list(language, overlay, env)
        if workflows.count("servicegen.temporal-endpoint.v1") < jobs * 2:
            raise RuntimeError("Temporal endpoint Workflow executions are missing")
        if workflows.count("servicegen.durable-link.v1") < jobs:
            raise RuntimeError("DurableCall Workflow executions are missing")
        result = {
            "status": "pass",
            "queuedJobs": jobs,
            "activitySlots": 2,
            "localCronCalls": edge_calls(
                status, "Local Schedule", "Make Local Job"
            ),
            "temporalScheduleCalls": edge_calls(
                status, "Temporal Schedule", "Make Temporal Job"
            ),
            "durableCallActivations": edge_calls(
                status, "Consume Durable Job", "Process Durable Job"
            ),
        }
        (ARTIFACTS / language.name / "workflows.txt").write_text(workflows)
        (ARTIFACTS / language.name / "status.json").write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n"
        )
        return result
    finally:
        run(down, cwd=language.example, env=env, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", action="append", choices=sorted(LANGUAGES))
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()
    selected = args.language or list(LANGUAGES)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    implementations: dict[str, object] = {}
    failures: dict[str, str] = {}
    for name in selected:
        print(f"\n=== Temporal: {name} ===", flush=True)
        try:
            implementations[name] = exercise(
                LANGUAGES[name], skip_build=args.skip_build, jobs=args.jobs,
            )
        except Exception as error:
            failures[name] = str(error)
            print(f"ERROR: {name}: {error}", flush=True)
    summary = {
        "status": "pass" if not failures else "fail",
        "implementations": implementations,
        "failures": failures,
    }
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    if failures:
        print("Temporal conformance failed:", flush=True)
        for name, error in failures.items():
            print(f"- {name}: {error}", flush=True)
        return 1
    print("Temporal conformance passed: " + ", ".join(selected), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
