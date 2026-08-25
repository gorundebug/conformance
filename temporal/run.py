#!/usr/bin/env python3
"""Cron and Temporal symmetric endpoint conformance."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFORMANCE = Path(__file__).resolve().parents[1]
ROOT = (
    Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE.parent))
    .expanduser()
    .resolve()
)
ARTIFACTS = CONFORMANCE / ".artifacts" / "temporal"
ACTIVITY_SCHEDULE_ID = "example-automation-activity-schedule"
WORKFLOW_SCHEDULE_ID = "example-automation-workflow-schedule"
ACTIVITY_SCHEDULE_ENDPOINT_NAME = "Temporal Activity Schedule"
WORKFLOW_SCHEDULE_ENDPOINT_NAME = "Temporal Workflow Schedule"
ACTIVITY_JOB_ENDPOINT_NAME = "Activity Job"
WORKFLOW_JOB_ENDPOINT_NAME = "Workflow Job"
FANOUT_WORKFLOW_JOB_ENDPOINT_NAME = "Fan-Out Workflow Job"
SEQUENTIAL_ACTIVITY_A_ENDPOINT_NAME = "Sequential Activity A"
SEQUENTIAL_ACTIVITY_B_ENDPOINT_NAME = "Sequential Activity B"
FANOUT_ACTIVITY_A_ENDPOINT_NAME = "Fan-Out Activity A"
FANOUT_ACTIVITY_B_ENDPOINT_NAME = "Fan-Out Activity B"
FANOUT_ACTIVITY_C_ENDPOINT_NAME = "Fan-Out Activity C"
ENDPOINT_WORKFLOW_TYPE = "servicelib.temporal-endpoint.v1"
WORKFLOW_JOB_WORKFLOW_TYPE = "temporal.endpoint.workflow_job.workflow.v1"
FANOUT_WORKFLOW_JOB_WORKFLOW_TYPE = "temporal.endpoint.fan_out_workflow_job.workflow.v1"
SCHEDULED_WORKFLOW_TYPE = "temporal.endpoint.temporal_workflow_schedule.workflow.v1"
JAEGER_URL = "http://localhost:16686"
PROMETHEUS_URL = "http://localhost:9090"
TEMPORAL_SERVER_METRICS_URL = "http://localhost:18000/metrics"
TEMPORAL_SDK_METRICS_URL = "http://localhost:19464/metrics"
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
            "go",
            ROOT / "goexample",
            ROOT / "servicelib",
            "/app/config/overrides.yaml",
        ),
        Language(
            "python",
            ROOT / "pyexample",
            ROOT / "pyservicelib",
            "/workspace/config/docker_overrides.yaml",
        ),
        Language(
            "typescript",
            ROOT / "tsexample",
            ROOT / "tsservicelib",
            "/app/config/docker_overrides.yaml",
        ),
    )
}


def environment(language: Language) -> dict[str, str]:
    env = os.environ.copy()
    env["SERVICELIB_CONFORMANCE_DIR"] = str(CONFORMANCE)
    if language.name == "go":
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    else:
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    return env


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture: bool = False,
    check: bool = True,
    announce: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if announce:
        print("-", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def write_overrides(path: Path, *, production: bool) -> None:
    environment_name = "production" if production else ""
    path.write_text(
        f"""dataConnectors:
  temporal:
    address: temporal:7233
endpoints:
  activityJob:
    enabled: true
  fanOutActivityA:
    enabled: true
  fanOutActivityB:
    enabled: true
  fanOutActivityC:
    enabled: true
  fanOutWorkflowJob:
    enabled: true
  localSchedule:
    enabled: true
    schedule: "* * * * *"
  sequentialActivityA:
    enabled: true
  sequentialActivityB:
    enabled: true
  temporalActivitySchedule:
    enabled: true
    schedule: "* * * * *"
    overlapPolicy: Allow
    tracingEnabled: true
  temporalWorkflowSchedule:
    enabled: true
    schedule: "* * * * *"
    overlapPolicy: Allow
    tracingEnabled: true
  workflowJob:
    enabled: true
services:
  automationService:
    defaultGrpcTimeout: 0
    environment: "{environment_name}"
    grpcHost: 0.0.0.0
    grpcPort: 9204
    httpHost: 0.0.0.0
    httpPort: 9094
streams:
  activityPause:
    duration: 250
  scheduledActivityPause:
    duration: 250
  scheduledWorkflowPause:
    duration: 250
  workflowPause:
    duration: 250
