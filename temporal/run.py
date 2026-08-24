#!/usr/bin/env python3
"""Temporal Schedule, queued endpoint and DurableCall conformance."""

from __future__ import annotations

import argparse
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
ROOT = Path(
    os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "temporal"
SCHEDULE_ID = "example-automation-schedule"
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
    env["SERVICELIB_CONFORMANCE_DIR"] = str(CONFORMANCE)
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


def write_overrides(path: Path, *, production: bool) -> None:
    environment_name = "production" if production else ""
    path.write_text(
        f"""dataConnectors:
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
    environment: "{environment_name}"
    grpcHost: 0.0.0.0
    grpcPort: 9204
    httpHost: 0.0.0.0
    httpPort: 9094
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
        "      OTEL_EXPORTER_OTLP_INSECURE: \"true\"\n"
        "    ports:\n"
        "      - \"19464:9464\"\n"
        "    depends_on:\n"
        "      - otel-collector\n"
        "    volumes:\n"
        f"      - {overrides}:{language.override_target}:ro\n"
        "  temporal:\n"
        "    ports:\n"
        "      - \"18000:8000\"\n"
        "  jaeger:\n"
        "    image: jaegertracing/all-in-one:1.62.0\n"
        "    environment:\n"
        "      COLLECTOR_OTLP_ENABLED: \"true\"\n"
        "    ports:\n"
        "      - \"16686:16686\"\n"
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


def prometheus_query(expression: str) -> list[dict[str, object]]:
    encoded = urllib.parse.urlencode({"query": expression})
    payload = json.loads(fetch_text(f"{PROMETHEUS_URL}/api/v1/query?{encoded}"))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload!r}")
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        raise RuntimeError(f"Prometheus query returned invalid data: {payload!r}")
    return result


def exported_metric_names(text: str) -> list[str]:
    return sorted({
        line.split("{", 1)[0].split(" ", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    })


def verify_temporal_metric_sources(
    output: Path, timeout: float = 45,
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
                raise RuntimeError(f"official Temporal SDK metrics are absent: {missing}")
            if re.search(r"servicelib_.*durable.*latency", sdk):
                raise RuntimeError("Temporal latency was duplicated as a ServiceLib metric")
            server_up = prometheus_query(
                'up{telemetry_source="temporal-server"} == 1'
            )
            sdk_up = prometheus_query(
                'up{telemetry_source="temporal-sdk"} == 1'
            )
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
        except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"Temporal metric sources did not become ready: {last_error}")


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
    # Pause regular admission before the manual firing. Otherwise a test that
    # crosses a minute boundary can observe an unrelated scheduled Workflow
    # and incorrectly attribute its graph activation to the canceled firing.
    temporal_cli(
        language, overlay, env,
        "schedule", "toggle", "--schedule-id", SCHEDULE_ID, "--pause",
    )
    trigger_schedule(language, overlay, env, 1)
    workflow_id = running_scheduled_workflow_id(language, overlay, env)
    temporal_cli(
        language, overlay, env,
        "workflow", "cancel", "--workflow-id", workflow_id,
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


def durable_link_identity(workflows: str) -> tuple[int, int, int]:
    matches = {
        (int(service), int(source), int(target))
        for service, source, target in re.findall(
            r"servicegen/durable/(\d+)/(\d+)/(\d+)/", workflows
        )
    }
    if len(matches) != 1:
        raise RuntimeError(
            "expected exactly one DurableCall link identity, found "
            + repr(sorted(matches))
        )
    return next(iter(matches))


def invalid_durable_request(
    language: Language, service: int, source: int, target: int,
) -> dict[str, object]:
    activity_type = f"servicegen.durable.{service}.{source}.{target}.v1"
    if language.name == "python":
        return {
            "activity_type": activity_type,
            "activity_start_to_close_millis": 2_000,
            "activity_heartbeat_millis": 0,
            "maximum_attempts": 3,
            "priority": 3,
            "envelope": {
                "version": 0,
                "from_id": source,
                "to_id": target,
                "call_id": "conformance-retry",
                "stream_id": "conformance-retry",
                "priority": 0,
                "deadline_unix_nano": 0,
                "sampling_enabled": False,
                "payload": [],
            },
        }
    deadline_name = (
        "deadlineUnixNano" if language.name == "go" else "deadlineUnixMillis"
    )
    request: dict[str, object] = {
        "activityType": activity_type,
        "maximumAttempts": 3,
        "priority": 3,
        "envelope": {
            "version": 0,
            "from": source,
            "to": target,
            "callId": "conformance-retry",
            "streamId": "conformance-retry",
            "priority": 0,
            deadline_name: 0,
            "samplingEnabled": False,
            "payload": "" if language.name == "go" else [],
        },
    }
    if language.name == "go":
        request["activityStartToCloseMillis"] = 2_000
        request["activityHeartbeatMillis"] = 0
    else:
        request["activityStartToCloseTimeout"] = 2_000
        request["activityHeartbeatTimeout"] = 0
    return request


def wait_workflow_failed(
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
        if "FAILED" in last.upper():
            return last
        time.sleep(0.5)
    raise RuntimeError(f"Workflow {workflow_id} did not fail after retries\n{last}")


def maximum_activity_attempt(history: object) -> int:
    attempts: list[int] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "attempt":
                    try:
                        attempts.append(int(child))
                    except (TypeError, ValueError):
                        pass
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(history)
    return max(attempts, default=0)


def verify_durable_retry(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    workflows: str,
) -> tuple[int, str]:
    service, source, target = durable_link_identity(workflows)
    workflow_id = (
        f"servicegen/conformance/retry/{language.name}/{time.time_ns()}"
    )
    request = invalid_durable_request(language, service, source, target)
    temporal_cli(
        language, overlay, env,
        "workflow", "start",
        "--workflow-id", workflow_id,
        "--type", "servicegen.durable-link.v1",
        "--task-queue", "automation-durable-calls",
        "--execution-timeout", "20s",
        "--input", json.dumps(request, separators=(",", ":")),
    )
    wait_workflow_failed(language, overlay, env, workflow_id)
    history_result = temporal_cli(
        language, overlay, env,
        "workflow", "show", "--workflow-id", workflow_id,
        "--output", "json", capture=True,
    )
    try:
        history = json.loads(history_result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Temporal retry history returned invalid JSON") from error
    attempts = maximum_activity_attempt(history)
    if attempts != 3:
        raise RuntimeError(
            f"DurableCall retry used {attempts} attempts, expected exactly 3"
        )
    return attempts, history_result.stdout


def traced_schedule_request(
    language: Language,
    trace_id: str,
    execution_id: str,
    service_id: int,
    endpoint_id: int,
) -> dict[str, object]:
    trace_carrier = {
        "traceparent": f"00-{trace_id}-0123456789abcdef-01",
    }
    if language.name == "python":
        return {
            "activity_type": f"servicegen.endpoint.{service_id}.{endpoint_id}.v1",
            "activity_start_to_close_millis": 30_000,
            "activity_heartbeat_millis": 5_000,
            "maximum_attempts": 3,
            "priority": 3,
            "envelope": {
                "version": 1,
                "endpoint_id": endpoint_id,
                "execution_id": execution_id,
                "stream_id": execution_id,
                "priority": 0,
                "deadline_unix_nano": 0,
                "sampling_enabled": True,
                "trace_carrier": trace_carrier,
                "scheduled": True,
                "schedule_id": SCHEDULE_ID,
                "scheduled_at_unix_nano": 0,
                "fired_at_unix_nano": 0,
                "payload": [],
            },
        }
    deadline_name = (
        "deadlineUnixNano" if language.name == "go" else "deadlineUnixMillis"
    )
    scheduled_name = (
        "scheduledAtUnixNano"
        if language.name == "go"
        else "scheduledAtUnixMillis"
    )
    fired_name = (
        "firedAtUnixNano" if language.name == "go" else "firedAtUnixMillis"
    )
    request: dict[str, object] = {
        "activityType": f"servicegen.endpoint.{service_id}.{endpoint_id}.v1",
        "maximumAttempts": 3,
        "priority": 3,
        "envelope": {
            "version": 1,
            "endpointId": endpoint_id,
            "executionId": execution_id,
            "streamId": execution_id,
            "priority": 0,
            deadline_name: 0,
            "samplingEnabled": True,
            "traceCarrier": trace_carrier,
            "scheduled": True,
            "scheduleId": SCHEDULE_ID,
            scheduled_name: 0,
            fired_name: 0,
            "payload": "" if language.name == "go" else [],
        },
    }
    if language.name == "go":
        request["activityStartToCloseMillis"] = 30_000
        request["activityHeartbeatMillis"] = 5_000
    else:
        request["activityStartToCloseTimeout"] = 30_000
        request["activityHeartbeatTimeout"] = 5_000
    return request


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
            language, overlay, env,
            "workflow", "describe", "--workflow-id", workflow_id,
            "--output", "json", capture=True,
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


def schedule_endpoint_identity(workflows: str) -> tuple[int, int]:
    identities = {
        (int(service), int(endpoint))
        for service, endpoint in re.findall(
            r"servicegen/schedule/(\d+)/(\d+)", workflows
        )
    }
    if len(identities) != 1:
        raise RuntimeError(
            "expected exactly one Temporal Schedule endpoint identity, found "
            + repr(sorted(identities))
        )
    return next(iter(identities))


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


def is_descendant(
    child: dict[str, Any], ancestor: dict[str, Any], spans: dict[str, dict[str, Any]],
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
            span for span in spans
            if str(span.get("operationName", "")).lower() == operation
            and stream_or_endpoint in {
                span_tags(span).get("stream"),
                span_tags(span).get("endpoint"),
            }
        ]

    schedule_inputs = matching("temporal.input", "Temporal Schedule")
    durable_inputs = matching("temporal.input", "Durable Job")
    durable_outputs = matching("temporal.output", "Durable Job")
    process_maps = matching("stream.map", "Process Durable Job")
    durable_calls = [
        span for span in spans
        if str(span.get("operationName", "")).lower() == "stream.call"
        and span_tags(span).get("from") == "Consume Durable Job"
        and span_tags(span).get("to") == "Process Durable Job"
        and span_tags(span).get("type") == "durable"
    ]
    if not schedule_inputs:
        raise RuntimeError("Temporal trace has no scheduled endpoint input span")
    if not durable_inputs or not durable_outputs:
        raise RuntimeError("Temporal trace does not cross the symmetric endpoint boundary")
    if not process_maps or not durable_calls:
        raise RuntimeError("Temporal trace does not cross the DurableCall link boundary")
    if not any(
        is_descendant(child, parent, by_id)
        for parent in durable_outputs for child in durable_inputs
    ):
        raise RuntimeError(
            "Durable Job temporal.input is not a descendant of temporal.output"
        )
    if not any(
        is_descendant(child, parent, by_id)
        for parent in durable_calls for child in process_maps
    ):
        raise RuntimeError(
            "Process Durable Job is not a descendant of its DurableCall link"
        )
    if not any(
        is_descendant(child, parent, by_id)
        for parent in schedule_inputs for child in durable_outputs
    ):
        raise RuntimeError(
            "Temporal output is not a descendant of the scheduled input"
        )
    return {
        "spanCount": len(spans),
        "scheduleInputSpans": len(schedule_inputs),
        "durableEndpointInputSpans": len(durable_inputs),
        "durableEndpointOutputSpans": len(durable_outputs),
        "durableTargetSpans": len(process_maps),
    }


def verify_tracing(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    overrides: Path,
    workflows: str,
) -> tuple[dict[str, object], str, dict[str, Any]]:
    temporal_cli(
        language, overlay, env,
        "schedule", "toggle", "--schedule-id", SCHEDULE_ID, "--pause",
    )
    run(
        compose_command(language, overlay, "stop", "automationservice"),
        cwd=language.example, env=env,
    )
    write_overrides(overrides, production=True)
    run(
        compose_command(
            language, overlay, "up", "--detach", "--no-deps",
            "--force-recreate", "automationservice",
        ),
        cwd=language.example, env=env,
    )
    wait_status(language, overlay, env)
    trace_id = secrets.token_hex(16)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    workflow_id = f"servicegen/conformance/trace/{language.name}-{timestamp}"
    service_id, endpoint_id = schedule_endpoint_identity(workflows)
    request = traced_schedule_request(
        language, trace_id, workflow_id, service_id, endpoint_id
    )
    temporal_cli(
        language, overlay, env,
        "workflow", "start",
        "--workflow-id", workflow_id,
        "--type", "servicegen.temporal-endpoint.v1",
        "--task-queue", "automation-schedules",
        "--execution-timeout", "60s",
        "--input", json.dumps(request, separators=(",", ":")),
    )
    description = wait_workflow_completed(
        language, overlay, env, workflow_id
    )
    trace = fetch_trace(trace_id)
    (ARTIFACTS / language.name / "trace.raw.json").write_text(
        json.dumps(trace, indent=2, sort_keys=True) + "\n"
    )
    return verify_temporal_trace(trace), description, trace


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
    overrides, overlay = prepare_files(language)
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
                "temporal-create-namespace", "temporal-ui", "jaeger",
                "otel-collector", "prometheus", "automationservice",
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
        retry_attempts, retry_history = verify_durable_retry(
            language, overlay, env, workflows
        )
        metrics = verify_metrics(jobs)
        temporal_metrics = verify_temporal_metric_sources(
            ARTIFACTS / language.name
        )
        trace_summary, traced_workflow, trace = verify_tracing(
            language, overlay, env, overrides, workflows
        )
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
            "temporalMetrics": temporal_metrics,
            "scheduleReuse": True,
            "queuedCancellation": True,
            "durableRetryAttempts": retry_attempts,
            "traceContinuity": trace_summary,
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
        (ARTIFACTS / language.name / "retry-history.json").write_text(
            retry_history
        )
        (ARTIFACTS / language.name / "traced-workflow.json").write_text(
            traced_workflow
        )
        (ARTIFACTS / language.name / "trace.json").write_text(
            json.dumps(trace, indent=2, sort_keys=True) + "\n"
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
