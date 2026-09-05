#!/usr/bin/env python3

from __future__ import annotations

import argparse
import difflib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFORMANCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE))
import cpp_source_cache
import dependency_environment
import graph_profile

ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE.parent)).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "tracing"
COMMON_COMPOSE = Path(__file__).with_name("compose.common.yml")
JAEGER_URL = "http://localhost:16686"
ORDER_URL = "http://localhost:9091/v1/processorder"

APPLICATION_OPERATIONS = {
    "http.input",
    "http.output",
    "grpc.input",
    "grpc.output",
    "kafka.input",
    "kafka.output",
    "local.input",
    "local.output",
    "stream.call",
    "stream.case",
    "stream.cyclelink",
    "stream.delay",
    "stream.filter",
    "stream.flatmap",
    "stream.flatmapiterable",
    "stream.input",
    "stream.join",
    "stream.keyby",
    "stream.link",
    "stream.map",
    "stream.merge",
    "stream.multijoin",
    "stream.process",
    "stream.sink",
    "stream.split",
}
SEMANTIC_TAGS = {
    "connector",
    "endpoint",
    "from",
    "stream",
    "taskpoolname",
    "to",
    "type",
}


@dataclass(frozen=True)
class Language:
    name: str
    example: Path
    compose: Path

    @property
    def project(self) -> str:
        return f"servicelib-conformance-{self.name}"


LANGUAGES = (
    Language("go", ROOT / "goexample", Path(__file__).with_name("compose.go.yml")),
    Language(
        "cpp", ROOT / "cppexample", Path(__file__).with_name("compose.cpp.yml")
    ),
    Language(
        "cppboost",
        ROOT / "cppboostexample",
        Path(__file__).with_name("compose.cppboost.yml"),
    ),
    Language(
        "python",
        ROOT / "pyexample",
        Path(__file__).with_name("compose.python.yml"),
    ),
    Language(
        "rust",
        ROOT / "rustexample",
        Path(__file__).with_name("compose.rust.yml"),
    ),
    Language(
        "typescript",
        ROOT / "tsexample",
        Path(__file__).with_name("compose.typescript.yml"),
    ),
)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    retry_network: bool = False,
) -> None:
    print("+", " ".join(command), flush=True)
    if retry_network:
        dependency_environment.run_dependency_command(
            command, cwd=cwd, env=env or os.environ.copy()
        )
        return
    subprocess.run(command, cwd=cwd, env=env, check=True)


def compose_command(language: Language, *args: str) -> list[str]:
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
    # Canonical C++ tracing is compiled into the workspace build volume with
    # ENABLE_OTLP_TRACING=ON. Run that exact binary through the generated
    # integration overlay; the normal runtime image is telemetry-disabled and
    # its fixed entrypoint cannot execute a path from the build volume.
    if language.name == "cpp":
        command.extend(
            [
                "--file",
                str(language.example / "docker-compose.integration.generated.yml"),
            ]
        )
    else:
        for runtime_overlay in sorted(
            language.example.glob("docker-compose.*-runtime.generated.yml")
        ):
            command.extend(["--file", str(runtime_overlay)])
    command.extend([
        "--file", str(COMMON_COMPOSE),
        "--file", str(language.compose),
        *args,
    ])
    return command


def build(language: Language, env: dict[str, str]) -> None:
    if language.name == "go":
        run(["make", "docker-build"], cwd=language.example, env=env, retry_network=True)
    elif language.name == "cpp":
        run(
            ["./scripts/build.generated.sh", "docker-release"],
            cwd=language.example,
            env={**env, "ENABLE_OTLP_TRACING": "ON"},
            retry_network=True,
        )
    elif language.name == "cppboost":
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
            retry_network=True,
        )
    elif language.name in {"python", "rust"}:
        run(
            compose_command(
                language,
                "build",
                "analyticsservice",
                "inventoryservice",
                "orderservice",
            ),
            cwd=language.example,
            env=env,
            retry_network=True,
        )
    elif language.name == "typescript":
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
            retry_network=True,
        )


def wait_http(url: str, *, timeout: float, accepted: set[int]) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status in accepted:
                    return response.read()
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(f"timeout waiting for {url}: {last_error}")


