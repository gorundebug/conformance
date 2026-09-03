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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dependency_environment  # noqa: E402

CONFORMANCE = Path(__file__).resolve().parents[1]
ROOT = (
    Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE.parent))
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

    @property
    def automation_config(self) -> Path:
        return self.example / "automationservice" / "config" / "config.yaml"

    @property
    def automation_overrides(self) -> Path:
        filename = "overrides.yaml" if self.name == "go" else "docker_overrides.yaml"
        return self.example / "automationservice" / "config" / filename


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
    # Direct gate execution must use the same generated project contract as
    # Make targets and quickstart. DEPENDENCY_PROXY_DIR is the sole opt-in.
    env = dependency_environment.from_project(language.example)
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


def yaml_entries(text: str) -> list[tuple[int, int, tuple[str, ...], str | None]]:
    """Read paths from the generated, mapping-only configuration YAML subset."""
    entries: list[tuple[int, int, tuple[str, ...], str | None]] = []
    parents: list[tuple[int, str]] = []
    for index, line in enumerate(text.splitlines()):
        match = re.match(r"^( *)([^:#][^:]*):(?: (.*))?$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        key = match.group(2).strip()
        while parents and parents[-1][0] >= indent:
            parents.pop()
        path = tuple(key for _, key in parents) + (key,)
        value = match.group(3)
        entries.append((index, indent, path, value))
        if value is None:
            parents.append((indent, key))
    return entries


def validate_override_completeness(language: Language, overrides: str) -> None:
    config = language.automation_config.read_text()
    required = {
        path
        for _, _, path, value in yaml_entries(config)
        if value is not None and value.strip().startswith("$")
    }
    supplied = {
        path
        for _, _, path, value in yaml_entries(overrides)
        if value is not None
    }
    missing = sorted(".".join(path) for path in required - supplied)
    if missing:
        raise RuntimeError(
            f"{language.name} canonical automation override does not supply generated "
            f"configuration placeholders: {', '.join(missing)}"
        )


def set_yaml_scalar(text: str, path: tuple[str, ...], value: str) -> str:
    lines = text.splitlines()
    entries = yaml_entries(text)
    for index, indent, entry_path, old_value in entries:
        if entry_path == path:
            if old_value is None:
                raise RuntimeError(f"YAML path {'.'.join(path)} is not a scalar")
            lines[index] = f"{' ' * indent}{path[-1]}: {value}"
            return "\n".join(lines) + "\n"

    parent = path[:-1]
    for index, indent, entry_path, old_value in entries:
        if entry_path != parent:
            continue
        if old_value is not None:
            raise RuntimeError(f"YAML parent {'.'.join(parent)} is not a mapping")
        insert_at = len(lines)
        for next_index in range(index + 1, len(lines)):
            next_line = lines[next_index]
            if not next_line.strip() or next_line.lstrip().startswith("#"):
                continue
            next_indent = len(next_line) - len(next_line.lstrip(" "))
            if next_indent <= indent:
                insert_at = next_index
                break
        lines.insert(insert_at, f"{' ' * (indent + 2)}{path[-1]}: {value}")
        return "\n".join(lines) + "\n"
    raise RuntimeError(f"YAML parent {'.'.join(parent)} does not exist")


def write_overrides(language: Language, path: Path, *, production: bool) -> None:
    overrides = language.automation_overrides.read_text()
    validate_override_completeness(language, overrides)
    changes = {
        ("endpoints", "localSchedule", "schedule"): '"* * * * *"',
        ("endpoints", "temporalActivitySchedule", "schedule"): '"* * * * *"',
        ("endpoints", "temporalActivitySchedule", "overlapPolicy"): "Allow",
        ("endpoints", "temporalActivitySchedule", "tracingEnabled"): "true",
        ("endpoints", "temporalWorkflowSchedule", "schedule"): '"* * * * *"',
        ("endpoints", "temporalWorkflowSchedule", "overlapPolicy"): "Allow",
        ("endpoints", "temporalWorkflowSchedule", "tracingEnabled"): "true",
        ("services", "automationService", "environment"): (
            '"production"' if production else '""'
        ),
    }
    for field_path, value in changes.items():
        overrides = set_yaml_scalar(overrides, field_path, value)
    path.write_text(overrides)


def prepare_files(language: Language) -> tuple[Path, Path]:
    directory = ARTIFACTS / language.name
    directory.mkdir(parents=True, exist_ok=True)
    overrides = directory / "automationservice.overrides.yaml"
    write_overrides(language, overrides, production=False)
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
        f"      - {directory}:/conformance:ro\n"
        "      - ${SERVICELIB_CONFORMANCE_DIR}/temporal:/temporal-conformance:ro\n"
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
        [str(tool), "-config", "workflowcheck.generated.yaml", "./..."],
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


def metric_sum(text: str, metric: str, labels: dict[str, str]) -> float:
    total = 0.0
    found = False
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
            total += float(value_text.strip().split()[0])
            found = True
    return total if found else 0.0


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


def verify_workflow_pool_metrics(text: str) -> dict[str, object]:
    """Prove that the selected graph profile used its declared Workflow callers."""
    profile = os.environ.get("EXAMPLE_PROFILE", "function-call")
    task_total = metric_sum(text, "task_pool_tasks_total", {})
    priority_total = metric_sum(text, "priority_task_pool_tasks_total", {})
    if profile == "current":
        # The canonical sequential Workflow traverses two TaskPool links and
        # one PriorityTaskPool link. Its fan-out Workflow traverses one and two
        # respectively. These are Workflow SDK metrics, not process-pool data.
        if task_total < 3 or priority_total < 3:
            raise RuntimeError(
                "current profile did not execute both workflow-local pools: "
                f"task={task_total}, priority={priority_total}, expected >=3 each"
            )
        return {
            "profile": profile,
            "taskPoolTasks": task_total,
            "priorityTaskPoolTasks": priority_total,
            "workflowLocal": True,
        }
    if task_total != 0 or priority_total != 0:
        raise RuntimeError(
            "function-call profile unexpectedly emitted Workflow pool metrics: "
            f"task={task_total}, priority={priority_total}"
        )
    return {
        "profile": profile,
        "taskPoolTasks": 0,
        "priorityTaskPoolTasks": 0,
        "workflowLocal": True,
    }


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


def pause_temporal_schedules(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> None:
    """Keep the integration workload bounded between explicit triggers."""
    for schedule_id in (ACTIVITY_SCHEDULE_ID, WORKFLOW_SCHEDULE_ID):
        temporal_cli(
            language,
            overlay,
            env,
            "schedule",
            "toggle",
            "--schedule-id",
            schedule_id,
            "--pause",
        )


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
    output = ARTIFACTS / language.name
    history = workflow_history(language, overlay, env, workflow_id)
    (output / "cancellation-timeout-history.json").write_text(history)
    logs = diagnostics(language, overlay, env)
    (output / "cancellation-timeout.log").write_text(logs + "\n")
    raise RuntimeError(
        f"Workflow {workflow_id} was not canceled\n{last}\n{logs}"
    )


def verify_queued_cancellation(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> str:
    # Regular admission is paused by exercise(). Otherwise a test that crosses
    # a minute boundary can observe an unrelated scheduled Workflow and
    # incorrectly attribute its graph activation to the canceled firing.
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
) -> tuple[str, str]:
    for workflow_id in endpoint_workflow_ids(
        language,
        overlay,
        env,
        WORKFLOW_JOB_WORKFLOW_TYPE,
        "workflow_job",
    ):
        history = workflow_history(language, overlay, env, workflow_id)
        if "CONTINUE_AS_NEW_INITIATOR_WORKFLOW" in history:
            return workflow_id, history
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


def replay_workflow_histories(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    histories: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    """Replay real Event Histories with the generated service Workflow code."""
    output = ARTIFACTS / language.name
    replay_outputs: list[str] = []

    def replay(command: list[str], *, cwd: Path) -> None:
        result = run(
            command,
            cwd=cwd,
            env=env,
            timeout=180,
            capture=True,
        )
        combined = result.stdout + result.stderr
        replay_outputs.append(combined)
        if "temporal workflow graph started" in combined:
            raise RuntimeError(
                "offline Workflow replay emitted a production Workflow log"
            )

    if language.name == "go":
        image = "servicelib-temporal-conformance-go-replayer:local"
        service = language.example / "automationservice"
        replay_overlay = output / "go-replay.compose.yml"
        replay_overlay.write_text(
            "services:\n"
            "  automationservice:\n"
            f"    image: {image}\n"
            "    build:\n"
            "      target: temporal-replay\n"
        )
        # Keep replay on the generated service build boundary.  In particular,
        # its framework and module additional_contexts, dependency proxy setup,
        # retries and copied-source contract must be identical to docker-build.
        compose = " ".join(
            (
                "docker compose",
                f"--project-name {language.project}-replay",
                f"--project-directory {service}",
                f"--file {service / 'docker-compose.generated.yml'}",
                f"--file {replay_overlay}",
            )
        )
        run(
            [
                "make",
                "-C",
                "automationservice",
                "docker-build",
                f"COMPOSE={compose}",
            ],
            cwd=language.example,
            env=env,
            timeout=300,
        )
        for filename, workflow_id in histories:
            replay(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--volume",
                    f"{output}:/conformance:ro",
                    "--workdir",
                    "/workspace/automationservice",
                    image,
                    "/app/temporal-replay",
                    f"/conformance/{filename}",
                ],
                cwd=language.example,
            )
    elif language.name == "python":
        for filename, workflow_id in histories:
            replay(
                compose_command(
                    language,
                    overlay,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "python",
                    "automationservice",
                    "-m",
                    "automation_service.temporal_replay_generated",
                    f"/conformance/{filename}",
                    "--workflow-id",
                    workflow_id,
                ),
                cwd=language.example,
            )
    elif language.name == "typescript":
        for filename, workflow_id in histories:
            replay(
                compose_command(
                    language,
                    overlay,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "node",
                    "automationservice",
                    "/app/dist/temporal-replay.generated.js",
                    f"/conformance/{filename}",
                    workflow_id,
                ),
                cwd=language.example,
            )
    else:
        raise RuntimeError(f"Temporal replay is unsupported for {language.name}")
    return {
        "histories": len(histories),
        "workflowIds": [workflow_id for _, workflow_id in histories],
        "deterministic": True,
        "telemetryOutputSuppressed": all(
            "temporal workflow graph started" not in value
            for value in replay_outputs
        ),
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
                "envelope" in item
                and (
                    "activityType" in item
                    or "activity_type" in item
                    or (
                        ("runtimeConfig" in item or "runtime_config" in item)
                        and (
                            "connectorName" in item
                            or "connector_name" in item
                            or "endpoints" in item
                        )
                    )
                )
            ):
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


def fetch_recent_traces(service: str, limit: int = 20) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"service": service, "limit": str(limit), "lookback": "1h"}
    )
    with urllib.request.urlopen(f"{JAEGER_URL}/api/traces?{query}", timeout=5) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError("Jaeger returned a non-object recent trace response")
    return value


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


