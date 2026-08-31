#!/usr/bin/env python3
"""Exercise generated services under their native race/sanitizer tooling."""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE_DIR / ".artifacts" / "sanitizers"
ORDER_URL = "http://127.0.0.1:9091/v1/processorder"
ORDER_HOST = "127.0.0.1"
ORDER_PORT = 9091
ORDER_PATH = "/v1/processorder"
IMPLEMENTATIONS = {
    "go": ROOT / "goexample",
    "cpp": ROOT / "cppexample",
    "cppboost": ROOT / "cppboostexample",
    "python": ROOT / "pyexample",
    "rust": ROOT / "rustexample",
    "typescript": ROOT / "tsexample",
}
IMPLEMENTATION_LANGUAGES = {
    "go": "golang",
    "cpp": "cppUserver",
    "cppboost": "cppBoost",
    "python": "python",
    "rust": "rust",
    "typescript": "typescript",
}
IMPLEMENTATION_SANITIZERS = {
    "go": ("race",),
    "cpp": ("runtime", "asan", "tsan"),
    "cppboost": ("runtime", "asan", "tsan"),
    "python": ("runtime",),
    "rust": ("runtime",),
    "typescript": ("runtime",),
}
SANITIZERS = tuple(
    dict.fromkeys(
        sanitizer
        for sanitizers in IMPLEMENTATION_SANITIZERS.values()
        for sanitizer in sanitizers
    )
)
MODES = ("single-request", "load", "shutdown-load")


@dataclass(frozen=True)
class LifecycleContract:
    """One observable lifecycle scenario shared by every framework adapter."""

    single_request_runs: int = 3
    load_duration_seconds: float = 15.0
    shutdown_load_duration_seconds: float = 20.0
    shutdown_after_seconds: float = 10.0
    shutdown_timeout_seconds: float = 7.0
    workers: int = 4
    modes: tuple[str, ...] = MODES


LIFECYCLE_CONTRACT = LifecycleContract()
REQUEST_BODY = json.dumps(
    {
        "customer_id": "sanitizer-conformance",
        "items": [
            {
                "item_id": "sanitizer-out-of-stock",
                "sku": "UNKNOWN",
                "quantity": 1,
                "unit_price": 3.0,
            },
        ],
    }
).encode()


@dataclass(frozen=True)
class ServiceDescriptor:
    name: str
    language: str
    http_port: int
    grpc_port: int
    observe_calls: bool

    @property
    def status_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}/status/data"


def service_descriptors(
    language: str, *, include_fallback_services: bool = False
) -> tuple[ServiceDescriptor, ...]:
    manifest = (
        IMPLEMENTATIONS[language]
        / "scripts"
        / "lifecycle-services.generated.csv"
    )
    if not manifest.is_file():
        raise RuntimeError(f"missing generated sanitizer service manifest: {manifest}")
    with manifest.open(newline="") as source:
        rows = list(csv.DictReader(source))
    expected_fields = {
        "service", "language", "http_port", "grpc_port", "observe_calls"
    }
    if not rows or set(rows[0]) != expected_fields:
        raise RuntimeError(
            f"invalid generated sanitizer service manifest schema: {manifest}"
        )
    services: list[ServiceDescriptor] = []
    names: set[str] = set()
    for row in rows:
        if (
            not include_fallback_services
            and row["language"] != IMPLEMENTATION_LANGUAGES[language]
        ):
            continue
        name = row["service"].strip()
        if not name or name in names:
            raise RuntimeError(f"invalid or duplicate sanitizer service {name!r}")
        names.add(name)
        try:
            http_port = int(row["http_port"])
            grpc_port = int(row["grpc_port"])
        except ValueError as error:
            raise RuntimeError(
                f"invalid ports for sanitizer service {name!r}: {row}"
            ) from error
        if not 1 <= http_port <= 65535 or not 0 <= grpc_port <= 65535:
            raise RuntimeError(f"out-of-range ports for sanitizer service {name!r}")
        observe_calls = row["observe_calls"] == "1"
        if row["observe_calls"] not in {"0", "1"}:
            raise RuntimeError(
                f"invalid observe_calls for sanitizer service {name!r}: {row}"
            )
        services.append(
            ServiceDescriptor(
                name, row["language"], http_port, grpc_port, observe_calls
            )
        )
    if not services:
        raise RuntimeError(
            "generated lifecycle manifest has no "
            f"{IMPLEMENTATION_LANGUAGES[language]} services"
        )
    return tuple(services)


