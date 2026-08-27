#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tooling_lock

BENCHMARK_DIR = Path(__file__).resolve().parent
BENCHMARK_ROOT = BENCHMARK_DIR.parent
ROOT = Path(
    os.environ.get(
        "BENCHMARK_DEPENDENCIES_DIR",
        os.environ.get(
            "CONFORMANCE_DEPENDENCIES_DIR",
            str(BENCHMARK_ROOT.parent.parent),
        ),
    )
).expanduser().resolve()
NATIVE_ROOT = Path(
    os.environ.get(
        "PERFORMANCE_NATIVE_DEPENDENCIES_DIR",
        str(BENCHMARK_ROOT.parent / ".dependencies" / "performance-native"),
    )
).expanduser().resolve()
ARTIFACTS = BENCHMARK_DIR / ".artifacts"
COMMON_COMPOSE = BENCHMARK_DIR / "compose.common.yml"
USERVER_REMOTE_CONTEXT = (
    "https://github.com/userver-framework/userver.git"
    "#c9f77729c0edce7e423def2d4a4450aa7fc9d259"
)


def cppboost_dependency_context(dependency: str) -> str:
    versions = ROOT / "cppboostservicelib" / "cmake" / "DependencyVersions.cmake"
    try:
        contents = versions.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(
            f"cannot read pinned C++ dependency versions from {versions}: {error}"
        ) from error
    repositories = {
        "grpc": "https://github.com/grpc/grpc.git",
        "asio-grpc": "https://github.com/Tradias/asio-grpc.git",
    }
    repository = repositories.get(dependency)
    if repository is None:
        raise RuntimeError(f"unsupported Boost dependency context: {dependency}")
    prefix = f"CPPBOOSTSERVICELIB_{dependency.upper().replace('-', '_')}"
    match = re.search(
        rf'^set\({re.escape(prefix)}_VERSION "([^"]+)"',
        contents,
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError(f"{prefix}_VERSION is missing from {versions}")
    return f"{repository}#{match.group(1)}"


@dataclass(frozen=True)
class Language:
    name: str
    example: Path
    overlay: Path
    verify_framework_pool: bool = True
    repository: str | None = None
    revision: str | None = None


LANGUAGES = (
    Language("go", ROOT / "goexample", BENCHMARK_DIR / "compose.go.yml"),
    Language(
        "go-native",
        NATIVE_ROOT / "gonativeexample",
        BENCHMARK_DIR / "compose.go-native.yml",
        verify_framework_pool=False,
        repository="https://github.com/gorundebug/gonativeexample.git",
        revision="v0.2.19",
    ),
    Language("cpp", ROOT / "cppexample", BENCHMARK_DIR / "compose.cpp.yml"),
    Language(
        "cpp-native",
        NATIVE_ROOT / "cppnativeexample",
        BENCHMARK_DIR / "compose.cpp-native.yml",
        verify_framework_pool=False,
        repository="https://github.com/gorundebug/cppnativeexample.git",
        revision="v0.2.19",
    ),
    Language(
        "cpp-boost",
        ROOT / "cppboostexample",
        BENCHMARK_DIR / "compose.cpp-boost.yml",
    ),
    Language(
        "cpp-boost-native",
        NATIVE_ROOT / "cppboostnativeexample",
        BENCHMARK_DIR / "compose.cpp-boost-native.yml",
        verify_framework_pool=False,
        repository="https://github.com/gorundebug/cppboostnativeexample.git",
        revision="v0.2.19",
    ),
    Language("python", ROOT / "pyexample", BENCHMARK_DIR / "compose.python.yml"),
    Language(
        "python-native",
        NATIVE_ROOT / "pynativeexample",
        BENCHMARK_DIR / "compose.python-native.yml",
        verify_framework_pool=False,
        repository="https://github.com/gorundebug/pynativeexample.git",
        revision="v0.2.19",
    ),
    Language("rust", ROOT / "rustexample", BENCHMARK_DIR / "compose.rust.yml"),
    Language(
        "rust-native",
        NATIVE_ROOT / "rustnativeexample",
        BENCHMARK_DIR / "compose.rust-native.yml",
        verify_framework_pool=False,
        repository="https://github.com/gorundebug/rustnativeexample.git",
        revision="v0.2.19",
    ),
    Language(
        "typescript",
        ROOT / "tsexample",
        BENCHMARK_DIR / "compose.typescript.yml",
    ),
    Language(
        "typescript-native",
        NATIVE_ROOT / "tsnativeexample",
        BENCHMARK_DIR / "compose.typescript-native.yml",
        verify_framework_pool=False,
        repository="https://github.com/gorundebug/tsnativeexample.git",
        # Managed by servicegen's atomic release script. ``main`` is permitted
        # only until the first TypeScript-native release tag is published.
        revision="v0.2.19",
    ),
)


def ensure_example(language: Language, env: dict[str, str]) -> None:
    compose_file = language.example / "docker-compose.yml"
    if compose_file.is_file():
        if (
            language.repository is not None
            and language.revision is not None
            and (
                env.get("BENCHMARK_UPDATE_MANAGED_DEPENDENCIES") == "1"
                or language.example.parent == NATIVE_ROOT
            )
        ):
            status = run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=language.example,
                env=env,
                capture=True,
            )
            if status.stdout.strip():
                raise RuntimeError(
                    f"managed {language.name} checkout has local changes; "
                    "refusing to update"
                )
            run(
                [
                    "git", "fetch", "--depth", "1", "origin",
                    f"refs/tags/{language.revision}:refs/tags/{language.revision}",
                ],
                cwd=language.example,
                env=env,
            )
            head = run(
                ["git", "rev-parse", "HEAD"],
                cwd=language.example,
                env=env,
                capture=True,
            ).stdout.strip()
            pinned = run(
                ["git", "rev-list", "-n", "1", language.revision],
                cwd=language.example,
                env=env,
                capture=True,
            ).stdout.strip()
            if head != pinned:
                print(
                    f"Updating {language.name} to {language.revision}",
                    flush=True,
                )
                run(
                    ["git", "checkout", "--detach", language.revision],
                    cwd=language.example,
                    env=env,
                )
        return
    if language.example.exists():
        raise RuntimeError(
            f"{language.name} example exists at {language.example}, but "
            f"{compose_file.name} is missing; refusing to replace it"
        )
    if language.repository is None or language.revision is None:
        raise RuntimeError(
            f"{language.name} example is missing at {language.example} and "
            "has no configured repository"
        )

    language.example.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"Fetching {language.name} {language.revision} from "
        f"{language.repository}",
        flush=True,
    )
    with tempfile.TemporaryDirectory(
        prefix=f".{language.example.name}-clone-",
        dir=language.example.parent,
    ) as temporary_directory:
        checkout = Path(temporary_directory) / language.example.name
        run(
            [
                "git", "clone", "--branch", language.revision,
                "--depth", "1", language.repository, str(checkout),
            ],
            cwd=language.example.parent,
            env=env,
        )
        if not (checkout / "docker-compose.yml").is_file():
            raise RuntimeError(
                f"downloaded {language.name} does not contain docker-compose.yml"
            )
        checkout.rename(language.example)


