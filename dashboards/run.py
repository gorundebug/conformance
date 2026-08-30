#!/usr/bin/env python3
"""Validate generated Grafana dashboards against live exported metrics."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


CONFORMANCE = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get("DEPENDENCIES_DIR", CONFORMANCE.parent)
).expanduser().resolve()
METRICS_ARTIFACTS = CONFORMANCE / ".artifacts" / "metrics"
KAFKA_ARTIFACTS = CONFORMANCE / ".artifacts" / "kafka"
TRANSPORTS_ARTIFACT = CONFORMANCE / ".artifacts" / "transports" / "summary.json"
TEMPORAL_ARTIFACT = CONFORMANCE / ".artifacts" / "temporal" / "summary.json"
ARTIFACT = CONFORMANCE / ".artifacts" / "dashboards" / "summary.json"
SERVICES = ("analyticsservice", "inventoryservice", "orderservice")
LANGUAGES = ("go", "cpp", "cppboost", "python", "rust", "typescript")
EXAMPLES = {
    "go": "goexample",
    "cpp": "cppexample",
    "cppboost": "cppboostexample",
    "python": "pyexample",
    "rust": "rustexample",
    "typescript": "tsexample",
}
SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{|\s)")
PROM_QUERY_RE = re.compile(
    r"lib\.promQ\(\s*'((?:\\.|[^'])*)'", re.DOTALL
)
RATE_QUERY_RE = re.compile(r"lib\.rate\(\s*'([a-zA-Z_:][a-zA-Z0-9_:]*)'")
HEATMAP_QUERY_RE = re.compile(
    r"lib\.heatmap\([^)]*?metric\s*=\s*'([a-zA-Z_:][a-zA-Z0-9_:]*)'",
    re.DOTALL,
)
HISTOGRAM_QUERY_RE = re.compile(
    r"lib\.hQuantile(?:By)?\(\s*[^,]+,\s*'([a-zA-Z_:][a-zA-Z0-9_:]*)'"
)
PROM_METRIC_RE = re.compile(
    r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?=\{|\[)"
)
CONDITIONALLY_EMITTED_METRICS = {
    # OTel exporters may omit this asynchronous gauge once the only request
    # has completed. Its query is still checked, but proving a non-empty panel
    # requires a scrape while a request is in flight.
    "http_server_active_requests",
    # Error counters are created on the first matching event. The successful
    # canonical request proves the surrounding endpoint families, while error
    # and recovery scenarios own non-zero evidence for these series.
    "datasource_endpoint_events_total",
    "datasink_endpoint_events_total",
    # prom-client observes V8 GC through PerformanceObserver and does not emit
    # the histogram until a GC cycle occurs during the process lifetime.
    "nodejs_gc_duration_seconds_bucket",
    "nodejs_gc_duration_seconds_count",
}


@dataclass(frozen=True)
class DashboardCheck:
    dashboard: str
    service: str | None
    query_metric: str
    exported_metric: str | None
    evidence: str


CHECKS = {
    "go": (
        DashboardCheck("07_http_server", "orderservice", "http_server_request_duration_seconds", "http_server_request_duration_seconds_bucket", "live"),
        DashboardCheck("08_http_client", None, "http_client_request_duration_seconds", None, "transport-live"),
        DashboardCheck("09_grpc_server", "inventoryservice", "rpc_server_call_duration_seconds", "rpc_server_call_duration_seconds_bucket", "live"),
        DashboardCheck("10_grpc_client", "orderservice", "rpc_client_call_duration_seconds", "rpc_client_call_duration_seconds_bucket", "live"),
        DashboardCheck("11_runtime", "orderservice", "go_goroutines", "go_goroutines", "live"),
        DashboardCheck("12_kafka_client", "orderservice", "sarama_kafka_client_request_rate", "sarama_kafka_client_requests_count", "live"),
        DashboardCheck("12_kafka_client", "analyticsservice", "sarama_kafka_client_fetch_rate", "sarama_kafka_client_consumer_group_joins_count", "live"),
    ),
    "cpp": (
        DashboardCheck("07_http_server", "orderservice", "http_handler_rps", "http_handler_rps", "live"),
        DashboardCheck("08_http_client", None, "datasink_endpoint_messages_total", None, "transport-live"),
        DashboardCheck("09_grpc_server", "inventoryservice", "grpc_server_by_destination_rps", "grpc_server_by_destination_rps", "live"),
        DashboardCheck("10_grpc_client", "orderservice", "grpc_client_by_destination_rps", "grpc_client_by_destination_rps", "live"),
        DashboardCheck("11_runtime", "orderservice", "engine_task_processors_worker_threads", "engine_task_processors_worker_threads", "live"),
        DashboardCheck("12_kafka_client", "orderservice", "kafka_producer_messages_total", "kafka_producer_messages_total", "live"),
        DashboardCheck("12_kafka_client", "analyticsservice", "kafka_consumer_messages_total", "kafka_consumer_messages_total", "live"),
    ),
    "cppboost": (
        DashboardCheck("07_http_server", "orderservice", "datasource_endpoint_messages_total", "datasource_endpoint_messages_total", "live"),
        DashboardCheck("08_http_client", None, "datasink_endpoint_messages_total", None, "transport-live"),
        DashboardCheck("09_grpc_server", "inventoryservice", "datasource_endpoint_messages_total", "datasource_endpoint_messages_total", "live"),
        DashboardCheck("10_grpc_client", "orderservice", "datasink_endpoint_messages_total", "datasink_endpoint_messages_total", "live"),
        DashboardCheck("11_runtime", "orderservice", "runtime_worker_utilization", "runtime_worker_utilization", "live"),
        DashboardCheck("12_kafka_client", "orderservice", "kafka_client_consumer_lag", "kafka_client_brokers", "live"),
    ),
    "python": (
        DashboardCheck("07_http_server", "orderservice", "http_server_request_duration_seconds", "http_server_request_duration_seconds_bucket", "live"),
        DashboardCheck("08_http_client", None, "http_client_request_duration_seconds", None, "transport-live"),
        DashboardCheck("09_grpc_server", "inventoryservice", "rpc_server_call_duration_seconds", "rpc_server_call_duration_seconds_bucket", "live"),
        DashboardCheck("10_grpc_client", "orderservice", "rpc_client_call_duration_seconds", "rpc_client_call_duration_seconds_bucket", "live"),
        DashboardCheck("11_runtime", "orderservice", "python_gc_collections_total", "python_gc_collections_total", "live"),
    ),
    "rust": (
        DashboardCheck("07_http_server", "orderservice", "http_server_request_duration_seconds", "http_server_request_duration_seconds_bucket", "live"),
        DashboardCheck("08_http_client", None, "http_client_request_duration_seconds", None, "transport-live"),
        DashboardCheck("09_grpc_server", "inventoryservice", "rpc_server_call_duration_seconds", "rpc_server_call_duration_seconds_bucket", "live"),
        DashboardCheck("10_grpc_client", "orderservice", "rpc_client_call_duration_seconds", "rpc_client_call_duration_seconds_bucket", "live"),
        DashboardCheck("11_runtime", "orderservice", "tokio_workers_count", "tokio_workers_count", "live"),
        DashboardCheck("12_kafka_client", "orderservice", "kafka_client_consumer_lag", "kafka_client_brokers", "live"),
    ),
    "typescript": (
        DashboardCheck("07_http_server", "orderservice", "datasource_endpoint_messages_total", "datasource_endpoint_messages_total", "live"),
        DashboardCheck("08_http_client", None, "datasink_endpoint_messages_total", None, "transport-live"),
        DashboardCheck("09_grpc_server", "inventoryservice", "datasource_endpoint_messages_total", "datasource_endpoint_messages_total", "live"),
        DashboardCheck("10_grpc_client", "orderservice", "datasink_endpoint_messages_total", "datasink_endpoint_messages_total", "live"),
        DashboardCheck("11_runtime", "orderservice", "nodejs_heap_size_used_bytes", "nodejs_heap_size_used_bytes", "live"),
        DashboardCheck("12_kafka_client", "orderservice", "kafka_client_consumer_lag", "kafka_client_brokers", "live"),
    ),
}


def metric_names(raw: str) -> set[str]:
    names: set[str] = set()
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if match:
            names.add(match.group(1))
    return names


def dashboard_query_metrics(source: str) -> set[str]:
    """Return every metric family referenced by a generated panel target."""
    metrics = set(RATE_QUERY_RE.findall(source))
    metrics.update(
        f"{metric}_bucket" for metric in HEATMAP_QUERY_RE.findall(source)
    )
    metrics.update(
        f"{metric}_bucket" for metric in HISTOGRAM_QUERY_RE.findall(source)
    )
    for expression in PROM_QUERY_RE.findall(source):
        metrics.update(PROM_METRIC_RE.findall(expression))
    return metrics


def load_metrics(language: str, service: str) -> set[str]:
    path = METRICS_ARTIFACTS / language / f"{service}.metrics.raw.txt"
    if not path.is_file():
        raise RuntimeError(f"missing live metrics artifact: {path}")
    return metric_names(path.read_text())


def load_kafka_phase_metrics(
    language: str, phase: str, service: str,
) -> set[str]:
    path = (
        KAFKA_ARTIFACTS
        / language
        / phase
        / f"{service}.metrics.raw.txt"
    )
    if not path.is_file():
        raise RuntimeError(f"missing Kafka metric phase artifact: {path}")
    return metric_names(path.read_text())


def dashboard_path(language: str, service: str, name: str) -> Path:
    return (
        ROOT
        / EXAMPLES[language]
        / service
        / "grafana"
        / "dashboards"
        / f"{name}.jsonnet"
    )


def validate_language(
    language: str,
    http_client_evidence: dict[str, str],
    passed_transport_runs: set[str],
) -> dict[str, object]:
    errors: list[str] = []
    results: list[dict[str, object]] = []
    expected_names = {check.dashboard for check in CHECKS[language]}
    live_metrics_by_dashboard: dict[str, set[str]] = {}
    for check in CHECKS[language]:
        if check.service and check.exported_metric and check.evidence == "live":
            live_metrics_by_dashboard.setdefault(check.dashboard, set()).update(
                load_metrics(language, check.service)
            )
    for check in CHECKS[language]:
        copies: list[str] = []
        contents: list[str] = []
        query_metrics: set[str] = set()
        for service in SERVICES:
            path = dashboard_path(language, service, check.dashboard)
            if not path.is_file():
                errors.append(f"missing dashboard: {path}")
                continue
            text = path.read_text()
            copies.append(str(path.relative_to(ROOT)))
            contents.append(text)
            query_metrics.update(dashboard_query_metrics(text))
            if check.query_metric not in text:
                errors.append(
                    f"{path}: query does not reference {check.query_metric}"
                )
        if contents and any(text != contents[0] for text in contents[1:]):
            errors.append(
                f"{language} {check.dashboard}: service dashboard sources differ"
            )
        if not query_metrics:
            errors.append(
                f"{language} {check.dashboard}: no panel metric queries found"
            )

        exported = None
        missing_live_metrics: list[str] = []
        unobserved_ephemeral_metrics: list[str] = []
        kafka_phase_evidence: dict[str, dict[str, list[str]]] = {}
        transport_evidence_run = None
        if check.evidence == "transport-live":
            transport_evidence_run = http_client_evidence.get(language)
            if not transport_evidence_run:
                errors.append(
                    f"{language} {check.dashboard}: live HTTP-client evidence "
                    "run is not declared"
                )
            elif transport_evidence_run not in passed_transport_runs:
                errors.append(
                    f"{language} {check.dashboard}: transport evidence "
                    f"{transport_evidence_run} did not pass"
                )
        if check.service and check.exported_metric:
            live_metrics = load_metrics(language, check.service)
            exported = check.exported_metric in live_metrics
            if not exported:
                errors.append(
                    f"{language} {check.dashboard}: live {check.service} metrics "
                    f"do not export {check.exported_metric}"
                )
            if check.evidence == "live":
                # A shared dashboard may combine panels for different service
                # roles. For example, the canonical Kafka dashboard reads
                # producer metrics from orderservice and consumer metrics from
                # analyticsservice. Keep checking each required exported
                # metric against its owning service above, while proving the
                # complete panel surface against all live services assigned to
                # this dashboard.
                dashboard_live_metrics = live_metrics_by_dashboard[
                    check.dashboard
                ]
                missing_live_metrics = sorted(
                    query_metrics - dashboard_live_metrics
                )
                unobserved_ephemeral_metrics = sorted(
                    set(missing_live_metrics) & CONDITIONALLY_EMITTED_METRICS
                )
                missing_live_metrics = sorted(
                    set(missing_live_metrics) - CONDITIONALLY_EMITTED_METRICS
                )
                if missing_live_metrics:
                    errors.append(
                        f"{language} {check.dashboard}: live {check.service} "
                        "metrics do not export panel query families "
                        f"{missing_live_metrics}"
                    )
            if check.dashboard == "12_kafka_client":
                for phase in ("healthy", "broker-down", "recovered"):
                    phase_metrics = set().union(*(
                        load_kafka_phase_metrics(language, phase, service)
                        for service in ("orderservice", "analyticsservice")
                    ))
                    phase_missing = sorted(
                        query_metrics
                        - phase_metrics
                        - CONDITIONALLY_EMITTED_METRICS
                    )
                    kafka_phase_evidence[phase] = {
                        "observed": sorted(query_metrics & phase_metrics),
                        "missing": phase_missing,
                    }
                    if phase_missing:
                        errors.append(
                            f"{language} {check.dashboard}: {phase} Kafka "
                            "metrics do not export panel query families "
                            f"{phase_missing}"
                        )
        results.append(
            {
                "dashboard": check.dashboard,
                "copies": copies,
                "query_metric": check.query_metric,
                "panel_query_metrics": sorted(query_metrics),
                "exported_metric": check.exported_metric,
                "evidence": check.evidence,
                "live_metric_present": exported,
                "missing_live_query_metrics": missing_live_metrics,
                "unobserved_ephemeral_query_metrics": (
                    unobserved_ephemeral_metrics
                ),
                "kafka_phase_evidence": kafka_phase_evidence,
                "transport_evidence_run": transport_evidence_run,
            }
        )

    actual_names = {
        path.stem
        for path in (
            ROOT / EXAMPLES[language] / "orderservice" / "grafana" / "dashboards"
        ).glob("[0-9][0-9]_*.jsonnet")
    }
    missing = expected_names - actual_names
    if missing:
        errors.append(f"{language}: missing expected dashboards {sorted(missing)}")
    return {
        "status": "pass" if not errors else "fail",
        "checks": results,
        "errors": errors,
    }


def validate_temporal_dashboard() -> dict[str, object]:
    errors: list[str] = []
    copies: list[str] = []
    contents: dict[str, str] = {}
    for language in LANGUAGES:
        path = (
            ROOT
            / EXAMPLES[language]
            / "automationservice"
            / "grafana"
            / "dashboards" / "13_temporal.generated.jsonnet"
        )
        if not path.is_file():
            errors.append(f"missing Temporal dashboard: {path}")
            continue
        text = path.read_text()
        copies.append(str(path.relative_to(ROOT)))
        contents[language] = text

    required_common_queries = {
        "service_requests",
        "temporal_worker_task_slots_available",
        "temporal_worker_task_slots_used",
        "stream_messages_total",
    }
    metric_variants = {
        "seconds-suffixed": {
            "temporal_request_latency_seconds_bucket",
            "temporal_activity_schedule_to_start_latency_seconds_bucket",
            "temporal_activity_execution_latency_seconds_bucket",
        },
        "unsuffixed": {
            "temporal_request_latency_bucket",
            "temporal_activity_schedule_to_start_latency_bucket",
            "temporal_activity_execution_latency_bucket",
        },
    }
    language_variants = {
        "go": "seconds-suffixed",
        "cpp": "seconds-suffixed",
        "cppboost": "seconds-suffixed",
        "rust": "seconds-suffixed",
        "python": "unsuffixed",
        "typescript": "unsuffixed",
    }
    query_metrics: dict[str, list[str]] = {}
    for language, source in contents.items():
        names = dashboard_query_metrics(source)
        query_metrics[language] = sorted(names)
        required = required_common_queries | metric_variants[language_variants[language]]
        missing_queries = sorted(required - names)
        if missing_queries:
            errors.append(
                f"{language}: Temporal dashboard misses native queries "
                + repr(missing_queries)
            )
        if re.search(r"servicelib_.*durable.*latency", source):
            errors.append(
                f"{language}: Temporal dashboard uses a duplicate ServiceLib latency metric"
            )

    if not TEMPORAL_ARTIFACT.is_file():
        errors.append("Temporal artifacts are absent; run `make temporal` first")
        temporal_result: dict[str, object] = {}
    else:
        temporal_result = json.loads(TEMPORAL_ARTIFACT.read_text())
        if temporal_result.get("status") != "pass":
            errors.append("Temporal dashboard requires a passing Temporal run")
    implementations = temporal_result.get("implementations", {})
    live_evidence: dict[str, list[str]] = {}
    if isinstance(implementations, dict):
        for language in ("go", "python", "typescript"):
            implementation = implementations.get(language, {})
            metrics = (
                implementation.get("temporalMetrics", {})
                if isinstance(implementation, dict)
                else {}
            )
            names = (
                set(metrics.get("sdkMetricNames", []))
                if isinstance(metrics, dict)
                else set()
            )
            variant = "seconds-suffixed" if language == "go" else "unsuffixed"
            required_sdk_series = {
                "temporal_worker_task_slots_available",
                "temporal_worker_task_slots_used",
                *metric_variants[variant],
            }
            live_evidence[language] = sorted(names & required_sdk_series)
            missing = sorted(required_sdk_series - names)
            if missing:
                errors.append(
                    f"{language}: Temporal SDK does not export dashboard series {missing}"
                )

    return {
        "status": "pass" if not errors else "fail",
        "copies": copies,
        "panel_query_metrics": query_metrics,
        "language_metric_variants": language_variants,
        "live_sdk_evidence": live_evidence,
        "duplicate_latency_metric": False,
        "errors": errors,
    }


def main() -> int:
    metrics_summary = METRICS_ARTIFACTS / "summary.json"
    if not metrics_summary.is_file():
        raise RuntimeError(
            "metrics artifacts are absent; run `make metrics` before dashboards"
        )
    metrics_result = json.loads(metrics_summary.read_text())
    if metrics_result.get("failed"):
        raise RuntimeError("dashboard validation requires a passing metrics run")
    missing_metrics_languages = sorted(
        set(LANGUAGES) - set(metrics_result.get("passed", []))
    )
    if missing_metrics_languages:
        raise RuntimeError(
            "dashboard validation requires fresh metrics for "
            + ", ".join(missing_metrics_languages)
        )
    kafka_summary = KAFKA_ARTIFACTS / "summary.json"
    if not kafka_summary.is_file():
        raise RuntimeError(
            "Kafka artifacts are absent; run `make kafka` before dashboards"
        )
    kafka_result = json.loads(kafka_summary.read_text())
    if kafka_result.get("failed"):
        raise RuntimeError("dashboard validation requires a passing Kafka run")
    kafka_metric_languages = set(LANGUAGES) - {"python"}
    missing_kafka_languages = sorted(
        kafka_metric_languages - set(kafka_result.get("languages", []))
    )
    if missing_kafka_languages:
        raise RuntimeError(
            "dashboard validation requires fresh Kafka recovery metrics for "
            + ", ".join(missing_kafka_languages)
        )

    if not TRANSPORTS_ARTIFACT.is_file():
        raise RuntimeError(
            "transport artifacts are absent; run `make transports` before "
            "dashboards"
        )
    transport_result = json.loads(TRANSPORTS_ARTIFACT.read_text())
    if transport_result.get("status") != "pass":
        raise RuntimeError(
            "dashboard validation requires a passing transports run"
        )
    passed_transport_runs = {
        run["name"]
        for run in transport_result.get("runs", [])
        if run.get("exit_code") == 0 and isinstance(run.get("name"), str)
    }
    http_client_evidence = transport_result.get(
        "http_client_live_metric_evidence", {}
    )
    if not isinstance(http_client_evidence, dict):
        raise RuntimeError("transport HTTP-client evidence matrix is invalid")

    implementations = {
        language: validate_language(
            language,
            http_client_evidence,
            passed_transport_runs,
        )
        for language in LANGUAGES
    }
    temporal_dashboard = validate_temporal_dashboard()
    errors = [
        f"{language}: {error}"
        for language, result in implementations.items()
        for error in result["errors"]
    ]
    errors.extend(
        f"temporal: {error}" for error in temporal_dashboard["errors"]
    )
    summary = {
        "status": "pass" if not errors else "fail",
        "languages": list(LANGUAGES),
        "implementations": implementations,
        "temporal": temporal_dashboard,
        "errors": errors,
        "http_client_live_traffic": True,
        "kafka_failure_recovery_metrics": True,
        "kafka_client_metrics_unsupported": {
            "python": "aiokafka exposes no stable public client statistics API"
        },
        "http_client_note": (
            "The canonical graph has no HTTP client edge. Focused transport "
            "integration tests issue a real loopback request through each "
            "language HTTP client and assert the dashboard metric families."
        ),
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if errors:
        raise RuntimeError("dashboard conformance failed:\n" + "\n".join(errors))
    print(
        "Dashboard conformance passed: " + ", ".join(LANGUAGES),
        flush=True,
    )
    print(f"Full report: {ARTIFACT.relative_to(CONFORMANCE)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