def run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command, cwd=cwd, check=check, text=True, env=env, timeout=timeout
    )


def capture_runtime_evidence(
    language: str,
    sanitizer: str,
    mode: str,
    services: tuple[ServiceDescriptor, ...],
) -> None:
    """Preserve container state and logs before the generated down target removes them."""
    destination = ARTIFACTS / f"{language}-{sanitizer}" / mode
    destination.mkdir(parents=True, exist_ok=True)
    if sanitizer == "runtime":
        container_name = lambda service: (  # noqa: E731 - compose convention
            f"{IMPLEMENTATIONS[language].name}-{service}-1"
        )
    elif language == "go":
        container_name = lambda service: (  # noqa: E731 - compact name policy
            f"{IMPLEMENTATIONS[language].name}-race-{service}-1"
        )
    else:
        container_name = lambda service: (  # noqa: E731 - compact name policy
            f"{IMPLEMENTATIONS[language].name}-sanitizer-{sanitizer}-{service}"
        )
    for service in services:
        container = container_name(service.name)
        state = subprocess.run(
            ["docker", "inspect", container], text=True, capture_output=True
        )
        if state.returncode == 0:
            (destination / f"{service.name}.inspect.json").write_text(
                state.stdout
            )
        logs = subprocess.run(
            ["docker", "logs", container], text=True, capture_output=True
        )
        if logs.returncode == 0:
            (destination / f"{service.name}.log").write_text(
                logs.stdout + logs.stderr
            )
        if language == "typescript":
            diagnostic_dir = destination / f"{service.name}.node-diagnostics"
            diagnostic_dir.mkdir(exist_ok=True)
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{container}:/tmp/node-diagnostics/.",
                    str(diagnostic_dir),
                ],
                text=True,
                capture_output=True,
            )


RUNTIME_FAILURE_MARKERS = {
    "cpp": (
        "Unhandled exception in components::Run",
        "terminate called after throwing",
        "Assertion failed",
        "Segmentation fault",
    ),
    "cppboost": (
        "terminate called after throwing",
        "Assertion failed",
        "Segmentation fault",
    ),
    "python": (
        "Traceback (most recent call last):",
        "Task exception was never retrieved",
        "Fatal Python error",
    ),
    "rust": (
        "panicked at",
        "fatal error: concurrent map",
        "WARNING: DATA RACE",
    ),
    "typescript": (
        "UnhandledPromiseRejection",
        "uncaughtException",
        "ERR_ASSERTION",
        "FATAL ERROR:",
    ),
}