def matching_spans(
    trace: dict[str, Any], operation: str, stream_or_endpoint: str
) -> list[dict[str, Any]]:
    return [
        span
        for span in trace.get("spans", [])
        if isinstance(span, dict)
        and str(span.get("operationName", "")).lower() == operation
        and stream_or_endpoint
        in {
            span_tags(span).get("stream"),
            span_tags(span).get("endpoint"),
        }
    ]


def normalized_servicelib_trace(trace: dict[str, Any]) -> list[dict[str, object]]:
    operations = {"stream.input", "stream.call", "stream.delay", "stream.map"}
    spans = [
        span
        for span in trace.get("spans", [])
        if isinstance(span, dict)
        and str(span.get("operationName", "")).lower() in operations
    ]
    by_id = {
        str(span.get("spanID")): span
        for span in trace.get("spans", [])
        if isinstance(span, dict) and isinstance(span.get("spanID"), str)
    }

    def identity(span: dict[str, Any]) -> str:
        operation = str(span.get("operationName", "")).lower()
        tags = span_tags(span)
        if operation == "stream.call":
            return "|".join(
                (
                    operation,
                    tags.get("from", ""),
                    tags.get("to", ""),
                    tags.get("type", ""),
                    tags.get("taskpoolname", ""),
                )
            )
        return "|".join((operation, tags.get("stream", "")))

    def servicelib_parent(span: dict[str, Any]) -> str:
        parent = span_parent(span)
        visited: set[str] = set()
        while parent and parent not in visited:
            visited.add(parent)
            candidate = by_id.get(parent)
            if candidate is None:
                break
            if str(candidate.get("operationName", "")).lower() in operations:
                return identity(candidate)
            parent = span_parent(candidate)
        return "external"

    result = [
        {"id": identity(span), "parent": servicelib_parent(span)}
        for span in spans
    ]
    identities = [str(item["id"]) for item in result]
    duplicates = sorted(
        identity for identity in set(identities) if identities.count(identity) > 1
    )
    if duplicates:
        raise RuntimeError(
            "Workflow replay duplicated ServiceLib spans: " + ", ".join(duplicates)
        )
    return sorted(result, key=lambda item: str(item["id"]))


