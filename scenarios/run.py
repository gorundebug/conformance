#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_source_cache
import go_toolchain

HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACTS = Path(
    os.environ.get(
        "SERVICELIB_SCENARIO_ARTIFACTS_DIR",
        CONFORMANCE_DIR / ".artifacts" / "scenarios",
    )
).expanduser().resolve()
PROJECT_SUFFIX = os.environ.get("SERVICELIB_SCENARIO_PROJECT_SUFFIX", "")
REQUIRE_POOL_ACTIVITY = os.environ.get("SERVICELIB_SCENARIO_REQUIRE_POOLS") == "1"
ORDER_URL = "http://localhost:9091/v1/processorder"
GRPC_PROBE_OVERLAY = HERE / "compose.grpc-probe.yml"
GRPC_PROBE_BINARY = ARTIFACTS / "grpc-probe"
GRPC_PROBE_BUILD_CACHE = "servicelib-conformance-scenario-grpc-probe-build-cache"
GRPC_PROBE_MODULE_CACHE = "servicelib-conformance-scenario-grpc-probe-module-cache"


@dataclass(frozen=True)
class Implementation:
    name: str
    example: Path
    overlay: Path


@dataclass(frozen=True)
class HttpObservation:
    status: int
    content_type: str
    body: bytes
    payload: Any

    def artifact(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "headers": {"content-type": self.content_type},
            "body_utf8": self.body.decode("utf-8"),
            "body_hex": self.body.hex(),
            "payload": self.payload,
        }


IMPLEMENTATIONS = (
    Implementation("go", ROOT / "goexample", HERE / "compose.go.yml"),
    Implementation(
        "go-native", ROOT / "gonativeexample", HERE / "compose.go-native.yml"
    ),
    Implementation("cpp", ROOT / "cppexample", HERE / "compose.cpp.yml"),
    Implementation(
        "cpp-native", ROOT / "cppnativeexample", HERE / "compose.cpp-native.yml"
    ),
    Implementation("cppboost", ROOT / "cppboostexample", HERE / "compose.cppboost.yml"),
    Implementation("cppboost-native", ROOT / "cppboostnativeexample", HERE / "compose.cppboost-native.yml"),
    Implementation("python", ROOT / "pyexample", HERE / "compose.python.yml"),
    Implementation(
        "python-native",
        ROOT / "pynativeexample",
        HERE / "compose.python-native.yml",
    ),
    Implementation("rust", ROOT / "rustexample", HERE / "compose.rust.yml"),
    Implementation(
        "rust-native", ROOT / "rustnativeexample", HERE / "compose.rust-native.yml"
    ),
    Implementation("typescript", ROOT / "tsexample", HERE / "compose.typescript.yml"),
    Implementation(
        "typescript-native",
        ROOT / "tsnativeexample",
        HERE / "compose.typescript-native.yml",
    ),
)

NATIVE_SOURCE_CONTEXTS: tuple[Path, Path] | None = None


def command(implementation: Implementation, *args: str) -> list[str]:
    result = [
        "docker", "compose", "--project-name",
        f"servicelib-scenario-conformance-{implementation.name}{PROJECT_SUFFIX}",
        "--project-directory", str(implementation.example),
        "--file", str(implementation.example / "docker-compose.yml"),
    ]
    if implementation.name in {"cpp", "python", "typescript"}:
        runtime_overlays = sorted(
            implementation.example.glob("docker-compose.*-runtime.generated.yml")
        )
        if len(runtime_overlays) != 1:
            raise RuntimeError(
                f"{implementation.name} requires exactly one generated runtime "
                f"compose file, found {len(runtime_overlays)}"
            )
        result.extend(["--file", str(runtime_overlays[0])])
    result.extend(
        [
            "--file",
            str(implementation.overlay),
            "--file",
            str(GRPC_PROBE_OVERLAY),
            *args,
        ]
    )
    return result


