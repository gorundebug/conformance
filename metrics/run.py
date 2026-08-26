#!/usr/bin/env python3

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CONFORMANCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE))
import cpp_source_cache

ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE.parent)).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "metrics"
ORDER_URL = "http://localhost:9091/v1/processorder"
SERVICE_URLS = {
    "analyticsservice": "http://localhost:9093",
    "inventoryservice": "http://localhost:9092",
    "orderservice": "http://localhost:9091",
}

FRAMEWORK_PREFIXES = (
    "caller_",
    "datasink_connector_",
    "datasink_endpoint_",
    "datasource_connector_",
    "datasource_endpoint_",
    "delay_pool_",
    "hash_map_",
    "join_storage_",
    "priority_task_pool_",
    "rotating_map_",
    "service_",
    "stream_",
    "task_pool_",
)
EXPORTER_LABELS = {
    "otel_scope_name",
    "otel_scope_schema_url",
    "otel_scope_version",
}
SHAPE_ONLY_SUFFIXES = ("_sum",)
IGNORED_SUFFIXES = ("_bucket", "_created")

SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?P<labels>\{.*\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|"
    r"[-+]?Inf|NaN)(?:\s+\d+)?$"
)
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"')
TYPE_RE = re.compile(
    r"^# TYPE (?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*) "
    r"(?P<type>counter|gauge|histogram|summary|untyped)$"
)


def load_tracing_runner() -> Any:
    source = CONFORMANCE / "tracing" / "run.py"
    spec = importlib.util.spec_from_file_location("tracing_conformance", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shared conformance helpers from {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TRACING = load_tracing_runner()
Language = TRACING.Language
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


def compose_command(language: Any, *args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        f"servicelib-metrics-conformance-{language.name}",
        "--project-directory",
        str(language.example),
        "--file",
        str(language.example / "docker-compose.yml"),
    ]
    for runtime_overlay in sorted(
        language.example.glob("docker-compose.*-runtime.generated.yml")
    ):
        command.extend(["--file", str(runtime_overlay)])
    command.extend(["--file", str(language.compose), *args])
    return command


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def build(language: Any, env: dict[str, str]) -> None:
    if language.name == "go":
        run(["make", "docker-build"], cwd=language.example, env=env)
    elif language.name == "typescript":
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
        )
    elif language.name in {"cpp", "cppboost"}:
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
        )
    else:
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
        )


def language_env(language: Any) -> dict[str, str]:
    env = os.environ.copy()
    env["SERVICELIB_CONFORMANCE_DIR"] = str(CONFORMANCE)
    # C++ examples use the generated Go automation service as their Temporal
    # fallback, so a local mixed-language build needs both runtime contexts.
    env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    if language.name == "cpp":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
    elif language.name == "cppboost":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppboostservicelib")
        env["CPPBOOSTSERVICELIB_SOURCE_CONTEXT"] = str(
            ROOT / "cppboostservicelib"
        )
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


def prepare_cpp_configs() -> None:
    output = ARTIFACTS / "cpp"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("analyticsservice", "inventoryservice", "orderservice"):
        shutil.copyfile(
            ROOT / "cppexample" / service / "static_config.yaml",
            output / f"{service}.static_config.yaml",
        )
        override = (
            ROOT
            / "cppexample"
            / service
            / "config"
            / "overrides.integration.generated.yaml"
        ).read_text()
        override = override.replace(
            'environment: ""', "environment: debug", 1
        )
        (output / f"{service}.overrides.yaml").write_text(override)
        config_vars = (
            ROOT
            / "cppexample"
            / service
            / "config"
            / "config_vars.integration.yaml"
        ).read_text()
        config_vars = config_vars.replace(
            f"{service.removesuffix('service')}ServiceConfigOverridePath: "
            "config/overrides.integration.generated.yaml",
            f"{service.removesuffix('service')}ServiceConfigOverridePath: "
            f"/workspace/conformance/{service}.overrides.yaml",
            1,
        ).replace('Environment: ""', "Environment: debug", 1)
        (output / f"{service}.config_vars.yaml").write_text(config_vars)