def verify_activity_trace(trace: dict[str, Any]) -> dict[str, object]:
    raw_spans = trace.get("spans", [])
    spans = [span for span in raw_spans if isinstance(span, dict)]
    by_id = {
        str(span.get("spanID")): span
        for span in spans
        if isinstance(span.get("spanID"), str)
    }

    schedule_inputs = matching_spans(
        trace, "temporal.input", ACTIVITY_SCHEDULE_ENDPOINT_NAME
    )
    process_maps = matching_spans(trace, "stream.map", "Process Scheduled Activity")
    delay_spans = matching_spans(trace, "stream.delay", "Scheduled Activity Pause")
    if len(schedule_inputs) != 1:
        raise RuntimeError(
            f"Temporal Activity trace has {len(schedule_inputs)} endpoint input spans"
        )
    heartbeat_inputs = [
        span
        for span in schedule_inputs
        if "temporal.activity.heartbeat" in span_events(span)
    ]
    if not heartbeat_inputs:
        raise RuntimeError(
            "Temporal heartbeat is not attached to its scheduled endpoint input span"
        )
    if len(delay_spans) != 1 or len(process_maps) != 1:
        raise RuntimeError(
            "Temporal Activity trace does not execute each scheduled graph node once"
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


def verify_workflow_trace(trace: dict[str, Any]) -> dict[str, object]:
    inputs = matching_spans(trace, "stream.input", WORKFLOW_SCHEDULE_ENDPOINT_NAME)
    delays = matching_spans(trace, "stream.delay", "Scheduled Workflow Pause")
    maps = matching_spans(trace, "stream.map", "Process Scheduled Workflow")
    if len(inputs) != 1 or len(delays) != 1 or len(maps) != 1:
        raise RuntimeError(
            "direct Workflow trace must contain exactly one input, Delay and Map span: "
            f"input={len(inputs)}, delay={len(delays)}, map={len(maps)}"
        )
    spans = {
        str(span.get("spanID")): span
        for span in trace.get("spans", [])
        if isinstance(span, dict) and isinstance(span.get("spanID"), str)
    }
    if not is_descendant(delays[0], inputs[0], spans):
        raise RuntimeError("direct Workflow Delay did not preserve the input trace parent")
    if not is_descendant(maps[0], delays[0], spans):
        raise RuntimeError("direct Workflow Map is not a descendant of Delay")
    normalized = normalized_servicelib_trace(trace)
    return {
        "spanCount": len(trace.get("spans", [])),
        "inputSpans": 1,
        "delaySpans": 1,
        "mapSpans": 1,
        "normalizedTree": normalized,
    }


def fetch_trace_with_diagnostics(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    trace_id: str,
    artifact_prefix: str,
) -> dict[str, Any]:
    output = ARTIFACTS / language.name
    try:
        trace = fetch_trace(trace_id)
    except RuntimeError:
        try:
            recent = fetch_recent_traces("Automation Service")
            (output / f"{artifact_prefix}-trace-miss.recent.json").write_text(
                json.dumps(recent, indent=2, sort_keys=True) + "\n"
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as error:
            (output / f"{artifact_prefix}-trace-miss.jaeger-error.txt").write_text(
                f"{error}\n"
            )
        (output / f"{artifact_prefix}-trace-miss.runtime.log").write_text(
            diagnostics(language, overlay, env) + "\n"
        )
        collector_logs = run(
            compose_command(
                language,
                overlay,
                "logs",
                "--no-color",
                "--tail",
                "400",
                "otel-collector",
                "jaeger",
            ),
            cwd=language.example,
            env=env,
            capture=True,
            check=False,
            announce=False,
        )
        (output / f"{artifact_prefix}-trace-miss.collector.log").write_text(
            collector_logs.stdout + collector_logs.stderr
        )
        raise
    (output / f"{artifact_prefix}-trace.raw.json").write_text(
        json.dumps(trace, indent=2, sort_keys=True) + "\n"
    )
    return trace


def start_traced_workflow(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    *,
    workflow_type: str,
    task_queue: str,
    request: dict[str, object],
    artifact_prefix: str,
) -> tuple[str, str, str, dict[str, Any]]:
    trace_id = secrets.token_hex(16)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    workflow_id = f"conformance/trace/{artifact_prefix}/{language.name}-{timestamp}"
    temporal_cli(
        language,
        overlay,
        env,
        "workflow",
        "start",
        "--workflow-id",
        workflow_id,
        "--type",
        workflow_type,
        "--task-queue",
        task_queue,
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
    history = workflow_history(language, overlay, env, workflow_id)
    (ARTIFACTS / language.name / f"{artifact_prefix}-workflow-history.json").write_text(
        history
    )
    trace = fetch_trace_with_diagnostics(
        language, overlay, env, trace_id, artifact_prefix
    )
    return workflow_id, description, history, trace


def workflow_link_metric_total(text: str, language: Language) -> float:
    normalize = (
        (lambda value: re.sub(r"[^A-Za-z0-9]", "_", value))
        if language.name == "go"
        else (lambda value: value)
    )
    return metric_sum(
        text,
        "stream_messages_total",
        {
            "from": normalize(WORKFLOW_SCHEDULE_ENDPOINT_NAME),
            "to": normalize("Scheduled Workflow Pause"),
        },
    )


def automationservice_logs(
    language: Language, overlay: Path, env: dict[str, str]
) -> str:
    result = run(
        compose_command(
            language,
            overlay,
            "logs",
            "--no-color",
            "automationservice",
            "otel-collector",
        ),
        cwd=language.example,
        env=env,
        capture=True,
        announce=False,
    )
    return result.stdout + result.stderr


def verify_tracing(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    overrides: Path,
    schedule_descriptions: dict[str, str],
) -> tuple[dict[str, object], dict[str, str], dict[str, dict[str, Any]]]:
    run(
        compose_command(language, overlay, "stop", "automationservice"),
        cwd=language.example,
        env=env,
    )
    write_overrides(language, overrides, production=True)
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

    activity = start_traced_workflow(
        language,
        overlay,
        env,
        workflow_type=ENDPOINT_WORKFLOW_TYPE,
        task_queue="automation-activity-schedules",
        request=schedule_workflow_request(
            schedule_descriptions[ACTIVITY_SCHEDULE_ID]
        ),
        artifact_prefix="activity-trace",
    )
    activity_summary = verify_activity_trace(activity[3])

    sdk_before = fetch_text(TEMPORAL_SDK_METRICS_URL)
    workflow_metric_before = workflow_link_metric_total(sdk_before, language)
    logs_before_workflow = automationservice_logs(language, overlay, env)
    workflow_logs_before = logs_before_workflow.count(
        "temporal workflow graph started"
    )
    direct = start_traced_workflow(
        language,
        overlay,
        env,
        workflow_type=SCHEDULED_WORKFLOW_TYPE,
        task_queue="automation-workflow-schedules",
        request=schedule_workflow_request(
            schedule_descriptions[WORKFLOW_SCHEDULE_ID]
        ),
        artifact_prefix="workflow-trace",
    )
    workflow_summary = verify_workflow_trace(direct[3])
    sdk_after = fetch_text(TEMPORAL_SDK_METRICS_URL)
    workflow_metric_after = workflow_link_metric_total(sdk_after, language)
    metric_delta = workflow_metric_after - workflow_metric_before
    if metric_delta != 1:
        raise RuntimeError(
            "direct Workflow emitted a non-replay-safe link metric delta: "
            f"{metric_delta}, expected 1"
        )

    logs_before_replay = automationservice_logs(language, overlay, env)
    (ARTIFACTS / language.name / "workflow-trace.runtime.log").write_text(
        logs_before_replay
    )
    workflow_log_count = (
        logs_before_replay.count("temporal workflow graph started")
        - workflow_logs_before
    )
    if workflow_log_count != 1:
        raise RuntimeError(
            "direct Workflow replay-safe activation log count is "
            f"{workflow_log_count}, expected 1"
        )

    replay = replay_workflow_histories(
        language,
        overlay,
        env,
        (("workflow-trace-workflow-history.json", direct[0]),),
    )
    replayed_trace = fetch_trace_with_diagnostics(
        language,
        overlay,
        env,
        str(direct[3].get("traceID", "")),
        "workflow-trace-after-replay",
    )
    replayed_summary = verify_workflow_trace(replayed_trace)
    if replayed_summary["normalizedTree"] != workflow_summary["normalizedTree"]:
        raise RuntimeError("offline replay changed the exported Workflow trace tree")
    sdk_after_replay = fetch_text(TEMPORAL_SDK_METRICS_URL)
    if workflow_link_metric_total(sdk_after_replay, language) != workflow_metric_after:
        raise RuntimeError("offline replay duplicated direct Workflow metrics")
    logs_after_replay = automationservice_logs(language, overlay, env)
    if logs_after_replay.count(direct[0]) != logs_before_replay.count(direct[0]):
        raise RuntimeError("offline replay duplicated direct Workflow logs")

    workflow_summary["linkMetricDelta"] = metric_delta
    workflow_summary["activationLogs"] = workflow_log_count
    workflow_summary["offlineReplay"] = replay
    return (
        {"activity": activity_summary, "workflow": workflow_summary},
        {"activity": activity[1], "workflow": direct[1]},
        {"activity": activity[3], "workflow": direct[3]},
    )


def wait_graph(
    language: Language,
    overlay: Path,
    env: dict[str, str],
    jobs: int,
    timeout: float = 120,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        try:
            last = wait_status(language, overlay, env, timeout=5)
        except RuntimeError as error:
            last_error = error
            continue
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
        f"only partial Temporal graph execution after {jobs} queued jobs; "
        f"last readiness error: {last_error}\n"
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
    required = (
        ("Local Schedule", "Split On-Demand Jobs"),
        ("Split On-Demand Jobs", "Submit Activity Job"),
        ("Split On-Demand Jobs", "Submit Fan-Out Workflow Job"),
        ("Split On-Demand Jobs", "Submit Workflow Job"),
        ("Consume Activity Job", "Activity Pause"),
        ("Activity Pause", "Process Activity Job"),
        ("Submit Activity Job", "Observe Activity Result"),
        ("Consume Sequential Activity A", "Process Sequential Activity A"),
        ("Consume Sequential Activity B", "Process Sequential Activity B"),
        ("Consume Fan-Out Activity A", "Process Fan-Out Activity A"),
        ("Consume Fan-Out Activity B", "Process Fan-Out Activity B"),
        ("Consume Fan-Out Activity C", "Process Fan-Out Activity C"),
    )
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        try:
            last = wait_status(language, overlay, env, timeout=5)
        except RuntimeError as error:
            last_error = error
            continue
        if all(edge_calls(last, source, target) >= 1 for source, target in required):
            return last
        time.sleep(0.5)
    snapshot = ARTIFACTS / language.name / "local-cron-timeout-status.json"
    snapshot.write_text(json.dumps(last, indent=2, sort_keys=True) + "\n")
    logs = diagnostics(language, overlay, env)
    (ARTIFACTS / language.name / "local-cron-timeout.log").write_text(logs + "\n")
    for workflow_type, endpoint_identity, prefix in (
        (WORKFLOW_JOB_WORKFLOW_TYPE, "workflow_job", "sequential"),
        (FANOUT_WORKFLOW_JOB_WORKFLOW_TYPE, "fan_out_workflow_job", "fanout"),
    ):
        for index, workflow_id in enumerate(
            endpoint_workflow_ids(
                language,
                overlay,
                env,
                workflow_type,
                endpoint_identity,
            )
        ):
            history = workflow_history(language, overlay, env, workflow_id)
            (ARTIFACTS / language.name / f"local-cron-{prefix}-{index}.json").write_text(
                history
            )
    observed = "\n".join(
        f"- {source} -> {target}: {edge_calls(last, source, target)}/1"
        for source, target in required
    )
    raise RuntimeError(
        "local cron did not complete its configured graph within 75 seconds\n"
        + f"last readiness error: {last_error}\n"
        + observed
        + "\n"
        + logs
    )


def verify_workflow_status_counters_are_process_local(
    status: dict[str, object],
) -> None:
    """Direct Workflow execution must not mutate process-local status counts.

    The receiving Activity graphs are covered by ``wait_local_cron`` above and
    must increment their ordinary runtime counters.  These links execute in a
    Temporal Workflow isolate and are intentionally observable through
    replay-safe SDK metrics/traces instead of the process status registry.
    """

    workflow_links = (
        ("Consume Workflow Job", "Workflow Pause"),
        ("Workflow Pause", "Call Sequential Activity A"),
        ("Call Sequential Activity A", "Call Sequential Activity B"),
        ("Call Sequential Activity B", "Process Workflow Job"),
        ("Consume Fan-Out Workflow Job", "Call Fan-Out Activity A"),
        ("Call Fan-Out Activity A", "Split Activity A Result"),
        ("Split Activity A Result", "Call Fan-Out Activity B"),
        ("Split Activity A Result", "Call Fan-Out Activity C"),
        ("Temporal Workflow Schedule", "Scheduled Workflow Pause"),
        ("Scheduled Workflow Pause", "Process Scheduled Workflow"),
    )
    unexpected = [
        (source, target, edge_calls(status, source, target))
        for source, target in workflow_links
        if edge_calls(status, source, target) != 0
    ]
    if unexpected:
        raise RuntimeError(
            "direct Workflow links leaked into process-local status counters: "
            + ", ".join(
                f"{source}->{target}={calls}"
                for source, target, calls in unexpected
            )
        )


def verify_python_workflow_sandbox(
    language: Language,
    overlay: Path,
    env: dict[str, str],
) -> bool | None:
    """Prove Python Workers retain the SDK's default Workflow sandbox."""
    if language.name != "python":
        return None
    result = run(
        compose_command(
            language,
            overlay,
            "exec",
            "--no-TTY",
            "automationservice",
            "python",
            "/temporal-conformance/python_sandbox_probe.py",
        ),
        cwd=language.example,
        env=env,
        capture=True,
        timeout=30,
    )
    if "default sandbox rejected os.getcwd: PASS" not in result.stdout:
        raise RuntimeError(
            "Python Temporal sandbox probe did not report the expected rejection:\n"
            + result.stdout
            + result.stderr
        )
    return True


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
        python_sandbox = verify_python_workflow_sandbox(language, overlay, env)
        pause_temporal_schedules(language, overlay, env)

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
        verify_workflow_status_counters_are_process_local(status)
        workflow_composition = verify_workflow_composition(language, overlay, env)
        continue_as_new_id, continue_as_new = verify_continue_as_new(
            language, overlay, env
        )
        (ARTIFACTS / language.name / "continue-as-new-history.json").write_text(
            continue_as_new
        )
        replay = replay_workflow_histories(
            language,
            overlay,
            env,
            (
                (
                    "sequential-workflow.json",
                    str(workflow_composition["sequentialWorkflowId"]),
                ),
                (
                    "fanout-workflow.json",
                    str(workflow_composition["fanOutWorkflowId"]),
                ),
                ("continue-as-new-history.json", continue_as_new_id),
            ),
        )
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
        workflow_pools = verify_workflow_pool_metrics(
            fetch_text(TEMPORAL_SDK_METRICS_URL)
        )
        trace_summary, traced_workflows, traces = verify_tracing(
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
            "workflowStatusCalls": 0,
            "workflowComposition": workflow_composition,
            "historyReplay": replay,
            "continueAsNew": True,
            "workflowcheck": workflowcheck,
            "pythonSandbox": python_sandbox,
            "metrics": metrics,
            "temporalMetrics": temporal_metrics,
            "workflowPools": workflow_pools,
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
        for kind, description in traced_workflows.items():
            (ARTIFACTS / language.name / f"traced-{kind}-workflow.json").write_text(
                description
            )
        for kind, trace in traces.items():
            (ARTIFACTS / language.name / f"{kind}-trace.json").write_text(
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
    workflow_trace_trees = {
        name: implementation["traceContinuity"]["workflow"]["normalizedTree"]
        for name, implementation in implementations.items()
        if isinstance(implementation, dict)
    }
    if len(workflow_trace_trees) > 1:
        reference_name = next(iter(workflow_trace_trees))
        reference = workflow_trace_trees[reference_name]
        mismatches = [
            name
            for name, tree in workflow_trace_trees.items()
            if tree != reference
        ]
        if mismatches:
            failures["workflow-trace-equivalence"] = (
                f"direct Workflow trace tree differs from {reference_name}: "
                + ", ".join(mismatches)
            )
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