def environment(implementation: Implementation) -> dict[str, str]:
    env = os.environ.copy()
    env["SERVICELIB_CONFORMANCE_DEPENDENCIES_DIR"] = str(ROOT)
    env["SERVICELIB_CONFORMANCE_DIR"] = str(CONFORMANCE_DIR)
    env["SERVICELIB_SCENARIO_ARTIFACTS_DIR"] = str(ARTIFACTS)
    env["SERVICEGEN_GO_TOOLCHAIN_IMAGE"] = go_toolchain.docker_image(ROOT)
    # The C++ framework examples include the Go Temporal fallback service.
    env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
    if implementation.name == "cpp":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
        env["USERVER_LTO"] = "ON"
    elif implementation.name == "cpp-native":
        userver = ROOT / "userver"
        env["USERVER_SOURCE_CONTEXT"] = (
            str(userver)
            if userver.is_dir()
            else "https://github.com/userver-framework/userver.git#c9f77729c0edce7e423def2d4a4450aa7fc9d259"
        )
        env["USERVER_LTO"] = "ON"
    elif implementation.name == "cppboost":
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppboostservicelib")
        # The generated build compose and the runtime overlay must mount the
        # same build volume.  Keep the pooled profile isolated from the normal
        # scenario profile because both contain generated graph semantics in
        # the compiled service binaries.
        env["SERVICEGEN_CPPBOOST_BUILD_VOLUME"] = (
            f"servicelib-scenario-conformance-cppboost{PROJECT_SUFFIX}-build"
        )
    elif implementation.name == "cppboost-native" and NATIVE_SOURCE_CONTEXTS:
        grpc_source, asio_grpc_source = NATIVE_SOURCE_CONTEXTS
        env["SERVICEGEN_GRPC_SOURCE_CONTEXT"] = str(grpc_source)
        env["SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT"] = str(asio_grpc_source)
    elif implementation.name == "typescript":
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "tsservicelib")
    elif implementation.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "pyservicelib")
    elif implementation.name == "rust":
        env["RUSTSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "rustservicelib")
    return env


def run(implementation: Implementation, *args: str, check: bool = True) -> None:
    cmd = command(implementation, *args)
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=implementation.example,
                   env=environment(implementation), check=check)


def prepare_grpc_probe() -> None:
    """Build the reusable static probe once instead of running `go run` per RPC."""
    command_line = [
        "docker",
        "run",
        "--rm",
        "--volume",
        f"{ROOT / 'goexample'}:/repo/goexample:ro",
        "--volume",
        f"{CONFORMANCE_DIR}:/repo/conformance:ro",
        "--volume",
        f"{ARTIFACTS}:/out",
        "--volume",
        f"{GRPC_PROBE_BUILD_CACHE}:/go-cache",
        "--volume",
        f"{GRPC_PROBE_MODULE_CACHE}:/go/pkg/mod",
        "--workdir",
        "/repo/conformance/scenarios/grpc_probe",
        "--env",
        "CGO_ENABLED=0",
        "--env",
        "GOWORK=off",
        "--env",
        "GOCACHE=/go-cache",
        "--env",
        "GOMODCACHE=/go/pkg/mod",
        go_toolchain.docker_image(ROOT),
        "go",
        "build",
        "-trimpath",
        "-buildvcs=false",
        "-o",
        "/out/grpc-probe",
        ".",
    ]
    print("+", " ".join(command_line), flush=True)
    subprocess.run(command_line, cwd=CONFORMANCE_DIR, check=True)
    if not GRPC_PROBE_BINARY.is_file():
        raise RuntimeError(f"gRPC probe build did not produce {GRPC_PROBE_BINARY}")


def grpc_request(
    implementation: Implementation, mode: str, item_id: str
) -> dict[str, Any]:
    cmd = command(
        implementation,
        "run",
        "--rm",
        "--no-deps",
        "grpc-probe",
        "--address",
        "inventoryservice:9202",
        "--mode",
        mode,
        "--item-id",
        item_id,
    )
    print("+", " ".join(cmd), flush=True)
    completed = subprocess.run(
        cmd,
        cwd=implementation.example,
        env=environment(implementation),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{implementation.name} gRPC {mode} probe failed with exit "
            f"{completed.returncode}:\n{completed.stdout[-2000:]}"
            f"{completed.stderr[-2000:]}"
        )
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("mode") == mode:
            return value
    raise RuntimeError(
        f"{implementation.name} gRPC {mode} probe emitted no JSON result: "
        f"{completed.stdout[-500:]} {completed.stderr[-500:]}"
    )