def stop_runtime_stack(
    language: str,
    mode: str,
    services: tuple[ServiceDescriptor, ...],
    env: dict[str, str],
    timeout: float,
) -> float:
    """Stop application services concurrently and validate graceful exits.

    ``docker compose stop`` observes Compose dependency order and can therefore
    stop the application in several waves.  That is the opposite of the
    production condition exercised here: every service receives the same
    termination event and must drain without relying on another service's stop
    order.  Signal every application container first, then apply one shared
    lifecycle deadline to the whole set.
    """
    example = IMPLEMENTATIONS[language]
    stop_timeout = int(env["LIFECYCLE_STOP_TIMEOUT"])
    containers = tuple(
        f"{example.name}-{service.name}-1" for service in services
    )
    started = time.monotonic()
    signalled = run(
        ["docker", "kill", "--signal", "SIGTERM", *containers],
        cwd=example,
        check=False,
        env=env,
        timeout=timeout,
    )
    overdue: tuple[str, ...] = ()
    if signalled.returncode == 0:
        overdue = wait_for_container_exit(containers, stop_timeout, env=env)
        if overdue:
            if language == "typescript":
                # Generated Node images expose the standard diagnostic report
                # on SIGUSR1. Preserve the actual active handles before the
                # emergency kill so a lifecycle regression is actionable.
                run(
                    ["docker", "kill", "--signal", "SIGUSR1", *overdue],
                    cwd=example,
                    check=False,
                    env=env,
                    timeout=timeout,
                )
                time.sleep(1.0)
            run(
                ["docker", "kill", "--signal", "SIGKILL", *overdue],
                cwd=example,
                check=False,
                env=env,
                timeout=timeout,
            )
    elapsed = time.monotonic() - started
    capture_runtime_evidence(language, "runtime", mode, services)
    failures: list[str] = []
    if signalled.returncode != 0:
        failures.append(f"concurrent SIGTERM exited {signalled.returncode}")
    if overdue:
        failures.append(
            "service shutdown exceeded "
            f"{stop_timeout}s: {', '.join(container_service_name(name) for name in overdue)}"
        )
    if elapsed > stop_timeout + 0.5:
        failures.append(
            f"service shutdown exceeded {stop_timeout}s ({elapsed:.3f}s)"
        )
    artifact_dir = ARTIFACTS / f"{language}-runtime" / mode
    for service in services:
        container = f"{example.name}-{service.name}-1"
        inspected = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.ExitCode}}", container],
            text=True,
            capture_output=True,
        )
        if inspected.returncode != 0:
            failures.append(f"cannot inspect stopped container {container}")
        elif inspected.stdout.strip() != "0":
            failures.append(
                f"{service.name} exited with code {inspected.stdout.strip()}"
            )
        log = (artifact_dir / f"{service.name}.log").read_text(errors="replace")
        for marker in RUNTIME_FAILURE_MARKERS[language]:
            if marker in log:
                failures.append(f"{service.name} log contains {marker!r}")
                break
    if failures:
        raise RuntimeError(f"{language} runtime lifecycle failed: " + "; ".join(failures))
    return elapsed


def wait_for_container_exit(
    containers: tuple[str, ...],
    timeout: float,
    *,
    env: dict[str, str],
) -> tuple[str, ...]:
    """Return containers still running after one shared monotonic deadline."""
    deadline = time.monotonic() + max(0.0, timeout)
    running = containers
    while running:
        inspected = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", *running],
            text=True,
            capture_output=True,
            env=env,
        )
        if inspected.returncode != 0:
            # Preserve the names for the caller's evidence/error path instead
            # of treating an observation failure as successful termination.
            return running
        states = inspected.stdout.splitlines()
        if len(states) != len(running):
            return running
        running = tuple(
            name
            for name, state in zip(running, states, strict=True)
            if state.strip().lower() == "true"
        )
        if not running:
            return ()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return running
        time.sleep(min(0.05, remaining))
    return ()


def container_service_name(container: str) -> str:
    """Extract the generated Compose service name for concise diagnostics."""
    prefix, separator, suffix = container.rpartition("-")
    if separator and suffix == "1":
        _project, separator, service = prefix.rpartition("-")
        if separator and service:
            return service
    return container


def cleanup_runtime_stack(
    language: str,
    env: dict[str, str],
    timeout: float,
) -> None:
    """Remove infrastructure after the timed lifecycle exercise is complete."""
    example = IMPLEMENTATIONS[language]
    stop_timeout = int(env["LIFECYCLE_STOP_TIMEOUT"])
    removed = run(
        [
            "docker", "compose", "down", "--timeout", str(stop_timeout),
            "--volumes", "--remove-orphans",
        ],
        cwd=example,
        check=False,
        env=env,
        # The application SLA is measured by stop_runtime_stack above. This
        # cleanup also removes Temporal, Redpanda, telemetry, volumes, and the
        # network, so its command timeout must not be confused with the
        # per-service graceful-stop bound.
        timeout=max(timeout, stop_timeout + 30),
    )
    if removed.returncode != 0:
        raise RuntimeError(f"docker compose down exited {removed.returncode}")