"""
    )


def prepare_files(language: Language) -> tuple[Path, Path]:
    directory = ARTIFACTS / language.name
    directory.mkdir(parents=True, exist_ok=True)
    overrides = directory / "automationservice.overrides.yaml"
    write_overrides(overrides, production=False)
    otlp_endpoint = (
        "otel-collector:4317"
        if language.name == "python"
        else "http://otel-collector:4317"
    )
    compose = directory / "compose.yml"
    compose.write_text(
        "services:\n"
        "  automationservice:\n"
        "    environment:\n"
        f"      OTEL_EXPORTER_OTLP_ENDPOINT: {otlp_endpoint}\n"
        '      OTEL_EXPORTER_OTLP_INSECURE: "true"\n'
        "    ports:\n"
        '      - "19464:9464"\n'
        "    depends_on:\n"
        "      - otel-collector\n"
        "    volumes:\n"
        f"      - {overrides}:{language.override_target}:ro\n"
        "  temporal:\n"
        "    ports:\n"
        '      - "18000:8000"\n'
        "  jaeger:\n"
        "    image: jaegertracing/all-in-one:1.62.0\n"
        "    environment:\n"
        '      COLLECTOR_OTLP_ENABLED: "true"\n'
        "    ports:\n"
        '      - "16686:16686"\n'
        "    networks:\n"
        "      - app_net\n"
        "  otel-collector:\n"
        "    image: otel/opentelemetry-collector-contrib:0.136.0\n"
        "    command:\n"
        "      - --config=/etc/otelcol-contrib/config.yaml\n"
        "    volumes:\n"
        "      - ${SERVICELIB_CONFORMANCE_DIR}/tracing/otel-collector.yaml:/etc/otelcol-contrib/config.yaml:ro\n"
        "    depends_on:\n"
        "      - jaeger\n"
        "    networks:\n"
        "      - app_net\n"
    )
    return overrides, compose


def compose_command(language: Language, overlay: Path, *args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        language.project,
        "--project-directory",
        str(language.example),
        "--file",
        str(language.example / "docker-compose.yml"),
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
                "make",
                "-C",
                "automationservice",
                "docker-build",
                f"PROJECT_DIR={language.example}",
            ],
            cwd=language.example,
            env=env,
        )
        return
    run(
        compose_command(language, overlay, "build", "automationservice"),
        cwd=language.example,
        env=env,
    )


def verify_go_workflowcheck(language: Language, env: dict[str, str]) -> bool:
    if language.name != "go":
        return False
    tool = ARTIFACTS / "tools" / "workflowcheck-v0.5.0"
    if not tool.exists():
        tool.parent.mkdir(parents=True, exist_ok=True)
        install_env = env.copy()
        install_env["GOBIN"] = str(tool.parent)
        run(
            [
                "go",
                "install",
                "go.temporal.io/sdk/contrib/tools/workflowcheck@v0.5.0",
            ],
            cwd=language.example,
            env=install_env,
        )
        installed = tool.parent / "workflowcheck"
        if not installed.exists():
            raise RuntimeError("workflowcheck v0.5.0 was not installed")
        installed.rename(tool)
    workspace = ARTIFACTS / "go-workflowcheck.work"
    go_version = "1.25.4"
    match = re.search(
        r"^go\s+(\S+)$",
        (language.example / "go.work").read_text(),
        flags=re.MULTILINE,
    )
    if match:
        go_version = match.group(1)
    workspace.write_text(
        f"go {go_version}\n\nuse (\n"
        f"\t{language.example / 'automationservice'}\n"
        f"\t{language.runtime}\n"
        ")\n"
    )
    check_env = env.copy()
    check_env["GOWORK"] = str(workspace)
    check_env["GOCACHE"] = str(ARTIFACTS / "go-build-cache")
    run(
        [str(tool), "./..."],
        cwd=language.example / "automationservice",
        env=check_env,
    )
    return True


def diagnostics(language: Language, overlay: Path, env: dict[str, str]) -> str:
    result = run(
        compose_command(
            language,
            overlay,
            "logs",
            "--no-color",
            "--tail",
            "160",
            "automationservice",
            "temporal",
        ),
        cwd=language.example,
        env=env,
        capture=True,
        check=False,
        announce=False,
    )
    return (result.stdout + result.stderr).strip() or "logs unavailable"


def wait_status(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    timeout: float = 90,
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


def verify_metrics(text: str, jobs: int) -> dict[str, float]:
    expected = {
        "localCronMessages": (
            "datasource_endpoint_messages_total",
            {"connector": "Local Cron", "endpoint": "Local Schedule"},
            1,
        ),
        "temporalActivityScheduleMessages": (
            "datasource_endpoint_messages_total",
            {
                "connector": "Temporal",
                "endpoint": ACTIVITY_SCHEDULE_ENDPOINT_NAME,
            },
            jobs,
        ),
        "activityJobInputs": (
            "datasource_endpoint_messages_total",
            {"connector": "Temporal", "endpoint": ACTIVITY_JOB_ENDPOINT_NAME},
            1,
        ),
        "activityJobSubmissions": (
            "datasink_endpoint_messages_total",
            {"connector": "Temporal", "endpoint": ACTIVITY_JOB_ENDPOINT_NAME},
            1,
        ),
        "workflowJobSubmissions": (
            "datasink_endpoint_messages_total",
            {"connector": "Temporal", "endpoint": WORKFLOW_JOB_ENDPOINT_NAME},
            1,
        ),
        "fanOutWorkflowJobSubmissions": (
            "datasink_endpoint_messages_total",
            {
                "connector": "Temporal",
                "endpoint": FANOUT_WORKFLOW_JOB_ENDPOINT_NAME,
            },
            1,
        ),
        "sequentialActivityAInputs": (
            "datasource_endpoint_messages_total",
            {
                "connector": "Temporal",
                "endpoint": SEQUENTIAL_ACTIVITY_A_ENDPOINT_NAME,
            },
            1,
        ),
        "sequentialActivityBInputs": (
            "datasource_endpoint_messages_total",
            {
                "connector": "Temporal",
                "endpoint": SEQUENTIAL_ACTIVITY_B_ENDPOINT_NAME,
            },
            1,
        ),
        "fanOutActivityAInputs": (
            "datasource_endpoint_messages_total",
            {
                "connector": "Temporal",
                "endpoint": FANOUT_ACTIVITY_A_ENDPOINT_NAME,
            },
            1,
        ),
        "fanOutActivityBInputs": (
            "datasource_endpoint_messages_total",
            {
                "connector": "Temporal",
                "endpoint": FANOUT_ACTIVITY_B_ENDPOINT_NAME,
            },
            1,
        ),
        "fanOutActivityCInputs": (
            "datasource_endpoint_messages_total",
            {
                "connector": "Temporal",
                "endpoint": FANOUT_ACTIVITY_C_ENDPOINT_NAME,
            },
            1,
        ),
        "activityHeartbeats": (
            "temporal_activity_events_total",
            {"connector": "Temporal", "boundary": "endpoint", "event": "heartbeat"},
            1,
        ),
    }
    result: dict[str, float] = {}
    for name, (metric, labels, minimum) in expected.items():
        value = metric_value(text, metric, labels)
        if value < minimum:
            raise RuntimeError(f"{metric}{labels}={value}, expected at least {minimum}")
        result[name] = value
    return result


def prometheus_query(expression: str) -> list[dict[str, object]]:
    encoded = urllib.parse.urlencode({"query": expression})
    payload = json.loads(fetch_text(f"{PROMETHEUS_URL}/api/v1/query?{encoded}"))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload!r}")
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        raise TypeError(f"Prometheus query returned invalid data: {payload!r}")
    return result


def exported_metric_names(text: str) -> list[str]:
    return sorted(
        {
            line.split("{", 1)[0].split(" ", 1)[0]
            for line in text.splitlines()
            if line and not line.startswith("#")
        }
    )


def verify_temporal_metric_sources(
    output: Path,
    timeout: float = 45,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            server = fetch_text(TEMPORAL_SERVER_METRICS_URL)
            sdk = fetch_text(TEMPORAL_SDK_METRICS_URL)
            output.mkdir(parents=True, exist_ok=True)
            (output / "temporal-server.metrics.txt").write_text(server)
            (output / "temporal-sdk.metrics.txt").write_text(sdk)
            required_sdk = (
                ("temporal_worker_task_slots_available",),
                (
                    "temporal_activity_execution_latency_seconds_bucket",
                    "temporal_activity_execution_latency_bucket",
                ),
                (
                    "temporal_activity_schedule_to_start_latency_seconds_bucket",
                    "temporal_activity_schedule_to_start_latency_bucket",
                ),
            )
            if "service_requests" not in server:
                raise RuntimeError("official Temporal Server metrics are absent")
            missing = [
                alternatives
                for alternatives in required_sdk
                if not any(metric in sdk for metric in alternatives)
            ]
            if missing:
                raise RuntimeError(
                    f"official Temporal SDK metrics are absent: {missing}"
                )
            if re.search(r"servicelib_.*durable.*latency", sdk):
                raise RuntimeError(
                    "Temporal latency was duplicated as a ServiceLib metric"
                )
            server_up = prometheus_query('up{telemetry_source="temporal-server"} == 1')
            sdk_up = prometheus_query('up{telemetry_source="temporal-sdk"} == 1')
            if not server_up or not sdk_up:
                raise RuntimeError(
                    "Prometheus has not scraped both Temporal metric owners yet"
                )
            return {
                "serverSeriesPresent": True,
                "sdkSeriesPresent": True,
                "serverTargetsUp": len(server_up),
                "sdkTargetsUp": len(sdk_up),
                "duplicateServiceLibLatency": False,
                "serverMetricNames": exported_metric_names(server),
                "sdkMetricNames": exported_metric_names(sdk),
            }
        except (
            OSError,
            urllib.error.URLError,
            json.JSONDecodeError,
            RuntimeError,
        ) as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"Temporal metric sources did not become ready: {last_error}")


def edge_calls(status: dict[str, object], source: str, target: str) -> int:
    nodes = status.get("nodes")
    edges = status.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise TypeError("/status/data has no nodes/edges arrays")
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


def wait_edge_calls(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    source: str,
    target: str,
    *,
    timeout: float = 30,
) -> int:
    """Wait until the restarted service has published its generated graph."""
    deadline = time.monotonic() + timeout
    last_error: RuntimeError | None = None
    last_status: dict[str, object] = {}
    while time.monotonic() < deadline:
        status = wait_status(language, overlay, env, timeout=5)
        last_status = status
        try:
            return edge_calls(status, source, target)
        except RuntimeError as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError(
        f"status graph did not become ready for {source!r}->{target!r}: "
        f"{last_error}; last status={json.dumps(last_status, sort_keys=True)}\n"
        f"{diagnostics(language, overlay, env)}"
    )


def trigger_schedule(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    schedule_id: str,
    count: int,
) -> None:
    for index in range(count):
        run(
            compose_command(
                language,
                overlay,
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "temporal",
                "temporal-create-namespace",
                "schedule",
                "trigger",
                "--address",
                "temporal:7233",
                "--namespace",
                "default",
                "--schedule-id",
                schedule_id,
                "--overlap-policy",
                "AllowAll",
            ),
            cwd=language.example,
            env=env,
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
            language,
            overlay,
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "temporal",
            "temporal-create-namespace",
            *arguments,
            "--address",
            "temporal:7233",
            "--namespace",
            "default",
        ),
        cwd=language.example,
        env=env,
        capture=capture,
        check=check,
    )


def verify_schedule_reuse(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> dict[str, str]:
    schedules = {
        ACTIVITY_SCHEDULE_ID: ENDPOINT_WORKFLOW_TYPE,
        WORKFLOW_SCHEDULE_ID: SCHEDULED_WORKFLOW_TYPE,
    }
    descriptions: dict[str, str] = {}
    for schedule_id, workflow_type in schedules.items():
        result = temporal_cli(
            language,
            overlay,
            env,
            "schedule",
            "describe",
            "--schedule-id",
            schedule_id,
            "--output",
            "json",
            capture=True,
        )
        description = result.stdout
        if workflow_type not in description:
            raise RuntimeError(
                f"Temporal Schedule {schedule_id!r} does not reference "
                f"Workflow type {workflow_type!r}"
            )
        if (
            "servicelib.managedBy" not in description
            or "servicelib.owner" not in description
            or "servicelib.callId" not in description
        ):
            raise RuntimeError(
                f"Temporal Schedule {schedule_id!r} ownership memo is absent"
            )
        descriptions[schedule_id] = description
    return descriptions


def verify_schedule_ownership_collision(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> str:
    run(
        compose_command(language, overlay, "stop", "automationservice"),
        cwd=language.example,
        env=env,
    )
    temporal_cli(
        language,
        overlay,
        env,
        "schedule",
        "delete",
        "--schedule-id",
        ACTIVITY_SCHEDULE_ID,
    )
    temporal_cli(
        language,
        overlay,
        env,
        "schedule",
        "create",
        "--schedule-id",
        ACTIVITY_SCHEDULE_ID,
        "--cron",
        "0 0 1 1 *",
        "--time-zone",
        "UTC",
        "--workflow-id",
        "conformance/foreign-schedule",
        "--type",
        ENDPOINT_WORKFLOW_TYPE,
        "--task-queue",
        "automation-activity-schedules",
        "--paused",
        "--schedule-memo",
        'servicelib.managedBy="foreign"',
        "--schedule-memo",
        'servicelib.owner="foreign"',
        "--schedule-memo",
        f'servicelib.callId="{ACTIVITY_SCHEDULE_ID}"',
    )
    try:
        result = run(
            compose_command(
                language,
                overlay,
                "run",
                "--rm",
                "--no-deps",
                "automationservice",
            ),
            cwd=language.example,
            env=env,
            capture=True,
            check=False,
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
    language: Language,
    overlay: Path,
    env: dict[str, str],
    endpoint_name: str,
) -> str:
    result = temporal_cli(
        language,
        overlay,
        env,
        "workflow",
        "list",
        "--query",
        f'WorkflowType="{ENDPOINT_WORKFLOW_TYPE}" AND ExecutionStatus="Running"',
        "--output",
        "json",
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
        elif isinstance(item, str) and item.startswith(
            "temporal/schedule/"
            + re.sub(r"[^a-z0-9]+", "_", endpoint_name.lower()).strip("_")
            + "-"
        ):
            candidates.append(item)

    visit(value)
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise RuntimeError(
            "expected exactly one queued scheduled Workflow, found " + repr(unique)
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
            language,
            overlay,
            env,
            "workflow",
            "describe",
            "--workflow-id",
            workflow_id,
            "--output",
            "json",
            capture=True,
        )
        last = result.stdout
        if "CANCELED" in last.upper() or "CANCELLED" in last.upper():
            return last
        time.sleep(0.5)
    raise RuntimeError(f"Workflow {workflow_id} was not canceled\n{last}")


def verify_queued_cancellation(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> str:
    # Pause regular admission before the manual firing. Otherwise a test that
    # crosses a minute boundary can observe an unrelated scheduled Workflow
    # and incorrectly attribute its graph activation to the canceled firing.
    temporal_cli(
        language,
        overlay,
        env,
        "schedule",
        "toggle",
        "--schedule-id",
        ACTIVITY_SCHEDULE_ID,
        "--pause",
    )
    trigger_schedule(language, overlay, env, ACTIVITY_SCHEDULE_ID, 1)
    workflow_id = running_scheduled_workflow_id(
        language, overlay, env, ACTIVITY_SCHEDULE_ENDPOINT_NAME
    )
    temporal_cli(
        language,
        overlay,
        env,
        "workflow",
        "cancel",
        "--workflow-id",
        workflow_id,
    )
    run(
        compose_command(
            language,
            overlay,
            "up",
            "--detach",
            "--no-deps",
            "automationservice",
        ),
        cwd=language.example,
        env=env,
    )
    canceled = wait_workflow_canceled(language, overlay, env, workflow_id)
    if (
        wait_edge_calls(
            language,
            overlay,
            env,
            ACTIVITY_SCHEDULE_ENDPOINT_NAME,
            "Scheduled Activity Pause",
        )
        != 0
    ):
        raise RuntimeError("canceled Workflow activated the Temporal input graph")
    run(
        compose_command(language, overlay, "stop", "automationservice"),
        cwd=language.example,
        env=env,
    )
    temporal_cli(
        language,
        overlay,
        env,
        "schedule",
        "toggle",
        "--schedule-id",
        ACTIVITY_SCHEDULE_ID,
        "--unpause",
    )
    return canceled


def workflow_list(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> str:
    result = run(
        compose_command(
            language,
            overlay,
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "temporal",
            "temporal-create-namespace",
            "workflow",
            "list",
            "--address",
            "temporal:7233",
            "--namespace",
            "default",
        ),
        cwd=language.example,
        env=env,
        capture=True,
    )
    return result.stdout


def verify_continue_as_new(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> str:
    for workflow_id in endpoint_workflow_ids(
        language,
        overlay,
        env,
        WORKFLOW_JOB_WORKFLOW_TYPE,
        "workflow_job",
    ):
        history = workflow_history(language, overlay, env, workflow_id)
        if "CONTINUE_AS_NEW_INITIATOR_WORKFLOW" in history:
            return history
    raise RuntimeError("Workflow Job did not execute Continue-As-New")


def endpoint_workflow_ids(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    workflow_type: str,
    endpoint_identity: str,
) -> list[str]:
    result = temporal_cli(
        language,
        overlay,
        env,
        "workflow",
        "list",
        "--query",
        f'WorkflowType="{workflow_type}"',
        "--output",
        "json",
        capture=True,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Temporal workflow list returned invalid JSON") from error
    prefix = f"temporal/endpoint/{endpoint_identity}/"
    workflow_ids: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    key in {"workflowId", "workflow_id"}
                    and isinstance(child, str)
                    and child.startswith(prefix)
                ):
                    workflow_ids.append(child)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return list(dict.fromkeys(workflow_ids))


def workflow_history(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    workflow_id: str,
) -> str:
    return temporal_cli(
        language,
        overlay,
        env,
        "workflow",
        "show",
        "--workflow-id",
        workflow_id,
        "--output",
        "json",
        capture=True,
    ).stdout


def decoded_history_text(history: str) -> str:
    """Expose nested Temporal Payload data without depending on one SDK codec."""
    try:
        root: object = json.loads(history)
    except json.JSONDecodeError as error:
        raise RuntimeError("Temporal workflow history returned invalid JSON") from error
    fragments: list[str] = []
    visited_payloads: set[bytes] = set()

    def visit_bytes(value: bytes) -> None:
        if not value or value in visited_payloads:
            return
        visited_payloads.add(value)
        decoded = value.decode("utf-8", errors="ignore")
        if decoded:
            fragments.append(decoded)
            try:
                visit(json.loads(decoded))
            except json.JSONDecodeError:
                pass

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            if value and all(
                isinstance(child, int) and 0 <= child <= 255 for child in value
            ):
                visit_bytes(bytes(value))
            for child in value:
                visit(child)
        elif isinstance(value, str):
            fragments.append(value)
            try:
                visit_bytes(base64.b64decode(value, validate=True))
            except (ValueError, TypeError):
                pass

    visit(root)
    return "\n".join(fragments)


def wait_composed_workflow(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    workflow_type: str,
    endpoint_identity: str,
    required_fragments: tuple[str, ...],
    timeout: float = 90,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    last_error = "workflow was not created"
    while time.monotonic() < deadline:
        for workflow_id in endpoint_workflow_ids(
            language, overlay, env, workflow_type, endpoint_identity
        ):
            description = temporal_cli(
                language,
                overlay,
                env,
                "workflow",
                "describe",
                "--workflow-id",
                workflow_id,
                "--output",
                "json",
                capture=True,
            ).stdout
            if workflow_execution_status(description) != (
                "WORKFLOW_EXECUTION_STATUS_COMPLETED"
            ):
                last_error = f"{workflow_id} is not completed"
                continue
            history = workflow_history(language, overlay, env, workflow_id)
            decoded = decoded_history_text(history)
            missing = [
                fragment for fragment in required_fragments if fragment not in decoded
            ]
            if not missing:
                return workflow_id, history
            last_error = f"{workflow_id} is missing payload markers {missing!r}"
        time.sleep(0.5)
    raise RuntimeError(
        f"Temporal composition {workflow_type!r} was not observed: {last_error}"
    )


def verify_workflow_composition(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> dict[str, object]:
    sequential_id, sequential_history = wait_composed_workflow(
        language,
        overlay,
        env,
        WORKFLOW_JOB_WORKFLOW_TYPE,
        "workflow_job",
        (
            "temporal.endpoint.sequential_activity_a.v1",
            "temporal.endpoint.sequential_activity_b.v1",
            "sequential:a:local:",
            "sequential:b:sequential:a:local:",
            "workflow:processed:sequential:b:sequential:a:",
        ),
    )
    fanout_id, fanout_history = wait_composed_workflow(
        language,
        overlay,
        env,
        FANOUT_WORKFLOW_JOB_WORKFLOW_TYPE,
        "fan_out_workflow_job",
        (
            "temporal.endpoint.fan_out_activity_a.v1",
            "temporal.endpoint.fan_out_activity_b.v1",
            "temporal.endpoint.fan_out_activity_c.v1",
            "fanout:a:local:",
            "fanout:b:fanout:a:local:",
            "fanout:c:fanout:a:local:",
        ),
    )
    output = ARTIFACTS / language.name
    (output / "sequential-workflow.json").write_text(sequential_history)
    (output / "fanout-workflow.json").write_text(fanout_history)
    return {
        "sequentialWorkflowId": sequential_id,
        "sequentialActivities": 2,
        "fanOutWorkflowId": fanout_id,
        "fanOutActivities": 3,
        "typedPayloadsVerified": True,
    }


def wait_workflow_completed(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    workflow_id: str,
    timeout: float = 60,
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = temporal_cli(
            language,
            overlay,
            env,
            "workflow",
            "describe",
            "--workflow-id",
            workflow_id,
            "--output",
            "json",
            capture=True,
        )
        last = result.stdout
        status = workflow_execution_status(last)
        if status == "WORKFLOW_EXECUTION_STATUS_COMPLETED":
            return last
        if status in {
            "WORKFLOW_EXECUTION_STATUS_FAILED",
            "WORKFLOW_EXECUTION_STATUS_CANCELED",
            "WORKFLOW_EXECUTION_STATUS_TERMINATED",
            "WORKFLOW_EXECUTION_STATUS_TIMED_OUT",
        }:
            raise RuntimeError(
                f"traced Workflow {workflow_id} ended with {status}\n{last}"
            )
        time.sleep(0.5)
    raise RuntimeError(f"traced Workflow {workflow_id} did not complete\n{last}")


def workflow_execution_status(description: str) -> str:
    try:
        value = json.loads(description)
    except json.JSONDecodeError:
        return ""
    statuses: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    key == "status"
                    and isinstance(child, str)
                    and child.startswith("WORKFLOW_EXECUTION_STATUS_")
                ):
                    statuses.append(child)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return statuses[0] if statuses else ""


def schedule_endpoint_id(schedule_description: str) -> int:
    try:
        value = json.loads(schedule_description)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Temporal schedule description returned invalid JSON"
        ) from error
    endpoint_ids: set[int] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            metadata = item.get("metadata")
            data = item.get("data")
            if isinstance(metadata, dict) and isinstance(data, str):
                encoding = metadata.get("encoding")
                if isinstance(encoding, str):
                    try:
                        if base64.b64decode(encoding).decode() == "json/plain":
                            visit(json.loads(base64.b64decode(data)))
                    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                        pass
            for key, child in item.items():
                if key in ("endpointId", "endpoint_id") and isinstance(child, int):
                    endpoint_ids.add(child)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if len(endpoint_ids) != 1:
        raise RuntimeError(
            "expected exactly one Temporal Schedule endpoint id, found "
            + repr(sorted(endpoint_ids))
        )
    return next(iter(endpoint_ids))


def schedule_workflow_request(schedule_description: str) -> dict[str, object]:
    """Extract the SDK-specific action argument without duplicating its wire ABI."""
    try:
        value = json.loads(schedule_description)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Temporal schedule description returned invalid JSON"
        ) from error
    requests: list[dict[str, object]] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if (
                "activityType" in item or "activity_type" in item
            ) and "envelope" in item:
                requests.append(item)
            metadata = item.get("metadata")
            data = item.get("data")
            if isinstance(metadata, dict) and isinstance(data, str):
                encoding = metadata.get("encoding")
                if isinstance(encoding, str):
                    try:
                        if base64.b64decode(encoding).decode() == "json/plain":
                            visit(json.loads(base64.b64decode(data)))
                    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                        pass
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    unique = {json.dumps(request, sort_keys=True): request for request in requests}
    if len(unique) != 1:
        raise RuntimeError(
            "expected exactly one Temporal Schedule action request, found "
            f"{len(unique)}"
        )
    return next(iter(unique.values()))


def fetch_trace(trace_id: str, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    latest_count = -1
    stable_polls = 0
    first_seen_at: float | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{JAEGER_URL}/api/traces/{trace_id}", timeout=3
            ) as response:
                payload = json.loads(response.read())
            data = payload.get("data", [])
            if data:
                trace = data[0]
                if not isinstance(trace, dict):
                    raise RuntimeError("Jaeger returned a non-object trace")
                latest = trace
                count = len(trace.get("spans", []))
                now = time.monotonic()
                if first_seen_at is None:
                    first_seen_at = now
                if count == latest_count:
                    stable_polls += 1
                else:
                    latest_count = count
                    stable_polls = 0
                if now - first_seen_at >= 7 and stable_polls >= 3:
                    return trace
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    if latest is not None:
        return latest
    raise RuntimeError(f"Temporal trace {trace_id} was not exported to Jaeger")


def span_tags(span: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in span.get("tags", []):
        if isinstance(tag, dict) and isinstance(tag.get("key"), str):
            result[str(tag["key"])] = str(tag.get("value", ""))
    return result


def span_parent(span: dict[str, Any]) -> str | None:
    for reference in span.get("references", []):
        if not isinstance(reference, dict):
            continue
        parent = reference.get("spanID")
        if isinstance(parent, str) and parent:
            return parent
    return None


def span_events(span: dict[str, Any]) -> list[str]:
    events: list[str] = []
    for log in span.get("logs", []):
        if not isinstance(log, dict):
            continue
        for field in log.get("fields", []):
            if (
                isinstance(field, dict)
                and field.get("key") == "event"
                and isinstance(field.get("value"), str)
            ):
                events.append(str(field["value"]))
    return events


def is_descendant(
    child: dict[str, Any],
    ancestor: dict[str, Any],
    spans: dict[str, dict[str, Any]],
) -> bool:
    ancestor_id = str(ancestor.get("spanID", ""))
    parent = span_parent(child)
    visited: set[str] = set()
    while parent and parent not in visited:
        if parent == ancestor_id:
            return True
        visited.add(parent)
        parent_span = spans.get(parent)
        if parent_span is None:
            return False
        parent = span_parent(parent_span)
    return False


def verify_temporal_trace(trace: dict[str, Any]) -> dict[str, object]:
    raw_spans = trace.get("spans", [])
    spans = [span for span in raw_spans if isinstance(span, dict)]
    by_id = {
        str(span.get("spanID")): span
        for span in spans
        if isinstance(span.get("spanID"), str)
    }

    def matching(operation: str, stream_or_endpoint: str) -> list[dict[str, Any]]:
        return [
            span
            for span in spans
            if str(span.get("operationName", "")).lower() == operation
            and stream_or_endpoint
            in {
                span_tags(span).get("stream"),
                span_tags(span).get("endpoint"),
            }
        ]

    schedule_inputs = matching("temporal.input", ACTIVITY_SCHEDULE_ENDPOINT_NAME)
    process_maps = matching("stream.map", "Process Scheduled Activity")
    delay_spans = matching("stream.delay", "Scheduled Activity Pause")
    if not schedule_inputs:
        raise RuntimeError("Temporal trace has no scheduled endpoint input span")
    heartbeat_inputs = [
        span
        for span in schedule_inputs
        if "temporal.activity.heartbeat" in span_events(span)
    ]
    if not heartbeat_inputs:
        raise RuntimeError(
            "Temporal heartbeat is not attached to its scheduled endpoint input span"
        )
    if not delay_spans or not process_maps:
        raise RuntimeError(
            "Temporal trace does not execute the scheduled Activity graph"
        )
    if not any(
        is_descendant(child, parent, by_id)
        for parent in schedule_inputs
        for child in delay_spans
    ):
        raise RuntimeError("scheduled Delay did not preserve the trace parent")
    if not any(
        is_descendant(child, parent, by_id)
        for parent in delay_spans
        for child in process_maps
    ):
        raise RuntimeError("scheduled Map is not a descendant of Delay")
    return {
        "spanCount": len(spans),
        "scheduleInputSpans": len(schedule_inputs),
        "heartbeatInputSpans": len(heartbeat_inputs),
        "endpointActivitySpans": len(schedule_inputs),
        "scheduledDelaySpans": len(delay_spans),
        "scheduledMapSpans": len(process_maps),
    }


def verify_tracing(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    overrides: Path,
    schedule_descriptions: dict[str, str],
) -> tuple[dict[str, object], str, dict[str, Any]]:
    temporal_cli(
        language,
        overlay,
        env,
        "schedule",
        "toggle",
        "--schedule-id",
        ACTIVITY_SCHEDULE_ID,
        "--pause",
    )
    run(
        compose_command(language, overlay, "stop", "automationservice"),
        cwd=language.example,
        env=env,
    )
    write_overrides(overrides, production=True)
    run(
        compose_command(
            language,
            overlay,
            "up",
            "--detach",
            "--no-deps",
            "--force-recreate",
            "automationservice",
        ),
        cwd=language.example,
        env=env,
    )
    wait_status(language, overlay, env)
    trace_id = secrets.token_hex(16)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    workflow_id = f"conformance/trace/{language.name}-{timestamp}"
    schedule_description = schedule_descriptions[ACTIVITY_SCHEDULE_ID]
    request = schedule_workflow_request(schedule_description)
    temporal_cli(
        language,
        overlay,
        env,
        "workflow",
        "start",
        "--workflow-id",
        workflow_id,
        "--type",
        ENDPOINT_WORKFLOW_TYPE,
        "--task-queue",
        "automation-activity-schedules",
        "--execution-timeout",
        "60s",
        "--input",
        json.dumps(request, separators=(",", ":")),
        "--headers",
        f'traceparent="00-{trace_id}-0123456789abcdef-01"',
        "--headers",
        'x-trace="1"',
        "--headers",
        f'x-stream-id="{workflow_id}"',
    )
    description = wait_workflow_completed(language, overlay, env, workflow_id)
    trace = fetch_trace(trace_id)
    (ARTIFACTS / language.name / "trace.raw.json").write_text(
        json.dumps(trace, indent=2, sort_keys=True) + "\n"
    )
    return verify_temporal_trace(trace), description, trace


def wait_graph(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    jobs: int,
    timeout: float = 120,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = wait_status(language, overlay, env, timeout=5)
        if (
            edge_calls(
                last,
                ACTIVITY_SCHEDULE_ENDPOINT_NAME,
                "Scheduled Activity Pause",
            )
            >= jobs
            and edge_calls(
                last, "Scheduled Activity Pause", "Process Scheduled Activity"
            )
            >= jobs
        ):
            return last
        time.sleep(0.5)
    raise RuntimeError(
        f"only partial Temporal graph execution after {jobs} queued jobs\n"
        + diagnostics(language, overlay, env)
    )


def wait_local_cron(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    timeout: float = 75,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = wait_status(language, overlay, env, timeout=5)
        if (
            edge_calls(last, "Local Schedule", "Split On-Demand Jobs") >= 1
            and edge_calls(last, "Split On-Demand Jobs", "Submit Activity Job") >= 1
            and edge_calls(last, "Split On-Demand Jobs", "Submit Fan-Out Workflow Job")
            >= 1
            and edge_calls(last, "Split On-Demand Jobs", "Submit Workflow Job") >= 1
            and edge_calls(last, "Consume Activity Job", "Activity Pause") >= 1
            and edge_calls(last, "Activity Pause", "Process Activity Job") >= 1
            and edge_calls(last, "Submit Activity Job", "Observe Activity Result") >= 1
            and edge_calls(
                last, "Consume Sequential Activity A", "Process Sequential Activity A"
            )
            >= 1
            and edge_calls(
                last, "Consume Sequential Activity B", "Process Sequential Activity B"
            )
            >= 1
            and edge_calls(
                last, "Consume Fan-Out Activity A", "Process Fan-Out Activity A"
            )
            >= 1
            and edge_calls(
                last, "Consume Fan-Out Activity B", "Process Fan-Out Activity B"
            )
            >= 1
            and edge_calls(
                last, "Consume Fan-Out Activity C", "Process Fan-Out Activity C"
            )
            >= 1
        ):
            return last
        time.sleep(0.5)
    raise RuntimeError(
        "local cron did not activate its configured input within one minute\n"
        + diagnostics(language, overlay, env)
    )


def exercise(language: Language, *, skip_build: bool, jobs: int) -> dict[str, object]:
    overrides, overlay = prepare_files(language)
    env = environment(language)
    workflowcheck = verify_go_workflowcheck(language, env)
    if not skip_build:
        build(language, overlay, env)
    down = compose_command(
        language,
        overlay,
        "down",
        "--volumes",
        "--remove-orphans",
    )
    run(down, cwd=language.example, env=env, check=False)
    try:
        run(
            compose_command(
                language,
                overlay,
                "up",
                "--detach",
                "temporal-postgresql",
                "temporal-schema",
                "temporal",
                "temporal-create-namespace",
                "temporal-ui",
                "jaeger",
                "otel-collector",
                "prometheus",
                "automationservice",
            ),
            cwd=language.example,
            env=env,
        )
        wait_status(language, overlay, env)

        # Schedule state belongs to Temporal, not to the Worker process. Stop
        # admission, enqueue more jobs than the configured two Activity slots,
        # then prove the same graph consumes the durable backlog after restart.
        run(
            compose_command(language, overlay, "stop", "automationservice"),
            cwd=language.example,
            env=env,
        )
        canceled = verify_queued_cancellation(language, overlay, env)
        trigger_schedule(language, overlay, env, ACTIVITY_SCHEDULE_ID, jobs)
        trigger_schedule(language, overlay, env, WORKFLOW_SCHEDULE_ID, jobs)
        run(
            compose_command(
                language,
                overlay,
                "up",
                "--detach",
                "--no-deps",
                "automationservice",
            ),
            cwd=language.example,
            env=env,
        )
        status = wait_graph(language, overlay, env, jobs)
        status = wait_local_cron(language, overlay, env)
        workflow_composition = verify_workflow_composition(language, overlay, env)
        continue_as_new = verify_continue_as_new(language, overlay, env)
        schedule_description = verify_schedule_reuse(language, overlay, env)
        workflows = workflow_list(language, overlay, env)
        if workflows.count(ENDPOINT_WORKFLOW_TYPE) < jobs:
            raise RuntimeError("scheduled Activity Workflow executions are missing")
        if workflows.count(SCHEDULED_WORKFLOW_TYPE) < jobs:
            raise RuntimeError("scheduled direct Workflow executions are missing")
        metrics_text = fetch_text("http://localhost:9094/metrics")
        (ARTIFACTS / language.name / "automationservice.metrics.txt").write_text(
            metrics_text
        )
        metrics = verify_metrics(metrics_text, jobs)
        temporal_metrics = verify_temporal_metric_sources(ARTIFACTS / language.name)
        trace_summary, traced_workflow, trace = verify_tracing(
            language, overlay, env, overrides, schedule_description
        )
        result = {
            "status": "pass",
            "queuedJobs": jobs,
            "activitySlots": 2,
            "localCronCalls": edge_calls(
                status, "Local Schedule", "Split On-Demand Jobs"
            ),
            "temporalScheduleCalls": edge_calls(
                status,
                ACTIVITY_SCHEDULE_ENDPOINT_NAME,
                "Scheduled Activity Pause",
            ),
            "workflowComposition": workflow_composition,
            "continueAsNew": True,
            "workflowcheck": workflowcheck,
            "metrics": metrics,
            "temporalMetrics": temporal_metrics,
            "scheduleReuse": True,
            "queuedCancellation": True,
            "traceContinuity": trace_summary,
        }
        (ARTIFACTS / language.name / "workflows.txt").write_text(workflows)
        (ARTIFACTS / language.name / "status.json").write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n"
        )
        (ARTIFACTS / language.name / "schedule.json").write_text(
            json.dumps(schedule_description, indent=2, sort_keys=True) + "\n"
        )
        (ARTIFACTS / language.name / "canceled-workflow.json").write_text(canceled)
        (ARTIFACTS / language.name / "continue-as-new-history.json").write_text(
            continue_as_new
        )
        (ARTIFACTS / language.name / "traced-workflow.json").write_text(traced_workflow)
        (ARTIFACTS / language.name / "trace.json").write_text(
            json.dumps(trace, indent=2, sort_keys=True) + "\n"
        )
        collision = verify_schedule_ownership_collision(language, overlay, env)
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
                LANGUAGES[name],
                skip_build=args.skip_build,
                jobs=args.jobs,
            )
        except Exception as error:  # noqa: BLE001 - aggregate language failures
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
