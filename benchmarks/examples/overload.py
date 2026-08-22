#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import capacity
import run as benchmark
import tooling_lock


def median(values: list[float | int]) -> float:
    return float(statistics.median(values))


def run_once(
    language: benchmark.Language,
    args: argparse.Namespace,
    env: dict[str, str],
    index: int,
) -> dict[str, Any]:
    try:
        capacity.start_services(language, env)
        if args.warmup not in ("0", "0s"):
            warmup_env = {
                **env,
                "BENCHMARK_RATE": str(args.warmup_rate),
            }
            benchmark.load(
                language,
                warmup_env,
                duration=args.warmup,
                result_name=f"overload.{language.name}.{index}.warmup.json",
            )

        with capacity.DockerStatsSampler(
            capacity.project_name(language),
            args.cores,
            args.loadgen_cores,
        ) as sampler:
            result = benchmark.load(
                language,
                env,
                duration=args.duration,
                result_name=f"overload.{language.name}.{index}.json",
            )
        result["run"] = index
        result["cpu"] = sampler.summary()
        print(
            f"{language.name} run {index}: "
            f"completed={result['requests_per_second']:.1f}/s, "
            f"p99={result['latency_ms']['p99']:.2f}ms, "
            f"dropped={result['dropped_rate'] * 100:.3f}%, "
            f"errors={result['error_rate'] * 100:.3f}%",
            flush=True,
        )
        return result
    finally:
        benchmark.run(
            benchmark.compose_command(
                language, "down", "--volumes", "--remove-orphans"
            ),
            cwd=language.example,
            env=env,
            check=False,
        )


def aggregate(
    language: benchmark.Language,
    runs: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    expected_requests = args.rate * benchmark.duration_seconds(args.duration)
    completed_ratio = median(
        [result["request_count"] / expected_requests for result in runs]
    )
    dropped_rate = median(
        [result["dropped_iterations"] / expected_requests for result in runs]
    )
    return {
        "language": language.name,
        "target_rps": args.rate,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
        "runs": len(runs),
        "completed_rps": median(
            [result["requests_per_second"] for result in runs]
        ),
        "completed_ratio": completed_ratio,
        "started_ratio": max(0.0, 1.0 - dropped_rate),
        "dropped_rate": dropped_rate,
        "error_rate": median([result["error_rate"] for result in runs]),
        "latency_ms": {
            key: median([result["latency_ms"][key] for result in runs])
            for key in ("avg", "p50", "p90", "p95", "p99", "max")
        },
        "raw_runs": runs,
    }


def write_results(
    results: list[dict[str, Any]], args: argparse.Namespace
) -> None:
    ranked = sorted(results, key=lambda result: result["latency_ms"]["p99"])
    document = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "architecture": platform.machine(),
            "os": platform.platform(),
        },
        "parameters": {
            "build": "reused" if args.skip_build else "release",
            "target_rps": args.rate,
            "service_cores": args.cores,
            "loadgen_cores": args.loadgen_cores,
            "duration": args.duration,
            "warmup": args.warmup,
            "warmup_rate": args.warmup_rate,
            "runs": args.runs,
            "preallocated_vus": args.preallocated_vus,
            "max_vus": args.max_vus,
        },
        "ranking": [result["language"] for result in ranked],
        "results": results,
    }
    (benchmark.ARTIFACTS / "overload.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )

    rows = [
        "| Rank | Language | p99 | Completed RPS | Started | Dropped | Errors |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for rank, result in enumerate(ranked, 1):
        rows.append(
            f"| {rank} | {result['language']} | "
            f"{result['latency_ms']['p99']:.2f} ms | "
            f"{result['completed_rps']:.1f} | "
            f"{result['started_ratio'] * 100:.3f}% | "
            f"{result['dropped_rate'] * 100:.3f}% | "
            f"{result['error_rate'] * 100:.3f}% |"
        )
    markdown = (
        "# Fixed overload comparison\n\n"
        f"- Target arrival rate: `{args.rate} RPS`\n"
        f"- Service CPU quota: `{args.cores}` cores per service container\n"
        f"- Load generator CPU quota: `{args.loadgen_cores}` cores\n"
        f"- Measurement: `{args.runs}` × `{args.duration}` per language\n\n"
        + "\n".join(rows)
        + "\n\n"
        "The p99 ranking covers requests that actually reached and completed "
        "at the service. Started/dropped values must be considered with it: "
        "a low p99 does not imply that the target arrival rate was sustained.\n"
    )
    (benchmark.ARTIFACTS / "overload.md").write_text(markdown)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare p99 latency under the same fixed overload"
    )
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--loadgen-cores", type=int, default=6)
    parser.add_argument("--rate", type=int, default=50_000)
    parser.add_argument("--duration", default="15s")
    parser.add_argument("--warmup", default="3s")
    parser.add_argument("--warmup-rate", type=int, default=100)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--preallocated-vus", type=int, default=512)
    parser.add_argument("--max-vus", type=int, default=8192)
    parser.add_argument(
        "--max-map-count",
        type=int,
        default=0,
        help="vm.max_map_count to set host/VM-wide before running (0 to leave it untouched)",
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--language",
        action="append",
        choices=[language.name for language in benchmark.LANGUAGES],
    )
    args = parser.parse_args()
    try:
        tooling_lock.acquire()
    except RuntimeError as error:
        parser.error(str(error))
    if (
        args.cores <= 0
        or args.loadgen_cores <= 0
        or args.rate <= 0
        or args.warmup_rate <= 0
        or args.runs <= 0
        or args.preallocated_vus <= 0
        or args.max_vus < args.preallocated_vus
        or args.max_map_count < 0
    ):
        parser.error("invalid overload-test parameters")
    args.vus = 1

    selected = [
        language
        for language in benchmark.LANGUAGES
        if not args.language or language.name in args.language
    ]
    benchmark.ensure_examples(selected, args)
    benchmark.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    cpp_selected = any(language.name.startswith("cpp") for language in selected)
    if cpp_selected:
        if any(language.name == "cpp" for language in selected):
            benchmark.prepare_cpp_configs(args.cores)
        if any(language.name == "cpp-boost" for language in selected):
            benchmark.prepare_cppboost_configs(args.cores)
        if args.max_map_count:
            benchmark.raise_max_map_count(args.max_map_count)
    if not args.skip_build:
        for language in selected:
            benchmark.build(language, benchmark.environment(args, language))

    results = []
    for language in selected:
        print(f"\n=== overload: {language.name} ===", flush=True)
        env = benchmark.environment(args, language)
        env.update(
            {
                "BENCHMARK_MODE": "arrival-rate",
                "BENCHMARK_RATE": str(args.rate),
                "BENCHMARK_PRE_ALLOCATED_VUS": str(args.preallocated_vus),
                "BENCHMARK_MAX_VUS": str(args.max_vus),
            }
        )
        runs = [
            run_once(language, args, env, index)
            for index in range(1, args.runs + 1)
        ]
        results.append(aggregate(language, runs, args))
        write_results(results, args)

    print("\n" + (benchmark.ARTIFACTS / "overload.md").read_text(), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