def cleanup_sanitizer_stack(
    target: str,
    example: Path,
    env: dict[str, str],
    timeout: float,
) -> None:
    """Remove shared test infrastructure outside the service shutdown SLA."""
    cleaned = run(
        ["make", f"{target}-clean", "USE_LOCAL_MODULES=1"],
        cwd=example,
        check=False,
        env=env,
        timeout=max(timeout + 30, 30),
    )
    if cleaned.returncode != 0:
        raise RuntimeError(f"{target} infrastructure cleanup exited {cleaned.returncode}")


def sanitizer_stop_command_timeout(service_timeout: float) -> float:
    """Bound orchestration without weakening the generated service deadline."""
    return max(30.0, service_timeout + 5.0)


def implementation_env(language: str) -> dict[str, str]:
    env = os.environ.copy()
    # This gate always invokes generated Make targets with
    # USE_LOCAL_MODULES=1. Reflect that contract in the subprocess environment
    # as well, so a framework context inherited by the top-level harness can
    # never leak from one language implementation into the next one.
    env["USE_LOCAL_MODULES"] = "1"
    # A completed request may legitimately reach the canonical soft deadline
    # just below five seconds when its downstream gRPC service stops first.
    # Keep a two-second transport/response margin while remaining far below
    # the application's 30-second emergency shutdown budget. Generated gates
    # reject every SIGKILL exit code, so this is a graceful-stop bound, not a
    # way to hide an unfinished process.
    env.setdefault("SANITIZER_STOP_TIMEOUT", "7")
    env.setdefault("RACE_STOP_TIMEOUT", "7")
    env.setdefault("LIFECYCLE_STOP_TIMEOUT", "7")
    if language == "go":
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    elif language == "cpp":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
    elif language == "cppboost":
        source = str(ROOT / "cppboostservicelib")
        env["SERVICELIB_SOURCE_CONTEXT"] = source
    elif language == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "pyservicelib")
    elif language == "rust":
        env["RUSTSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "rustservicelib")
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    elif language == "typescript":
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "tsservicelib")
    return env


def target_name(language: str, sanitizer: str) -> str:
    if sanitizer not in IMPLEMENTATION_SANITIZERS[language]:
        raise RuntimeError(
            f"{sanitizer} is not supported for {language}; expected one of "
            f"{', '.join(IMPLEMENTATION_SANITIZERS[language])}"
        )
    if language == "go":
        return "golang-race"
    if sanitizer == "runtime":
        return "docker"
    return f"cpp-{sanitizer}"


def validate_adapter_coverage() -> None:
    """Fail before builds when a framework adapter escapes the common contract."""
    languages = set(IMPLEMENTATIONS)
    if set(IMPLEMENTATION_LANGUAGES) != languages:
        raise RuntimeError("lifecycle language-name adapters are incomplete")
    if set(IMPLEMENTATION_SANITIZERS) != languages:
        raise RuntimeError("lifecycle sanitizer adapters are incomplete")
    runtime_languages = {
        language
        for language, sanitizers in IMPLEMENTATION_SANITIZERS.items()
        if "runtime" in sanitizers
    }
    if not runtime_languages.issubset(RUNTIME_FAILURE_MARKERS):
        missing = sorted(runtime_languages - set(RUNTIME_FAILURE_MARKERS))
        raise RuntimeError(
            "lifecycle runtime failure markers are missing for: "
            + ", ".join(missing)
        )
    if LIFECYCLE_CONTRACT.modes != MODES:
        raise RuntimeError("lifecycle modes diverge from the canonical contract")
    for language, sanitizers in IMPLEMENTATION_SANITIZERS.items():
        if not sanitizers:
            raise RuntimeError(f"lifecycle adapter {language} has no execution mode")
        for sanitizer in sanitizers:
            target_name(language, sanitizer)


