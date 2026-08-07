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


ROOT = Path(__file__).resolve().parents[2]
CONFORMANCE = ROOT / "conformance"
ARTIFACTS = CONFORMANCE / ".artifacts" / "metrics"
ORDER_URL = "http://localhost:9091/v1/processorder"
SERVICE_URLS = {
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


def compose_command(language: Any, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        f"servicelib-metrics-conformance-{language.name}",
        "--project-directory",
        str(language.example),
        "--file",
        str(language.example / "docker-compose.yml"),
        "--file",
        str(language.compose),
        *args,
    ]


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def build(language: Any, env: dict[str, str]) -> None:
    if language.name == "go":
        run(["make", "docker-build"], cwd=language.example, env=env)
    elif language.name == "cpp":
        run(
            ["./scripts/build.generated.sh", "docker-release"],
            cwd=language.example,
            env={**env, "SERVICEGEN_ENABLE_OTLP_TRACING": "ON"},
        )
    else:
        run(
            compose_command(language, "build", "inventoryservice", "orderservice"),
            cwd=language.example,
            env=env,
        )


def language_env(language: Any) -> dict[str, str]:
    env = os.environ.copy()
    if language.name == "cpp":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
        env["SERVICEGEN_ENABLE_OTLP_TRACING"] = "ON"
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "pyservicelib")
    elif language.name == "rust":
        env["RUSTSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "rustservicelib")
    return env


def prepare_cpp_configs() -> None:
    output = ARTIFACTS / "cpp"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("inventoryservice", "orderservice"):
        shutil.copyfile(
            ROOT / "cppexample" / service / "static_config.yaml",
            output / f"{service}.static_config.yaml",
        )
    (output / "inventoryservice.config_vars.yaml").write_text(
        "inventoryPriorityWorkersExecutorsCount: 2\n"
        "inventoryServiceApiAddress: dns:///inventoryservice:9202\n"
        "inventoryServiceApiConnectionsCount: 1\n"
        "inventoryServiceConfigOverridePath: config/overrides.integration.generated.yaml\n"
        "inventoryServiceDefaultGrpcTimeout: 0\n"
        "inventoryServiceEnvironment: debug\n"
        "inventoryServiceGrpcHost: 0.0.0.0\n"
        "inventoryServiceGrpcPort: 9202\n"
        "inventoryServiceHttpHost: 0.0.0.0\n"
        "inventoryServiceHttpPort: 9092\n"
        "inventoryServiceOtlpEndpoint: jaeger:4317\n"
    )
    (output / "orderservice.config_vars.yaml").write_text(
        "defaultPoolExecutorsCount: 2\n"
        "inventoryServiceApiAddress: dns:///inventoryservice:9202\n"
        "inventoryServiceApiConnectionsCount: 1\n"
        "orderServiceConfigOverridePath: config/overrides.integration.generated.yaml\n"
        "orderServiceDefaultGrpcTimeout: 5000\n"
        "orderServiceEnvironment: debug\n"
        "orderServiceGrpcHost: 0.0.0.0\n"
        "orderServiceGrpcPort: 9201\n"
        "orderServiceHttpHost: 0.0.0.0\n"
        "orderServiceHttpPort: 9091\n"
        "orderServiceOtlpEndpoint: jaeger:4317\n"
        "softDeadlineDuration: 1000\n"
    )


def prepare_python_configs() -> None:
    output = ARTIFACTS / "python"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("inventoryservice", "orderservice"):
        source = ROOT / "pyexample" / service / "config" / "docker_overrides.yaml"
        text = source.read_text().replace(
            'environment: ""', "environment: debug", 1
        )
        (output / f"{service}.overrides.yaml").write_text(text)


def wait_service(
    language: Any, service: str, base_url: str, env: dict[str, str]
) -> None:
    TRACING.wait_service_http(
        language,
        service,
        f"{base_url}/status/data",
        timeout=90,
        env=env,
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
            if parsed == 0 and metric_type in {"counter", "histogram", "summary"}:
                continue
            labels = parse_labels(match.group("labels"))
            labels = {
                key: value
                for key, value in labels.items()
                if key not in EXPORTER_LABELS
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
) -> dict[str, Any]:
    env = language_env(language)
    if language.name == "cpp":
        prepare_cpp_configs()
    elif language.name == "python":
        prepare_python_configs()
    if not skip_build:
        build(language, env)

    try:
        services = ["inventoryservice"]
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
        run(
            compose_command(language, "up", "--detach", "orderservice"),
            cwd=language.example,
            env=env,
        )
        wait_service(
            language, "orderservice", SERVICE_URLS["orderservice"], env
        )
        send_request()
        raw_by_service = {
            service: fetch(f"{base_url}/metrics")
            for service, base_url in SERVICE_URLS.items()
        }
        language_dir = ARTIFACTS / language.name
        language_dir.mkdir(parents=True, exist_ok=True)
        for service, raw in raw_by_service.items():
            (language_dir / f"{service}.metrics.raw.txt").write_text(raw)
        normalized = normalize(raw_by_service)
        (ARTIFACTS / f"{language.name}.metrics.json").write_text(
            json.dumps(normalized, indent=2, sort_keys=True) + "\n"
        )
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
        failures.append(
            "\n".join(
                difflib.unified_diff(
                    baseline,
                    actual,
                    fromfile="go.metrics.json",
                    tofile=f"{name}.metrics.json",
                    lineterm="",
                )
            )
        )
    if failures:
        raise RuntimeError(
            "cross-language metrics semantics differ:\n\n"
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
    failures: dict[str, str] = {}
    for language in selected:
        print(f"\n=== {language.name} metrics conformance ===", flush=True)
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