def ensure_examples(languages: list[Language], args: argparse.Namespace) -> None:
    for language in languages:
        ensure_example(language, environment(args, language))


def duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", value)
    if match is None:
        raise ValueError(f"unsupported duration: {value}")
    amount = float(match.group(1))
    return amount * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def compose_command(language: Language, *args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        f"servicelib-example-benchmark-{language.name}",
        "--project-directory",
        str(language.example),
        "--file",
        str(language.example / "docker-compose.yml"),
    ]
    for runtime_overlay in sorted(
        language.example.glob("docker-compose.*-runtime.generated.yml")
    ):
        command.extend(["--file", str(runtime_overlay)])
    command.extend([
        "--file", str(COMMON_COMPOSE),
        "--file", str(language.overlay),
        *args,
    ])
    return command


def environment(args: argparse.Namespace, language: Language) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "BENCHMARK_ARTIFACTS_DIR": str(ARTIFACTS),
            "BENCHMARK_CPP_CONFIG_DIR": str(ARTIFACTS / "cpp-config"),
            "BENCHMARK_CPPBOOST_CONFIG_DIR": str(ARTIFACTS / "cppboost-config"),
            "BENCHMARK_PYTHON_CONFIG_DIR": str(ARTIFACTS / "python-config"),
            "BENCHMARK_DIR": str(BENCHMARK_DIR),
            "BENCHMARK_DURATION": args.duration,
            "BENCHMARK_DURATION_SECONDS": str(duration_seconds(args.duration)),
            "BENCHMARK_GRPC_CONNECTIONS": str(
                getattr(args, "grpc_connections", None) or args.cores
            ),
            "BENCHMARK_LOAD_SCRIPT": "/scripts/load.js",
            "BENCHMARK_LOADGEN_CORES": str(args.loadgen_cores),
            "BENCHMARK_METHOD": getattr(args, "method", "POST"),
            "BENCHMARK_PAYLOAD_MODE": getattr(args, "payload_mode", "normal"),
            "BENCHMARK_EXPECTED_STATUS": str(
                getattr(args, "expected_status", 200)
            ),
            "BENCHMARK_RESULT_FILE": "/results/result.json",
            "BENCHMARK_RESULT_HOST_FILE": str(ARTIFACTS / "unused.json"),
            "BENCHMARK_SCENARIO": getattr(
                args, "scenario", "process_order_out_of_stock"
            ),
            "BENCHMARK_SERVICE_CORES": str(args.cores),
            "BENCHMARK_TARGET": getattr(
                args,
                "target",
                "http://orderservice:9091/v1/processorder",
            ),
            "BENCHMARK_VUS": str(args.vus),
            "SERVICEGEN_DOCKER_TARGET": "runtime",
            "SERVICEGEN_EXAMPLE_PROFILE": getattr(
                args, "graph_profile", "function-call"
            ),
        }
    )
    # Framework C++ examples contain a Go automation service until a supported
    # Temporal C++ SDK exists. Keep its runtime local in mixed-language builds.
    env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    if language.name == "cpp":
        env["COMPOSE_PROJECT_NAME"] = "cppexample"
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
        env["USERVER_LTO"] = "ON"
    elif language.name == "cpp-native":
        local_userver = ROOT / "userver"
        env["USERVER_SOURCE_CONTEXT"] = os.environ.get("USERVER_SOURCE_CONTEXT") or (
            str(local_userver)
            if local_userver.is_dir()
            else USERVER_REMOTE_CONTEXT
        )
        env["USERVER_LTO"] = "ON"
    elif language.name == "cpp-boost-native":
        env["NATIVE_DIAGNOSTIC_BYPASS_GRPC"] = (
            "true"
            if getattr(args, "native_diagnostic_bypass_grpc", False)
            else "false"
        )
    elif language.name == "cpp-boost":
        env["COMPOSE_PROJECT_NAME"] = "cppboostexample"
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppboostservicelib")
        env["CPPBOOSTSERVICELIB_SOURCE_CONTEXT"] = str(
            ROOT / "cppboostservicelib"
        )

    if language.name in {"cpp-boost", "cpp-boost-native"}:
        # docker-compose.cmake.generated.yml must never fall back to `.` for
        # these named contexts: the example checkout contains matching gRPC
        # headers under its build tree, so Docker can otherwise mount the
        # example itself as /servicegen-grpc-source and CMake configures the
        # wrong project.  Explicit remote contexts are pinned to the same
        # versions as cppboostservicelib and remain cached by BuildKit.
        if "SERVICEGEN_GRPC_SOURCE_CONTEXT" not in env:
            env["SERVICEGEN_GRPC_SOURCE_CONTEXT"] = (
                cppboost_dependency_context("grpc")
            )
        if "SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT" not in env:
            env["SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT"] = (
                cppboost_dependency_context("asio-grpc")
            )
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "pyservicelib")
    elif language.name == "rust":
        local_rustservicelib = ROOT / "rustservicelib"
        if local_rustservicelib.is_dir():
            env["RUSTSERVICELIB_SOURCE_CONTEXT"] = str(local_rustservicelib)
    elif language.name == "typescript":
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "tsservicelib")
    return env