def prepare_cppboost() -> None:
    output = ARTIFACTS / "cppboost"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("inventoryservice", "orderservice"):
        source = ROOT / "cppboostexample" / service / "config" / "overrides.yaml"
        (output / f"{service}.overrides.yaml").write_text(source.read_text())


def prepare_cpp() -> None:
    output = ARTIFACTS / "cpp"
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / "cppexample" / "orderservice" / "config"
    overrides = (source / "overrides.integration.generated.yaml").read_text()
    overrides = overrides.replace(
        "  orderProcessed:\n    enabled: true",
        "  orderProcessed:\n    enabled: false",
    )
    (output / "orderservice.overrides.yaml").write_text(overrides)
    variables = (source / "config_vars.integration.yaml").read_text()
    variables = variables.replace(
        "orderServiceConfigOverridePath: config/overrides.integration.generated.yaml",
        "orderServiceConfigOverridePath: /scenario/orderservice.overrides.yaml",
    ).replace("orderProcessedEnabled: true", "orderProcessedEnabled: false")
    (output / "orderservice.config_vars.yaml").write_text(variables)


def prepare_python() -> None:
    output = ARTIFACTS / "python"
    output.mkdir(parents=True, exist_ok=True)
    source = (
        ROOT
        / "pyexample"
        / "orderservice"
        / "config"
        / "docker_overrides.yaml"
    )
    overrides = source.read_text().replace(
        "  orderProcessed:\n    enabled: true",
        "  orderProcessed:\n    enabled: false",
    )
    (output / "orderservice.overrides.yaml").write_text(overrides)


def prepare_cppboost_build_cache() -> Path:
    framework = ROOT / "cppboostservicelib"
    subprocess.run(
        ["docker", "build", "-f", "Dockerfile.cmake", "-t",
         "cppboostservicelib-build", "."],
        cwd=framework,
        check=True,
    )
    subprocess.run(
        cpp_source_cache.prepare_command(framework),
        cwd=framework,
        check=True,
    )
    source_dir = cpp_source_cache.source_dir(framework)
    output = ARTIFACTS / "cppboost"
    cmake_cache = output / "conformance-source-cache.generated.cmake"
    cmake_cache.write_text(cpp_source_cache.cmake_cache_contents())
    override = output / "compose.source-cache.generated.yml"
    override.write_text(json.dumps({
        "services": {
            "cpp-build": {
                "volumes": [
                    f"{source_dir}:{cpp_source_cache.CONTAINER_SOURCE_DIR}:ro",
                    f"{output}:/workspace/conformance:ro",
                ],
            },
        },
    }, indent=2) + "\n")
    return override


def build_cppboost(implementation: Implementation, override: Path) -> None:
    cmd = [
        "docker", "compose", "--project-name",
        f"servicelib-scenario-conformance-{implementation.name}{PROJECT_SUFFIX}",
        "--project-directory", str(implementation.example),
        "--file", str(implementation.example / "docker-compose.cmake.generated.yml"),
        "--file", str(override),
        "run", "--build", "--rm",
        "-e", "SERVICEGEN_CPP_CMAKE_PRESET=docker-release",
        "cpp-build", "/bin/bash", "-lc",
        "source scripts/configure-git-auth.generated.sh && "
        "./scripts/conan-install.generated.sh Release "
        "/workspace/build/conan-release && "
        "conan_toolchain=$(cat /workspace/build/conan-release/toolchain.path) && "
        "cmake --fresh --preset docker-release "
        "-DCMAKE_TOOLCHAIN_FILE=\"$conan_toolchain\" "
        "-C /workspace/conformance/conformance-source-cache.generated.cmake "
        "-DSERVICEGEN_FETCH_CPP_DEPENDENCIES=OFF && "
        "cmake --build --preset docker-release --parallel",
    ]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(
        cmd,
        cwd=implementation.example,
        env=environment(implementation),
        check=True,
    )


