#!/usr/bin/env python3
"""Cross-language Kafka endpoint lifecycle conformance on real Redpanda."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


CONFORMANCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE))
import cpp_source_cache

ROOT = Path(
    os.environ.get("DEPENDENCIES_DIR", CONFORMANCE.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "kafka"
COMMON_COMPOSE = Path(__file__).with_name("compose.common.yml")
TOPIC = "order-processed"
GROUP = "analytics-service"
METRIC_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{|\s)")


@dataclass(frozen=True)
class Language:
    name: str
    example: Path

    @property
    def project(self) -> str:
        return f"servicelib-kafka-conformance-{self.name}"


LANGUAGES = (
    Language("go", ROOT / "goexample"),
    Language("cpp", ROOT / "cppexample"),
    Language("cppboost", ROOT / "cppboostexample"),
    Language("python", ROOT / "pyexample"),
    Language("rust", ROOT / "rustexample"),
    Language("typescript", ROOT / "tsexample"),
)


def compose_command(language: Language, *args: str) -> list[str]:
    command = [
        "docker", "compose",
        "--project-name", language.project,
        "--project-directory", str(language.example),
        "--file", str(language.example / "docker-compose.yml"),
    ]
    for runtime_overlay in sorted(
        language.example.glob("docker-compose.*-runtime.generated.yml")
    ):
        command.extend(["--file", str(runtime_overlay)])
    return [*command, "--file", str(COMMON_COMPOSE), *args]


def language_env(language: Language) -> dict[str, str]:
    env = os.environ.copy()
    if language.name == "go":
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    elif language.name == "cpp":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
        # Temporal is not supported by the C++ runtime yet, so the generated
        # mixed-language example contains a Go automation service.
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    elif language.name == "cppboost":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppboostservicelib")
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
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


def run(
    command: list[str], *, cwd: Path, env: dict[str, str],
    capture: bool = False, check: bool = True, announce: bool = True,
) -> subprocess.CompletedProcess[str]:
    if announce:
        print("+", " ".join(command), flush=True)
    return subprocess.run(
        command, cwd=cwd, env=env, check=check,
        capture_output=capture, text=True,
    )


def build(language: Language, env: dict[str, str]) -> None:
    if language.name == "go":
        run(["make", "docker-build"], cwd=language.example, env=env)
    elif language.name == "typescript":
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
        )
    elif language.name in {"cpp", "cppboost"}:
        # The Kafka suite runs with the generated runtime overlay.  Building
        # only the development CMake volume here leaves the independent
        # runtime images stale and can silently execute binaries from an
        # earlier generation.  Exercise the same per-service release-image
        # path that benchmarks, profiling and Kubernetes consume.
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example, env=env,
        )
    else:
        run(
            compose_command(
                language, "build", "analyticsservice",
                "inventoryservice", "orderservice",
            ),
            cwd=language.example, env=env,
        )


def service_diagnostics(
    language: Language, service: str, env: dict[str, str]
) -> str:
    result = run(
        compose_command(language, "logs", "--no-color", "--tail", "80", service),
        cwd=language.example, env=env, capture=True, check=False,
    )
    return (result.stdout + result.stderr).strip() or "logs unavailable"


def wait_http(
    language: Language, service: str, url: str, env: dict[str, str],
    *, timeout: float = 60,
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
        exited = run(
            compose_command(language, "ps", "--status", "exited", "--services"),
            cwd=language.example, env=env, capture=True, check=False,
        )
        if service in exited.stdout.split():
            raise RuntimeError(
                f"{service} exited before readiness\n"
                + service_diagnostics(language, service, env)
            )
        time.sleep(0.5)
    raise RuntimeError(
        f"timeout waiting for {url}: {last_error}\n"
        + service_diagnostics(language, service, env)
    )


def rpk(
    language: Language, env: dict[str, str], *args: str, check: bool = True,
    announce: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        compose_command(language, "exec", "-T", "redpanda", "rpk", *args),
        cwd=language.example, env=env, capture=True, check=check,
        announce=announce,
    )


def wait_redpanda(language: Language, env: dict[str, str]) -> None:
    deadline = time.monotonic() + 60
    last = ""
    while time.monotonic() < deadline:
        result = rpk(
            language, env, "cluster", "health", check=False, announce=False
        )
        last = (result.stdout + result.stderr).strip()
        healthy = any(
            line.startswith("Healthy:") and line.rsplit(maxsplit=1)[-1] == "true"
            for line in result.stdout.splitlines()
        )
        if result.returncode == 0 and healthy:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Redpanda did not become healthy: {last}")


def topic_names(language: Language, env: dict[str, str]) -> set[str]:
    result = rpk(language, env, "topic", "list")
    return {
        fields[0]
        for line in result.stdout.splitlines()[1:]
        if (fields := line.split())
    }


def assert_topic(
    language: Language, env: dict[str, str], *, creator: str,
    expected_partitions: int = 1,
) -> None:
    if TOPIC not in topic_names(language, env):
        raise RuntimeError(
            f"{TOPIC!r} was not explicitly created by the enabled {creator} endpoint"
        )
    result = rpk(language, env, "topic", "describe", TOPIC, "-p")
    partitions = [
        line for line in result.stdout.splitlines()
        if line.split() and line.split()[0].isdigit()
    ]
    if len(partitions) != expected_partitions:
        raise RuntimeError(
            f"expected {expected_partitions} partition(s) for {TOPIC!r}, "
            f"got {len(partitions)}:\n"
            + result.stdout
        )


def add_partition(language: Language, env: dict[str, str]) -> None:
    rpk(language, env, "topic", "add-partitions", TOPIC, "--num", "1")
    assert_topic(language, env, creator="broker expansion", expected_partitions=2)


def send_order(sequence: int) -> None:
    body = json.dumps({
        "customer_id": f"kafka-conformance-customer-{sequence}",
        "items": [{
            "item_id": f"kafka-conformance-item-{sequence}", "sku": "SKU-001",
            "quantity": 1, "unit_price": 10.0,
        }],
    }).encode()
    request = urllib.request.Request(
        "http://localhost:9091/v1/processorder", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
        status = response.status
    if status != 200 or payload.get("status") != "CONFIRMED":
        raise RuntimeError(f"unexpected ProcessOrder response: {payload}")


def metric_names(raw: str) -> list[str]:
    """Return the public Prometheus families present in one scrape."""
    return sorted({
        match.group(1)
        for line in raw.splitlines()
        if line and not line.startswith("#")
        and (match := METRIC_SAMPLE_RE.match(line))
    })


def record_metric_phase(
    language: Language, env: dict[str, str], phase: str,
) -> dict[str, list[str]]:
    """Persist phase evidence without relying on Kafka-driver internals."""
    phase_dir = ARTIFACTS / language.name / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, list[str]] = {}
    for service, url in (
        ("orderservice", "http://localhost:9091/metrics"),
        ("analyticsservice", "http://localhost:9093/metrics"),
    ):
        raw = wait_http(language, service, url, env, timeout=15).decode()
        (phase_dir / f"{service}.metrics.raw.txt").write_text(raw)
        result[service] = metric_names(raw)
    return result


def assert_service_handlers(
    language: Language,
    service: str,
    port: int,
    env: dict[str, str],
) -> None:
    """Prove the generated ordinary-Compose service exposes its public handlers."""
    for path in (
        "/health/startup",
        "/health/ready",
        "/health/live",
        "/metrics",
        "/status/data",
    ):
        print(
            f"[kafka:{language.name}] CHECK {service} {path}", flush=True,
        )
        wait_http(
            language,
            service,
            f"http://localhost:{port}{path}",
            env,
            timeout=15,
        )
        print(
            f"[kafka:{language.name}] PASS  {service} {path}", flush=True,
        )


def wait_consumed(
    language: Language, env: dict[str, str], *, expected_messages: int,
    timeout: float = 30,
    retry_publish: Callable[[], None] | None = None,
) -> str:
    deadline = time.monotonic() + timeout
    next_publish = 0.0
    latest = ""
    while time.monotonic() < deadline:
        result = rpk(
            language, env, "group", "describe", GROUP,
            check=False, announce=False,
        )
        latest = result.stdout + result.stderr
        committed: dict[int, tuple[int, int]] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if not fields or fields[0] != TOPIC or len(fields) < 5:
                continue
            numeric = [field for field in fields[1:] if field.lstrip("-").isdigit()]
            if len(numeric) >= 5:
                committed[int(numeric[0])] = (int(numeric[1]), int(numeric[4]))
        if (
            set(committed) == {0, 1}
            and all(offset > 0 and lag == 0 for offset, lag in committed.values())
            and sum(offset for offset, _ in committed.values()) >= expected_messages
        ):
            return result.stdout
        now = time.monotonic()
        if retry_publish is not None and now >= next_publish:
            retry_publish()
            next_publish = now + 5.0
        time.sleep(0.5)
    raise RuntimeError(
        f"{GROUP!r} did not commit the produced {TOPIC!r} event:\n{latest}"
    )


def run_language(
    language: Language, *, skip_build: bool, keep: bool,
) -> dict[str, object]:
    env = language_env(language)
    metric_phases: dict[str, dict[str, list[str]]] = {}
    try:
        # A previously interrupted suite may have left the same Compose
        # project running.  Reusing that Redpanda container also reuses its
        # topics and invalidates the explicit-topic-creation assertion.
        # This removes only this language's disposable conformance project;
        # build/source caches and the canonical example remain untouched.
        run(
            compose_command(language, "down", "--remove-orphans"),
            cwd=language.example, env=env, check=False,
        )
        if not skip_build:
            print(f"[kafka:{language.name}] START build", flush=True)
            build(language, env)
            print(f"[kafka:{language.name}] PASS  build", flush=True)

        print(f"[kafka:{language.name}] START explicit topic creation", flush=True)
        run(
            compose_command(language, "up", "--detach", "redpanda"),
            cwd=language.example, env=env,
        )
        wait_redpanda(language, env)
        if TOPIC in topic_names(language, env):
            raise RuntimeError(
                f"{TOPIC!r} exists before any endpoint starts; broker auto-create is not disabled"
            )
        run(
            compose_command(
                language, "up", "--detach", "--no-deps", "analyticsservice"
            ),
            cwd=language.example, env=env,
        )
        wait_http(
            language, "analyticsservice",
            "http://localhost:9093/status/data", env,
        )
        assert_service_handlers(language, "analyticsservice", 9093, env)
        assert_topic(language, env, creator="source")
        print(f"[kafka:{language.name}] PASS  source topic creation", flush=True)

        # Use a fresh broker for the sink phase. Reusing a broker after
        # deleting the topic leaves consumer-group membership and offsets
        # alive long enough to make the second phase depend on Kafka session
        # timeouts rather than endpoint semantics.
        run(
            compose_command(language, "down", "--remove-orphans"),
            cwd=language.example, env=env,
        )
        run(
            compose_command(language, "up", "--detach", "redpanda"),
            cwd=language.example, env=env,
        )
        wait_redpanda(language, env)
        if TOPIC in topic_names(language, env):
            raise RuntimeError(
                f"{TOPIC!r} exists before the sink endpoint starts"
            )

        print(f"[kafka:{language.name}] START sink topic creation", flush=True)
        run(
            compose_command(
                language, "up", "--detach", "--no-deps", "inventoryservice"
            ),
            cwd=language.example, env=env,
        )
        wait_http(
            language, "inventoryservice",
            "http://localhost:9092/status/data", env,
        )
        assert_service_handlers(language, "inventoryservice", 9092, env)
        run(
            compose_command(
                language, "up", "--detach", "--no-deps", "orderservice"
            ),
            cwd=language.example, env=env,
        )
        wait_http(
            language, "orderservice", "http://localhost:9091/status/data", env,
        )
        assert_service_handlers(language, "orderservice", 9091, env)
        assert_topic(language, env, creator="sink")
        print(f"[kafka:{language.name}] PASS  sink topic creation", flush=True)

        print(
            f"[kafka:{language.name}] START broker partition discovery",
            flush=True,
        )
        run(
            compose_command(language, "stop", "orderservice"),
            cwd=language.example, env=env,
        )
        add_partition(language, env)
        run(
            compose_command(
                language, "up", "--detach", "--no-deps", "orderservice"
            ),
            cwd=language.example, env=env,
        )
        wait_http(
            language, "orderservice", "http://localhost:9091/status/data", env,
        )
        print(
            f"[kafka:{language.name}] PASS  broker partition discovery",
            flush=True,
        )

        print(f"[kafka:{language.name}] START publish and consume", flush=True)
        run(
            compose_command(
                language, "up", "--detach", "--no-deps", "analyticsservice"
            ),
            cwd=language.example, env=env,
        )
        wait_http(
            language, "analyticsservice",
            "http://localhost:9093/status/data", env,
        )
        message_count = 32
        for sequence in range(message_count):
            send_order(sequence)
        group = wait_consumed(
            language, env, expected_messages=message_count,
        )
        (ARTIFACTS / f"{language.name}.consumer-group.txt").write_text(group)
        metric_phases["healthy"] = record_metric_phase(
            language, env, "healthy"
        )
        print(f"[kafka:{language.name}] PASS  publish and consume", flush=True)

        print(f"[kafka:{language.name}] START broker-loss recovery", flush=True)
        run(
            compose_command(language, "stop", "redpanda"),
            cwd=language.example, env=env,
        )
        # The HTTP/business graph must remain available while Kafka is down.
        # This message may either remain buffered or receive a final delivery
        # error; it is deliberately excluded from the required commit count.
        send_order(message_count)
        time.sleep(1.0)
        metric_phases["broker-down"] = record_metric_phase(
            language, env, "broker-down"
        )
        run(
            compose_command(language, "start", "redpanda"),
            cwd=language.example, env=env,
        )
        wait_redpanda(language, env)
        wait_http(
            language, "orderservice", "http://localhost:9091/status/data", env,
        )
        wait_http(
            language, "analyticsservice",
            "http://localhost:9093/status/data", env,
        )
        recovery_count = 4
        recovery_sequence = message_count + 1

        def publish_recovery_probe() -> None:
            nonlocal recovery_sequence
            for _ in range(recovery_count):
                send_order(recovery_sequence)
                recovery_sequence += 1

        recovered_group = wait_consumed(
            language, env,
            expected_messages=message_count + recovery_count,
            timeout=90,
            retry_publish=publish_recovery_probe,
        )
        (ARTIFACTS / f"{language.name}.consumer-group.recovered.txt").write_text(
            recovered_group
        )
        metric_phases["recovered"] = record_metric_phase(
            language, env, "recovered"
        )
        print(f"[kafka:{language.name}] PASS  broker-loss recovery", flush=True)
        evidence = {
            "client_metrics_supported": language.name != "python",
            "unsupported_reason": (
                "aiokafka exposes no stable public client statistics API"
                if language.name == "python" else None
            ),
            "phases": metric_phases,
        }
        (ARTIFACTS / language.name / "metrics-summary.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        )
        return evidence
    except Exception:
        print(
            f"[kafka:{language.name}] FAIL; recent service logs:",
            file=sys.stderr, flush=True,
        )
        for service in (
            "redpanda", "analyticsservice", "inventoryservice", "orderservice"
        ):
            print(service_diagnostics(language, service, env), file=sys.stderr)
        raise
    finally:
        if not keep:
            run(
                compose_command(
                    language, "down", "--remove-orphans"
                ),
                cwd=language.example, env=env, check=False,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--language", action="append",
        choices=[language.name for language in LANGUAGES],
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    selected = set(args.language or [language.name for language in LANGUAGES])
    languages = [language for language in LANGUAGES if language.name in selected]
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    failures: dict[str, str] = {}
    passed: list[str] = []
    metric_evidence: dict[str, object] = {}
    for language in languages:
        try:
            metric_evidence[language.name] = run_language(
                language, skip_build=args.skip_build, keep=args.keep
            )
            passed.append(language.name)
        except Exception as error:  # noqa: BLE001
            failures[language.name] = str(error)
    summary = {
        "status": "pass" if not failures else "fail",
        "languages": passed,
        "failed": failures,
        "broker_auto_create": False,
        "metric_evidence": metric_evidence,
        "checks": [
            "source endpoint creates configured topic",
            "sink endpoint creates configured topic",
            "source accepts an already existing topic",
            "producer discovers broker-side partition count on restart",
            "both partitions are published, consumed and committed in full",
            "services survive broker loss and the same group commits after recovery",
        ],
    }
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    if failures:
        print("Kafka conformance failed:", file=sys.stderr)
        for language, error in failures.items():
            print(f"- {language}: {error}", file=sys.stderr)
        return 1
    print("Kafka conformance passed: " + ", ".join(passed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