def build(language: Language, env: dict[str, str]) -> None:
    if language.name == "go":
        run(["make", "docker-build"], cwd=language.example, env=env)
    elif language.name == "typescript":
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
        )
    elif language.name in {"cpp", "cpp-boost"}:
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
        )
    else:
        run(
            compose_command(language, "build", "inventoryservice", "orderservice"),
            cwd=language.example,
            env=env,
        )


# userver splits work across several independently-pooled task processors,
# unlike Go's single GOMAXPROCS-sized scheduler. Setting every pool's
# worker_threads to `cores` (as before) oversubscribes the cgroup CPU quota
# 3x over (main + fs + grpc-blocking, each sized at `cores`). For parity with
# Go: only main-task-processor (the CPU-bound pool that actually runs request
# handling fibers) gets `cores` threads, matching GOMAXPROCS exactly. The
# blocking/I/O-only pools (fs-task-processor, grpc-blocking-task-processor,
# and the gRPC server's completion queues) get a minimal fixed size, the same
# role Go's runtime fills with extra OS threads for blocking syscalls that
# don't count against GOMAXPROCS.
AUX_TASK_PROCESSOR_THREADS = 1


def _set_worker_threads(static_config: str, processor: str, threads: int) -> str:
    pattern = re.compile(rf"(?m)^(\s*{re.escape(processor)}:\n\s+worker_threads:)\s+\d+\s*$")
    new_config, count = pattern.subn(rf"\1 {threads}", static_config)
    if count != 1:
        raise RuntimeError(
            f"expected exactly one worker_threads under {processor}, found {count}"
        )
    return new_config


def _disable_userver_request_middlewares(
    static_config: str, service: str
) -> str:
    static_config, server_count = re.subn(
        r"(?m)^    server:\n",
        "    server:\n"
        "      middleware-pipeline-builder: "
        "servicelib-disabled-server-middlewares\n",
        static_config,
    )
    if server_count != 1:
        raise RuntimeError(
            f"{service} must define exactly one userver server component, "
            f"found {server_count}"
        )

    has_grpc_client = bool(
        re.search(r"(?m)^    grpc-client-factory:\n", static_config)
    )
    static_config, grpc_client_count = re.subn(
        r"(?m)^    grpc-client-factory:\n",
        "    grpc-client-factory:\n"
        "      disable-all-pipeline-middlewares: true\n",
        static_config,
    )
    expected_grpc_clients = 1 if has_grpc_client else 0
    if grpc_client_count != expected_grpc_clients:
        raise RuntimeError(
            f"{service} must define exactly {expected_grpc_clients} gRPC client "
            f"factories, found {grpc_client_count}"
        )
    return static_config