def wait_service_http(
    language: Language,
    service: str,
    url: str,
    *,
    timeout: float,
    env: dict[str, str],
) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return response.read()
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        status = subprocess.run(
            compose_command(language, "ps", "--status", "exited", "--services"),
            cwd=language.example,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if service in status.stdout.split():
            raise RuntimeError(f"{language.name} {service} exited before readiness")
        time.sleep(0.5)
    diagnostics = service_diagnostics(language, service, env)
    raise RuntimeError(
        f"{language.name}/{service} readiness timed out at {url}: {last_error}"
        f"\n{diagnostics}"
    )


def service_diagnostics(
    language: Language, service: str, env: dict[str, str]
) -> str:
    ps = subprocess.run(
        compose_command(language, "ps", "--all", "--format", "json", service),
        cwd=language.example,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    logs = subprocess.run(
        compose_command(
            language, "logs", "--no-color", "--tail", "20", service
        ),
        cwd=language.example,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    state = ps.stdout.strip() or ps.stderr.strip() or "state unavailable"
    log_lines = [
        line for line in (logs.stdout + logs.stderr).splitlines() if line.strip()
    ]
    recent = "\n".join(log_lines[-12:]) or "logs unavailable"
    return f"container state: {state}\nrecent {service} logs:\n{recent}"


def send_request(trace_id: str | None = None) -> None:
    body = json.dumps(
        {
            "customer_id": "conformance-customer",
            "items": [
                {
                    "item_id": "conformance-item-1",
                    "sku": "SKU-001",
                    "quantity": 2,
                    "unit_price": 799.0,
                },
                {
                    "item_id": "conformance-item-2",
                    "sku": "SKU-002",
                    "quantity": 1,
                    "unit_price": 399.0,
                }
            ],
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if trace_id is not None:
        headers.update(
            {
                "x-trace": "1",
                "traceparent": f"00-{trace_id}-0123456789abcdef-01",
            }
        )
    request = urllib.request.Request(
        ORDER_URL,
        data=body,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status: {response.status}")
        if payload.get("status") != "CONFIRMED":
            raise RuntimeError(f"unexpected response: {payload}")


def wait_kafka_processing(*, timeout: float = 30) -> None:
    url = "http://localhost:9093/status/data"
    deadline = time.monotonic() + timeout
    latest = ""
    while time.monotonic() < deadline:
        try:
            latest = wait_http(url, timeout=3, accepted={200}).decode()
            graph = json.loads(latest)
            if any(
                isinstance(edge, dict)
                and "calls: 0" not in str(edge.get("label", ""))
                and "calls:" in str(edge.get("label", ""))
                for edge in graph.get("edges", [])
            ):
                return
        except (RuntimeError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise RuntimeError(
        "Analytics Service did not process the Kafka order event; latest "
        f"/status/data: {latest}"
    )


def application_service_operations(
    trace: dict[str, Any],
) -> set[tuple[str, str]]:
    processes = trace.get("processes", {})
    return {
        (
            str(processes.get(span.get("processID"), {}).get("serviceName", "")),
            str(span.get("operationName", "")).lower(),
        )
        for span in trace.get("spans", [])
        if str(span.get("operationName", "")).lower() in APPLICATION_OPERATIONS
    } - {("", "")}


def assert_no_application_trace(
    service_operations: set[tuple[str, str]],
    *,
    started_at_us: int,
    excluded_trace_ids: set[str],
    timeout: float = 8,
) -> None:
    """Verify that an ordinary request did not create ServiceLib spans."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)

    ended_at_us = time.time_ns() // 1_000
    for service, operation in sorted(service_operations):
        query = urllib.parse.urlencode(
            {
                "service": service,
                "operation": operation,
                "start": started_at_us,
                "end": ended_at_us,
                "limit": 20,
            }
        )
        with urllib.request.urlopen(
            f"{JAEGER_URL}/api/traces?{query}", timeout=3
        ) as response:
            payload = json.loads(response.read())
        unexpected = [
            trace
            for trace in payload.get("data", [])
            if str(trace.get("traceID", "")) not in excluded_trace_ids
        ]
        if unexpected:
            trace_ids = sorted(
                str(trace.get("traceID", "")) for trace in unexpected
            )
            raise RuntimeError(
                "request without X-Trace or sampled remote parent exported "
                f"{operation!r} spans for {service!r}: {trace_ids}"
            )


def fetch_trace(trace_id: str, *, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    url = f"{JAEGER_URL}/api/traces/{trace_id}"
    first_seen_at: float | None = None
    latest: dict[str, Any] | None = None
    latest_count = -1
    stable_polls = 0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                payload = json.loads(response.read())
            if payload.get("data"):
                trace = payload["data"][0]
                count = len(trace.get("spans", []))
                now = time.monotonic()
                if first_seen_at is None:
                    first_seen_at = now
                if count == latest_count:
                    stable_polls += 1
                else:
                    stable_polls = 0
                    latest_count = count
                latest = trace
                # Go and Python use batch span processors. Wait at least one
                # complete export interval after the trace first appears, then
                # require several unchanged observations.
                if now - first_seen_at >= 7 and stable_polls >= 3:
                    return trace
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    if latest is not None:
        return latest
    raise RuntimeError(f"trace {trace_id} was not exported to Jaeger")


def assert_unique_grpc_request_stream_ids(trace: dict[str, Any]) -> None:
    processes = trace.get("processes", {})
    requests: list[tuple[str, int, int]] = []
    for span in trace.get("spans", []):
        process = processes.get(span.get("processID"), {})
        if str(process.get("serviceName", "")).lower().replace(" ", "") != "inventoryservice":
            continue
        if str(span.get("operationName", "")).lower() != "grpc.input":
            continue
        tags = {
            str(tag.get("key", "")): str(tag.get("value", ""))
            for tag in span.get("tags", [])
        }
        stream_id = tags.get("stream_id")
        if not stream_id:
            raise RuntimeError("inventory grpc.input span has no stream_id")
        started = int(span.get("startTime", 0))
        duration = int(span.get("duration", 0))
        requests.append((stream_id, started, started + duration))
    stream_ids = [request[0] for request in requests]
    if len(requests) != 2:
        raise RuntimeError(
            f"expected two inventory unary requests, found {len(stream_ids)}"
        )
    if len(set(stream_ids)) != len(stream_ids):
        raise RuntimeError(
            f"unary requests reused stream_id: {stream_ids!r}"
        )
    if os.environ.get("EXAMPLE_PROFILE") == "function-call":
        ordered = sorted(requests, key=lambda request: request[1])
        if ordered[1][1] < ordered[0][2]:
            raise RuntimeError(
                "FunctionCall overlapped unary requests from one parent stream: "
                f"{ordered!r}"
            )


def _span_tags(span: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tag in span.get("tags", []):
        key = str(tag.get("key", ""))
        if key in SEMANTIC_TAGS:
            result[key] = tag.get("value")
    return dict(sorted(result.items()))


def _span_events(span: dict[str, Any]) -> list[str]:
    """Return ServiceLib events in their recorded logical order."""
    events: list[str] = []
    for log in span.get("logs", []):
        fields = {
            str(field.get("key", "")): field.get("value")
            for field in log.get("fields", [])
        }
        event = fields.get("event")
        if event is not None:
            events.append(str(event))
    return events


def normalize(trace: dict[str, Any]) -> dict[str, Any]:
    processes = trace.get("processes", {})
    selected: dict[str, dict[str, Any]] = {}
    parent_by_span: dict[str, str | None] = {}

    for span in trace.get("spans", []):
        span_id = str(span["spanID"])
        parent_by_span[span_id] = next(
            (
                str(reference.get("spanID"))
                for reference in span.get("references", [])
                if reference.get("refType") == "CHILD_OF"
            ),
            None,
        )
        operation = str(span.get("operationName", "")).lower()
        if operation not in APPLICATION_OPERATIONS:
            continue
        process = processes.get(span.get("processID"), {})
        selected[span_id] = {
            "operation": operation,
            "service": str(process.get("serviceName", "")).lower().replace(" ", ""),
            "attributes": _span_tags(span),
            "events": _span_events(span),
            "children": [],
            # Jaeger timestamps are used only to retain the observed logical
            # dispatch order. They are removed from normalized output because
            # their absolute values and durations are implementation details.
            "_started": int(span.get("startTime", 0)),
        }

    if not selected:
        raise RuntimeError("trace contains no ServiceLib application spans")

    roots: list[dict[str, Any]] = []
    for span_id, node in selected.items():
        parent_id = parent_by_span[span_id]
        while parent_id is not None and parent_id not in selected:
            parent_id = parent_by_span.get(parent_id)
        if parent_id is None:
            roots.append(node)
        else:
            selected[parent_id]["children"].append(node)

    def descriptor(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation": node["operation"],
            "service": node["service"],
            "attributes": node["attributes"],
        }

    order_constraints: list[dict[str, Any]] = []

    def record_order(
        parent: dict[str, Any] | None, children: list[dict[str, Any]]
    ) -> None:
        for index, left in enumerate(children):
            for right in children[index + 1:]:
                left_key = descriptor(left)
                right_key = descriptor(right)
                if left_key == right_key or left["_started"] == right["_started"]:
                    # Jaeger exposes microsecond timestamps. Equal timestamps
                    # do not contain enough information to assert either order.
                    continue
                before, after = (
                    (left_key, right_key)
                    if left["_started"] < right["_started"]
                    else (right_key, left_key)
                )
                order_constraints.append(
                    {
                        "parent": None if parent is None else descriptor(parent),
                        "before": before,
                        "after": after,
                    }
                )
        for child in children:
            record_order(child, child["children"])

    record_order(None, roots)

    def canonicalize(node: dict[str, Any]) -> None:
        for child in node["children"]:
            canonicalize(child)
        node["children"].sort(
            key=lambda child: json.dumps(
                child, sort_keys=True, separators=(",", ":")
            )
        )
        node.pop("_started")

    for root in roots:
        canonicalize(root)
    roots.sort(key=lambda node: json.dumps(node, sort_keys=True, separators=(",", ":")))
    order_constraints.sort(
        key=lambda constraint: json.dumps(
            constraint, sort_keys=True, separators=(",", ":")
        )
    )
    return {"roots": roots, "order_constraints": order_constraints}


def validate_pipeline(language: str, trace: dict[str, Any]) -> None:
    nodes: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []

    def visit(
        node: dict[str, Any], parent: dict[str, Any] | None = None
    ) -> None:
        nodes.append((parent, node))
        for child in node["children"]:
            visit(child, node)

    for root in trace["roots"]:
        visit(root)

    kafka_outputs = [
        node for _, node in nodes if node["operation"] == "kafka.output"
    ]
    if len(kafka_outputs) != 2:
        raise RuntimeError(
            f"{language} trace must contain exactly two kafka.output spans; "
            f"found {len(kafka_outputs)}"
        )

    delay_nodes = [
        (parent, node)
        for parent, node in nodes
        if node["operation"] == "stream.delay"
        and node["attributes"] == {"stream": "Soft Deadline"}
    ]
    if len(delay_nodes) != 1:
        raise RuntimeError(
            f"{language} trace must contain exactly one Soft Deadline span"
        )
    delay_parent, _ = delay_nodes[0]
    if (
        delay_parent is None
        or delay_parent["operation"] != "stream.call"
        or delay_parent["attributes"].get("to") != "Soft Deadline"
    ):
        raise RuntimeError(
            f"{language} Soft Deadline must run through its configured caller"
        )

    timeout_maps = [
        node
        for _, node in nodes
        if node["operation"] == "stream.map"
        and node["attributes"].get("stream") == "Map To Order State"
    ]
    if timeout_maps:
        raise RuntimeError(
            f"{language} continued after the cancelled Soft Deadline branch"
        )


def inject_cpp_otlp_config(
    text: str,
    *,
    source: Path,
    otlp_variable: str,
    service: str,
) -> str:
    if "    grpc-blocking-task-processor:\n" not in text:
        marker = "\n  default_task_processor: main-task-processor\n"
        processor = (
            "    grpc-blocking-task-processor:\n"
            "      worker_threads: 1\n"
            "      thread_name: grpc-worker\n"
        )
        if marker not in text:
            raise RuntimeError(f"cannot inject gRPC task processor into {source}")
        text = text.replace(marker, processor + marker, 1)
    if "grpc-otlp-factory:" not in text:
        marker = "    servicelib-runtime:\n"
        grpc_common = ""
        if "    grpc-client-common:\n" not in text:
            grpc_common = (
                "    grpc-client-common:\n"
                "      blocking-task-processor: grpc-blocking-task-processor\n"
            )
        otlp = (
            "    grpc-otlp-factory:\n"
            "      disable-all-pipeline-middlewares: true\n"
            "      channel-args: {}\n"
            "      middlewares:\n"
            "        grpc-client-logging:\n"
            "          enabled: false\n"
            "    otlp-logger:\n"
            f"      logs-endpoint: ${otlp_variable}\n"
            f"      tracing-endpoint: ${otlp_variable}\n"
            "      client-factory-name: grpc-otlp-factory\n"
            f"      service-name: {service}\n"
            "      log-level: info\n"
            "      sinks:\n"
            "        logs: default\n"
            "        tracing: otlp\n"
        )
        if marker not in text:
            raise RuntimeError(f"cannot inject OTLP config into {source}")
        # OTLP-only generated services append userver's gRPC client
        # MinimalComponentList alongside the named OTLP factory. That list
        # registers the default grpc-client-common component as well.
        text = text.replace(marker, grpc_common + otlp + marker, 1)
    return text


def prepare_cpp_configs() -> None:
    output = ARTIFACTS / "cpp"
    output.mkdir(parents=True, exist_ok=True)
    config_var_updates = {
        "analyticsservice": {
            "analyticsServiceConfigOverridePath": "/workspace/conformance/analyticsservice.overrides.yaml",
            "analyticsServiceEnvironment": "production",
            "analyticsServiceOtlpEndpoint": "otel-collector:4317",
        },
        "inventoryservice": {
            "inventoryServiceConfigOverridePath": "/workspace/conformance/inventoryservice.overrides.yaml",
            "inventoryServiceEnvironment": "production",
            "inventoryServiceOtlpEndpoint": "otel-collector:4317",
        },
        "orderservice": {
            "orderServiceConfigOverridePath": "/workspace/conformance/orderservice.overrides.yaml",
            "orderServiceEnvironment": "production",
            "orderServiceOtlpEndpoint": "otel-collector:4317",
        },
    }
    for service in ("analyticsservice", "inventoryservice", "orderservice"):
        otlp_variable = (
            "inventoryServiceOtlpEndpoint" if service == "inventoryservice"
            else "orderServiceOtlpEndpoint" if service == "orderservice"
            else "analyticsServiceOtlpEndpoint"
        )
        source = ROOT / "cppexample" / service / "static_config.yaml"
        text = inject_cpp_otlp_config(
            source.read_text(),
            source=source,
            otlp_variable=otlp_variable,
            service=service,
        )
        (output / f"{service}.static_config.yaml").write_text(text)
        override = (
            ROOT
            / "cppexample"
            / service
            / "config"
            / "overrides.integration.generated.yaml"
        ).read_text()
        override = override.replace(
            'environment: ""', "environment: production", 1
        )
        (output / f"{service}.overrides.yaml").write_text(override)
        config_vars_source = (
            ROOT / "cppexample" / service / "config" / "config_vars.integration.yaml"
        )
        updates = config_var_updates[service]
        remaining = dict(updates)
        config_vars_lines = []
        for line in config_vars_source.read_text().splitlines():
            key, separator, _ = line.partition(":")
            if separator and key in updates:
                config_vars_lines.append(f"{key}: {updates[key]}")
                remaining.pop(key, None)
            else:
                config_vars_lines.append(line)
        config_vars_lines.extend(
            f"{key}: {value}" for key, value in remaining.items()
        )
        (output / f"{service}.config_vars.yaml").write_text(
            "\n".join(config_vars_lines) + "\n"
        )


def prepare_python_configs() -> None:
    output = ARTIFACTS / "python"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("analyticsservice", "inventoryservice", "orderservice"):
        source = ROOT / "pyexample" / service / "config" / "docker_overrides.yaml"
        text = source.read_text()
        text = text.replace('environment: ""', "environment: production", 1)
        if service == "orderservice":
            text = text.rstrip() + "\n" + "endpoints:\n  orderProcessed:\n    enabled: true\n"
        (output / f"{service}.overrides.yaml").write_text(text)


def language_env(language: Language) -> dict[str, str]:
    env = os.environ.copy()
    env["SERVICELIB_CONFORMANCE_DIR"] = str(CONFORMANCE)
    # C++ examples contain the Go automation service while Temporal has no
    # supported C++ SDK. Keep that generated fallback on the same local
    # runtime revision as the rest of the mixed-language project.
    env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    if language.name == "cpp":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
        env["ENABLE_OTLP_TRACING"] = "ON"
    elif language.name == "cppboost":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppboostservicelib")
        cpp_source_cache.configure_environment(
            env, ROOT / "cppboostservicelib"
        )
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "pyservicelib")
    elif language.name == "rust":
        env["RUSTSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "rustservicelib")
    elif language.name == "typescript":
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "tsservicelib")
    return env


def print_failure_logs(language: Language, env: dict[str, str]) -> None:
    result = subprocess.run(
        compose_command(
            language,
            "logs",
            "--no-color",
            "--tail",
            "200",
            "inventoryservice",
            "orderservice",
            "analyticsservice",
        ),
        cwd=language.example,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    lines = (result.stdout + result.stderr).splitlines()
    markers = (
        "ERROR",
        "CRITICAL",
        "Invariant",
        "Cannot start",
        "Loading failed",
        "Traceback",
    )
    important = [line for line in lines if any(marker in line for marker in markers)]
    selected = important[-40:] if important else lines[-40:]
    if selected:
        print("\n".join(selected), file=sys.stderr, flush=True)


def run_language(
    language: Language, *, skip_build: bool, keep: bool
) -> dict[str, Any]:
    env = language_env(language)
    graph_profile.verify_generated_project(
        language.example, os.environ.get("EXAMPLE_PROFILE", "function-call")
    )
    if language.name == "cpp":
        prepare_cpp_configs()
    elif language.name == "python":
        prepare_python_configs()
    if not skip_build:
        print(f"[tracing:{language.name}] START build", flush=True)
        build(language, env)
        print(f"[tracing:{language.name}] PASS  build", flush=True)

    try:
        print(
            f"[tracing:{language.name}] START infrastructure and inventory readiness",
            flush=True,
        )
        run(
            compose_command(
                language,
                "up",
                "--detach",
                "jaeger",
                "redpanda",
                "analyticsservice",
                "inventoryservice",
            ),
            cwd=language.example,
            env=env,
        )
        wait_http(f"{JAEGER_URL}/api/services", timeout=60, accepted={200})
        wait_service_http(
            language,
            "inventoryservice",
            "http://localhost:9092/status/data",
            timeout=90,
            env=env,
        )
        wait_service_http(
            language,
            "analyticsservice",
            "http://localhost:9093/status/data",
            timeout=90,
            env=env,
        )
        print(
            f"[tracing:{language.name}] PASS  infrastructure and inventory readiness",
            flush=True,
        )
        print(
            f"[tracing:{language.name}] START orderservice readiness", flush=True
        )
        run(
            compose_command(
                language,
                "up",
                "--detach",
                "orderservice",
            ),
            cwd=language.example,
            env=env,
        )
        wait_service_http(
            language,
            "orderservice",
            ORDER_URL.replace("/v1/processorder", "/status/data"),
            timeout=90,
            env=env,
        )
        for service, port in (
            ("analyticsservice", 9093),
            ("inventoryservice", 9092),
            ("orderservice", 9091),
        ):
            graph_profile.verify_live_service(language.example, service, port)
        print(
            f"[tracing:{language.name}] PASS  orderservice readiness", flush=True
        )
        print(
            f"[tracing:{language.name}] START sampled trace validation", flush=True
        )
        trace_id = secrets.token_hex(16)
        print(f"trace id: {trace_id}", flush=True)
        send_request(trace_id)
        wait_kafka_processing()
        raw = fetch_trace(trace_id)
        assert_unique_grpc_request_stream_ids(raw)
        raw_output = ARTIFACTS / f"{language.name}.trace.raw.json"
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
        normalized = normalize(raw)
        validate_pipeline(language.name, normalized)
        print(
            f"[tracing:{language.name}] PASS  sampled trace validation", flush=True
        )
        print(
            f"[tracing:{language.name}] START unsampled trace validation", flush=True
        )
        service_operations = application_service_operations(raw)
        unsampled_started_at_us = time.time_ns() // 1_000
        send_request()
        assert_no_application_trace(
            service_operations,
            started_at_us=unsampled_started_at_us,
            excluded_trace_ids={trace_id},
        )
        print(
            f"[tracing:{language.name}] PASS  unsampled trace validation", flush=True
        )
        output = ARTIFACTS / f"{language.name}.trace.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
        return normalized
    except Exception:
        print_failure_logs(language, env)
        raise
    finally:
        if not keep:
            subprocess.run(
                compose_command(
                    language, "down", "--volumes", "--remove-orphans"
                ),
                cwd=language.example,
                env=env,
                check=False,
            )


def compare(results: dict[str, dict[str, Any]]) -> None:
    baseline_name = "go"
    baseline_result = results[baseline_name]
    baseline = json.dumps(
        {"roots": baseline_result["roots"]}, indent=2, sort_keys=True
    ).splitlines()
    baseline_order = {
        json.dumps(value, sort_keys=True, separators=(",", ":"))
        for value in baseline_result["order_constraints"]
    }
    failures: list[str] = []
    for name, value in results.items():
        if name == baseline_name:
            continue
        actual = json.dumps(
            {"roots": value["roots"]}, indent=2, sort_keys=True
        ).splitlines()
        if actual != baseline:
            failures.append(
                "\n".join(
                    difflib.unified_diff(
                        baseline,
                        actual,
                        fromfile=f"{baseline_name}.trace.json",
                        tofile=f"{name}.trace.json",
                        lineterm="",
                    )
                )
            )
        actual_order = {
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in value["order_constraints"]
        }
        contradictions: list[dict[str, Any]] = []
        for item in value["order_constraints"]:
            reverse = {
                "parent": item["parent"],
                "before": item["after"],
                "after": item["before"],
            }
            if json.dumps(reverse, sort_keys=True, separators=(",", ":")) in baseline_order:
                contradictions.append(item)
        for item in baseline_result["order_constraints"]:
            reverse = {
                "parent": item["parent"],
                "before": item["after"],
                "after": item["before"],
            }
            if json.dumps(reverse, sort_keys=True, separators=(",", ":")) in actual_order:
                contradictions.append(reverse)
        if contradictions:
            failures.append(
                f"{name} trace reverses Go sibling dispatch order:\n"
                + json.dumps(contradictions, indent=2, sort_keys=True)
            )
    if failures:
        raise RuntimeError(
            "cross-language tracing semantics differ:\n\n" + "\n\n".join(failures)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--language",
        action="append",
        choices=[language.name for language in LANGUAGES],
        help="run only selected language; may be repeated",
    )
    args = parser.parse_args()

    if not args.keep:
        shutil.rmtree(ARTIFACTS, ignore_errors=True)
    selected = [
        language
        for language in LANGUAGES
        if not args.language or language.name in args.language
    ]
    results: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for language in selected:
        print(f"\n=== {language.name} tracing conformance ===", flush=True)
        try:
            results[language.name] = run_language(
                language, skip_build=args.skip_build, keep=args.keep
            )
        except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
            failures[language.name] = str(error)
            print(
                f"[tracing:{language.name}] FAIL  {error}",
                file=sys.stderr,
                flush=True,
            )

    if len(results) > 1 and "go" in results:
        try:
            compare(results)
        except RuntimeError as error:
            failures["comparison"] = str(error)
            (ARTIFACTS / "comparison.diff").write_text(str(error) + "\n")

    summary = {
        "passed": sorted(results),
        "failed": dict(sorted(failures.items())),
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    if failures:
        details = "\n".join(f"- {name}: {error}" for name, error in failures.items())
        raise RuntimeError(f"tracing conformance failed:\n{details}")
    print("\nTracing conformance passed:", ", ".join(results), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