def lifecycle_contract_summary() -> dict[str, Any]:
    contract = LIFECYCLE_CONTRACT
    return {
        "single_request_runs": contract.single_request_runs,
        "load_duration_seconds": contract.load_duration_seconds,
        "shutdown_load_duration_seconds": (
            contract.shutdown_load_duration_seconds
        ),
        "shutdown_after_seconds": contract.shutdown_after_seconds,
        "shutdown_timeout_seconds": contract.shutdown_timeout_seconds,
        "workers": contract.workers,
        "modes": list(contract.modes),
    }


def fetch_graph(url: str, timeout: float = 3) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        value = json.load(response)
    if not isinstance(value, dict) or not value.get("nodes") or not value.get("edges"):
        raise RuntimeError(f"{url} did not return a populated runtime graph")
    return value


def wait_ready(
    services: tuple[ServiceDescriptor, ...], timeout: float = 30
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    latest: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return {
                service.name: fetch_graph(service.status_url)
                for service in services
            }
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            latest = error
            time.sleep(0.25)
    raise RuntimeError(f"sanitized services did not become ready: {latest}")


def validate_order_response(status: int, body: dict[str, Any]) -> None:
    items = body.get("confirmed_items", [])
    if (
        status != 200
        or body.get("status") != "PARTIALLY_CONFIRMED"
        or len(items) != 1
        or items[0].get("status") != "OUT_OF_STOCK"
    ):
        raise RuntimeError(f"unexpected order response: HTTP {status}: {body}")


def request_once(
    timeout: float = 30,
    connection: http.client.HTTPConnection | None = None,
) -> None:
    if connection is not None:
        connection.timeout = timeout
        connection.request(
            "POST",
            ORDER_PATH,
            body=REQUEST_BODY,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        validate_order_response(response.status, body)
        return
    request = urllib.request.Request(
        ORDER_URL,
        data=REQUEST_BODY,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
        validate_order_response(response.status, body)


def load(duration: float, workers: int) -> dict[str, Any]:
    deadline = time.monotonic() + duration
    lock = threading.Lock()
    successes = 0
    errors: list[str] = []

    def worker() -> None:
        nonlocal successes
        connection = http.client.HTTPConnection(ORDER_HOST, ORDER_PORT, timeout=30)
        try:
            while time.monotonic() < deadline:
                try:
                    request_once(connection=connection)
                    with lock:
                        successes += 1
                except Exception as error:  # noqa: BLE001 - preserve load failures
                    with lock:
                        errors.append(f"{type(error).__name__}: {error}")
                    return
        finally:
            connection.close()

    started = time.monotonic()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=duration + 35)
    elapsed = time.monotonic() - started
    alive = sum(thread.is_alive() for thread in threads)
    if alive:
        errors.append(f"{alive} load workers did not stop")
    if errors:
        raise RuntimeError("lifecycle load failed: " + "; ".join(errors[:10]))
    if successes < workers:
        raise RuntimeError(
            f"sanitizer load completed only {successes} requests for {workers} workers"
        )
    return {
        "duration_seconds": elapsed,
        "workers": workers,
        "requests": successes,
        "errors": 0,
        "requests_per_second": successes / elapsed,
    }


def load_while_stopping(
    duration: float,
    stop_after: float,
    workers: int,
    stop: Callable[[], None],
) -> dict[str, Any]:
    """Keep clients active for the full window and stop services mid-flight."""
    started = time.monotonic()
    deadline = started + duration
    shutdown_started = threading.Event()
    lock = threading.Lock()
    successes_before_shutdown = 0
    errors_before_shutdown: list[str] = []
    errors_after_shutdown = 0

    def worker() -> None:
        nonlocal successes_before_shutdown, errors_after_shutdown
        connection = http.client.HTTPConnection(ORDER_HOST, ORDER_PORT, timeout=5)
        try:
            while time.monotonic() < deadline:
                during_shutdown = shutdown_started.is_set()
                try:
                    request_once(timeout=5, connection=connection)
                    if not during_shutdown:
                        with lock:
                            successes_before_shutdown += 1
                except Exception as error:  # noqa: BLE001 - lifecycle evidence
                    with lock:
                        if shutdown_started.is_set():
                            errors_after_shutdown += 1
                        else:
                            errors_before_shutdown.append(
                                f"{type(error).__name__}: {error}"
                            )
                    # Avoid a busy loop after the listener has disappeared.
                    if shutdown_started.is_set():
                        time.sleep(0.01)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    sleep_until = started + stop_after
    while time.monotonic() < sleep_until:
        time.sleep(min(0.05, sleep_until - time.monotonic()))
    shutdown_started.set()
    stop_started = time.monotonic()
    stop()
    shutdown_elapsed = time.monotonic() - stop_started
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()) + 6)
    alive = sum(thread.is_alive() for thread in threads)
    if alive:
        raise RuntimeError(f"{alive} load workers hung across service shutdown")
    if errors_before_shutdown:
        raise RuntimeError(
            "load failed before shutdown: " + "; ".join(errors_before_shutdown[:10])
        )
    if successes_before_shutdown < workers:
        raise RuntimeError(
            "interrupted load did not establish concurrent successful traffic: "
            f"{successes_before_shutdown} requests for {workers} workers"
        )
    return {
        "duration_seconds": time.monotonic() - started,
        "configured_duration_seconds": duration,
        "shutdown_after_seconds": stop_after,
        "shutdown_seconds": shutdown_elapsed,
        "workers": workers,
        "requests_before_shutdown": successes_before_shutdown,
        "errors_before_shutdown": 0,
        "errors_after_shutdown": errors_after_shutdown,
    }