def prepare_cpp_configs(service_cores: int) -> None:
    output = ARTIFACTS / "cpp-config"
    output.mkdir(parents=True, exist_ok=True)
    for service, prefix, port, grpc_port in (
        ("inventoryservice", "inventoryService", 9092, 9202),
        ("orderservice", "orderService", 9091, 9201),
    ):
        static_config = (
            ROOT / "cppexample" / service / "static_config.yaml"
        ).read_text()
        # "none" fully suppresses log output (userver logging::Level::kNone).
        # servicelib bridges its own logging through userver's LOG_* macros
        # too, so this also silences servicelib's warnings/errors -- fine
        # here since a non-zero error rate already fails the benchmark on
        # its own, independent of whether we can see a log line about it.
        static_config = static_config.replace("level: info", "level: none")
        # The comparative benchmark measures framework/business execution, not
        # userver's default per-request tracing, statistics, and logging
        # middleware. The generated component remains dormant in normal
        # service configs and is selected only in this benchmark copy.
        static_config = _disable_userver_request_middlewares(
            static_config, service
        )
        static_config = _set_worker_threads(static_config, "main-task-processor", service_cores)
        static_config = _set_worker_threads(
            static_config, "fs-task-processor", AUX_TASK_PROCESSOR_THREADS
        )
        if "grpc-blocking-task-processor:" in static_config:
            static_config = _set_worker_threads(
                static_config,
                "grpc-blocking-task-processor",
                AUX_TASK_PROCESSOR_THREADS,
            )
        static_config, completion_queue_count = re.subn(
            r"(?m)^(\s+completion-queue-count:)\s+\d+\s*$",
            rf"\1 {AUX_TASK_PROCESSOR_THREADS}",
            static_config,
        )
        expected_completion_queues = 1 if "grpc-server:" in static_config else 0
        if completion_queue_count != expected_completion_queues:
            raise RuntimeError(
                f"{service} must define exactly {expected_completion_queues} "
                f"gRPC completion-queue counts, found {completion_queue_count}"
            )
        (output / f"{service}.static_config.yaml").write_text(static_config)

        override_path = output / f"{service}.overrides.yaml"
        if service == "orderservice":
            override_path.write_text(
                "streams:\n"
                "  publishOrderProcessed:\n"
                "    enabled: false\n"
            )
        else:
            override_path.write_text("{}\n")

        values = {
            f"{prefix}ConfigOverridePath": f"/benchmark-config/{service}.overrides.yaml",
            f"{prefix}Environment": "",
            f"{prefix}GrpcHost": "0.0.0.0",
            f"{prefix}GrpcPort": grpc_port,
            f"{prefix}HttpHost": "0.0.0.0",
            f"{prefix}HttpPort": port,
            "inventoryServiceApiConnectionsCount": service_cores,
        }
        if service == "inventoryservice":
            values["inventoryServiceApiAddress"] = "dns:///inventoryservice:9202"
            values["inventoryPriorityWorkersExecutorsCount"] = service_cores
            values["inventoryServiceDefaultGrpcTimeout"] = 0
        else:
            values["inventoryServiceApiAddress"] = "dns:///inventoryservice:9202"
            values["defaultPoolExecutorsCount"] = service_cores
            values["orderEventsBrokers"] = "redpanda:9092"
            values["orderServiceDefaultGrpcTimeout"] = 5000
            values["softDeadlineDuration"] = 1000
            values["orderProcessedEnabled"] = False
        source = (
            ROOT / "cppexample" / service / "config"
            / "config_vars.integration.yaml"
        )
        remaining = dict(values)
        lines = []
        for line in source.read_text().splitlines():
            key, separator, _ = line.partition(":")
            if separator and key in values:
                lines.append(f"{key}: {json.dumps(values[key])}")
                remaining.pop(key, None)
            else:
                lines.append(line)
        lines.extend(
            f"{key}: {json.dumps(value)}"
            for key, value in remaining.items()
        )
        text = "\n".join(lines) + "\n"
        (output / f"{service}.config_vars.yaml").write_text(text)


def disable_order_processed_endpoint(values: str) -> str:
    endpoint = "  orderProcessed:\n    enabled: false\n"
    pattern = re.compile(
        r"(?m)^  orderProcessed:\n    enabled: (?:true|false)\n"
    )
    if pattern.search(values):
        values = pattern.sub(endpoint, values, count=1)
    elif "endpoints:\n" in values:
        values = values.replace("endpoints:\n", "endpoints:\n" + endpoint, 1)
    else:
        values = values + ("\n" if values and not values.endswith("\n") else "")
        values += "endpoints:\n" + endpoint
    if values.count(endpoint) != 1:
        raise RuntimeError(
            "orderservice values must disable exactly one orderProcessed endpoint"
        )
    return values


