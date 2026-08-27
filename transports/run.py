#!/usr/bin/env python3
"""Canonical C++ and Boost.Asio gRPC transport conformance gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_source_cache
import cpp_userver
import go_toolchain
import typescript_toolchain


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "transports" / "summary.json"
CANONICAL = ROOT / "cppservicelib"
BOOST = ROOT / "cppboostservicelib"
SERVICEGEN = ROOT / "servicegen"
TYPESCRIPT = ROOT / "tsservicelib"
GO = ROOT / "servicelib"
RUST = ROOT / "rustservicelib"
PYTHON = ROOT / "pyservicelib"

SOURCE_CASES = {
    GO / "datasource/http/nethttp.go": (
        "type EndpointHandler[HandlerState, ReqT, ResR, T, R, E any] interface",
        "BeginRequest(ctx context.Context, sc StreamContext[T, R, E], data HandlerData)",
        "ConsumeMessage(ctx context.Context, sc StreamContext[T, R, E]",
        "GetMessageID(ctx context.Context, sc StreamContext[T, R, E]",
        "EndRequest(ctx context.Context, sc StreamContext[T, R, E]",
    ),
    GO / "datasource/kafka/sarama.go": (
        "type EndpointHandler[HandlerState, T, R, E any] interface",
        "Concurrency(sc StreamContext[T, R, E]) int",
        "BeginRequest(ctx context.Context, sc StreamContext[T, R, E])",
        "ConsumeMessage(ctx context.Context, sc StreamContext[T, R, E]",
        "GetMessageID(ctx context.Context, sc StreamContext[T, R, E]",
        "EndRequest(ctx context.Context, sc StreamContext[T, R, E]",
    ),
    GO / "datasource/localsource/custom.go": (
        "type DataProducer[T any] interface",
        "type EndpointHandler[HandlerState, T, R, E any] interface",
        "MakeCustomEndpointConsumer[HandlerState, T, R, E any]",
    ),
    GO / "datasource/grpc/grpc.go": (
        "type EndpointHandler[HandlerState, ReqT, ResR, T, R, E any] interface",
        "BeginRequest(",
        "ConsumeMessage(",
        "GetMessageID(",
        "Eof(",
        "EndRequest(",
    ),
    GO / "runtime/telemetry/opentelemetry/metric_names_test.go": (
        't.Run("HTTPClient"',
        "httptest.NewServer",
        "otelhttp.NewTransport",
        "http_client_request_duration_seconds",
    ),
    RUST / "src/datasource/http/axum.rs": (
        "pub trait EndpointHandler<HandlerState, ReqT, ResR, T, R, E>",
        "async fn begin_request(",
        "async fn consume_message(",
        "async fn get_message_id(",
        "async fn end_request(",
    ),
    RUST / "src/datasource/kafka/rdkafka.rs": (
        "pub trait EndpointHandler<HandlerState, T, R, E>",
        "fn concurrency(",
        "async fn begin_request(",
        "async fn consume_message(",
        "fn get_message_id(",
        "async fn end_request(",
    ),
    RUST / "src/datasource/localsource/custom.rs": (
        "pub trait DataProducer<T>",
        "pub trait EndpointHandler<HandlerState, T, R, E>",
        "pub fn make_custom_endpoint_consumer<HandlerState, T, R, E, H, P>",
    ),
    RUST / "src/datasource/grpc/mod.rs": (
        "pub trait EndpointHandler<HandlerState, ReqT, ResR, T, R, E>",
        "async fn begin_request(",
        "async fn consume_message(",
        "fn get_message_id(",
        "async fn eof(",
        "async fn end_request(",
    ),
    PYTHON / "src/pyservicelib_gorundebug/datasource/http/aiohttpds.py": (
        "class EndpointHandler[HandlerState, T, R, E](Protocol)",
        "async def begin_request(",
        "async def consume_message(",
        "def get_message_id(",
        "async def end_request(",
    ),
    PYTHON / "src/pyservicelib_gorundebug/datasource/kafka/aiokafkads.py": (
        "class EndpointHandler[HandlerState, T, R, E](Protocol)",
        "def concurrency(",
        "async def begin_request(",
        "async def consume_message(",
        "def get_message_id(",
        "async def end_request(",
    ),
    PYTHON / "src/pyservicelib_gorundebug/datasource/localsource/custom.py": (
        "class DataProducer[T](Protocol)",
        "class EndpointHandler[HandlerState, T, R, E](Protocol)",
        "class TypedCustomEndpointConsumer[HandlerState, T, R, E]",
    ),
    PYTHON / "src/pyservicelib_gorundebug/datasource/grpc/grpcds.py": (
        "class EndpointHandler[HandlerState, ReqT, ResR, T, R, E](Protocol)",
        "async def begin_request(",
        "async def consume_message(",
        "def get_message_id(",
        "def eof(",
        "async def end_request(",
    ),
    PYTHON / "tests/test_transportmetrics.py": (
        "async def test_http_client_metrics_match_dashboard_contract",
        "TestServer(web.Application())",
        "Requester(session).new_request",
        '"http_client_request_duration_seconds"',
    ),
    RUST / "tests/http_sink.rs": (
        "ReqwestClient::default()",
        "tokio::net::TcpListener::bind",
        '"http_client_request_duration_seconds_count"',
    ),
    CANONICAL / "tests/http_endpoints_test.cpp": (
        "CorrelatesPipelineResultAndPropagatesStreamId",
        "NoResultDoesNotWaitAndRejectsInvalidMethod",
        "BeginFailureUsesHandlerResponseAndSkipsEnd",
        "CancellationIsVisibleAndPendingAgeIsObservable",
        "ExecutesUserRequestAndPropagatesStreamId",
        "MissingRequestIsReportedToEndRequest",
        '"datasink_endpoint.request_duration_seconds"',
    ),
    BOOST / "tests/http_endpoints_test.cpp": (
        "PreservesCanonicalHandlerAndCorrelationContract",
        "GracefulStopDrainsAcceptedRequestAndClosesKeepAlive",
        "ShutdownDeadlineForcesCancellationOfAcceptedRequest",
        "RoutesGetPostKeepsConnectionAndEnforcesBodyLimit",
        "RequestDeadlineCancelsAcceptedReadAndMapsTimeout",
        "ExternalCancellationInterruptsAcceptedRead",
        "PoolLimitQueuesAndHonorsAcquisitionDeadline",
        "ReusesKeepAliveConnectionWithinConfiguredPool",
        "MapsResolveConnectAndBodyLimitErrors",
        "RoundTripsSupportedPropagationOverRealTcp",
        "PreservesCanonicalRequestLifecycleAndStreamId",
        '"datasink_endpoint.request_duration_seconds"',
    ),
    BOOST / "tests/custom_endpoints_test.cpp": (
        "RunsProducerAndHandlerLifecycle",
        "BlockingHandlerDoesNotBlockReactorWorkers",
        "CorrelatesPipelineResultUsingStreamContext",
        "SupportsMultiPushAndPersistentResultCallback",
        "PreservesLifecycleAndCollectsResult",
        "SupportsMultiPushAndPropagatesErrors",
    ),
    CANONICAL / "tests/grpc_endpoints_test.cpp": (
        "SupportsAllFourMethodTypesAndCorrelation",
        "SupportsAllFourMethodTypesAndStreamIdSessions",
        "RequiresExplicitSampledTraceParent",
        "NoStreamingEndpoint",
        "BidirectionalStreamingEndpoint",
    ),
    BOOST / "tests/grpc_endpoints_test.cpp": (
        "SupportsAllFourMethodTypesAndCorrelation",
        "SupportsAllFourMethodTypesAndStreamIdSessions",
        "RequiresExplicitSampledTraceParent",
        "NoStreamingEndpoint",
        "BidirectionalStreamingEndpoint",
    ),
    BOOST / "tests/grpc_unary_test.cpp": (
        "accepted unary cancellation did not reach MessageContext",
        "unary cancellation/deadline status differs",
        "a suspended gRPC coroutine inflated worker utilization",
    ),
    BOOST / "tests/grpc_streaming_test.cpp": (
        '"server-streaming"',
        '"client-streaming"',
        '"bidirectional-streaming"',
        "pooled server-streaming payload/EOF contract differs",
        "pooled client-streaming payload/EOF contract differs",
        "pooled bidirectional payload/EOF contract differs",
        "framework/native streaming payload, EOF or status differs",
    ),
    CANONICAL / "tests/custom_kafka_endpoints_test.cpp": (
        "SendsThroughAdapterAndCollectsDeliveryResult",
        '"events:key:payload"',
        "return {partition, 17, {}}",
        '"key", "payload", "events", 2, 9',
        "CopiesRecordAndExposesUserverCommit",
        "consumer.committed.load()",
    ),
    BOOST / "tests/kafka_endpoints_test.cpp": (
        "SendsThroughAdapterAndCollectsDeliveryResult",
        '"events:key:payload"',
        "return {partition, 17, {}}",
        '"key", "payload", "events", 2, 9',
        "CopiesRecordAndExposesCommit",
        "consumer.committed.load()",
        "ProduceConsumeAndCommitAgainstBrokerProtocol",
        "MapsDeliveryFailureToErrorOutputAndEndRequest",
        "BrokerLossReturnsErrorAndConsumerRemainsStoppable",
    ),
    BOOST / "include/servicelib/datasource/kafka/librdkafka.hpp": (
        "IsTransientError",
        "RD_KAFKA_RESP_ERR__ALL_BROKERS_DOWN",
        "RD_KAFKA_RESP_ERR_NOT_COORDINATOR",
    ),
    SERVICEGEN / "internal/codegenerator/cpp/boost_generation_test.go": (
        "TestGeneratedCppBoostKafkaRuntimeRecoversInDocker",
        "TestBoostMainConfiguresGrpcRuntimeOnlyForGrpcServices",
    ),
    SERVICEGEN / "internal/codegenerator/cpp/type_test.go": (
        "runGeneratedCppBoostKafkaRuntimeDocker",
        "cppBoostKafkaRuntimeFixture",
        "kafka-runtime-errors.txt",
        "kafka-runtime-observed.txt",
        "redpandadata/redpanda:v24.2.5",
    ),
    TYPESCRIPT / "test/http-source.test.ts": (
        "preserves handler lifecycle, method gate and stream ID",
        "correlates an asynchronous pipeline result and retires once",
        "cancellation retires the request before a late result",
        "closes admission and cancels accepted work at shutdown deadline",
        "creates spans only for sampled requests",
    ),
    TYPESCRIPT / "src/datasource/http/node-http.ts": (
        "export interface EndpointHandler<HandlerState, ReqT, ResR, T, R, E>",
        "beginRequest(",
        "consumeMessage(",
        "getMessageId(",
        "endRequest(",
    ),
    TYPESCRIPT / "test/http-sink.test.ts": (
        "reuses keep-alive connections and preserves transport metadata",
        "preserves every canonical failure branch",
        "cancels accepted calls at the shutdown deadline",
        "creates spans only for sampled messages",
        "Node HTTP sink records canonical endpoint metrics",
        '"datasink_endpoint_request_duration_seconds"',
    ),
    TYPESCRIPT / "test/grpc-source.test.ts": (
        "gRPC unary source carries a pipeline result back to the client",
        "gRPC source dispatches accepted unary calls concurrently",
        "gRPC source closes admission and gracefully drains an accepted call",
        "gRPC source force-cancels an accepted call at the shutdown deadline",
    ),
    TYPESCRIPT / "test/grpc-streaming-source.test.ts": (
        "gRPC streaming sources preserve the canonical request lifecycle",
        "makeGrpcClientStreamingEndpointConsumer",
        "makeGrpcServerStreamingEndpointConsumer",
        "makeGrpcBidiStreamingEndpointConsumer",
    ),
    TYPESCRIPT / "src/datasource/grpc/grpc-js.ts": (
        "export interface EndpointHandler<HandlerState, ReqT, ResR, T, R, E>",
        "beginRequest(",
        "consumeMessage(",
        "getMessageId(",
        "eof(",
        "endRequest(",
    ),
    TYPESCRIPT / "test/grpc-sink.test.ts": (
        "gRPC unary sink sends a request and collects its response",
        "assert.equal(peers.size, 3)",
    ),
    TYPESCRIPT / "test/grpc-streaming-sink.test.ts": (
        "gRPC streaming sinks preserve stream identity and canonical lifecycle",
        "makeGrpcClientStreamingEndpointConsumer",
        "makeGrpcServerStreamingEndpointConsumer",
        "makeGrpcBidiStreamingEndpointConsumer",
        "baselineAbortListeners",
    ),
    TYPESCRIPT / "test/grpc-cross-language.test.ts": (
        "official generated Go client interoperates with all TypeScript gRPC method modes",
        "InteropService.method.unary",
        "InteropService.method.clientStreaming",
        "InteropService.method.serverStreaming",
        "InteropService.method.bidirectionalStreaming",
        'GOWORK: "off"',
    ),
    TYPESCRIPT / "test/kafka-source.test.ts": (
        "Kafka source correlates a pipeline result before marking the offset",
        "disabled Kafka source remains registered without connecting",
    ),
    TYPESCRIPT / "src/datasource/kafka/confluent.ts": (
        "export interface EndpointHandler<HandlerState, T, R, E>",
        "concurrency(",
        "beginRequest(",
        "consumeMessage(",
        "getMessageId(",
        "endRequest(",
    ),
    TYPESCRIPT / "test/custom-source.test.ts": (
        "custom source preserves the Go handler lifecycle and result correlation",
    ),
    TYPESCRIPT / "src/datasource/localsource/custom.ts": (
        "export interface DataProducer<T>",
        "export interface EndpointHandler<HandlerState, T, R, E>",
        "export function makeCustomEndpointConsumer<HandlerState, T, R, E>",
    ),
    TYPESCRIPT / "test/kafka-sink.test.ts": (
        "Kafka sink creates its topic, publishes asynchronously and drains delivery",
        "disabled Kafka sink remains present without connecting or publishing",
        "Kafka sink applies the configured handler partitioner",
    ),
    TYPESCRIPT / "test/data-connectors.test.ts": (
        "connector and endpoint identities are stable while reloadable config stays live",
        "endpoints reject ownership by the wrong connector",
        "endpoint consumers retain the canonical typed stream boundaries",
    ),
}

KAFKA_APPLICATION_WIRE = {
    "produced": {
        "topic": "events",
        "key_utf8": "key",
        "value_utf8": "payload",
        "partition": 3,
        "delivery_offset": 17,
    },
    "consumed": {
        "topic": "events",
        "key_utf8": "key",
        "value_utf8": "payload",
        "partition": 2,
        "offset": 9,
        "committed_after_handler": True,
    },
}


def verify_sources() -> dict[str, object]:
    files: dict[str, object] = {}
    errors: list[str] = []
    total = 0
    for path, markers in SOURCE_CASES.items():
        relative = str(path.relative_to(ROOT))
        if not path.is_file():
            errors.append(f"missing source: {relative}")
            continue
        source = path.read_text()
        missing = [marker for marker in markers if marker not in source]
        files[relative] = {
            "required_case_markers": len(markers),
            "missing": missing,
        }
        total += len(markers)
        errors.extend(f"{relative}: missing {marker}" for marker in missing)
    if errors:
        raise RuntimeError("transport source matrix failed:\n" + "\n".join(errors))
    return {"files": files, "required_case_markers": total}


def execute(name: str, command: list[str], cwd: Path,
            env: dict[str, str] | None = None) -> dict[str, object]:
    print(f"[transports] START {name}", file=sys.stderr, flush=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    output: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        output.append(line)
        print(line, end="", file=sys.stderr, flush=True)
    return_code = process.wait()
    combined_output = "".join(output)
    if return_code != 0:
        raise RuntimeError(f"{name} failed:\n{combined_output}")
    duration = round(time.monotonic() - started, 3)
    print(
        f"[transports] PASS  {name} ({duration:.3f}s)",
        file=sys.stderr,
        flush=True,
    )
    return {
        "name": name,
        "command": command,
        "exit_code": return_code,
        "duration_seconds": duration,
        "output_tail": combined_output[-12000:],
    }


def docker_image_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def python_image_build() -> tuple[list[str], dict[str, str]]:
    example = ROOT / "pyexample"
    return (
        [
            "docker", "compose",
            "--project-name", "servicelib-transports-conformance-python",
            "--project-directory", str(example),
            "--file", str(example / "docker-compose.yml"),
            "build", "inventoryservice",
        ],
        {**os.environ, "PYSERVICELIB_SOURCE_CONTEXT": str(PYTHON)},
    )


def previous_successful_run(name: str) -> dict[str, object]:
    if not ARTIFACT.is_file():
        raise RuntimeError(
            f"--skip-build requires prior successful {name} evidence"
        )
    try:
        summary = json.loads(ARTIFACT.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("previous transport artifact is invalid") from error
    for run in summary.get("runs", []):
        if run.get("name") == name and run.get("exit_code") == 0:
            return {**run, "reused": True}
    raise RuntimeError(
        f"--skip-build requires prior successful {name} evidence"
    )


def canonical_command(skip_build: bool) -> list[str]:
    script = (
        "./build/servicelib_grpc_endpoints_test"
        if skip_build
        else cpp_userver.configure_script() + " && "
        "cmake --build --preset docker --parallel --target "
        "servicelib_grpc_endpoints_test && "
        "./build/servicelib_grpc_endpoints_test"
    )
    command = [
        "docker", "compose", "-f", "docker-compose.cmake.yml", "run"
    ]
    if not skip_build:
        command.append("--build")
    command.extend(["--rm", "test", "/bin/bash", "-lc", script])
    return command


def canonical_kafka_command(skip_build: bool) -> list[str]:
    script = (
        "./build/servicelib_custom_kafka_endpoints_test"
        if skip_build
        else cpp_userver.configure_script() + " && "
        "cmake --build --preset docker --parallel --target "
        "servicelib_custom_kafka_endpoints_test && "
        "./build/servicelib_custom_kafka_endpoints_test"
    )
    command = [
        "docker", "compose", "-f", "docker-compose.cmake.yml", "run"
    ]
    if not skip_build:
        command.append("--build")
    command.extend(["--rm", "test", "/bin/bash", "-lc", script])
    return command


def canonical_http_command(skip_build: bool) -> list[str]:
    script = (
        "./build/servicelib_http_endpoints_test"
        if skip_build
        else cpp_userver.configure_script() + " && "
        "cmake --build --preset docker --parallel --target "
        "servicelib_http_endpoints_test && "
        "./build/servicelib_http_endpoints_test"
    )
    command = [
        "docker", "compose", "-f", "docker-compose.cmake.yml", "run"
    ]
    if not skip_build:
        command.append("--build")
    command.extend(["--rm", "test", "/bin/bash", "-lc", script])
    return command


def boost_source_cache_build_dir() -> str:
    return cpp_source_cache.build_dir(BOOST)


def boost_source_cache_cmake_args() -> str:
    return cpp_source_cache.cmake_args(BOOST)


def boost_source_cache_command() -> list[str]:
    return cpp_source_cache.prepare_command(BOOST)


def boost_generator_environment(*, prepare_source_cache: bool = True) -> dict[str, str]:
    environment = os.environ.copy()
    environment["SERVICEGEN_RUN_DOCKER_TESTS"] = "1"
    if prepare_source_cache:
        cpp_source_cache.configure_environment(environment, BOOST)
    else:
        environment["SERVICEGEN_CPPBOOST_SOURCE_CACHE_DIR"] = str(
            cpp_source_cache.source_dir(BOOST)
        )
        environment["SERVICEGEN_CPPBOOST_BUILD_VOLUME"] = (
            cpp_source_cache.build_volume_name(BOOST)
        )
    environment["GOCACHE"] = "/tmp/servicegen-go-build"
    # Conan packages are dependency caches, not suite results.  Keeping them
    # below .artifacts made every graph-profile switch delete many gigabytes
    # and forced generated transport fixtures to rebuild the same packages.
    conan_home = CONFORMANCE_DIR / ".conan2-cache"
    conan_home.mkdir(parents=True, exist_ok=True)
    environment["DEPENDENCY_CONAN_HOME"] = str(conan_home)
    go_work = ARTIFACT.parent / "go.work"
    go_work.parent.mkdir(parents=True, exist_ok=True)
    go_work.write_text(go_toolchain.render_workspace(
        go_toolchain.workspace_version(SERVICEGEN / "go.mod"),
        (SERVICEGEN, GO),
    ))
    environment["GOWORK"] = str(go_work)
    return environment


def boost_command(build_dir: str, sanitizer: bool,
                  skip_build: bool) -> list[str]:
    tests = "^cppboostservicelib_grpc_(runtime|endpoints|unary|streaming)_test$"
    run = (
        f"ctest --test-dir {build_dir} --output-on-failure -R "
        f"'{tests}'"
    )
    if sanitizer:
        run = (
            "ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 "
            "UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 " + run
        )
    if not skip_build:
        sanitizer_flags = (
            " -DCPPBOOSTSERVICELIB_ASAN=ON -DCPPBOOSTSERVICELIB_UBSAN=ON"
            if sanitizer else ""
        )
        run = (
            "cmake -U'FETCHCONTENT_SOURCE_DIR_OPENTELEMETRY-CPP' "
            f"-S . -B {build_dir} -G Ninja "
            f"-DCMAKE_BUILD_TYPE={'Debug' if sanitizer else 'Release'} "
            "-DCPPBOOSTSERVICELIB_DEPENDENCY_MODE=FETCH "
            "-DCPPBOOSTSERVICELIB_ENABLE_GRPC=ON "
            "-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON "
            f"{boost_source_cache_cmake_args()}"
            f"{sanitizer_flags} && "
            f"cmake --build {build_dir} --parallel --target "
            "cppboostservicelib_grpc_runtime_test "
            "cppboostservicelib_grpc_endpoints_test "
            "cppboostservicelib_grpc_unary_test "
            "cppboostservicelib_grpc_streaming_test && " + run
        )
    return [
        "docker", "run", "--rm", "-v",
        cpp_source_cache.source_mount(BOOST),
        "-v", f"{BOOST}:/workspace",
        *cpp_source_cache.build_volume_mount_args(
            BOOST, "cppboostservicelib-transports"
        ), "-w",
        "/workspace", "cppboostservicelib-build:latest", "/bin/bash", "-lc",
        run,
    ]


def boost_kafka_command(build_dir: str, sanitizer: bool,
                        skip_build: bool) -> list[str]:
    run = (
        f"ctest --test-dir {build_dir} --output-on-failure -R "
        "'^cppboostservicelib_kafka_endpoints_test$'"
    )
    if sanitizer:
        run = (
            "ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 "
            "UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 " + run
        )
    if not skip_build:
        sanitizer_flags = (
            " -DCPPBOOSTSERVICELIB_ASAN=ON -DCPPBOOSTSERVICELIB_UBSAN=ON"
            if sanitizer else ""
        )
        run = (
            f"cmake -S . -B {build_dir} -G Ninja "
            f"-DCMAKE_BUILD_TYPE={'Debug' if sanitizer else 'Release'} "
            "-DCPPBOOSTSERVICELIB_DEPENDENCY_MODE=FETCH "
            "-DCPPBOOSTSERVICELIB_ENABLE_KAFKA=ON "
            "-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON "
            f"{boost_source_cache_cmake_args()}"
            f"{sanitizer_flags} && "
            f"cmake --build {build_dir} --parallel --target "
            "cppboostservicelib_kafka_endpoints_test && " + run
        )
    return [
        "docker", "run", "--rm", "-v",
        cpp_source_cache.source_mount(BOOST),
        "-v", f"{BOOST}:/workspace",
        *cpp_source_cache.build_volume_mount_args(
            BOOST, "cppboostservicelib-transports"
        ), "-w",
        "/workspace", "cppboostservicelib-build:latest", "/bin/bash", "-lc",
        run,
    ]


def boost_http_custom_command(skip_build: bool) -> list[str]:
    build_dir = "build/grpc-conformance-release"
    tests = (
        "^(cppboostservicelib_http_endpoints_test|"
        "cppboostservicelib_custom_endpoints_test)$"
    )
    run = (
        f"ctest --test-dir {build_dir} --output-on-failure -R '{tests}'"
    )
    if not skip_build:
        run = (
            f"cmake --build {build_dir} --parallel --target "
            "cppboostservicelib_http_endpoints_test "
            "cppboostservicelib_custom_endpoints_test && " + run
        )
    return [
        "docker", "run", "--rm", "-v",
        cpp_source_cache.source_mount(BOOST),
        "-v", f"{BOOST}:/workspace",
        *cpp_source_cache.build_volume_mount_args(
            BOOST, "cppboostservicelib-transports"
        ), "-w",
        "/workspace", "cppboostservicelib-build:latest", "/bin/bash", "-lc",
        run,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    source_matrix = verify_sources()
    runs: list[dict[str, object]] = []
    if not docker_image_exists("example-python:latest"):
        command, environment = python_image_build()
        runs.append(execute(
            "python-runtime-image", command, ROOT / "pyexample", environment,
        ))
    if not docker_image_exists("rustservicelib-toolchain:latest"):
        runs.append(execute(
            "rust-toolchain-image",
            [
                "docker", "build", "--target", "toolchain", "--tag",
                "rustservicelib-toolchain:latest", ".",
            ],
            RUST,
        ))
    runs.append(execute(
        "go-http-client-metrics-real-transport",
        [
            "docker", "run", "--rm",
            "--volume", f"{GO}:/workspace:ro",
            "--volume", "servicelib-conformance-go-mod-cache:/go/pkg/mod",
            "--volume", (
                "servicelib-conformance-go-build-cache:"
                "/root/.cache/go-build"
            ),
            "--workdir", "/workspace", go_toolchain.docker_image(ROOT),
            "go", "test", "./runtime/telemetry/opentelemetry", "-run",
            "^TestOTelMetricNames/HTTPClient$", "-count=1",
        ],
        GO,
    ))
    runs.append(execute(
        "python-http-client-metrics-real-transport",
        [
            "docker", "run", "--rm",
            "--volume", f"{PYTHON}:/workspace/.pyservicelib:ro",
            "--workdir", "/workspace/.pyservicelib",
            "--env", "PYTHONPATH=/workspace/.pyservicelib/src",
            "example-python:latest", "/workspace/.venv/bin/python", "-m",
            "pytest", "-q", "-p", "no:cacheprovider",
            "tests/test_transportmetrics.py",
        ],
        PYTHON,
    ))
    runs.append(execute(
        "rust-http-client-metrics-real-transport",
        [
            "docker", "run", "--rm",
            # rustservicelib is a library and intentionally does not track
            # Cargo.lock. Cargo must be able to create its ignored lockfile
            # while the compiled target remains isolated in the cache volume.
            "--volume", f"{RUST}:/workspace",
            "--volume", (
                "servicelib-conformance-rust-cargo-registry:"
                "/usr/local/cargo/registry"
            ),
            "--volume", (
                "servicelib-conformance-rust-transports-target:"
                "/cargo-target"
            ),
            "--env", "CARGO_TARGET_DIR=/cargo-target",
            "--workdir", "/workspace", "rustservicelib-toolchain:latest",
            "cargo", "test", "--test", "http_sink",
        ],
        RUST,
    ))
    if not args.skip_build:
        runs.append(execute(
            "typescript-transports-dependencies",
            typescript_toolchain.install_command(),
            TYPESCRIPT,
            typescript_toolchain.environment(),
        ))
        runs.append(execute(
            "typescript-transports-build",
            typescript_toolchain.tsc_command(
                "tsconfig.test.json", force=True
            ),
            TYPESCRIPT,
        ))
        runs.append(execute(
            "typescript-transports-runtime-assets",
            typescript_toolchain.copy_runtime_assets_command(),
            TYPESCRIPT,
        ))
    runs.append(execute(
        "typescript-http-grpc-kafka-endpoint-semantics",
        [
            "node", "--test", "--enable-source-maps",
            "dist-test/test/data-connectors.test.js",
            "dist-test/test/http-source.test.js",
            "dist-test/test/http-sink.test.js",
            "dist-test/test/grpc-source.test.js",
            "dist-test/test/grpc-sink.test.js",
            "dist-test/test/grpc-streaming-source.test.js",
            "dist-test/test/grpc-streaming-sink.test.js",
            "dist-test/test/kafka-source.test.js",
            "dist-test/test/kafka-sink.test.js",
        ],
        TYPESCRIPT,
    ))
    interop_env = os.environ.copy()
    interop_env["SERVICEGEN_RUN_CROSS_LANGUAGE_GRPC"] = "1"
    runs.append(execute(
        "typescript-grpc-official-go-client-interoperability",
        [
            "node", "--test", "--enable-source-maps",
            "dist-test/test/grpc-cross-language.test.js",
        ],
        TYPESCRIPT,
        interop_env,
    ))
    runs.append(execute(
        "canonical-cpp-http-lifecycle",
        canonical_http_command(args.skip_build), CANONICAL,
    ))
    runs.append(execute(
        "canonical-cpp-grpc-endpoint-semantics",
        canonical_command(args.skip_build), CANONICAL,
    ))
    runs.append(execute(
        "canonical-cpp-kafka-application-wire",
        canonical_kafka_command(args.skip_build), CANONICAL,
    ))
    if not args.skip_build:
        runs.append(execute(
            "boost-build-image",
            ["docker", "build", "-f", "Dockerfile.cmake", "-t",
             "cppboostservicelib-build", "."],
            BOOST,
        ))
        runs.append(execute(
            "boost-source-cache",
            boost_source_cache_command(),
            BOOST,
        ))
    runs.append(execute(
        "boost-grpc-release",
        boost_command("build/grpc-conformance-release", False, args.skip_build),
        BOOST,
    ))
    runs.append(execute(
        "boost-http-and-custom-lifecycle",
        boost_http_custom_command(args.skip_build), BOOST,
    ))
    runs.append(execute(
        "boost-grpc-asan-ubsan",
        boost_command("build/grpc-conformance-asan", True, args.skip_build),
        BOOST,
    ))
    runs.append(execute(
        "boost-kafka-application-and-broker-wire",
        boost_kafka_command(
            "build/kafka-conformance-release",
            False,
            args.skip_build,
        ),
        BOOST,
    ))
    runs.append(execute(
        "boost-kafka-asan-ubsan",
        boost_kafka_command(
            "build/kafka-asan" if args.skip_build
            else "build/kafka-conformance-asan",
            True,
            args.skip_build,
        ),
        BOOST,
    ))

    generator_env = boost_generator_environment()
    generated_run_name = "generated-four-method-workspace"
    if args.skip_build:
        runs.append(previous_successful_run(generated_run_name))
    else:
        runs.append(execute(
            generated_run_name,
            ["go", "test", "-timeout", "20m", "./internal/codegenerator/cpp",
             "-run", "^TestGeneratedCppBoostGrpcStreamingSourcesBuildsInDocker$",
             "-count=1", "-v"],
            SERVICEGEN, generator_env,
        ))

    generated_kafka_run_name = "generated-kafka-broker-recovery"
    if args.skip_build:
        runs.append(previous_successful_run(generated_kafka_run_name))
    else:
        runs.append(execute(
            generated_kafka_run_name,
            ["go", "test", "-timeout", "20m",
             "./internal/codegenerator/cpp", "-run",
             "^TestGeneratedCppBoostKafkaRuntimeRecoversInDocker$",
             "-count=1", "-v"],
            SERVICEGEN, generator_env,
        ))

    summary = {
        "status": "pass",
        "languages": [
            "go", "canonical-cpp", "cppboost", "python", "rust",
            "typescript",
        ],
        "source_matrix": source_matrix,
        "runs": runs,
        "profiles": ["Release", "ASan+UBSan"],
        "grpc_method_types": [
            "unary", "client-streaming", "server-streaming",
            "bidirectional-streaming",
        ],
        "typescript_transport_coverage": {
            "http": [
                "source", "sink", "keep-alive", "deadline",
                "cancellation", "shutdown", "correlation", "tracing",
            ],
            "grpc": [
                "unary-source", "unary-sink",
                "client-streaming-source", "client-streaming-sink",
                "server-streaming-source", "server-streaming-sink",
                "bidirectional-streaming-source",
                "bidirectional-streaming-sink", "connection-count",
                "round-robin", "concurrent-dispatch", "deadline",
                "cancellation", "shutdown", "listener-cleanup",
                "official-generated-go-client", "payload", "metadata",
                "status", "recovery",
            ],
            "kafka": [
                "source", "sink", "topic-create", "delivery", "commit",
                "disabled-endpoint", "partitioner", "correlation",
            ],
        },
        "http_client_live_metric_evidence": {
            "go": "go-http-client-metrics-real-transport",
            "cpp": "canonical-cpp-http-lifecycle",
            "cppboost": "boost-http-and-custom-lifecycle",
            "python": "python-http-client-metrics-real-transport",
            "rust": "rust-http-client-metrics-real-transport",
            "typescript": "typescript-http-grpc-kafka-endpoint-semantics",
        },
        "accepted_call_cancellation": True,
        "framework_native_streaming_transcript": {
            "client": "protobuf bytes + EOF + status",
            "server": "ordered protobuf bytes + EOF + status",
            "bidirectional": "ordered protobuf bytes + EOF + status",
            "native_client": "standard generated synchronous gRPC stub",
            "blocking_isolation": "dedicated std::async thread",
        },
        "endpoint_lifecycle_matrix": {
            "http": [
                "success", "error", "deadline", "external-cancellation",
                "disconnect/shutdown", "correlation", "keep-alive",
                "connection-pool", "body-limit", "context-propagation",
            ],
            "grpc": [
                "unary", "client-streaming", "server-streaming",
                "bidirectional-streaming", "deadline", "cancellation",
                "shutdown", "correlation",
            ],
            "kafka": [
                "produce", "delivery", "consume", "commit",
                "delivery-error", "broker-loss", "recovery",
                "stop-cancellation", "correlation",
            ],
            "local_custom": [
                "source", "sink", "multi-push", "persistent-result",
                "error-propagation", "stop-cancellation",
            ],
        },
        "kafka_application_wire": KAFKA_APPLICATION_WIRE,
        "kafka_comparison": (
            "field-for-field plus Boost librdkafka broker round-trip"
        ),
        "kafka_broker_loss": {
            "producer_error": True,
            "transient_consumer_poll_survives": True,
            "consumer_stoppable": True,
            "producer_recovery": True,
            "fresh_group_consumer_commit_after_recovery": True,
            "sanitizers": "ASan+UBSan+leak",
        },
        "generated_kafka_broker_recovery": {
            "broker": "redpandadata/redpanda:v24.2.5",
            "generated_service_graph": True,
            "delivery_error_during_outage": True,
            "service_restart": False,
            "same_consumer_group": True,
            "consume_commit_after_broker_restart": True,
            "clean_shutdown": True,
        },
        "kafka_protocol_note": (
            "Kafka request envelopes are owned by librdkafka/userver and are "
            "not a deterministic ServiceLib byte contract; ServiceLib-owned "
            "topic/key/value/partition/offset/commit fields are compared."
        ),
        "unrestricted_build_parallelism": True,
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"Transport conformance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