def edge_has_calls(graph: dict[str, Any]) -> bool:
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        label = str(edge.get("label", ""))
        for line in label.splitlines():
            if not line.startswith("calls: "):
                continue
            value = line.removeprefix("calls: ").split(" ", 1)[0]
            try:
                if int(value) > 0:
                    return True
            except ValueError:
                pass
    return False


def wait_observed_calls(
    services: tuple[ServiceDescriptor, ...], timeout: float = 30
) -> dict[str, dict[str, Any]]:
    observed = tuple(service for service in services if service.observe_calls)
    if not observed:
        return {}
    deadline = time.monotonic() + timeout
    latest: dict[str, dict[str, Any]] = {}
    latest_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            latest = {
                service.name: fetch_graph(service.status_url)
                for service in observed
            }
            latest_error = None
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            latest_error = error
            time.sleep(0.25)
            continue
        if all(edge_has_calls(graph) for graph in latest.values()):
            return latest
        time.sleep(0.25)
    raise RuntimeError(
        "generated sanitizer services marked observe_calls did not process "
        f"the canonical result: {sorted(latest)}; latest error: {latest_error}"
    )


def exercise(
    language: str,
    sanitizer: str,
    args: argparse.Namespace,
    *,
    mode: str,
) -> dict[str, Any]:
    example = IMPLEMENTATIONS[language]
    if not example.is_dir():
        raise RuntimeError(f"missing generated example: {example}")
    target = target_name(language, sanitizer)
    services = service_descriptors(
        language, include_fallback_services=sanitizer == "runtime"
    )
    env = implementation_env(language)
    down = ["make", f"{target}-stop", "USE_LOCAL_MODULES=1"]
    if sanitizer == "runtime":
        run(
            ["docker", "compose", "down", "--volumes", "--remove-orphans"],
            cwd=example,
            check=False,
            env=env,
            timeout=args.shutdown_timeout,
        )
    else:
        run(
            ["make", f"{target}-clean", "USE_LOCAL_MODULES=1"],
            cwd=example,
            check=False,
            env=env,
            timeout=max(args.shutdown_timeout + 30, 30),
        )
    started = time.monotonic()
    failure: BaseException | None = None
    result: dict[str, Any] = {}
    stopped = False
    shutdown_elapsed: float | None = None
    try:
        run(
            ["make", f"{target}-start", "USE_LOCAL_MODULES=1"],
            cwd=example,
            env=env,
        )
        initial_graphs = wait_ready(services)
        if mode == "single-request":
            request_once()
            load_result = {
                "duration_seconds": 0,
                "workers": 1,
                "requests": 1,
                "errors": 0,
                "requests_per_second": None,
            }
            # This diagnostic intentionally tears everything down immediately
            # after the one completed request. The normal sanitizer gate below
            # remains responsible for waiting for asynchronous Kafka delivery.
            analytics_calls_observed = any(
                edge_has_calls(fetch_graph(service.status_url))
                for service in services
                if service.observe_calls
            )
        elif mode == "load":
            load_result = load(args.duration, args.workers)
            observed_graphs = wait_observed_calls(services)
            analytics_calls_observed = bool(observed_graphs)
        elif mode == "shutdown-load":
            def stop_during_load() -> None:
                nonlocal stopped, shutdown_elapsed
                if sanitizer == "runtime":
                    # Avoid a second stop attempt when lifecycle validation
                    # itself reports a failure; cleanup remains independent.
                    stopped = True
                    shutdown_elapsed = stop_runtime_stack(
                        language, mode, services, env, args.shutdown_timeout
                    )
                    completed_returncode = 0
                else:
                    stop_started = time.monotonic()
                    completed = run(
                        down,
                        cwd=example,
                        check=False,
                        env=env,
                        # The generated target enforces the exact shared
                        # service deadline itself. This outer timeout also
                        # covers sanitizer log inspection and container removal.
                        timeout=sanitizer_stop_command_timeout(
                            args.shutdown_timeout
                        ),
                    )
                    shutdown_elapsed = time.monotonic() - stop_started
                    completed_returncode = completed.returncode
                stopped = True
                if completed_returncode != 0:
                    raise RuntimeError(
                        f"{language} {sanitizer} shutdown/log sanitizer gate "
                        f"failed with exit {completed.returncode}"
                    )

            load_result = load_while_stopping(
                args.shutdown_load_duration,
                args.shutdown_after,
                args.workers,
                stop_during_load,
            )
            analytics_calls_observed = False
        else:
            raise ValueError(f"unsupported exercise mode: {mode}")
        result = {
            "status": "pass",
            "language": language,
            "sanitizer": sanitizer,
            "load": load_result,
            "services": sorted(initial_graphs),
            "analytics_calls_observed": analytics_calls_observed,
            "mode": mode,
            "elapsed_seconds": time.monotonic() - started,
        }
    except BaseException as error:  # ensure shutdown also on Ctrl-C
        capture_runtime_evidence(language, sanitizer, mode, services)
        failure = error
    finally:
        if not stopped:
            if sanitizer == "runtime":
                try:
                    shutdown_elapsed = stop_runtime_stack(
                        language, mode, services, env, args.shutdown_timeout
                    )
                except BaseException as error:
                    if failure is None:
                        failure = error
            else:
                try:
                    stop_started = time.monotonic()
                    completed = run(
                        down,
                        cwd=example,
                        check=False,
                        env=env,
                        timeout=sanitizer_stop_command_timeout(
                            args.shutdown_timeout
                        ),
                    )
                    shutdown_elapsed = time.monotonic() - stop_started
                    if completed.returncode != 0 and failure is None:
                        failure = RuntimeError(
                            f"{language} {sanitizer} shutdown/log sanitizer gate "
                            f"failed with exit {completed.returncode}"
                        )
                except BaseException as error:
                    if failure is None:
                        failure = error
        if sanitizer == "runtime":
            try:
                cleanup_runtime_stack(language, env, args.shutdown_timeout)
            except BaseException as error:
                if failure is None:
                    failure = error
        else:
            try:
                cleanup_sanitizer_stack(
                    target, example, env, args.shutdown_timeout
                )
            except BaseException as error:
                if failure is None:
                    failure = error
    if result and shutdown_elapsed is not None:
        result["shutdown_seconds"] = shutdown_elapsed
    if failure is not None:
        raise failure
    return result