def prepare_cppboost_configs(service_cores: int, grpc_connections: int) -> None:
    output = ARTIFACTS / "cppboost-config"
    output.mkdir(parents=True, exist_ok=True)
    for service, pool in (
        ("inventoryservice", "inventoryPriorityWorkers"),
        ("orderservice", "defaultPool"),
    ):
        values = (
            ROOT / "cppboostexample" / service / "config" / "overrides.yaml"
        ).read_text()
        values = values.replace(
            "connectionsCount: 1", f"connectionsCount: {grpc_connections}"
        )
        values = values.replace("executorsCount: 2", f"executorsCount: {service_cores}")
        if service == "orderservice":
            values = disable_order_processed_endpoint(values)
        if f"  {pool}:" not in values:
            raise RuntimeError(f"missing canonical pool {pool} in {service} values")
        (output / f"{service}.overrides.yaml").write_text(values)


def prepare_python_configs() -> None:
    output = ARTIFACTS / "python-config"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("inventoryservice", "orderservice"):
        values = (
            ROOT / "pyexample" / service / "config" / "docker_overrides.yaml"
        ).read_text()
        if service == "orderservice":
            values = disable_order_processed_endpoint(values)
        (output / f"{service}.overrides.yaml").write_text(values)


def raise_max_map_count(value: int) -> None:
    """vm.max_map_count is a global, non-namespaced host sysctl -- Docker
    does not allow setting it per-container (`--sysctl vm.max_map_count=...`
    is rejected outright). userver mmaps a stack per coroutine, and this
    example's pipeline fans out into far more coroutines than VUs (e.g.
    ~130k coroutines observed at VUS=512), so high-concurrency runs can
    exhaust the default host limit and make every request fail with
    "Failed to allocate a coroutine (ENOMEM)". Raise it once via a
    throwaway --privileged container before the benchmark starts; this
    persists host/VM-wide across all subsequent containers."""
    subprocess.run(
        [
            "docker", "run", "--rm", "--privileged", "debian:bookworm-slim",
            "sh", "-c", f"echo {value} > /proc/sys/vm/max_map_count",
        ],
        check=True,
    )


def wait_for_service(
    language: Language, service: str, url: str, env: dict[str, str]
) -> None:
    deadline = time.monotonic() + 90
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.5)
    logs = run(
        compose_command(language, "logs", "--no-color", "--tail", "100", service),
        cwd=language.example,
        env=env,
        capture=True,
        check=False,
    )
    print(logs.stdout + logs.stderr, file=sys.stderr)
    raise RuntimeError(f"{language.name} {service} did not become ready: {last_error}")


def runtime_pool_executor_counts(text: str) -> list[int]:
    """Read effective pool sizes from the YAML returned by /status/graph.

    The benchmark deliberately uses the noop metrics engine, so runtime
    configuration must not be verified through Prometheus.  The status graph
    is language-neutral and contains the configuration snapshot actually used
    to construct the graph.
    """
    lines = text.splitlines()
    section: list[str] = []
    in_pools = False
    for line in lines:
        if not in_pools:
            if line.strip() == "pools:" and not line[:1].isspace():
                in_pools = True
            continue
        # YAML permits an indentationless sequence directly below a mapping
        # key. Rust uses that canonical form (`pools:\n- name: ...`), while
        # the other generators indent the sequence or emit a mapping.
        if (
            line.strip()
            and not line[:1].isspace()
            and not line.startswith("- ")
        ):
            break
        section.append(line)
    return [
        int(value)
        for value in re.findall(
            r"(?m)^\s+executorsCount:\s*([0-9]+)\s*$",
            "\n".join(section),
        )
    ]


def service_pool_metrics(language: Language, service: str) -> tuple[str, ...]:
    graph_path = (
        language.example / service / "graph" / f"{service}.generated.yaml"
    )
    try:
        graph = graph_path.read_text()
    except OSError as error:
        raise RuntimeError(
            f"cannot determine {language.name} {service} call semantics: "
            f"failed to read {graph_path}: {error}"
        ) from error
    metrics = []
    if re.search(r"(?m)^\s*callSemantics:\s*TaskPool\s*$", graph):
        metrics.append("task_pool_executors_target")
    if re.search(r"(?m)^\s*callSemantics:\s*PriorityTaskPool\s*$", graph):
        metrics.append("priority_task_pool_executors_target")
    return tuple(metrics)


def service_uses_priority_task_pool(language: Language, service: str) -> bool:
    return "priority_task_pool_executors_target" in service_pool_metrics(
        language, service
    )


def verify_configured_pool_size(
    language: Language,
    service: str,
    port: int,
    expected: int,
) -> None:
    if not language.verify_framework_pool:
        return
    metrics = service_pool_metrics(language, service)
    if not metrics:
        print(
            f"Skipping {language.name} {service} task-pool check: "
            "the generated graph uses no TaskPool or PriorityTaskPool links",
            flush=True,
        )
        return
    verify_pool_size(language, service, port, expected)


