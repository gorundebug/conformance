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


ROOT = Path(__file__).resolve().parents[2]
CONFORMANCE = ROOT / "conformance"
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
        "python",
        ROOT / "pyexample",
        Path(__file__).with_name("compose.python.yml"),
    ),
    Language(
        "rust",
        ROOT / "rustexample",
        Path(__file__).with_name("compose.rust.yml"),
    ),
)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def compose_command(language: Language, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        language.project,
        "--project-directory",
        str(language.example),
        "--file",
        str(language.example / "docker-compose.yml"),
        "--file",
        str(COMMON_COMPOSE),
        "--file",
        str(language.compose),
        *args,
    ]


def build(language: Language, env: dict[str, str]) -> None:
    if language.name == "go":
        run(["make", "docker-build"], cwd=language.example, env=env)
    elif language.name == "cpp":
        run(
            ["./scripts/build.generated.sh", "docker-release"],
            cwd=language.example,
            env={**env, "SERVICEGEN_ENABLE_OTLP_TRACING": "ON"},
        )
    elif language.name in {"python", "rust"}:
        run(
            compose_command(language, "build", "inventoryservice", "orderservice"),
            cwd=language.example,
            env=env,
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
    raise RuntimeError(f"timeout waiting for {url}: {last_error}")


def send_request(trace_id: str | None = None) -> None:
    body = json.dumps(
        {
            "customer_id": "conformance-customer",
            "items": [
                {
                    "item_id": "conformance-item",
                    "sku": "SKU-001",
                    "quantity": 2,
                    "unit_price": 799.0,
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


def _span_tags(span: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tag in span.get("tags", []):
        key = str(tag.get("key", ""))
        if key in SEMANTIC_TAGS:
            result[key] = tag.get("value")
    return dict(sorted(result.items()))


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
            "children": [],
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

    def sort_tree(node: dict[str, Any]) -> None:
        for child in node["children"]:
            sort_tree(child)
        node["children"].sort(
            key=lambda child: json.dumps(child, sort_keys=True, separators=(",", ":"))
        )

    for root in roots:
        sort_tree(root)
    roots.sort(key=lambda node: json.dumps(node, sort_keys=True, separators=(",", ":")))
    return {"roots": roots}


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


def prepare_cpp_configs() -> None:
    output = ARTIFACTS / "cpp"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("inventoryservice", "orderservice"):
        otlp_variable = (
            "inventoryServiceOtlpEndpoint"
            if service == "inventoryservice"
            else "orderServiceOtlpEndpoint"
        )
        source = ROOT / "cppexample" / service / "static_config.yaml"
        text = source.read_text()
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
            text = text.replace(marker, otlp + marker, 1)
        if "    grpc-client-common:\n" not in text:
            marker = "    grpc-otlp-factory:\n"
            text = text.replace(
                marker,
                "    grpc-client-common:\n"
                "      blocking-task-processor: grpc-blocking-task-processor\n"
                "    grpc-client-middlewares-pipeline:\n"
                "    grpc-client-deadline-propagation:\n"
                "    grpc-client-logging:\n"
                + marker,
                1,
            )
        (output / f"{service}.static_config.yaml").write_text(text)

    (output / "inventoryservice.config_vars.yaml").write_text(
        "inventoryPriorityWorkersExecutorsCount: 2\n"
        "inventoryServiceApiAddress: dns:///inventoryservice:9202\n"
        "inventoryServiceApiConnectionsCount: 1\n"
        "inventoryServiceConfigOverridePath: config/overrides.integration.generated.yaml\n"
        "inventoryServiceDefaultGrpcTimeout: 0\n"
        "inventoryServiceEnvironment: production\n"
        "inventoryServiceGrpcHost: 0.0.0.0\n"
        "inventoryServiceGrpcPort: 9202\n"
        "inventoryServiceHttpHost: 0.0.0.0\n"
        "inventoryServiceHttpPort: 9092\n"
        "inventoryServiceOtlpEndpoint: otel-collector:4317\n"
    )
    (output / "orderservice.config_vars.yaml").write_text(
        "defaultPoolExecutorsCount: 2\n"
        "inventoryServiceApiAddress: dns:///inventoryservice:9202\n"
        "inventoryServiceApiConnectionsCount: 1\n"
        "orderServiceConfigOverridePath: config/overrides.integration.generated.yaml\n"
        "orderServiceDefaultGrpcTimeout: 5000\n"
        "orderServiceEnvironment: production\n"
        "orderServiceGrpcHost: 0.0.0.0\n"
        "orderServiceGrpcPort: 9201\n"
        "orderServiceHttpHost: 0.0.0.0\n"
        "orderServiceHttpPort: 9091\n"
        "orderServiceOtlpEndpoint: otel-collector:4317\n"
        "softDeadlineDuration: 1000\n"
    )


def prepare_python_configs() -> None:
    output = ARTIFACTS / "python"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("inventoryservice", "orderservice"):
        source = ROOT / "pyexample" / service / "config" / "docker_overrides.yaml"
        text = source.read_text()
        text = text.replace('environment: ""', "environment: production", 1)
        (output / f"{service}.overrides.yaml").write_text(text)


def language_env(language: Language) -> dict[str, str]:
    env = os.environ.copy()
    env["SERVICELIB_CONFORMANCE_ROOT"] = str(ROOT)
    if language.name == "cpp":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
        env["SERVICEGEN_ENABLE_OTLP_TRACING"] = "ON"
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "pyservicelib")
    elif language.name == "rust":
        env["RUSTSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "rustservicelib")
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
    if language.name == "cpp":
        prepare_cpp_configs()
    elif language.name == "python":
        prepare_python_configs()
    if not skip_build:
        build(language, env)

    try:
        run(
            compose_command(
                language,
                "up",
                "--detach",
                "jaeger",
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
        trace_id = secrets.token_hex(16)
        print(f"trace id: {trace_id}", flush=True)
        send_request(trace_id)
        raw = fetch_trace(trace_id)
        raw_output = ARTIFACTS / f"{language.name}.trace.raw.json"
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
        normalized = normalize(raw)
        validate_pipeline(language.name, normalized)
        service_operations = application_service_operations(raw)
        unsampled_started_at_us = time.time_ns() // 1_000
        send_request()
        assert_no_application_trace(
            service_operations,
            started_at_us=unsampled_started_at_us,
            excluded_trace_ids={trace_id},
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
    baseline = json.dumps(results[baseline_name], indent=2, sort_keys=True).splitlines()
    failures: list[str] = []
    for name, value in results.items():
        if name == baseline_name:
            continue
        actual = json.dumps(value, indent=2, sort_keys=True).splitlines()
        if actual == baseline:
            continue
        diff = "\n".join(
            difflib.unified_diff(
                baseline,
                actual,
                fromfile=f"{baseline_name}.trace.json",
                tofile=f"{name}.trace.json",
                lineterm="",
            )
        )
        failures.append(diff)
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
            print(f"{language.name}: FAILED: {error}", file=sys.stderr, flush=True)

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