def parse_args() -> argparse.Namespace:
    contract = LIFECYCLE_CONTRACT
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--duration", type=float, default=contract.load_duration_seconds
    )
    parser.add_argument(
        "--shutdown-load-duration",
        type=float,
        default=contract.shutdown_load_duration_seconds,
    )
    parser.add_argument(
        "--shutdown-after", type=float, default=contract.shutdown_after_seconds
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=contract.shutdown_timeout_seconds,
    )
    parser.add_argument("--workers", type=int, default=contract.workers)
    parser.add_argument(
        "--single-request",
        action="store_true",
        help="run only the short start/one-request/immediate-stop checks",
    )
    parser.add_argument(
        "--single-request-runs",
        type=int,
        default=contract.single_request_runs,
    )
    parser.add_argument("--mode", action="append", choices=MODES)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--language", action="append", choices=tuple(IMPLEMENTATIONS))
    parser.add_argument("--sanitizer", action="append", choices=SANITIZERS)
    args = parser.parse_args()
    if not args.single_request and not 10 <= args.duration <= 20:
        parser.error("--duration must be between 10 and 20 seconds")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if args.single_request_runs <= 0:
        parser.error("--single-request-runs must be positive")
    if args.single_request and args.mode:
        parser.error("--single-request and --mode cannot be combined")
    if args.shutdown_load_duration != 20:
        parser.error("--shutdown-load-duration must be exactly 20 seconds")
    if args.shutdown_after != 10:
        parser.error("--shutdown-after must be exactly 10 seconds")
    return args