def verify_pool_size(
    language: Language,
    service: str,
    port: int,
    expected: int,
) -> None:
    url = f"http://localhost:{port}/status/graph"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status = response.read().decode("utf-8")
    except (OSError, UnicodeError, urllib.error.URLError) as error:
        raise RuntimeError(
            f"cannot verify {language.name} {service} pool size from {url}: {error}"
        ) from error

    values = runtime_pool_executor_counts(status)
    if not values:
        raise RuntimeError(
            f"{language.name} {service} status graph contains no configured pools; "
            "the reused image is probably stale, run `make run` or `make build` first"
        )
    unexpected = [value for value in values if value != expected]
    if unexpected:
        raise RuntimeError(
            f"{language.name} {service} effective pool size is {values}, "
            f"expected {expected} from CORES"
        )
    print(
        f"Verified {language.name} {service}: pool executors={values}",
        flush=True,
    )


def load(
    language: Language,
    env: dict[str, str],
    *,
    duration: str,
    result_name: str,
) -> dict[str, Any]:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    result_path = ARTIFACTS / result_name
    result_path.write_text("")
    load_env = {
        **env,
        "BENCHMARK_DURATION": duration,
        "BENCHMARK_DURATION_SECONDS": str(duration_seconds(duration)),
        "BENCHMARK_RESULT_FILE": "/results/result.json",
        "BENCHMARK_RESULT_HOST_FILE": str(result_path),
    }
    run(
        compose_command(
            language,
            "--profile",
            "benchmark",
            "run",
            "--rm",
            "--no-deps",
            "loadgen",
        ),
        cwd=language.example,
        env=load_env,
    )
    if not result_path.exists() or result_path.stat().st_size == 0:
        raise RuntimeError(f"k6 did not write {result_path}")
    return json.loads(result_path.read_text())


def verify_boost_worker_configuration(
    language: Language,
    expected: int,
    env: dict[str, str],
    services: dict[str, Any] | None = None,
) -> None:
    if language.name not in {"cpp-boost", "cpp-boost-native"}:
        return
    if services is None:
        services = resolved_compose_services(language, env)
    for service in ("inventoryservice", "orderservice"):
        config = services[service]
        if language.name == "cpp-boost":
            command_line = config.get("command", [])
            pairs = list(zip(command_line, command_line[1:]))
            if ("--workers", str(expected)) not in pairs:
                raise RuntimeError(
                    f"{language.name} {service} does not resolve --workers {expected}"
                )
        else:
            actual = str(config.get("environment", {}).get("NATIVE_WORKER_THREADS", ""))
            if actual != str(expected):
                raise RuntimeError(
                    f"{language.name} {service} resolves NATIVE_WORKER_THREADS={actual}, "
                    f"expected {expected}"
                )
    print(f"Verified {language.name}: runtime workers={expected}", flush=True)


def verify_cpp_compose_isolation(
    language: Language,
    env: dict[str, str],
    services: dict[str, Any] | None = None,
) -> None:
    expected_prefixes = {
        "cpp": "cppexample",
        "cpp-boost": "cppboostexample",
    }
    expected_prefix = expected_prefixes.get(language.name)
    if expected_prefix is None:
        return
    if services is None:
        services = resolved_compose_services(language, env)
    for service in ("inventoryservice", "orderservice"):
        expected_image = f"{expected_prefix}-{service}"
        actual_image = services[service].get("image")
        if actual_image != expected_image:
            raise RuntimeError(
                f"{language.name} {service} resolves image={actual_image}, "
                f"expected {expected_image}; refusing to mix C++ build artifacts"
            )
    print(f"Verified {language.name}: independent runtime images", flush=True)


def resolved_compose_services(
    language: Language, env: dict[str, str]
) -> dict[str, Any]:
    resolved = run(
        compose_command(language, "config", "--format", "json"),
        cwd=language.example,
        env=env,
        capture=True,
    )
    return json.loads(resolved.stdout)["services"]


def verify_noop_telemetry_configuration(
    language: Language,
    env: dict[str, str],
    services: dict[str, Any] | None = None,
) -> None:
    if services is None:
        services = resolved_compose_services(language, env)
    required = (
        "SERVICELIB_NOOP_LOGS",
        "SERVICELIB_NOOP_METRICS",
        "SERVICELIB_NOOP_TRACING",
    )
    for service in ("inventoryservice", "orderservice"):
        service_environment = services[service].get("environment", {})
        missing = [
            name
            for name in required
            if str(service_environment.get(name, "")) != "1"
        ]
        if missing:
            raise RuntimeError(
                f"{language.name} {service} benchmark telemetry is not disabled: "
                f"{', '.join(missing)} must resolve to 1"
            )
    print(f"Verified {language.name}: benchmark telemetry=noop", flush=True)


