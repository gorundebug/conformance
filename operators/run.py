#!/usr/bin/env python3
"""Differential core stream-operator and topology conformance gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE_DIR))
import cpp_source_cache
import typescript_toolchain

ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "operators" / "summary.json"
GO = ROOT / "servicelib"
CANONICAL = ROOT / "cppservicelib"
BOOST = ROOT / "cppboostservicelib"
TYPESCRIPT = ROOT / "tsservicelib"


FUNCTION_CONTRACTS = {
    ROOT / "servicelib/operators/functions.go": {
        "required": (
            "Filter(context.Context, runtime.Stream, T) bool",
            "FlatMap(context.Context, runtime.Stream, T, runtime.Collect[R])",
            "Join(context.Context, runtime.Stream, K, []T1, []T2, runtime.Collect[R]) bool",
            "KeyBy(context.Context, runtime.Stream, T, runtime.Collect[datastruct.KeyValue[K, V]])",
            "Map(context.Context, runtime.Stream, T, runtime.Collect[R])",
            "MultiJoin(context.Context, runtime.Stream, K, [][]interface{}, runtime.Collect[R]) bool",
            "Process(context.Context, runtime.Stream, T, runtime.Collect[R], runtime.Collect[E])",
            "Duration(context.Context, runtime.Stream, T) time.Duration",
        ),
        "forbidden": ("...runtime.Collect",),
    },
    ROOT / "pyservicelib/src/pyservicelib_gorundebug/operators/functions.py": {
        "required": (
            "async def map(self, stream: Stream, value: T, out: Collect[R])",
            "async def filter(self, stream: Stream, value: T) -> bool",
            "async def flatmap(self, stream: Stream, value: T, out: Collect[R])",
            "async def join(self, stream: Stream, key: K, left_values: list[T1], right_values: list[T2], out: Collect[R])",
            "async def multi_join(self, stream: Stream, key: K, values: list[list[Any]], out: Collect[R])",
            "async def key_by(self, stream: Stream, value: T, out: Collect[KeyValue[K, V]])",
            "async def process(self, stream: Stream, value: T, out: Collect[R], err_out: Collect[E])",
            "async def duration(self, stream: Stream, value: T) -> timedelta",
        ),
        "forbidden": ("*outputs", "*collectors"),
    },
    ROOT / "rustservicelib/src/operators/map.rs": {
        "required": ("stream: &dyn RuntimeStream", "value: &T", "out: &Collector<R>"),
        "forbidden": ("outputs: &[", "collectors: &[", "value: Payload<T>", "out: &Stream<R>"),
    },
    ROOT / "rustservicelib/src/operators/filter.rs": {
        "required": ("stream: &dyn RuntimeStream", "value: &T"),
        "forbidden": ("outputs: &[", "collectors: &[", "value: Payload<T>"),
    },
    ROOT / "rustservicelib/src/operators/flatmap.rs": {
        "required": ("stream: &dyn RuntimeStream", "value: &T", "out: &Collector<R>"),
        "forbidden": ("outputs: &[", "collectors: &[", "value: Payload<T>", "out: &Stream<R>"),
    },
    ROOT / "rustservicelib/src/operators/keyby.rs": {
        "required": ("stream: &dyn RuntimeStream", "value: &T", "out: &Collector<KeyValue<K, V>>"),
        "forbidden": ("outputs: &[", "collectors: &[", "value: Payload<T>", "out: &Stream<KeyValue<K, V>>"),
    },
    ROOT / "rustservicelib/src/operators/process.rs": {
        "required": ("stream: &dyn RuntimeStream", "value: &T", "out: &Collector<R>", "error: &Collector<E>"),
        "forbidden": ("outputs: &[", "collectors: &[", "value: Payload<T>", "out: &Stream<R>", "error: &Stream<E>"),
    },
    ROOT / "rustservicelib/src/operators/delay.rs": {
        "required": ("stream: &dyn RuntimeStream", "value: &T", "_out: &Collector<T>"),
        "forbidden": ("outputs: &[", "collectors: &[", "value: Payload<T>", "_out: &Stream<T>"),
    },
    ROOT / "rustservicelib/src/operators/join.rs": {
        "required": ("stream: &dyn RuntimeStream", "left: Vec<L>", "right: Vec<R>", "out: &Collector<O>"),
        "forbidden": ("outputs: &[", "collectors: &[", "Vec<Payload<L>>", "Vec<Payload<R>>", "out: &Stream<O>"),
    },
    ROOT / "rustservicelib/src/operators/multijoin.rs": {
        "required": ("stream: &dyn RuntimeStream", "out: &Collector<O>"),
        "forbidden": ("outputs: &[", "collectors: &[", "Vec<Payload<", "out: &Stream<O>"),
    },
    TYPESCRIPT / "src/operators/functions.ts": {
        "required": (
            "filter(context: MessageContext, stream: Stream, value: Readonly<T>)",
            "flatMap(context: MessageContext, stream: Stream, value: Readonly<T>, out: Collector<R>)",
            "map(context: MessageContext, stream: Stream, value: Readonly<T>, out: Collector<R>)",
            "out: Collector<KeyValue<K, V>>",
            "left: readonly Readonly<L>[]",
            "right: readonly Readonly<R>[]",
            "values: readonly [readonly Readonly<T>[], ...(readonly (readonly unknown[])[])]",
            "errorOut: Collector<E>",
        ),
        "forbidden": ("...outputs", "...collectors", "Payload<T>", "out?: Collector"),
    },
    ROOT / "goexample/orderservice/internal/functions/processorderitems.go": {
        "required": (
            "FlatMap(ctx context.Context, _ runtime.Stream, value *types2.Order, out runtime.Collect[*types.OrderItem])",
            "(*ProcessOrderItems, error)",
        ),
        "forbidden": ("...runtime.Collect",),
    },
    ROOT / "cppexample/orderservice/internal/functions/process_order_items.hpp": {
        "required": (
            "template <typename Output>", "servicelib::StreamBase& stream",
            "Output&& out", "std::unique_ptr<ProcessOrderItems> MakeProcessOrderItems",
        ),
        "forbidden": ("typename... Outputs", "std::get<0>(collectors)"),
    },
    ROOT / "cppboostexample/orderservice/internal/functions/process_order_items.hpp": {
        "required": (
            "template <typename Output>", "servicelib::StreamBase& stream",
            "Output&& out", "std::unique_ptr<ProcessOrderItems> MakeProcessOrderItems",
        ),
        "forbidden": ("typename... Outputs", "std::get<0>(collectors)"),
    },
    ROOT / "pyexample/orderservice/src/order_service/internal/functions/process_order_items.py": {
        "required": ("stream: Stream", "value: Order", "out: Collect[OrderItem]"),
        "forbidden": ("*outputs", "*collectors"),
    },
    ROOT / "rustexample/orderservice/src/internal/functions/process_order_items.rs": {
        "required": (
            "_stream: &dyn RuntimeStream", "value: &Order",
            "out: &Collector<OrderItem>",
        ),
        "forbidden": ("outputs: &[", "collectors: &[", "Payload<Order>", "&Stream<OrderItem>"),
    },
    ROOT / "tsexample/orderservice/src/internal/functions/process-order-items.ts": {
        "required": (
            "_stream: Stream",
            "value: Readonly<Order>",
            "out: Collector<OrderItem>",
            "export function makeProcessOrderItems(",
            "): ProcessOrderItems",
        ),
        "forbidden": ("...outputs", "...collectors", "Payload<Order>", "stream?: Stream"),
    },
    ROOT / "servicegen/internal/codegenerator/templates/cpp/functions/function_hpp.tmpl": {
        "required": (
            "servicelib::StreamBase& stream", "template <typename Output>",
            "inline std::unique_ptr<{{.Name}}> Make{{.Name}}",
        ),
        "forbidden": ("typename... Outputs", "std::get<0>"),
    },
    ROOT / "servicegen/internal/codegenerator/templates/cppboost/functions/function_hpp.tmpl": {
        "required": (
            "servicelib::StreamBase& stream", "template <typename Output>",
            "inline std::unique_ptr<{{.Name}}> Make{{.Name}}",
        ),
        "forbidden": ("typename... Outputs", "std::get<0>"),
    },
}


SOURCE_CASES = {
    GO / "runtime/caller_test.go": (
        "TestFunctionCallAsyncFlagOnlyChangesCallerMetadata",
    ),
    GO / "tests/operators/operators_test.go": (
        "TestMap",
        "TestFilter_Pass",
        "TestFilter_Block",
        "TestFlatMap",
        "TestFlatMapIterable",
        "TestProcess_Success",
        "TestProcess_Error",
        "TestSplit",
        "TestMerge",
        "TestCase_A",
        "TestCase_B",
        "TestDelay",
        "TestDelay_ZeroDuration",
        "TestKeyBy",
        "TestJoin",
        "TestJoinLeft_LeftOnly",
        "TestJoinLeft_RightOnly",
        "TestJoinLeft_BothSides",
        "TestJoinRight_RightOnly",
        "TestJoinRight_LeftOnly",
        "TestJoinRight_BothSides",
        "TestJoinOuter_LeftOnly",
        "TestJoinOuter_RightOnly",
        "TestJoinOuter_BothSides",
        "TestMultiJoin_PartialNoEmit",
        "TestMultiJoin_Slot0Last",
    ),
    CANONICAL / "tests/operators_compile_test.cpp": (
        "PublicApiHeadersCompileTogether",
        "SplitBranchesInheritRuntimeEnvironment",
        "RegisteredInputFeedsConfiguredTerminalSink",
        "ConfiguredInputOwnsEndpointResultAndErrorChannels",
        "ProcessExposesGoStyleErrorOutput",
        "SinkResultReentersTheStreamGraph",
        "DelayUsesRuntimeSchedulerAndPreservesMessageContext",
        "GraphCanReferenceOneMoveOnlyFunctionWithoutCopyingIt",
        "CallerSemanticsDispatchPreserveContextPriorityAndStatistics",
    ),
    CANONICAL / "tests/serviceapp_test.cpp": (
        "DataConnectorTimeoutMatchesGoTelemetry",
    ),
    CANONICAL / "tests/status_test.cpp": (
        "BuildsLiveTopologyDataAndGraphYaml",
        "EmbedsTheSameBrowserAssetsAsOtherRuntimes",
    ),
    BOOST / "tests/operators_compile_test.cpp": (
        "PublicApiHeadersCompileTogether",
        "SplitBranchesInheritRuntimeEnvironment",
        "RegisteredInputFeedsConfiguredTerminalSink",
        "ConfiguredInputOwnsEndpointResultAndErrorChannels",
        "ProcessExposesGoStyleErrorOutput",
        "SinkResultReentersTheStreamGraph",
        "DelayUsesRuntimeSchedulerAndPreservesMessageContext",
        "MapCanEmitZeroOneOrManyValues",
        "FilterPassesOnlyMatchingValues",
        "FlatMapEmitsEveryProducedValue",
        "FlatMapIterablePreservesElementOrder",
        "KeyByEmitsCanonicalKeyValue",
        "GraphCanReferenceOneMoveOnlyFunctionWithoutCopyingIt",
        "MergeForwardsEveryParentIntoOneOrderedOutput",
        "CallerSemanticsDispatchPreserveContextPriorityAndStatistics",
    ),
    BOOST / "tests/operators_topology_test.cpp": (
        "SplitBroadcastsAndCaseRoutesExactlyOneBranch",
        "makeCycleLinkStream",
        "setSource",
        "ResultLinkDoesNotRetainReleasedInputGraph",
    ),
    BOOST / "tests/serviceapp_test.cpp": (
        "connectorTimeoutMatchesTelemetry",
    ),
    BOOST / "tests/status_test.cpp": (
        "BuildsLiveTopologyDataAndGraphYaml",
        "EmbedsTheSameBrowserAssetsAsOtherRuntimes",
        "BeastRoutesMatchCanonicalStatusAndMetricsHandlers",
    ),
    BOOST / "tests/join_topology_test.cpp": (
        "InnerJoinUsesRegisteredStorageLifecycle",
        "multiResults",
        "leftResults",
        "rightResults",
        "outerResults",
    ),
    TYPESCRIPT / "test/operators-basic.test.ts": (
        "map filter and flat-map preserve collector and stream function contracts",
        "key-by emits the canonical key/value shape",
        "process has independent result and virtual negative-id error outputs",
    ),
    TYPESCRIPT / "test/flat-map-iterable.test.ts": (
        "FlatMapIterable emits indexed array items in order without copying",
        "FlatMapIterable string int32 mode emits Unicode code points",
        "FlatMapIterable string uint8 mode emits UTF-8 bytes",
    ),
    TYPESCRIPT / "test/join.test.ts": (
        "inner join waits for both indexed sides and retires when function returns true",
        "left, right and outer join gates match canonical side availability",
        "join retains accumulated values while function returns false",
    ),
    TYPESCRIPT / "test/multi-join.test.ts": (
        "multi-join preserves root slot zero and right-link insertion order",
        "failed right binding does not consume a multi-join slot",
    ),
    TYPESCRIPT / "test/case.test.ts": (
        "case builds one selector and routes the exact value to its typed when branch",
    ),
    TYPESCRIPT / "test/delay.test.ts": (
        "positive delay schedules and later emits without blocking consume",
        "positive delay skips downstream when cancellation wins",
    ),
    TYPESCRIPT / "test/split.test.ts": (
        "split validates branches once and dispatches async branches first stably",
    ),
    TYPESCRIPT / "test/merge.test.ts": (
        "merge forwards every source through one identity without copying",
    ),
    TYPESCRIPT / "test/link.test.ts": (
        "cycle link binds its source late and forwards the exact message",
    ),
    TYPESCRIPT / "test/topology.test.ts": (
        "runtime topology reflects late cycle binding and validates config edges",
    ),
}


def verify_sources() -> dict[str, object]:
    result: dict[str, object] = {}
    errors: list[str] = []
    total = 0
    for path, markers in SOURCE_CASES.items():
        relative = str(path.relative_to(ROOT))
        if not path.is_file():
            errors.append(f"missing source: {relative}")
            continue
        source = path.read_text()
        missing = [marker for marker in markers if marker not in source]
        result[relative] = {
            "required_case_markers": len(markers),
            "missing": missing,
        }
        total += len(markers)
        errors.extend(f"{relative}: missing {marker}" for marker in missing)
    if errors:
        raise RuntimeError("operator source matrix failed:\n" + "\n".join(errors))
    return {"files": result, "required_case_markers": total}


def verify_function_contracts() -> dict[str, object]:
    result: dict[str, object] = {}
    errors: list[str] = []
    for path, contract in FUNCTION_CONTRACTS.items():
        relative = str(path.relative_to(ROOT))
        if not path.is_file():
            errors.append(f"missing function contract source: {relative}")
            continue
        source = path.read_text(encoding="utf-8")
        compact_source = re.sub(r"\s+", "", source)
        missing = [
            token
            for token in contract["required"]
            if re.sub(r"\s+", "", token) not in compact_source
        ]
        forbidden = [
            token
            for token in contract["forbidden"]
            if re.sub(r"\s+", "", token) in compact_source
        ]
        result[relative] = {"missing": missing, "forbidden_present": forbidden}
        errors.extend(f"{relative}: missing {token}" for token in missing)
        errors.extend(f"{relative}: forbidden {token}" for token in forbidden)

    cpp_function_files = tuple(
        path
        for example in (ROOT / "cppexample", ROOT / "cppboostexample")
        for path in example.glob("*service/internal/functions/*.hpp")
    )
    for path in cpp_function_files:
        relative = str(path.relative_to(ROOT))
        source = path.read_text(encoding="utf-8")
        forbidden = [
            token for token in ("typename... Outputs", "std::get<0>(", "Outputs&&... outputs")
            if token in source
        ]
        missing = []
        if " Make" in source and "std::unique_ptr<" not in source:
            missing.append("maker returning std::unique_ptr")
        result[relative] = {"missing": missing, "forbidden_present": forbidden}
        errors.extend(f"{relative}: missing {token}" for token in missing)
        errors.extend(f"{relative}: forbidden {token}" for token in forbidden)

    typescript_operator_files = tuple((TYPESCRIPT / "src/operators").glob("*.ts"))
    for path in typescript_operator_files:
        source = path.read_text(encoding="utf-8")
        if "export function make" not in source:
            continue
        relative = str(path.relative_to(ROOT))
        maker_signatures = "\n".join(
            match.group(0)
            for match in re.finditer(r"export function make[^;{]+[;{]", source, re.DOTALL)
        )
        forbidden = [
            token
            for token in (
                "outputType:",
                "outputSerde:",
                "resultSerde:",
                "keySerde:",
                "valueSerde:",
                "SerdeType<",
                "StreamSerde<",
            )
            if token in maker_signatures
        ]
        result[relative] = {
            "maker_uses_graph_metadata": not forbidden,
            "forbidden_present": forbidden,
        }
        errors.extend(f"{relative}: forbidden maker metadata {token}" for token in forbidden)
    if errors:
        raise RuntimeError("function contract matrix failed:\n" + "\n".join(errors))
    return {"files": result, "status": "pass"}


def execute(
    name: str, command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> dict[str, object]:
    print(f"[operators] START {name}", file=sys.stderr, flush=True)
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
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        lines.append(line)
        print(line, end="", file=sys.stderr, flush=True)
    return_code = process.wait()
    output = "".join(lines)
    duration = round(time.monotonic() - started, 3)
    if return_code != 0:
        raise RuntimeError(f"{name} failed:\n{output}")
    print(f"[operators] PASS  {name} ({duration:.1f}s)", file=sys.stderr, flush=True)
    return {
        "name": name,
        "command": command,
        "exit_code": return_code,
        "duration_seconds": duration,
        "output_tail": output[-12000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    source_matrix = verify_sources()
    function_contracts = verify_function_contracts()
    runs: list[dict[str, object]] = []
    go_env = os.environ.copy()
    go_env["GOCACHE"] = "/tmp/servicelib-go-build"
    go_env["GOWORK"] = "off"
    runs.append(
        execute(
            "go-core-operators",
            ["go", "test", "./tests/operators", "-count=1", "-v"],
            GO,
            go_env,
        )
    )
    runs.append(
        execute(
            "go-caller-semantics",
            [
                "go", "test", "./runtime", "-run",
                "^TestFunctionCallAsyncFlagOnlyChangesCallerMetadata$",
                "-count=1", "-v",
            ],
            GO,
            go_env,
        )
    )
    runs.append(
        execute(
            "typescript-operators-dependencies",
            typescript_toolchain.install_command(),
            TYPESCRIPT,
            typescript_toolchain.environment(),
        )
    )
    runs.append(
        execute(
            "typescript-operators",
            [
                "/bin/bash",
                "-lc",
                "node node_modules/typescript/bin/tsc "
                "--build tsconfig.test.json && "
                "node scripts/copy-status-assets.mjs dist && "
                "node --test --enable-source-maps "
                "dist-test/test/operators-basic.test.js "
                "dist-test/test/flat-map-iterable.test.js "
                "dist-test/test/join.test.js "
                "dist-test/test/multi-join.test.js "
                "dist-test/test/case.test.js "
                "dist-test/test/delay.test.js "
                "dist-test/test/split.test.js "
                "dist-test/test/merge.test.js "
                "dist-test/test/link.test.js "
                "dist-test/test/topology.test.js",
            ],
            TYPESCRIPT,
        )
    )

    canonical_script = (
        "./build/servicelib_operators_test"
        if args.skip_build
        else "cmake --preset docker && "
        "cmake --build --preset docker --parallel --target "
        "servicelib_operators_test && ./build/servicelib_operators_test"
    )
    canonical_command = [
        "docker",
        "compose",
        "-f",
        "docker-compose.cmake.yml",
        "run",
    ]
    if not args.skip_build:
        canonical_command.append("--build")
    canonical_command.extend(
        ["--rm", "test", "/bin/bash", "-lc", canonical_script]
    )
    runs.append(execute("canonical-cpp-operators", canonical_command, CANONICAL))
    canonical_runtime_script = (
        "./build/servicelib_serviceapp_test && "
        "./build/servicelib_status_test"
    )
    runs.append(
        execute(
            "canonical-cpp-link-connector-status",
            [
                "docker", "compose", "-f", "docker-compose.cmake.yml",
                "run", "--rm", "test", "/bin/bash", "-lc",
                canonical_runtime_script,
            ],
            CANONICAL,
        )
    )

    if not args.skip_build:
        boost_build_volume = cpp_source_cache.build_volume_name(
            BOOST, "cppboostservicelib-operators"
        )
        boost_env = os.environ.copy()
        boost_env["CPPBOOSTSERVICELIB_TEST_SOURCE_CACHE_DIR"] = str(
            cpp_source_cache.ensure(BOOST)
        )
        boost_env["CPPBOOSTSERVICELIB_TEST_BUILD_VOLUME"] = boost_build_volume
        runs.append(
            execute(
                "boost-framework-build-and-tests",
                ["./scripts/test.sh"],
                BOOST,
                boost_env,
            )
        )
    runs.append(
        execute(
            "boost-cpp-operators",
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{BOOST}:/workspace",
                "-v",
                f"{cpp_source_cache.build_volume_name(BOOST, 'cppboostservicelib-operators')}:/workspace/build",
                "-w",
                "/workspace",
                "cppboostservicelib-build:latest",
                "ctest",
                "--test-dir",
                "build/docker",
                "--output-on-failure",
                "-R",
                "cppboostservicelib_(operators|operators_topology|join_topology)_test",
            ],
            BOOST,
        )
    )
    runs.append(
        execute(
            "boost-cpp-link-connector-status",
            [
                "docker", "run", "--rm", "-v", f"{BOOST}:/workspace",
                "-v",
                f"{cpp_source_cache.build_volume_name(BOOST, 'cppboostservicelib-operators')}:/workspace/build",
                "-w", "/workspace", "cppboostservicelib-build:latest",
                "ctest", "--test-dir", "build/docker", "--output-on-failure",
                "-R",
                "cppboostservicelib_(serviceapp|status)_test",
            ],
            BOOST,
        )
    )

    summary = {
        "status": "pass",
        "languages": ["go", "canonical-cpp", "cppboost", "typescript"],
        "source_matrix": source_matrix,
        "function_contracts": function_contracts,
        "runs": runs,
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
        print(f"Operator conformance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