def main() -> int:
    validate_adapter_coverage()
    args = parse_args()
    languages = args.language or list(IMPLEMENTATIONS)
    modes = ["single-request"] if args.single_request else (args.mode or list(MODES))
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    try:
        for language in languages:
            sanitizers = args.sanitizer or list(
                IMPLEMENTATION_SANITIZERS[language]
            )
            for sanitizer in sanitizers:
                target = target_name(language, sanitizer)
                key = f"{language}-{sanitizer}"
                example = IMPLEMENTATIONS[language]
                env = implementation_env(language)
                if not args.skip_build:
                    print(f"[sanitizers] BUILD {key}", flush=True)
                    run(
                        ["make", f"{target}-build", "USE_LOCAL_MODULES=1"],
                        cwd=example,
                        env=env,
                    )
                mode_results: dict[str, Any] = {}
                if "single-request" in modes:
                    for attempt in range(1, args.single_request_runs + 1):
                        mode = f"single-request-{attempt}"
                        print(f"[sanitizers] START {key}-{mode}", flush=True)
                        mode_results[mode] = exercise(
                            language,
                            sanitizer,
                            args,
                            mode="single-request",
                        )
                        print(f"[sanitizers] PASS  {key}-{mode}", flush=True)
                if "load" in modes:
                    mode = "load"
                    print(f"[sanitizers] START {key}-{mode}", flush=True)
                    mode_results[mode] = exercise(
                        language,
                        sanitizer,
                        args,
                        mode="load",
                    )
                    print(f"[sanitizers] PASS  {key}-{mode}", flush=True)
                if "shutdown-load" in modes:
                    mode = "shutdown-load"
                    print(f"[sanitizers] START {key}-{mode}", flush=True)
                    mode_results[mode] = exercise(
                        language,
                        sanitizer,
                        args,
                        mode=mode,
                    )
                    print(f"[sanitizers] PASS  {key}-{mode}", flush=True)
                results[key] = mode_results
        summary = {
            "status": "pass",
            "contract": lifecycle_contract_summary(),
            "languages": languages,
            "sanitizers": args.sanitizer or "implementation-defaults",
            "implementations": results,
            "duration_seconds": args.duration,
            "workers": args.workers,
        }
    except BaseException as error:
        summary = {
            "status": "fail",
            "contract": lifecycle_contract_summary(),
            "languages": languages,
            "sanitizers": args.sanitizer or "implementation-defaults",
            "implementations": results,
            "duration_seconds": args.duration,
            "workers": args.workers,
            "error": f"{type(error).__name__}: {error}",
        }
        (ARTIFACTS / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )
        raise
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