def prepare_python_configs() -> None:
    output = ARTIFACTS / "python"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("analyticsservice", "inventoryservice", "orderservice"):
        source = ROOT / "pyexample" / service / "config" / "docker_overrides.yaml"
        text = source.read_text().replace(
            'environment: ""', "environment: debug", 1
        )
        if service == "orderservice":
            text = text.rstrip() + "\n" + "endpoints:\n  orderProcessed:\n    enabled: true\n"
        (output / f"{service}.overrides.yaml").write_text(text)


def prepare_cppboost_configs() -> None:
    output = ARTIFACTS / "cppboost"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("analyticsservice", "inventoryservice", "orderservice"):
        source = (
            ROOT
            / "cppboostexample"
            / service
            / "config"
            / "overrides.yaml"
        )
        text = (
            source.read_text()
            .replace('environment: ""', "environment: debug", 1)
            .replace("dns:///localhost:9202", "dns:///inventoryservice:9202")
        )
        if service == "orderservice":
            text = text.replace("enabled: false", "enabled: true")
        (output / f"{service}.overrides.yaml").write_text(text)


def wait_service(
    language: Any, service: str, base_url: str, env: dict[str, str]
) -> None:
    deadline = time.monotonic() + 90
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{base_url}/status/data", timeout=2
            ) as response:
                if response.status == 200:
                    return
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
            raise RuntimeError(
                f"{language.name} {service} exited before readiness"
            )
        time.sleep(0.5)
    raise RuntimeError(
        f"timeout waiting for {language.name} {service}: {last_error}"
    )