def wait_ready(implementation: Implementation) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            for port in (9091, 9092):
                with urllib.request.urlopen(f"http://localhost:{port}/status/data", timeout=2) as response:
                    if response.status != 200:
                        raise RuntimeError(f"status endpoint returned {response.status}")
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    logs = subprocess.run(
        command(
            implementation,
            "logs",
            "--no-color",
            "--tail",
            "100",
            "inventoryservice",
            "orderservice",
        ),
        cwd=implementation.example,
        env=environment(implementation),
        check=False,
        capture_output=True,
        text=True,
    )
    output = (logs.stdout + logs.stderr).strip()
    raise RuntimeError(
        f"{implementation.name} services did not become ready"
        + (f":\n{output}" if output else "")
    )


def request(request_id: str, items: list[dict[str, Any]]) -> HttpObservation:
    body = json.dumps(
        {"customer_id": "conformance", "items": items},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    message = urllib.request.Request(
        ORDER_URL,
        data=body,
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(message, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        response_body = response.read()
        try:
            text = response_body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError(
                f"{request_id} returned invalid UTF-8: {response_body!r}"
            ) from error
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        return HttpObservation(
            status=response.status,
            content_type=response.headers.get("Content-Type", ""),
            body=response_body,
            payload=payload,
        )


def metric_text(port: int) -> str:
    with urllib.request.urlopen(f"http://localhost:{port}/metrics", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(
                f"pool metrics endpoint on port {port} returned {response.status}"
            )
        return response.read().decode("utf-8")


def graph_text(port: int) -> str:
    url = f"http://localhost:{port}/status/graph"
    try:
        response = urllib.request.urlopen(url, timeout=5)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"status graph endpoint on port {port} returned {error.code}: {body}"
        ) from error
    with response:
        if response.status != 200:
            raise RuntimeError(
                f"status graph endpoint on port {port} returned {response.status}"
            )
        return response.read().decode("utf-8")


def call_semantics_graph(implementation_name: str) -> dict[str, int]:
    graphs = {
        "orderservice": graph_text(9091),
        "inventoryservice": graph_text(9092),
    }
    for service, graph in graphs.items():
        (ARTIFACTS / f"{implementation_name}.{service}.graph.yaml").write_text(
            graph
        )

    expected = {
        "orderservice": {
            "task_pool_links": 0,
            "priority_task_pool_links": 1,
            "parallel_call_links": 2,
            "pool_name": "Default Pool",
        },
        "inventoryservice": {
            "task_pool_links": 1,
            "priority_task_pool_links": 0,
            "parallel_call_links": 1,
            "pool_name": "Inventory Priority Workers",
        },
    }
    observed: dict[str, int] = {}
    for service, requirement in expected.items():
        graph = graphs[service]
        task_count = sum(
            graph.count(token)
            for token in (
                "callSemantics: TaskPool",
                "call_semantics: TaskPool",
                "call_semantics: !TaskPool",
                "      taskPool:",
            )
        )
        # The public API model uses camelCase. Rust's idiomatic serde output
        # uses snake_case and represents the data-bearing priority variant as
        # a YAML tag. Both encodings carry the same graph semantics.
        priority_count = sum(
            graph.count(token)
            for token in (
                "callSemantics: PriorityTaskPool",
                "call_semantics: PriorityTaskPool",
                "call_semantics: !PriorityTaskPool",
                "      priorityTaskPool:",
            )
        )
        parallel_count = sum(
            graph.count(token)
            for token in (
                "callSemantics: ParallelCall",
                "call_semantics: ParallelCall",
                "      parallelCall:",
            )
        )
        require(
            task_count == requirement["task_pool_links"],
            f"{service} live graph has {task_count} TaskPool links",
        )
        require(
            priority_count == requirement["priority_task_pool_links"],
            f"{service} live graph has {priority_count} PriorityTaskPool links",
        )
        require(
            parallel_count == requirement["parallel_call_links"],
            f"{service} live graph has {parallel_count} ParallelCall links",
        )
        require(
            ("poolName:" in graph or "pool_name:" in graph)
            and str(requirement["pool_name"]) in graph,
            f"{service} live graph is missing pool {requirement['pool_name']!r}",
        )
        observed[f"{service}_task_pool_links"] = task_count
        observed[f"{service}_priority_task_pool_links"] = priority_count
        observed[f"{service}_parallel_call_links"] = parallel_count
    return observed


def counter_total(metrics: str, family: str, pool_name: str) -> float:
    total = 0.0
    found = False
    for line in metrics.splitlines():
        if not line.startswith(family):
            continue
        sample, separator, raw_value = line.rpartition(" ")
        if not separator or f'name="{pool_name}"' not in sample:
            continue
        try:
            total += float(raw_value)
        except ValueError as error:
            raise RuntimeError(f"invalid metric sample: {line}") from error
        found = True
    if not found:
        raise RuntimeError(
            f"missing {family} metric for configured pool {pool_name!r}"
        )
    return total


def pool_activity(implementation_name: str) -> dict[str, float]:
    order_metrics = metric_text(9091)
    inventory_metrics = metric_text(9092)
    (ARTIFACTS / f"{implementation_name}.orderservice.metrics.prom").write_text(
        order_metrics
    )
    (ARTIFACTS / f"{implementation_name}.inventoryservice.metrics.prom").write_text(
        inventory_metrics
    )
    observed = {
        "order_priority_tasks": counter_total(
            order_metrics,
            "priority_task_pool_tasks_total",
            "Default Pool",
        ),
        "inventory_tasks": counter_total(
            inventory_metrics,
            "task_pool_tasks_total",
            "Inventory Priority Workers",
        ),
    }
    for name, value in observed.items():
        require(value > 0, f"configured pooled call was not executed: {name}={value}")
    return observed


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.pop("processed_at", None)
    return payload


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def require_json_response(name: str, response: HttpObservation, status: int) -> None:
    require(
        response.status == status,
        f"{name} returned HTTP {response.status}, expected {status}",
    )
    media_type = response.content_type.partition(";")[0].strip().lower()
    require(
        media_type == "application/json",
        f"{name} returned Content-Type {response.content_type!r}, expected application/json",
    )
    require(
        isinstance(response.payload, dict),
        f"{name} returned a non-object JSON body: {response.payload!r}",
    )


def require_text_response(name: str, response: HttpObservation, status: int) -> None:
    require(
        response.status == status,
        f"{name} returned HTTP {response.status}, expected {status}",
    )
    media_type = response.content_type.partition(";")[0].strip().lower()
    require(
        media_type == "text/plain",
        f"{name} returned Content-Type {response.content_type!r}, expected text/plain",
    )
    require(
        isinstance(response.payload, str),
        f"{name} returned a non-text body: {response.payload!r}",
    )


def evaluate(implementation: Implementation) -> dict[str, Any]:
    observed: dict[str, Any] = {}
    try:
        run(implementation, "up", "--detach", "--no-deps", "inventoryservice")
        run(implementation, "up", "--detach", "--no-deps", "orderservice")
        wait_ready(implementation)
        observed["grpc_success"] = grpc_request(
            implementation, "success", "grpc-success"
        )
        response = request("confirmed", [{"item_id": "item-1", "sku": "SKU-001", "quantity": 2, "unit_price": 10.5}])
        require_json_response("confirmed", response, 200)
        observed["confirmed"] = response.artifact()
        observed["confirmed"]["payload"] = normalize(response.payload)

        response = request("out-of-stock", [{"item_id": "item-x", "sku": "UNKNOWN", "quantity": 1, "unit_price": 3}])
        require_json_response("out-of-stock", response, 200)
        observed["out_of_stock"] = response.artifact()
        observed["out_of_stock"]["payload"] = normalize(response.payload)

        response = request("partial", [
            {"item_id": "item-partial-ok", "sku": "SKU-003", "quantity": 1, "unit_price": 5},
            {"item_id": "item-partial-missing", "sku": "UNKNOWN", "quantity": 1, "unit_price": 3},
        ])
        require_json_response("partial", response, 200)
        observed["partial"] = response.artifact()
        observed["partial"]["payload"] = normalize(response.payload)

        response = request("invalid", [{"item_id": "item-bad", "sku": "SKU-001", "quantity": 0, "unit_price": 1}])
        require_text_response("invalid", response, 400)
        observed["invalid"] = response.artifact()

        run(implementation, "pause", "inventoryservice")
        try:
            observed["grpc_deadline"] = grpc_request(
                implementation, "deadline", "grpc-deadline"
            )
            observed["grpc_cancel"] = grpc_request(
                implementation, "cancel", "grpc-cancel"
            )
            response = request("timeout", [{"item_id": "item-timeout", "sku": "SKU-002", "quantity": 1, "unit_price": 7}])
        finally:
            run(implementation, "unpause", "inventoryservice", check=False)
        require_json_response("timeout", response, 200)
        observed["timeout"] = response.artifact()
        observed["timeout"]["payload"] = normalize(response.payload)
        observed["grpc_recovery"] = grpc_request(
            implementation, "success", "grpc-recovery"
        )

        run(implementation, "pause", "inventoryservice")
        delayed_unpause = threading.Thread(
            target=lambda: (
                time.sleep(0.25),
                run(implementation, "unpause", "inventoryservice", check=False),
            ),
            name=f"{implementation.name}-delayed-unpause",
        )
        delayed_unpause.start()
        delayed_started = time.monotonic()
        try:
            response = request("delayed", [{
                "item_id": "item-delayed", "sku": "SKU-003",
                "quantity": 1, "unit_price": 11,
            }])
        finally:
            delayed_unpause.join()
        delayed_elapsed_ms = (time.monotonic() - delayed_started) * 1000
        require_json_response("delayed", response, 200)
        observed["delayed"] = response.artifact()
        observed["delayed"]["payload"] = normalize(response.payload)
        observed["delayed"]["elapsed_ms"] = round(delayed_elapsed_ms, 3)

        if REQUIRE_POOL_ACTIVITY:
            observed["call_semantics_graph"] = call_semantics_graph(
                implementation.name
            )
            observed["pool_activity"] = pool_activity(implementation.name)

        (ARTIFACTS / f"{implementation.name}.json").write_text(
            json.dumps(observed, indent=2, sort_keys=True) + "\n")
        return observed
    finally:
        subprocess.run(command(implementation, "down", "--volumes", "--remove-orphans"),
                       cwd=implementation.example,
                       env=environment(implementation), check=False)


def assert_contract(result: dict[str, Any]) -> None:
    for name in ("grpc_success", "grpc_recovery"):
        require(result[name]["code"] == "OK", f"{name} status differs")
        require(
            result[name]["response"]
            == {"available_qty": 2, "reserved": True, "status": "CONFIRMED"},
            f"{name} response differs",
        )
    require(
        result["grpc_deadline"]["code"] == "DeadlineExceeded",
        "gRPC deadline status differs",
    )
    require(
        result["grpc_cancel"]["code"] == "Canceled",
        "gRPC cancellation status differs",
    )
    confirmed = result["confirmed"]["payload"]
    require(confirmed["order_id"] == "confirmed", "confirmed order_id differs")
    require(confirmed["status"] == "CONFIRMED", "confirmed status differs")
    require(confirmed["total_amount"] == 21, "confirmed total_amount differs")
    require(confirmed["confirmed_items"] == [{
        "item_id": "item-1", "sku": "SKU-001",
        "available_qty": 2, "reserved": True, "status": "CONFIRMED",
    }], "confirmed_items differ")
    out_of_stock = result["out_of_stock"]["payload"]
    require(
        out_of_stock["status"] == "PARTIALLY_CONFIRMED",
        "out-of-stock status differs",
    )
    require(
        out_of_stock["confirmed_items"][0]["status"] == "OUT_OF_STOCK",
        "out-of-stock item status differs",
    )
    partial = result["partial"]["payload"]
    require(partial["order_id"] == "partial", "partial order_id differs")
    require(
        partial["status"] == "PARTIALLY_CONFIRMED",
        "partial status differs",
    )
    require(partial["total_amount"] == 8, "partial total_amount differs")
    actual_partial_items = [
        (item["item_id"], item["status"], item["reserved"])
        for item in partial["confirmed_items"]
    ]
    expected_partial_items = [
        ("item-partial-ok", "CONFIRMED", True),
        ("item-partial-missing", "OUT_OF_STOCK", False),
    ]
    require(
        actual_partial_items == expected_partial_items,
        "partial item results differ: "
        f"actual={actual_partial_items!r}, expected={expected_partial_items!r}",
    )
    require(
        result["invalid"]["payload"] == "all quantities must be positive\n",
        "invalid-request response differs",
    )
    timeout = result["timeout"]["payload"]
    require(timeout["status"] == "TIMED_OUT", "timeout status differs")
    require(timeout.get("confirmed_items", []) == [], "timeout items differ")
    delayed = result["delayed"]
    require(
        delayed["elapsed_ms"] >= 150,
        f"delayed dependency resumed too early: {delayed['elapsed_ms']} ms",
    )
    require(
        delayed["payload"]["status"] == "CONFIRMED",
        "delayed dependency response status differs",
    )


def semantic_result(result: dict[str, Any]) -> dict[str, Any]:
    http = {
        name: {
            "status": observation["status"],
            "content_type": observation["headers"]["content-type"].partition(";")[0]
            .strip()
            .lower(),
            "payload": observation["payload"],
        }
        for name, observation in result.items()
        if not name.startswith("grpc_")
        and name not in {"pool_activity", "call_semantics_graph"}
    }
    grpc = {
        name: {
            "code": observation["code"],
            "response": observation.get("response"),
        }
        for name, observation in result.items()
        if name.startswith("grpc_")
    }
    return {"http": http, "grpc": grpc}


def build_implementation(
    implementation: Implementation,
    cppboost_cache_override: Path | None,
) -> None:
    if implementation.name == "go":
        print("+ make docker-build", flush=True)
        subprocess.run(
            ["make", "docker-build"],
            cwd=implementation.example,
            env=environment(implementation),
            check=True,
        )
    elif implementation.name == "cppboost":
        if cppboost_cache_override is None:
            raise RuntimeError("Boost source-cache override was not prepared")
        build_cppboost(implementation, cppboost_cache_override)
    elif implementation.name in {"cpp", "typescript"}:
        subprocess.run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=implementation.example,
            env=environment(implementation),
            check=True,
        )
    else:
        run(implementation, "build", "inventoryservice", "orderservice")


def main() -> int:
    global NATIVE_SOURCE_CONTEXTS
    parser = argparse.ArgumentParser(
        description="Cross-language framework/native scenario conformance"
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--language", action="append", choices=[value.name for value in IMPLEMENTATIONS])
    args = parser.parse_args()
    selected = [value for value in IMPLEMENTATIONS if not args.language or value.name in args.language]
    selected_names = {value.name for value in selected}
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    prepare_grpc_probe()
    if "cpp" in selected_names:
        prepare_cpp()
    if "cppboost" in selected_names:
        prepare_cppboost()
    if "python" in selected_names:
        prepare_python()
    cppboost_cache_override: Path | None = None
    if not args.skip_build and any(
        value.name == "cppboost" for value in selected
    ):
        cppboost_cache_override = prepare_cppboost_build_cache()
    if not args.skip_build and any(
        value.name == "cppboost-native" for value in selected
    ):
        sources = cpp_source_cache.ensure(ROOT / "cppboostservicelib")
        NATIVE_SOURCE_CONTEXTS = (
            sources / "grpc-src",
            sources / "asio-grpc-src",
        )
    if not args.skip_build:
        for implementation in selected:
            build_implementation(implementation, cppboost_cache_override)

    summary: dict[str, Any] = {"status": "passed", "implementations": {}}
    reference: dict[str, Any] | None = None
    for implementation in selected:
        result = evaluate(implementation)
        assert_contract(result)
        comparable = semantic_result(result)
        if reference is None:
            reference = comparable
        elif comparable != reference:
            raise RuntimeError(f"{implementation.name} observable payloads differ from {selected[0].name}")
        summary["implementations"][implementation.name] = {
            "status": "passed",
            "scenarios": sorted(result),
            "wire_artifact": f".artifacts/scenarios/{implementation.name}.json",
        }
        if REQUIRE_POOL_ACTIVITY:
            summary["implementations"][implementation.name]["pool_activity"] = result[
                "pool_activity"
            ]
            summary["implementations"][implementation.name][
                "call_semantics_graph"
            ] = result["call_semantics_graph"]
    (ARTIFACTS / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("Scenario conformance passed:", ", ".join(value.name for value in selected))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Scenario conformance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
