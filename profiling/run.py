#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE_DIR))
import cpp_source_cache

ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
PROFILING_ROOT = CONFORMANCE_DIR / "profiling"
PROFILING_RUNNER = PROFILING_ROOT / "examples" / "run.py"
PROFILE_ARTIFACTS = PROFILING_ROOT / "examples" / ".artifacts"
ARTIFACTS = CONFORMANCE_DIR / ".artifacts" / "profiling"
LANGUAGES = ("cppboost", "cppboost-native")
SERVICES = ("orderservice", "inventoryservice")
RUNTIME_METRICS = {
    "runtime_active_work",
    "runtime_event_loop_lag_seconds",
    "runtime_worker_utilization",
}


def prepare_cpp_source_contexts() -> tuple[Path, Path]:
    framework = ROOT / "cppboostservicelib"
    source_cache = cpp_source_cache.source_dir(framework)
    grpc_source = source_cache / "grpc-src"
    asio_grpc_source = source_cache / "asio-grpc-src"
    if not grpc_source.is_dir() or not asio_grpc_source.is_dir():
        subprocess.run(
            [
                "docker", "build", "-f", "Dockerfile.cmake", "-t",
                "cppboostservicelib-build:latest", ".",
            ],
            cwd=framework,
            check=True,
        )
        subprocess.run(
            cpp_source_cache.prepare_command(framework),
            cwd=framework,
            check=True,
        )
    require(grpc_source.is_dir(), f"missing shared gRPC source cache: {grpc_source}")
    require(
        asio_grpc_source.is_dir(),
        f"missing shared asio-grpc source cache: {asio_grpc_source}",
    )
    print(f"[source-cache] runtime builds reuse {grpc_source}", flush=True)
    return grpc_source, asio_grpc_source


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_text(path: Path) -> str:
    require(path.is_file(), f"missing profiling artifact: {path}")
    text = path.read_text()
    require(bool(text.strip()), f"empty profiling artifact: {path}")
    return text


def read_json(path: Path) -> Any:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"malformed JSON artifact {path}: {error}") from error


def finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    require(isinstance(value, (int, float)) and not isinstance(value, bool),
            f"{name} must be numeric")
    number = float(value)
    require(math.isfinite(number), f"{name} must be finite")
    if minimum is not None:
        require(number >= minimum, f"{name} must be >= {minimum}, got {number}")
    return number


def duration_milliseconds(value: str) -> float:
    text = value.strip()
    if text.endswith("ms"):
        return float(text[:-2])
    if text.endswith("s"):
        return float(text[:-1]) * 1_000
    if text.endswith("m"):
        return float(text[:-1]) * 60_000
    return float(text) * 1_000


def validate_folded(path: Path) -> dict[str, int]:
    lines = [line for line in read_text(path).splitlines() if line.strip()]
    total = 0
    for line_number, line in enumerate(lines, 1):
        try:
            stack, count_text = line.rsplit(" ", 1)
            count = int(count_text)
        except (ValueError, TypeError) as error:
            raise RuntimeError(
                f"invalid folded stack at {path}:{line_number}: {line!r}"
            ) from error
        require(bool(stack), f"empty folded stack at {path}:{line_number}")
        require(count > 0, f"non-positive folded sample at {path}:{line_number}")
        total += count
    require(total > 0, f"no profiler samples in {path}")
    return {"lines": len(lines), "samples": total}