def send_request() -> None:
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
    request = urllib.request.Request(
        ORDER_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
        if response.status != 200 or payload.get("status") != "CONFIRMED":
            raise RuntimeError(f"unexpected response: {response.status} {payload}")


def fetch(url: str, *, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return response.read().decode()
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(f"timeout fetching {url}: {last_error}")


def wait_kafka_processing(*, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    latest = ""
    while time.monotonic() < deadline:
        latest = fetch(f"{SERVICE_URLS['analyticsservice']}/status/data", timeout=3)
        graph = normalize_runtime_graphs({"analyticsservice": latest})[
            "analyticsservice"
        ]
        if any(edge.get("calls", 0) > 0 for edge in graph["edges"]):
            return
        time.sleep(0.2)
    raise RuntimeError(
        "Analytics Service did not process the Kafka order event; latest "
        f"/status/data: {latest}"
    )


def wait_metric(base_url: str, metric: str, *, timeout: float = 10) -> None:
    """Wait for metrics produced by a client's documented periodic callback."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            raw = fetch(f"{base_url}/metrics", timeout=3)
            if re.search(rf"(?m)^{re.escape(metric)}(?:\{{|\s)", raw):
                return
        except Exception as error:  # noqa: BLE001
            last_error = error
        time.sleep(0.2)
    detail = "" if last_error is None else f": {last_error}"
    raise RuntimeError(f"timed out waiting for metric {metric} at {base_url}{detail}")


def parse_labels(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    labels: dict[str, str] = {}
    for match in LABEL_RE.finditer(text[1:-1]):
        labels[match.group(1)] = json.loads(f'"{match.group(2)}"')
    return labels


def parse_value(text: str) -> float:
    if text == "+Inf":
        return float("inf")
    if text == "-Inf":
        return float("-inf")
    return float(text)


def normalize(raw_by_service: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    for service, raw in sorted(raw_by_service.items()):
        metric_types = {
            match.group("name").replace(":", "_"): match.group("type")
            for line in raw.splitlines()
            if (match := TYPE_RE.match(line))
        }
        observed_histograms: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for line in raw.splitlines():
            match = SAMPLE_RE.match(line)
            if match is None:
                continue
            name = match.group("name").replace(":", "_")
            if not name.endswith("_count") or parse_value(match.group("value")) <= 0:
                continue
            family_name = name[: -len("_count")]
            if metric_types.get(family_name) not in {"histogram", "summary"}:
                continue
            observed_histograms.add(
                (family_name, tuple(sorted(parse_labels(match.group("labels")).items())))
            )
        series: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            match = SAMPLE_RE.match(line)
            if not match:
                continue
            name = match.group("name").replace(":", "_")
            if not name.startswith(FRAMEWORK_PREFIXES):
                continue
            if name.endswith(IGNORED_SUFFIXES):
                continue
            family_name = name
            for suffix in ("_count", "_sum"):
                if family_name.endswith(suffix):
                    family_name = family_name[: -len(suffix)]
                    break
            metric_type = metric_types.get(
                name, metric_types.get(family_name, "untyped")
            )
            parsed = parse_value(match.group("value"))
            # Prometheus backends differ in whether a registered but untouched
            # counter/histogram is exported as zero or omitted. That is exporter
            # behavior, not a ServiceLib semantic difference. Gauges remain in
            # the contract even at zero because their idle value is observable.
            labels = parse_labels(match.group("labels"))
            if parsed == 0 and metric_type in {"counter", "histogram", "summary"}:
                observed_zero_sum = (
                    name.endswith("_sum")
                    and (
                        family_name,
                        tuple(sorted(labels.items())),
                    )
                    in observed_histograms
                )
                if not observed_zero_sum:
                    continue
            labels = {
                key: value
                for key, value in labels.items()
                if key not in EXPORTER_LABELS
                and not (key == "protocol" and value == "")
            }
            value: float | int | None
            if name.endswith(SHAPE_ONLY_SUFFIXES):
                value = None
            else:
                value = int(parsed) if parsed.is_integer() else parsed
            series.append(
                {
                    "name": name,
                    "labels": dict(sorted(labels.items())),
                    "value": value,
                }
            )
        series.sort(
            key=lambda item: (
                item["name"],
                json.dumps(item["labels"], sort_keys=True, separators=(",", ":")),
            )
        )
        if not series:
            raise RuntimeError(f"{service} exported no ServiceLib metrics")
        normalized[service] = series
    return normalized


def normalize_runtime_graphs(raw_by_service: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for service, raw in sorted(raw_by_service.items()):
        try:
            graph = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"{service} /status/data returned invalid JSON: {error}"
            ) from error
        if not isinstance(graph, dict):
            raise RuntimeError(f"{service} /status/data is not a JSON object")
        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise RuntimeError(
                f"{service} /status/data must contain node and edge arrays"
            )
        if not nodes:
            raise RuntimeError(f"{service} /status/data contains no graph nodes")
        if any(not isinstance(node, dict) or "id" not in node for node in nodes):
            raise RuntimeError(f"{service} /status/data contains an invalid node")
        if any(
            not isinstance(edge, dict)
            or "from" not in edge
            or "to" not in edge
            for edge in edges
        ):
            raise RuntimeError(f"{service} /status/data contains an invalid edge")

        id_to_node: dict[int, str] = {}
        normalized_nodes: list[dict[str, Any]] = []
        for node in nodes:
            node_id = node["id"]
            label = node.get("label")
            if not isinstance(node_id, int) or not isinstance(label, str):
                raise RuntimeError(
                    f"{service} /status/data node id/label has invalid type"
                )
            if node_id in id_to_node or label in id_to_node.values():
                raise RuntimeError(
                    f"{service} /status/data contains duplicate node identity"
                )
            id_to_node[node_id] = label
            # Numeric topology IDs are runtime implementation details. Keep
            # every observable node field and use its complete display label
            # as the stable cross-language identity.
            normalized_node = dict(node)
            normalized_node.pop("id")
            normalized_nodes.append(normalized_node)

        normalized_edges: list[dict[str, Any]] = []
        for edge in edges:
            source = id_to_node.get(edge["from"])
            target = id_to_node.get(edge["to"])
            if source is None or target is None:
                raise RuntimeError(
                    f"{service} /status/data edge {edge['from']} -> "
                    f"{edge['to']} references a missing node"
                )
            label = edge.get("label")
            if not isinstance(label, str):
                raise RuntimeError(
                    f"{service} /status/data edge label has invalid type"
                )
            label_lines = label.split("\n") if label else []
            type_name = label_lines[0] if label_lines else ""
            calls = 0
            side = ""
            if type_name.startswith("calls: "):
                calls_text = type_name.removeprefix("calls: ")
                type_name = ""
            elif len(label_lines) > 1 and label_lines[1].startswith("calls: "):
                calls_text = label_lines[1].removeprefix("calls: ")
            else:
                calls_text = "0"
            if calls_text.endswith(" (L)") or calls_text.endswith(" (R)"):
                side = calls_text[-3:]
                calls_text = calls_text[:-4]
            try:
                calls = int(calls_text)
            except ValueError as error:
                raise RuntimeError(
                    f"{service} /status/data has invalid call count: {label!r}"
                ) from error
            normalized_edge = dict(edge)
            normalized_edge.pop("from")
            normalized_edge.pop("to")
            normalized_edge.pop("label")
            normalized_edge.update(
                {
                    "from": source,
                    "to": target,
                    "type": type_name,
                    "calls": calls,
                    "join_side": side,
                }
            )
            normalized_edges.append(normalized_edge)

        # Array order is not part of the graph contract. No node/edge field is
        # discarded apart from implementation-local IDs and the parsed label.
        graph["nodes"] = sorted(
            normalized_nodes,
            key=lambda node: json.dumps(
                node, sort_keys=True, separators=(",", ":")
            ),
        )
        graph["edges"] = sorted(
            normalized_edges,
            key=lambda edge: json.dumps(
                edge, sort_keys=True, separators=(",", ":")
            ),
        )
        normalized[service] = graph
    return normalized


def print_failure_logs(language: Any, env: dict[str, str]) -> None:
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
    markers = ("ERROR", "CRITICAL", "Invariant", "Traceback", "panicked")
    important = [line for line in lines if any(marker in line for marker in markers)]
    selected = important[-40:] if important else lines[-40:]
    if selected:
        print("\n".join(selected), file=sys.stderr, flush=True)


def run_language(
    language: Any, *, skip_build: bool, keep: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    env = language_env(language)
    if language.name == "cpp":
        prepare_cpp_configs()
    elif language.name == "cppboost":
        prepare_cppboost_configs()
    elif language.name == "python":
        prepare_python_configs()
    if not skip_build:
        build(language, env)

    try:
        services = ["redpanda", "analyticsservice", "inventoryservice"]
        if language.name == "cpp":
            services.insert(0, "jaeger")
        run(
            compose_command(language, "up", "--detach", *services),
            cwd=language.example,
            env=env,
        )
        wait_service(
            language,
            "inventoryservice",
            SERVICE_URLS["inventoryservice"],
            env,
        )
        wait_service(
            language,
            "analyticsservice",
            SERVICE_URLS["analyticsservice"],
            env,
        )
        run(
            compose_command(language, "up", "--detach", "orderservice"),
            cwd=language.example,
            env=env,
        )
        wait_service(
            language, "orderservice", SERVICE_URLS["orderservice"], env
        )
        send_request()
        wait_kafka_processing()
        if language.name == "go":
            wait_metric(
                SERVICE_URLS["orderservice"],
                "sarama_kafka_client_requests_count",
            )
            wait_metric(
                SERVICE_URLS["analyticsservice"],
                "sarama_kafka_client_requests_count",
            )
        elif language.name == "cpp":
            wait_metric(
                SERVICE_URLS["orderservice"],
                "kafka_producer_messages_total",
            )
            wait_metric(
                SERVICE_URLS["analyticsservice"],
                "kafka_consumer_messages_total",
            )
        elif language.name in {"cppboost", "rust", "typescript"}:
            wait_metric(SERVICE_URLS["orderservice"], "kafka_client_brokers")
            wait_metric(SERVICE_URLS["analyticsservice"], "kafka_client_brokers")
        raw_by_service = {
            service: fetch(f"{base_url}/metrics")
            for service, base_url in SERVICE_URLS.items()
        }
        raw_graphs_by_service = {
            service: fetch(f"{base_url}/status/data")
            for service, base_url in SERVICE_URLS.items()
        }
        language_dir = ARTIFACTS / language.name
        language_dir.mkdir(parents=True, exist_ok=True)
        for service, raw in raw_by_service.items():
            (language_dir / f"{service}.metrics.raw.txt").write_text(raw)
        for service, raw in raw_graphs_by_service.items():
            (language_dir / f"{service}.status-data.raw.json").write_text(
                raw.rstrip() + "\n"
            )
        normalized = normalize(raw_by_service)
        normalized_graphs = normalize_runtime_graphs(raw_graphs_by_service)
        (ARTIFACTS / f"{language.name}.metrics.json").write_text(
            json.dumps(normalized, indent=2, sort_keys=True) + "\n"
        )
        (ARTIFACTS / f"{language.name}.runtime-graph.json").write_text(
            json.dumps(normalized_graphs, indent=2, sort_keys=True) + "\n"
        )
        return normalized, normalized_graphs
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


def compare(
    results: dict[str, dict[str, Any]], *, artifact: str, description: str
) -> None:
    baseline_name = "go"
    baseline = json.dumps(results[baseline_name], indent=2, sort_keys=True).splitlines()
    failures: list[str] = []
    for name, value in results.items():
        if name == baseline_name:
            continue
        actual = json.dumps(value, indent=2, sort_keys=True).splitlines()
        if value == results[baseline_name]:
            continue
        failures.append(
            "\n".join(
                difflib.unified_diff(
                    baseline,
                    actual,
                    fromfile=f"go.{artifact}.json",
                    tofile=f"{name}.{artifact}.json",
                    lineterm="",
                )
            )
        )
    if failures:
        raise RuntimeError(
            f"cross-language {description} semantics differ:\n\n"
            + "\n\n".join(failures)
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
    graph_results: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for language in selected:
        print(f"\n=== {language.name} metrics conformance ===", flush=True)
        try:
            metrics, runtime_graphs = run_language(
                language, skip_build=args.skip_build, keep=args.keep
            )
            results[language.name] = metrics
            graph_results[language.name] = runtime_graphs
        except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
            failures[language.name] = str(error)
            print(f"{language.name}: FAILED: {error}", file=sys.stderr, flush=True)

    if len(results) > 1 and "go" in results:
        try:
            compare(results, artifact="metrics", description="metrics")
        except RuntimeError as error:
            failures["metrics-comparison"] = str(error)
            (ARTIFACTS / "metrics-comparison.diff").write_text(
                str(error) + "\n"
            )
        try:
            compare(
                graph_results,
                artifact="runtime-graph",
                description="runtime graph",
            )
        except RuntimeError as error:
            failures["runtime-graph-comparison"] = str(error)
            (ARTIFACTS / "runtime-graph-comparison.diff").write_text(
                str(error) + "\n"
            )

    summary = {"passed": sorted(results), "failed": dict(sorted(failures.items()))}
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    if failures:
        details = "\n".join(f"- {name}: {error}" for name, error in failures.items())
        raise RuntimeError(f"metrics conformance failed:\n{details}")
    print("\nMetrics conformance passed:", ", ".join(results), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