def benchmark_language(
    language: Language, args: argparse.Namespace
) -> list[dict[str, Any]]:
    env = environment(args, language)
    services = resolved_compose_services(language, env)
    verify_noop_telemetry_configuration(language, env, services)
    verify_cpp_compose_isolation(language, env, services)
    verify_boost_worker_configuration(language, args.cores, env, services)
    try:
        run(
            compose_command(
                language, "up", "--detach", "--no-deps", "inventoryservice"
            ),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language,
            "inventoryservice",
            "http://localhost:9092/status/data",
            env,
        )
        verify_configured_pool_size(
            language,
            "inventoryservice",
            9092,
            args.cores,
        )
        run(
            compose_command(language, "up", "--detach", "--no-deps", "orderservice"),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language,
            "orderservice",
            "http://localhost:9091/status/data",
            env,
        )
        verify_configured_pool_size(
            language,
            "orderservice",
            9091,
            args.cores,
        )

        if args.warmup != "0" and args.warmup != "0s":
            load(
                language,
                env,
                duration=args.warmup,
                result_name=artifact_name(args, f"{language.name}.warmup.json"),
            )

        results = []
        for index in range(1, args.runs + 1):
            result = load(
                language,
                env,
                duration=args.duration,
                result_name=artifact_name(
                    args, f"{language.name}.run-{index}.json"
                ),
            )
            if result["error_rate"] > args.max_error_rate:
                raise RuntimeError(
                    f"{language.name} run {index} error rate "
                    f"{result['error_rate']:.6f} exceeds {args.max_error_rate:.6f}"
                )
            results.append(result)
        return results
    finally:
        run(
            compose_command(language, "down", "--volumes", "--remove-orphans"),
            cwd=language.example,
            env=env,
            check=False,
        )


def artifact_name(args: argparse.Namespace, name: str) -> str:
    return f"{args.result_prefix}.{name}" if args.result_prefix else name


def aggregate(
    language: Language, runs: list[dict[str, Any]], args: argparse.Namespace
) -> dict[str, Any]:
    best_run_index, best_run = max(
        enumerate(runs, start=1),
        key=lambda indexed_run: indexed_run[1]["requests_per_second"],
    )
    return {
        "language": language.name,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
        "vus": args.vus,
        "runs": len(runs),
        "best_run": best_run_index,
        "duration": args.duration,
        "requests_total": best_run["request_count"],
        "requests_per_second": best_run["requests_per_second"],
        "error_rate": best_run["error_rate"],
        "latency_ms": dict(best_run["latency_ms"]),
    }


def write_results(results: list[dict[str, Any]], args: argparse.Namespace) -> None:
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "architecture": platform.machine(),
            "os": platform.platform(),
        },
        "scenario": args.scenario,
        "graph_profile": getattr(args, "graph_profile", "function-call"),
        "parameters": {
            "build": "reused" if args.skip_build else "release",
            "service_cores": args.cores,
            "loadgen_cores": args.loadgen_cores,
            "grpc_connections": args.grpc_connections or args.cores,
            "vus": args.vus,
            "duration": args.duration,
            "warmup": args.warmup,
            "runs": args.runs,
            "max_map_count": args.max_map_count,
            "target": args.target,
            "method": args.method,
            "payload_mode": args.payload_mode,
            "expected_status": args.expected_status,
        },
        "results": results,
    }
    results_json = ARTIFACTS / artifact_name(args, "results.json")
    results_csv = ARTIFACTS / artifact_name(args, "results.csv")
    results_markdown = ARTIFACTS / artifact_name(args, "results.md")
    results_json.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )

    columns = [
        "language",
        "service_cores",
        "loadgen_cores",
        "vus",
        "runs",
        "requests_total",
        "requests_per_second",
        "error_rate_percent",
        "latency_avg_ms",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "latency_max_ms",
    ]
    rows = []
    for result in results:
        latency = result["latency_ms"]
        rows.append(
            {
                "language": result["language"],
                "service_cores": result["service_cores"],
                "loadgen_cores": result["loadgen_cores"],
                "vus": result["vus"],
                "runs": result["runs"],
                "requests_total": result["requests_total"],
                "requests_per_second": round(result["requests_per_second"], 2),
                "error_rate_percent": round(result["error_rate"] * 100, 6),
                "latency_avg_ms": round(latency["avg"], 3),
                "latency_p50_ms": round(latency["p50"], 3),
                "latency_p95_ms": round(latency["p95"], 3),
                "latency_p99_ms": round(latency["p99"], 3),
                "latency_max_ms": round(latency["max"], 3),
            }
        )
    with results_csv.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    header = (
        "| Language | Cores/service | Cores/loadgen | VUs | Runs | Requests/s | "
        "Errors | Avg ms | p50 ms | p95 ms | p99 ms | Max ms |\n"
    )
    separator = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    table_rows = []
    for row in rows:
        table_rows.append(
            f"| {row['language']} | {row['service_cores']} | "
            f"{row['loadgen_cores']} | {row['vus']} | "
            f"{row['runs']} | {row['requests_per_second']:.2f} | "
            f"{row['error_rate_percent']:.4f}% | {row['latency_avg_ms']:.3f} | "
            f"{row['latency_p50_ms']:.3f} | {row['latency_p95_ms']:.3f} | "
            f"{row['latency_p99_ms']:.3f} | {row['latency_max_ms']:.3f} |"
        )
    results_markdown.write_text(
        "# Framework example benchmark\n\n"
        f"- Scenario: `{args.scenario}`\n"
        f"- Graph profile: `{getattr(args, 'graph_profile', 'function-call')}`\n"
        + f"- Request: `{args.method} {args.target}` (expected `{args.expected_status}`)\n"
        + f"- Payload mode: `{args.payload_mode}`\n"
        f"- Service CPU quota: `{args.cores}` cores per container\n"
        f"- Load generator CPU quota: `{args.loadgen_cores}` cores\n"
        f"- Virtual users: `{args.vus}`\n"
        f"- Warm-up: `{args.warmup}`\n"
        f"- Measurement: `{args.runs} × {args.duration}`\n\n"
        + header
        + separator
        + "\n".join(table_rows)
        + "\n"
    )