def validate_load(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    data = read_json(path)
    require(isinstance(data, dict), f"load artifact must be an object: {path}")
    expected = {
        "scenario": "process_order_out_of_stock",
        "build_type": "Release",
        "mode": "closed",
        "duration": args.duration,
        "vus": args.vus,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
    }
    for key, value in expected.items():
        require(data.get(key) == value,
                f"{path.name}: {key}={data.get(key)!r}, expected {value!r}")
    finite_number(data.get("request_count"), f"{path.name}.request_count", minimum=1)
    finite_number(data.get("requests_per_second"),
                  f"{path.name}.requests_per_second", minimum=sys.float_info.min)
    actual_duration_ms = finite_number(
        data.get("test_run_duration_ms"),
        f"{path.name}.test_run_duration_ms", minimum=1,
    )
    maximum_duration_ms = duration_milliseconds(args.duration) + 2_000
    require(
        actual_duration_ms <= maximum_duration_ms,
        f"{path.name}: load ran for {actual_duration_ms:.0f}ms, expected at most "
        f"{maximum_duration_ms:.0f}ms; requests remained in graceful stop",
    )
    require(finite_number(data.get("error_rate"), f"{path.name}.error_rate") == 0,
            f"{path.name}: requests failed")
    require(finite_number(data.get("dropped_iterations"),
                          f"{path.name}.dropped_iterations") == 0,
            f"{path.name}: iterations were dropped")
    latency = data.get("latency_ms")
    require(isinstance(latency, dict), f"{path.name}.latency_ms must be an object")
    for percentile in ("p50", "p95", "p99", "max"):
        finite_number(latency.get(percentile),
                      f"{path.name}.latency_ms.{percentile}", minimum=0)
    return data


def validate_runtime_metrics(path: Path) -> dict[str, Any]:
    samples = read_json(path)
    require(isinstance(samples, list) and samples,
            f"runtime metrics must contain samples: {path}")
    for index, sample in enumerate(samples):
        require(isinstance(sample, dict), f"{path.name}[{index}] must be an object")
        finite_number(sample.get("elapsed_seconds"),
                      f"{path.name}[{index}].elapsed_seconds", minimum=0)
        values = sample.get("values")
        require(isinstance(values, dict), f"{path.name}[{index}].values must be an object")
        require(RUNTIME_METRICS.issubset(values),
                f"{path.name}[{index}] misses {sorted(RUNTIME_METRICS - set(values))}")
        for metric in RUNTIME_METRICS:
            finite_number(values[metric], f"{path.name}[{index}].{metric}", minimum=0)
    return {"samples": len(samples), "metrics": sorted(RUNTIME_METRICS)}


def validate_allocation_profile(
    path: Path, language: str, service: str, args: argparse.Namespace,
    load: dict[str, Any],
) -> dict[str, Any]:
    profile = read_json(path)
    require(isinstance(profile, dict), f"allocation profile must be an object: {path}")
    expected = {
        "schema_version": 1,
        "profiler": "allocator-neutral-ld-preload-counters",
        "language": language,
        "service": service,
        "scenario": "process_order_out_of_stock",
        "build_type": "Release",
        "duration": args.duration,
        "vus": args.vus,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
    }
    for key, value in expected.items():
        require(profile.get(key) == value,
                f"{path.name}: {key}={profile.get(key)!r}, expected {value!r}")
    require(profile.get("request_count") == load.get("request_count"),
            f"{path.name}: request_count differs from allocation load")
    for field in (
        "allocation_calls", "allocated_bytes", "allocations_per_request",
        "allocated_bytes_per_request",
    ):
        finite_number(profile.get(field), f"{path.name}.{field}",
                      minimum=sys.float_info.min)
    counters = profile.get("counters")
    require(isinstance(counters, dict), f"{path.name}.counters must be an object")
    required = {
        "malloc_calls", "calloc_calls", "realloc_calls", "memalign_calls",
        "free_calls", "allocation_failures", "malloc_bytes", "calloc_bytes",
        "realloc_bytes", "memalign_bytes", "freed_bytes", "peak_live_bytes",
    }
    require(required.issubset(counters),
            f"{path.name} misses counters {sorted(required - set(counters))}")
    for name in required:
        finite_number(counters[name], f"{path.name}.counters.{name}", minimum=0)
    return profile


def validate_allocation_stack_profile(
    svg: Path, language: str, service: str, args: argparse.Namespace,
    load: dict[str, Any],
) -> dict[str, Any]:
    folded = Path(str(svg) + ".folded.txt")
    bytes_folded = Path(str(svg) + ".bytes.folded.txt")
    top = Path(str(svg) + ".top.txt")
    summary_path = Path(str(svg) + ".summary.json")
    maps = Path(str(svg) + ".maps.txt")

    svg_text = read_text(svg)
    require("<svg" in svg_text and "Flame graph" in svg_text,
            f"invalid allocation call-stack flamegraph SVG: {svg}")
    folded_text = read_text(folded)
    calls = validate_folded(folded)
    sampled_bytes = validate_folded(bytes_folded)
    require(
        any(marker in folded_text.casefold() for marker in (
            "servicelib", "boost::asio", "boost::beast", "grpc",
        )),
        f"{folded.name}: no attributable framework/transport allocation call chain",
    )
    require("maybe_sample_allocation" not in folded_text and
            "allocation_profile.c" not in folded_text,
            f"{folded.name}: profiling interceptor frames leaked into call sites")
    top_text = read_text(top)
    require("self time" in top_text.lower() and "total time" in top_text.lower(),
            f"invalid allocation call-stack top summary: {top}")
    maps_text = read_text(maps)
    require("r-x" in maps_text, f"{maps.name}: no executable mappings")

    summary = read_json(summary_path)
    require(isinstance(summary, dict),
            f"allocation stack summary must be an object: {summary_path}")
    expected = {
        "schema_version": 1,
        "profiler": "allocator-neutral-sampled-call-stacks",
        "language": language,
        "service": service,
        "scenario": "process_order_out_of_stock",
        "build_type": "Release",
        "duration": args.duration,
        "vus": args.vus,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
        "request_count": load.get("request_count"),
    }
    for key, value in expected.items():
        require(summary.get(key) == value,
                f"{summary_path.name}: {key}={summary.get(key)!r}, "
                f"expected {value!r}")
    sample_every = finite_number(
        summary.get("sample_every"), f"{summary_path.name}.sample_every", minimum=1
    )
    sample_count = finite_number(
        summary.get("sample_count"), f"{summary_path.name}.sample_count", minimum=1
    )
    unresolved = finite_number(
        summary.get("unresolved_sample_count"),
        f"{summary_path.name}.unresolved_sample_count", minimum=0,
    )
    require(unresolved <= sample_count * 0.10,
            f"{summary_path.name}: more than 10% of samples are unresolved")
    finite_number(summary.get("unique_stack_count"),
                  f"{summary_path.name}.unique_stack_count", minimum=1)
    finite_number(summary.get("sampled_usable_bytes"),
                  f"{summary_path.name}.sampled_usable_bytes", minimum=1)
    require(calls["samples"] == int(sample_count),
            f"{summary_path.name}: sample_count differs from folded stacks")
    require(sampled_bytes["samples"] == int(summary["sampled_usable_bytes"]),
            f"{summary_path.name}: sampled bytes differ from folded stacks")
    leaves = summary.get("top_leaf_functions")
    require(isinstance(leaves, list) and leaves,
            f"{summary_path.name}: no attributed allocation leaves")
    for index, leaf in enumerate(leaves):
        require(isinstance(leaf, dict) and isinstance(leaf.get("function"), str)
                and bool(leaf["function"]),
                f"{summary_path.name}.top_leaf_functions[{index}] is invalid")
        finite_number(leaf.get("samples"),
                      f"{summary_path.name}.top_leaf_functions[{index}].samples",
                      minimum=1)
    return {
        "sample_every": int(sample_every),
        "sample_count": int(sample_count),
        "unresolved_sample_count": int(unresolved),
        "unique_stack_count": summary["unique_stack_count"],
        "sampled_usable_bytes": summary["sampled_usable_bytes"],
        "top_leaf_functions": leaves,
        "artifacts": {
            "svg": str(svg),
            "folded": str(folded),
            "bytes_folded": str(bytes_folded),
            "top": str(top),
            "summary": str(summary_path),
            "maps": str(maps),
        },
    }


def validate_scheduler_profile(
    path: Path, language: str, service: str, args: argparse.Namespace,
    load: dict[str, Any],
) -> dict[str, Any]:
    profile = read_json(path)
    require(isinstance(profile, dict), f"scheduler profile must be an object: {path}")
    require(profile.get("schema_version") == 1,
            f"{path.name}: unsupported scheduler schema")
    require(profile.get("profiler") == "procfs-schedstat-wchan",
            f"{path.name}: unexpected scheduler profiler")
    expected = {
        "language": language,
        "service": service,
        "scenario": "process_order_out_of_stock",
        "build_type": "Release",
        "duration": args.duration,
        "vus": args.vus,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
        "request_count": load.get("request_count"),
    }
    for key, value in expected.items():
        require(profile.get(key) == value,
                f"{path.name}: {key}={profile.get(key)!r}, expected {value!r}")
    finite_number(profile.get("duration_ns"), f"{path.name}.duration_ns", minimum=1)
    finite_number(profile.get("sample_count"), f"{path.name}.sample_count", minimum=1)
    threads = profile.get("threads")
    require(isinstance(threads, list) and threads,
            f"{path.name}: scheduler profile has no stable threads")
    totals = profile.get("totals")
    require(isinstance(totals, dict), f"{path.name}.totals must be an object")
    for field in (
        "runtime_ns", "runqueue_wait_ns", "timeslices",
        "voluntary_context_switches", "involuntary_context_switches",
    ):
        finite_number(totals.get(field), f"{path.name}.totals.{field}", minimum=0)
    require(finite_number(totals.get("runtime_ns"),
                          f"{path.name}.totals.runtime_ns") > 0,
            f"{path.name}: scheduler profile captured no CPU runtime")
    for field in ("thread_state_samples", "wait_channel_samples"):
        values = profile.get(field)
        require(isinstance(values, dict) and values,
                f"{path.name}.{field} must be a non-empty object")
        for name, value in values.items():
            require(isinstance(name, str) and name,
                    f"{path.name}.{field} has invalid key")
            finite_number(value, f"{path.name}.{field}.{name}", minimum=0)
    return profile


def validate_offcpu_profile(
    svg: Path,
    folded: Path,
    top: Path,
    load_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    svg_text = read_text(svg)
    require("<svg" in svg_text and "Flame graph" in svg_text,
            f"invalid off-CPU flamegraph SVG: {svg}")
    folded_text = read_text(folded)
    folded_result = validate_folded(folded)
    require(
        any(marker in folded_text.casefold() for marker in (
            "futex", "epoll", "schedule", "completionqueue", "grpc",
        )),
        f"{folded.name}: no attributable scheduler/mutex/CQ wait call chain",
    )
    top_text = read_text(top)
    require("self time" in top_text.lower() and "total time" in top_text.lower(),
            f"invalid off-CPU top-frame summary: {top}")
    return {
        "folded": folded_result,
        "load": validate_load(load_path, args),
        "artifacts": {
            "svg": str(svg),
            "folded": str(folded),
            "top": str(top),
            "load": str(load_path),
        },
    }


def validate_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for language in LANGUAGES:
        language_result: dict[str, Any] = {}
        for service in SERVICES:
            prefix = f"{language}.{service}"
            svg = PROFILE_ARTIFACTS / f"{prefix}.flamegraph.svg"
            folded = PROFILE_ARTIFACTS / f"{prefix}.flamegraph.svg.folded.txt"
            top = PROFILE_ARTIFACTS / f"{prefix}.flamegraph.svg.top.txt"
            load = PROFILE_ARTIFACTS / f"{prefix}.profiling-load.json"
            svg_text = read_text(svg)
            require("<svg" in svg_text and "Flame graph" in svg_text,
                    f"invalid flamegraph SVG: {svg}")
            top_text = read_text(top)
            require("self time" in top_text.lower() and
                    "total time" in top_text.lower(),
                    f"invalid top-frame summary: {top}")
            service_result: dict[str, Any] = {
                "folded": validate_folded(folded),
                "load": validate_load(load, args),
                "artifacts": {
                    "svg": str(svg),
                    "folded": str(folded),
                    "top": str(top),
                    "load": str(load),
                },
            }
            allocation_load_path = (
                PROFILE_ARTIFACTS / f"{language}.allocation.profiling-load.json"
            )
            allocation_load = validate_load(allocation_load_path, args)
            allocation_path = (
                PROFILE_ARTIFACTS / f"{language}.{service}.allocations.json"
            )
            service_result["allocation"] = validate_allocation_profile(
                allocation_path, language, service, args, allocation_load
            )
            allocation_stack_svg = (
                PROFILE_ARTIFACTS /
                f"{prefix}.allocation-stacks.flamegraph.svg"
            )
            service_result["allocation_stacks"] = (
                validate_allocation_stack_profile(
                    allocation_stack_svg, language, service, args,
                    allocation_load,
                )
            )
            scheduler_load_path = (
                PROFILE_ARTIFACTS / f"{language}.{service}.scheduler-load.json"
            )
            scheduler_load = validate_load(scheduler_load_path, args)
            scheduler_path = (
                PROFILE_ARTIFACTS / f"{language}.{service}.scheduler.json"
            )
            service_result["scheduler"] = validate_scheduler_profile(
                scheduler_path, language, service, args, scheduler_load
            )
            offcpu_svg = PROFILE_ARTIFACTS / f"{prefix}.offcpu.flamegraph.svg"
            offcpu_folded = Path(str(offcpu_svg) + ".folded.txt")
            offcpu_top = Path(str(offcpu_svg) + ".top.txt")
            offcpu_load = PROFILE_ARTIFACTS / f"{prefix}.offcpu-load.json"
            service_result["offcpu"] = validate_offcpu_profile(
                offcpu_svg, offcpu_folded, offcpu_top, offcpu_load, args
            )
            service_result["artifacts"].update(
                {
                    "allocation": str(allocation_path),
                    "allocation_load": str(allocation_load_path),
                    "allocation_stack_svg": str(allocation_stack_svg),
                    "allocation_stack_folded": str(allocation_stack_svg) +
                    ".folded.txt",
                    "allocation_stack_bytes_folded":
                    str(allocation_stack_svg) + ".bytes.folded.txt",
                    "allocation_stack_top": str(allocation_stack_svg) +
                    ".top.txt",
                    "allocation_stack_summary": str(allocation_stack_svg) +
                    ".summary.json",
                    "allocation_stack_maps": str(allocation_stack_svg) +
                    ".maps.txt",
                    "scheduler": str(scheduler_path),
                    "scheduler_load": str(scheduler_load_path),
                    "offcpu_svg": str(offcpu_svg),
                    "offcpu_folded": str(offcpu_folded),
                    "offcpu_top": str(offcpu_top),
                    "offcpu_load": str(offcpu_load),
                }
            )
            if language == "cppboost":
                metrics = PROFILE_ARTIFACTS / f"{prefix}.runtime-metrics.json"
                service_result["runtime_metrics"] = validate_runtime_metrics(metrics)
                service_result["artifacts"]["runtime_metrics"] = str(metrics)
            language_result[service] = service_result
        results[language] = language_result
    return results


def compare_framework_native(results: dict[str, Any]) -> dict[str, Any]:
    comparison: dict[str, Any] = {}
    for service in SERVICES:
        framework = results["cppboost"][service]["load"]
        native = results["cppboost-native"][service]["load"]
        framework_rps = finite_number(
            framework["requests_per_second"], f"cppboost.{service}.rps",
            minimum=sys.float_info.min,
        )
        native_rps = finite_number(
            native["requests_per_second"], f"cppboost-native.{service}.rps",
            minimum=sys.float_info.min,
        )
        latency_ratios: dict[str, float] = {}
        for percentile in ("p50", "p95", "p99"):
            framework_latency = finite_number(
                framework["latency_ms"][percentile],
                f"cppboost.{service}.{percentile}",
                minimum=sys.float_info.min,
            )
            native_latency = finite_number(
                native["latency_ms"][percentile],
                f"cppboost-native.{service}.{percentile}",
                minimum=sys.float_info.min,
            )
            latency_ratios[f"{percentile}_framework_to_native"] = (
                framework_latency / native_latency
            )
        comparison[service] = {
            "framework_rps": framework_rps,
            "native_rps": native_rps,
            "framework_to_native_rps": framework_rps / native_rps,
            "throughput_loss_percent": 100 * (1 - framework_rps / native_rps),
            "latency_ratios": latency_ratios,
            "allocation_ratios": {
                field: (
                    results["cppboost"][service]["allocation"][field]
                    / results["cppboost-native"][service]["allocation"][field]
                )
                for field in (
                    "allocations_per_request", "allocated_bytes_per_request"
                )
            },
            "scheduler_per_request": {
                language: {
                    field: (
                        results[language][service]["scheduler"]["totals"][field]
                        / results[language][service]["scheduler"]["request_count"]
                    )
                    for field in (
                        "runtime_ns", "runqueue_wait_ns", "timeslices",
                        "voluntary_context_switches",
                        "involuntary_context_switches",
                    )
                }
                for language in LANGUAGES
            },
        }
    return comparison


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run and validate the mandatory Boost framework/native profiling matrix"
    )
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--loadgen-cores", type=int, default=6)
    parser.add_argument("--vus", type=int, default=256)
    parser.add_argument("--duration", default="20s")
    parser.add_argument("--warmup", default="5s")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-run", action="store_true",
                        help="validate existing profiling artifacts only")
    args = parser.parse_args()
    for name in ("cores", "loadgen_cores", "vus"):
        require(getattr(args, name) > 0, f"--{name.replace('_', '-')} must be positive")

    command = [
        sys.executable,
        str(PROFILING_RUNNER),
        "--graph-profile",
        os.environ.get("CONFORMANCE_EXAMPLE_PROFILE", "function-call"),
        "--language", "cppboost",
        "--language", "cppboost-native",
        "--cores", str(args.cores),
        "--loadgen-cores", str(args.loadgen_cores),
        "--vus", str(args.vus),
        "--duration", args.duration,
        "--warmup", args.warmup,
    ]
    if args.skip_build:
        command.append("--skip-build")
    for profile_kind in ("cpu", "allocation", "scheduler", "offcpu"):
        command.extend(("--profile-kind", profile_kind))
    if not args.skip_run:
        grpc_source, asio_grpc_source = prepare_cpp_source_contexts()
        print("+", " ".join(command), flush=True)
        env = os.environ.copy()
        env["PROFILING_DEPENDENCIES_DIR"] = str(ROOT)
        env["SERVICEGEN_GRPC_SOURCE_CONTEXT"] = str(grpc_source)
        env["SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT"] = str(asio_grpc_source)
        subprocess.run(command, cwd=PROFILING_ROOT, env=env, check=True)

    results = validate_artifacts(args)
    summary = {
        "status": "pass",
        "workload": {
            "scenario": "process_order_out_of_stock",
            "graph_profile": os.environ.get(
                "CONFORMANCE_EXAMPLE_PROFILE", "function-call"
            ),
            "build_type": "Release",
            "cores": args.cores,
            "loadgen_cores": args.loadgen_cores,
            "vus": args.vus,
            "duration": args.duration,
            "warmup": args.warmup,
        },
        "languages": results,
        "framework_native_comparison": compare_framework_native(results),
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
