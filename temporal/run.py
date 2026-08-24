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
PROM_LABEL_RE = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')


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
            "/workspace/config/docker_overrides.yaml",
        ),
        Language(
            "typescript", ROOT / "tsexample", ROOT / "tsservicelib",
            "/app/config/docker_overrides.yaml",
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
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if announce:
        print("-", " ".join(command), flush=True)
    return subprocess.run(
        command, cwd=cwd, env=env, check=check, text=True,
        capture_output=capture, timeout=timeout,
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


def fetch_text(url: str, timeout: float = 5) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read().decode("utf-8")


def metric_value(text: str, metric: str, labels: dict[str, str]) -> float:
    for line in text.splitlines():
        if not line.startswith(metric + "{"):
            continue
        label_text, separator, value_text = line.partition("}")
        if separator == "":
            continue
        actual = {
            key: bytes(value, "utf-8").decode("unicode_escape")
            for key, value in PROM_LABEL_RE.findall(label_text)
        }
        if all(actual.get(key) == value for key, value in labels.items()):
            return float(value_text.strip().split()[0])
    raise RuntimeError(f"metric {metric}{labels} is absent")


def verify_metrics(jobs: int) -> dict[str, float]:
    text = fetch_text("http://localhost:9094/metrics")
    expected = {
        "localCronMessages": (
            "datasource_endpoint_messages_total",
            {"connector": "Local Cron", "endpoint": "Local Schedule"},
            1,
        ),
        "temporalScheduleMessages": (
            "datasource_endpoint_messages_total",
            {"connector": "Temporal", "endpoint": "Temporal Schedule"},
            jobs,
        ),
        "temporalJobInputs": (
            "datasource_endpoint_messages_total",
            {"connector": "Temporal", "endpoint": "Durable Job"},
            jobs,
        ),
        "temporalJobSubmissions": (
            "datasink_endpoint_messages_total",
            {"connector": "Temporal", "endpoint": "Durable Job"},
            jobs,
        ),
    }
    result: dict[str, float] = {}
    for name, (metric, labels, minimum) in expected.items():
        value = metric_value(text, metric, labels)
        if value < minimum:
            raise RuntimeError(
                f"{metric}{labels}={value}, expected at least {minimum}"
            )
        result[name] = value
    return result


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
    for index in range(count):
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
        # Temporal Schedule derives the started Workflow ID suffix from the
        # scheduled second. Keep distinct manual firings in distinct seconds;
        # all executions still remain queued because the Worker is stopped.
        if index + 1 < count:
            time.sleep(1.05)


def temporal_cli(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    *arguments: str,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        compose_command(
            language, overlay, "run", "--rm", "--no-deps",
            "--entrypoint", "temporal", "temporal-create-namespace",
            *arguments, "--address", "temporal:7233", "--namespace", "default",
        ),
        cwd=language.example, env=env, capture=capture, check=check,
    )


def verify_schedule_reuse(
    language: Language, overlay: Path, env: dict[str, str],
) -> str:
    result = temporal_cli(
        language, overlay, env,
        "schedule", "describe", "--schedule-id", SCHEDULE_ID,
        "--output", "json", capture=True,
    )
    description = result.stdout
    if "servicegen.temporal-endpoint.v1" not in description:
        raise RuntimeError("Temporal Schedule does not reference the endpoint Workflow")
    if (
        "servicegen.managedBy" not in description
        or "servicegen.owner" not in description
        or "servicegen.callId" not in description
    ):
        raise RuntimeError("Temporal Schedule ownership memo is absent")
    return description


def verify_schedule_ownership_collision(
    language: Language, overlay: Path, env: dict[str, str],
) -> str:
    run(
        compose_command(language, overlay, "stop", "automationservice"),
        cwd=language.example, env=env,
    )
    temporal_cli(
        language, overlay, env,
        "schedule", "delete", "--schedule-id", SCHEDULE_ID,
    )
    temporal_cli(
        language, overlay, env,
        "schedule", "create",
        "--schedule-id", SCHEDULE_ID,
        "--cron", "0 0 1 1 *",
        "--time-zone", "UTC",
        "--workflow-id", "servicegen/conformance/foreign-schedule",
        "--type", "servicegen.temporal-endpoint.v1",
        "--task-queue", "automation-schedules",
        "--paused",
        "--schedule-memo", 'servicegen.managedBy="foreign"',
        "--schedule-memo", 'servicegen.owner="foreign"',
        "--schedule-memo", f'servicegen.callId="{SCHEDULE_ID}"',
    )
    try:
        result = run(
            compose_command(
                language, overlay, "run", "--rm", "--no-deps",
                "automationservice",
            ),
            cwd=language.example, env=env, capture=True, check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "service adopted a foreign Temporal Schedule instead of rejecting it"
        ) from error
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0 or "ownership collision" not in output:
        raise RuntimeError(
            "service did not reject a foreign Temporal Schedule ownership boundary\n"
            + output
        )
    return output


def running_scheduled_workflow_id(
    language: Language, overlay: Path, env: dict[str, str],
) -> str:
    result = temporal_cli(
        language, overlay, env,
        "workflow", "list",
        "--query",
        'WorkflowType="servicegen.temporal-endpoint.v1" AND '
        'ExecutionStatus="Running"',
        "--output", "json",
        capture=True,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Temporal workflow list returned invalid JSON") from error

    candidates: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith("servicegen/schedule/"):
            candidates.append(item)

    visit(value)
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise RuntimeError(
            "expected exactly one queued scheduled Workflow, found "
            + repr(unique)
        )
    return unique[0]


def wait_workflow_canceled(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    workflow_id: str,
    timeout: float = 30,
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = temporal_cli(
            language, overlay, env,
            "workflow", "describe", "--workflow-id", workflow_id,
            "--output", "json", capture=True,
        )
        last = result.stdout
        if "CANCELED" in last.upper() or "CANCELLED" in last.upper():
            return last
        time.sleep(0.5)
    raise RuntimeError(f"Workflow {workflow_id} was not canceled\n{last}")


def verify_queued_cancellation(
    language: Language, overlay: Path, env: dict[str, str],
) -> str:
    trigger_schedule(language, overlay, env, 1)
    workflow_id = running_scheduled_workflow_id(language, overlay, env)
    temporal_cli(
        language, overlay, env,
        "workflow", "cancel", "--workflow-id", workflow_id,
    )
    temporal_cli(
        language, overlay, env,
        "schedule", "toggle", "--schedule-id", SCHEDULE_ID, "--pause",
    )
    run(
        compose_command(
            language, overlay, "up", "--detach", "--no-deps",
            "automationservice",
        ),
        cwd=language.example, env=env,
    )
    canceled = wait_workflow_canceled(
        language, overlay, env, workflow_id
    )
    status = wait_status(language, overlay, env)
    time.sleep(2)
    status = wait_status(language, overlay, env)
    if edge_calls(status, "Temporal Schedule", "Make Temporal Job") != 0:
        raise RuntimeError("canceled Workflow activated the Temporal input graph")
    run(
        compose_command(language, overlay, "stop", "automationservice"),
        cwd=language.example, env=env,
    )
    temporal_cli(
        language, overlay, env,
        "schedule", "toggle", "--schedule-id", SCHEDULE_ID, "--unpause",
    )
    return canceled


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
        canceled = verify_queued_cancellation(language, overlay, env)
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
        schedule_description = verify_schedule_reuse(language, overlay, env)
        workflows = workflow_list(language, overlay, env)
        if workflows.count("servicegen.temporal-endpoint.v1") < jobs * 2:
            raise RuntimeError("Temporal endpoint Workflow executions are missing")
        if workflows.count("servicegen.durable-link.v1") < jobs:
            raise RuntimeError("DurableCall Workflow executions are missing")
        metrics = verify_metrics(jobs)
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
            "metrics": metrics,
            "scheduleReuse": True,
            "queuedCancellation": True,
        }
        (ARTIFACTS / language.name / "workflows.txt").write_text(workflows)
        (ARTIFACTS / language.name / "status.json").write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n"
        )
        (ARTIFACTS / language.name / "schedule.json").write_text(
            schedule_description
        )
        (ARTIFACTS / language.name / "canceled-workflow.json").write_text(
            canceled
        )
        collision = verify_schedule_ownership_collision(
            language, overlay, env
        )
        (ARTIFACTS / language.name / "ownership-collision.log").write_text(
            collision + "\n"
        )
        result["ownershipCollisionRejected"] = True
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