def clean() -> None:
    for language in LANGUAGES:
        if not language.example.is_dir():
            continue
        args = argparse.Namespace(
            cores=1,
            loadgen_cores=1,
            duration="1s",
            vus=1,
        )
        run(
            compose_command(language, "down", "--volumes", "--remove-orphans"),
            cwd=language.example,
            env=environment(args, language),
            check=False,
        )
    shutil.rmtree(ARTIFACTS, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark equivalent ServiceLib and native framework baselines"
    )
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--loadgen-cores", type=int, default=6)
    parser.add_argument(
        "--grpc-connections",
        type=int,
        help="gRPC channels per connector (defaults to --cores)",
    )
    parser.add_argument("--vus", type=int, default=32)
    parser.add_argument("--duration", default="20s")
    parser.add_argument("--warmup", default="5s")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenario", default="process_order_out_of_stock")
    parser.add_argument(
        "--graph-profile",
        choices=("function-call", "current"),
        default="function-call",
        help="generated graph profile recorded in result metadata",
    )
    parser.add_argument(
        "--target", default="http://orderservice:9091/v1/processorder"
    )
    parser.add_argument("--method", choices=("GET", "POST"), default="POST")
    parser.add_argument(
        "--payload-mode", choices=("normal", "invalid-json"), default="normal"
    )
    parser.add_argument("--expected-status", type=int, default=200)
    parser.add_argument("--result-prefix", default="")
    parser.add_argument(
        "--native-diagnostic-bypass-grpc",
        action="store_true",
        help=(
            "cpp-boost-native only: retain HTTP/JSON/business processing but "
            "replace the inventory gRPC round-trip with the same out-of-stock result"
        ),
    )
    parser.add_argument("--max-error-rate", type=float, default=0.001)
    parser.add_argument(
        "--max-map-count",
        type=int,
        default=0,
        help="vm.max_map_count to set host/VM-wide before running (0 to leave it untouched)",
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument(
        "--fetch-native",
        action="store_true",
        help="fetch missing selected native example projects and exit",
    )
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--language",
        action="append",
        choices=[language.name for language in LANGUAGES],
    )
    args = parser.parse_args()

    if not args.fetch_native:
        try:
            tooling_lock.acquire()
        except RuntimeError as error:
            parser.error(str(error))

    if args.clean:
        clean()
        return 0
    if args.cores <= 0 or args.loadgen_cores <= 0:
        parser.error("CPU core counts must be positive integers")
    if args.grpc_connections is not None and args.grpc_connections <= 0:
        parser.error("--grpc-connections must be a positive integer")
    if args.vus <= 0 or args.runs <= 0:
        parser.error("VUs and runs must be positive integers")
    if args.max_map_count < 0:
        parser.error("--max-map-count must not be negative")
    if not 100 <= args.expected_status <= 599:
        parser.error("--expected-status must be between 100 and 599")
    if args.result_prefix and re.fullmatch(
        r"[A-Za-z0-9_.-]+", args.result_prefix
    ) is None:
        parser.error("--result-prefix contains unsupported characters")

    selected = [
        language
        for language in LANGUAGES
        if not args.language or language.name in args.language
    ]
    if args.fetch_native:
        ensure_examples(
            [language for language in selected if language.repository is not None],
            args,
        )
        return 0
    ensure_examples(selected, args)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    cpp_selected = any(language.name.startswith("cpp") for language in selected)
    if cpp_selected:
        if any(language.name == "cpp" for language in selected):
            prepare_cpp_configs(args.cores)
        if any(language.name == "cpp-boost" for language in selected):
            prepare_cppboost_configs(
                args.cores, args.grpc_connections or args.cores
            )
        if args.max_map_count:
            raise_max_map_count(args.max_map_count)
    if any(language.name == "python" for language in selected):
        prepare_python_configs()

    if not args.skip_build:
        for language in selected:
            build(language, environment(args, language))
    if args.build_only:
        return 0

    results = []
    for language in selected:
        print(f"\n=== {language.name} ===", flush=True)
        runs = benchmark_language(language, args)
        results.append(aggregate(language, runs, args))
        write_results(results, args)

    print(
        "\n" + (ARTIFACTS / artifact_name(args, "results.md")).read_text(),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
