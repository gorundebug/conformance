#!/usr/bin/env python3
"""Validate generated Helm charts and perform one real local k3s rollout."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


CONFORMANCE = Path(__file__).resolve().parent.parent
ROOT = Path(
    os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "kubernetes"
SUMMARY = ARTIFACTS / "summary.json"
HELM_IMAGE = "alpine/helm:4.2.3"
SERVICES = ("analyticsservice", "inventoryservice", "orderservice")
EXAMPLES = {
    "go": "goexample",
    "cpp": "cppexample",
    "cppboost": "cppboostexample",
    "python": "pyexample",
    "rust": "rustexample",
    "typescript": "tsexample",
}


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print("- " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=env, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: "
            + " ".join(command)
        )


def capture(
    command: list[str], cwd: Path, env: dict[str, str], *, input_value: str | None = None,
    verbose: bool = True,
) -> str:
    if verbose:
        print("- " + " ".join(command), flush=True)
    result = subprocess.run(
        command, cwd=cwd, env=env, input=input_value, text=True,
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: "
            + " ".join(command) + "\n" + result.stdout + result.stderr
        )
    return result.stdout


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def validate_example(language: str, example: Path) -> None:
    print(f"[kubernetes] START {language} static contract", flush=True)
    required = [
        example / "docker-compose.kubernetes.yml",
        example / "kubernetes" / "jaeger-values.generated.yaml",
        example / "kubernetes" / "loki-values.generated.yaml",
        example / "kubernetes" / "monitoring-values.generated.yaml",
        example / "kubernetes" / "otel-collector-values.generated.yaml",
        example / "kubernetes" / "registries.generated.yaml",
        example / "kubernetes" / "redpanda-values.generated.yaml",
        example / "scripts" / "kubernetes.generated.sh",
    ]
    for service in SERVICES:
        chart = example / service / "helm"
        required.extend(
            [
                chart / "Chart.yaml",
                chart / "values.generated.yaml",
                chart / "values.yaml",
                chart / "values.schema.json",
                chart / "templates" / "workload.generated.yaml",
                chart / "templates" / "configmap.generated.yaml",
                chart / "templates" / "extra-objects.generated.yaml",
                chart / "templates" / "service.generated.yaml",
                chart / "templates" / "servicemonitor.generated.yaml",
            ]
        )
    missing = [str(path.relative_to(example)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"{language} generated Kubernetes files are missing: {missing}")

    project_script = (example / "scripts" / "kubernetes.generated.sh").read_text()
    compose_text = (example / "docker-compose.kubernetes.yml").read_text()
    for token in (
        "kubernetes-image-cache:/var/lib/rancher/k3s/agent/containerd",
        "servicegen-kubernetes-image-cache-v1",
        "external: true",
    ):
        if token not in compose_text:
            raise RuntimeError(
                f"{language} Kubernetes tooling lacks persistent image cache: "
                f"{token}"
            )
    if (
        'create secret generic "${KAFKA_SERVICE_SECRET}"' not in project_script
        or "secretEnvFrom[0]" not in project_script
    ):
        raise RuntimeError(
            f"{language} Kubernetes tooling does not wire generated Kafka Secret env"
        )
    for token in (
        "kube-prometheus-stack", "opentelemetry-collector", "jaegertracing/jaeger",
        "grafana/loki", "OTEL_EXPORTER_OTLP_ENDPOINT", "grafana_dashboard=1",
        "annotate configmap",
    ):
        if token not in project_script:
            raise RuntimeError(
                f"{language} Kubernetes observability tooling is missing {token}"
            )
    otel_values = (
        example / "kubernetes" / "otel-collector-values.generated.yaml"
    ).read_text()
    for token in (
        "metricRelabelings:",
        "sourceLabels: [exported_service]",
        "targetLabel: service",
        "sourceLabels: [exported_job]",
        "targetLabel: job",
    ):
        if token in otel_values:
            continue
        raise RuntimeError(
            f"{language} Kubernetes Prometheus scrape does not normalize "
            f"dashboard labels: missing {token}"
        )
    for service in SERVICES:
        service_monitor = (
            example / service / "helm" / "templates" /
            "servicemonitor.generated.yaml"
        ).read_text()
        for token in (
            "kind: ServiceMonitor", "port: http", "path:",
            "honorLabels: true",
        ):
            if token not in service_monitor:
                raise RuntimeError(
                    f"{language} {service} ServiceMonitor is missing {token}"
                )
        workload = (
            example / service / "helm" / "templates" / "workload.generated.yaml"
        ).read_text()
        for token in ("startupProbe:", "readinessProbe:", "livenessProbe:", "envFrom:"):
            if token not in workload:
                raise RuntimeError(
                    f"{language} {service} workload is missing {token.rstrip(':')}"
                )
    if language == "cpp":
        static_config = (example / "analyticsservice" / "static_config.yaml").read_text()
        for token in (
            "security_protocol#env: ORDER_EVENTS_SECURITY_PROTOCOL",
            "sasl_mechanisms#env: ORDER_EVENTS_SASL_MECHANISM",
            'set -- "$@" --from-literal="SECDIST_CONFIG=',
        ):
            source = static_config if "#env:" in token else project_script
            if token not in source:
                raise RuntimeError(
                    "cpp userver Kubernetes Kafka SASL bridge is incomplete: " + token
                )

    run(["bash", "-n", "scripts/kubernetes.generated.sh"], example)
    run(
        [
            "docker", "compose", "-f",
            "docker-compose.kubernetes.yml", "config", "--quiet",
        ],
        example,
    )
    chart_command = (
        "for chart in analyticsservice/helm inventoryservice/helm "
        "orderservice/helm; do "
        "helm lint \"$chart\" --values \"$chart/values.generated.yaml\" "
        "--values \"$chart/values.yaml\"; "
        "helm template conformance \"$chart\" "
        "--values \"$chart/values.generated.yaml\" "
        "--values \"$chart/values.yaml\" "
        "--set metrics.serviceMonitor.enabled=true >/dev/null; done"
    )
    run(
        [
            "docker", "run", "--rm", "--entrypoint", "sh",
            "-v", f"{example}:/workspace", "-w", "/workspace",
            HELM_IMAGE, "-ec", chart_command,
        ],
        example,
    )
    print(f"[kubernetes] PASS  {language} static contract", flush=True)


def runtime_probe(example: Path) -> None:
    print("[kubernetes] START go local k3s runtime", flush=True)
    environment = dict(os.environ)
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": (
                f"servicelib-kubernetes-conformance-{os.getpid()}-{int(time.time())}"
            ),
            "KUBERNETES_API_PORT": str(free_port()),
            "KUBERNETES_REGISTRY_PORT": str(free_port()),
            "KUBERNETES_NAMESPACE": "servicelib-conformance",
            "KUBERNETES_TIMEOUT": "10m",
            "KUBERNETES_KAFKA_SASL_ENABLED": "true",
            "KUBERNETES_KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
            "KUBERNETES_KAFKA_USERNAME": "servicegen-conformance",
            "KUBERNETES_KAFKA_PASSWORD": (
                f"servicegen-conformance-{os.getpid()}-{int(time.time())}"
            ),
            "SERVICELIB_SOURCE_CONTEXT": str(ROOT / "servicelib"),
        }
    )
    script = ["bash", "scripts/kubernetes.generated.sh"]
    compose = ["docker", "compose", "-f", "docker-compose.kubernetes.yml"]
    kubectl = [*compose, "exec", "-T", "kubernetes", "kubectl"]
    try:
        run([*script, "up"], example, environment)
        run([*script, "test"], example, environment)
        request_body = json.dumps({
            "customer_id": "kubernetes-check",
            "items": [{
                "item_id": "kubernetes-check-item", "sku": "SKU-001",
                "quantity": 1, "unit_price": 10,
            }],
        })
        capture(
            [
                *kubectl, "create", "--raw",
                "/api/v1/namespaces/servicelib-conformance/services/"
                "http:orderservice:9091/proxy/v1/processorder",
                "-f", "-",
            ],
            example, environment, input_value=request_body,
        )
        service_ip = capture(
            [
                *kubectl, "--namespace", "servicelib-conformance", "get", "service",
                "orderservice", "-o", "jsonpath={.spec.clusterIP}",
            ],
            example, environment,
        ).strip()
        trace_id = "0123456789abcdef0123456789abcdef"
        run(
            [
                *compose, "exec", "-T", "kubernetes", "wget", "-qO-",
                "--header=Content-Type: application/json", "--header=x-trace: 1",
                "--header=traceparent: "
                f"00-{trace_id}-0123456789abcdef-01",
                f"--post-data={request_body}",
                f"http://{service_ip}:9091/v1/processorder",
            ],
            example, environment,
        )
        telemetry_deadline = time.monotonic() + 60
        telemetry_ready = False
        telemetry_status = ""
        while time.monotonic() < telemetry_deadline:
            jaeger = json.loads(capture(
                [
                    *kubectl, "get", "--raw",
                    "/api/v1/namespaces/servicelib-conformance/services/"
                    "http:jaeger:16686/proxy/api/services",
                ],
                example, environment, verbose=False,
            ))
            prometheus = json.loads(capture(
                [
                    *kubectl, "get", "--raw",
                    "/api/v1/namespaces/servicelib-conformance/services/"
                    "http:monitoring-kube-prometheus-prometheus:9090/proxy/"
                    "api/v1/query?query=service_info",
                ],
                example, environment, verbose=False,
            ))
            transport_metrics = json.loads(capture(
                [
                    *kubectl, "get", "--raw",
                    "/api/v1/namespaces/servicelib-conformance/services/"
                    "http:monitoring-kube-prometheus-prometheus:9090/proxy/"
                    "api/v1/query?query=http_server_request_duration_seconds_count",
                ],
                example, environment, verbose=False,
            ))
            loki = json.loads(capture(
                [
                    *kubectl, "get", "--raw",
                    "/api/v1/namespaces/servicelib-conformance/services/"
                    "http:loki-gateway:80/proxy/loki/api/v1/label/"
                    "service_name/values",
                ],
                example, environment, verbose=False,
            ))
            jaeger_services = set(jaeger.get("data", []))
            metric_services = {
                item.get("metric", {}).get("service")
                for item in prometheus.get("data", {}).get("result", [])
            }
            leaked_exported_labels = {
                label
                for result in (
                    *prometheus.get("data", {}).get("result", []),
                    *transport_metrics.get("data", {}).get("result", []),
                )
                for label in result.get("metric", {})
                if label in {"exported_service", "exported_job"}
            }
            transport_jobs = {
                item.get("metric", {}).get("job")
                for item in transport_metrics.get("data", {}).get("result", [])
            }
            normalized_transport_jobs = {
                "".join(character for character in value.lower() if character.isalnum())
                for value in transport_jobs
                if value
            }
            log_services = set(loki.get("data", []))
            expected_services = {
                "Analytics Service", "Inventory Service", "Order Service",
            }
            telemetry_status = (
                f"jaeger={sorted(jaeger_services)}, "
                f"prometheus={sorted(value for value in metric_services if value)}, "
                f"transport_jobs={sorted(value for value in transport_jobs if value)}, "
                f"exported_labels={sorted(leaked_exported_labels)}, "
                f"loki={sorted(log_services)}"
            )
            if (
                {"Inventory Service", "Order Service"} <= jaeger_services
                and expected_services <= metric_services
                and {"inventoryservice", "orderservice"}
                <= normalized_transport_jobs
                and not leaked_exported_labels
                and expected_services <= log_services
            ):
                telemetry_ready = True
                print(
                    "[kubernetes] PASS  sampled traces + OTLP metrics/logs",
                    flush=True,
                )
                break
            time.sleep(1)
        if not telemetry_ready:
            raise RuntimeError(
                "Kubernetes telemetry did not converge: " + telemetry_status
            )
        deadline = time.monotonic() + 60
        group_status = ""
        committed = False
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    *kubectl, "--namespace", "servicelib-conformance", "exec",
                    "redpanda-0", "-c", "redpanda", "--", "rpk", "group",
                    "describe", "analytics-service", "-X", "brokers=localhost:9093",
                    "-X", "user=servicegen-conformance", "-X",
                    f"pass={environment['KUBERNETES_KAFKA_PASSWORD']}", "-X",
                    "sasl.mechanism=SCRAM-SHA-512",
                ],
                cwd=example, env=environment, capture_output=True, text=True,
                check=False,
            )
            group_status = result.stdout + result.stderr
            for line in result.stdout.splitlines():
                fields = line.split()
                if (
                    len(fields) >= 6 and fields[0] == "order-processed"
                    and fields[2].isdigit() and int(fields[2]) > 0
                    and fields[5].isdigit() and int(fields[5]) == 0
                ):
                    print(
                        "[kubernetes] PASS  order -> authenticated Kafka -> analytics",
                        flush=True,
                    )
                    committed = True
                    break
            if committed:
                break
            time.sleep(1)
        if not committed:
            raise RuntimeError(
                "analytics-service did not commit the authenticated Kubernetes "
                "Kafka event:\n" + group_status
            )
    finally:
        subprocess.run(
            [*script, "clean"], cwd=example, env=environment, check=False,
        )
    print("[kubernetes] PASS  go local k3s runtime", flush=True)


def write_summary(status: str, error: str | None = None) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    value: dict[str, object] = {
        "status": status,
        "languages": list(EXAMPLES),
        "services": list(SERVICES),
        "runtime_language": "go",
        "helm_image": HELM_IMAGE,
    }
    if error is not None:
        value["error"] = error
    SUMMARY.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> int:
    try:
        for language, directory in EXAMPLES.items():
            validate_example(language, ROOT / directory)
        runtime_probe(ROOT / EXAMPLES["go"])
    except Exception as error:  # noqa: BLE001
        write_summary("fail", str(error))
        print(f"Kubernetes conformance failed: {error}", file=sys.stderr)
        return 1
    write_summary("pass")
    print("Kubernetes conformance passed: " + ", ".join(EXAMPLES), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
