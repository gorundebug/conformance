#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import run as benchmark
import tooling_lock


class DockerStatsSampler:
    def __init__(self, project: str, service_cores: int, loadgen_cores: int) -> None:
        self._project = project
        self._quotas = {
            "inventoryservice": service_cores,
            "orderservice": service_cores,
            "loadgen": loadgen_cores,
        }
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def __enter__(self) -> DockerStatsSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                containers = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "--filter",
                        f"label=com.docker.compose.project={self._project}",
                        "--format",
                        '{{.ID}}|{{.Label "com.docker.compose.service"}}',
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.splitlines()
                names_by_id = {}
                for row in containers:
                    container_id, separator, service = row.partition("|")
                    if separator and service in self._quotas:
                        names_by_id[container_id] = service
                if names_by_id:
                    output = subprocess.run(
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{.ID}}|{{.CPUPerc}}",
                            *names_by_id,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    for row in output.splitlines():
                        container_id, separator, percent = row.partition("|")
                        if not separator:
                            continue
                        service = next(
                            (
                                name
                                for known_id, name in names_by_id.items()
                                if known_id.startswith(container_id)
                                or container_id.startswith(known_id)
                            ),
                            None,
                        )
                        if service is not None:
                            self._samples[service].append(float(percent.rstrip("%")))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self._stop.wait(0.5)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for service, quota in self._quotas.items():
            samples = self._samples.get(service, [])
            if not samples:
                result[service] = {"samples": 0}
                continue
            average = statistics.fmean(samples)
            maximum = max(samples)
            result[service] = {
                "samples": len(samples),
                "cpu_percent_avg": average,
                "cpu_percent_max": maximum,
                "quota_utilization_avg": average / (quota * 100),
                "quota_utilization_max": maximum / (quota * 100),
            }
        return result


def project_name(language: benchmark.Language) -> str:
    return f"servicelib-example-benchmark-{language.name}"


def start_services(language: benchmark.Language, env: dict[str, str]) -> None:
    benchmark.run(
        benchmark.compose_command(
            language, "up", "--detach", "--no-deps", "inventoryservice"
        ),
        cwd=language.example,
        env=env,
    )
    benchmark.wait_for_service(
        language,
        "inventoryservice",
        "http://localhost:9092/status/data",
        env,
    )
    benchmark.run(
        benchmark.compose_command(
            language, "up", "--detach", "--no-deps", "orderservice"
        ),
        cwd=language.example,
        env=env,
    )
    benchmark.wait_for_service(
        language,
        "orderservice",
        "http://localhost:9091/status/data",
        env,
    )


def failure_reasons(result: dict[str, Any], args: argparse.Namespace) -> list[str]:
    reasons = []
    if result["request_count"] <= 0:
        reasons.append("no requests completed")
    if result["error_rate"] > args.max_error_rate:
        reasons.append(
            f"errors {result['error_rate'] * 100:.3f}% > "
            f"{args.max_error_rate * 100:.3f}%"
        )
    if result["latency_ms"]["p95"] > args.max_p95_ms:
        reasons.append(
            f"p95 {result['latency_ms']['p95']:.2f}ms > "
            f"{args.max_p95_ms:g}ms"
        )
    if result["latency_ms"]["p99"] > args.max_p99_ms:
        reasons.append(
            f"p99 {result['latency_ms']['p99']:.2f}ms > "
            f"{args.max_p99_ms:g}ms"
        )
    return reasons


def run_attempt(
    language: benchmark.Language,
    args: argparse.Namespace,
    vus: int,
    attempt: int,
) -> dict[str, Any]:
    env = benchmark.environment(args, language)
    env.update({"BENCHMARK_MODE": "closed", "BENCHMARK_VUS": str(vus)})
    print(
        f"{language.name}: VUs={vus}, attempt {attempt}/{args.attempts}",
        flush=True,
    )
    try:
        start_services(language, env)
        if args.warmup not in ("0", "0s"):
            benchmark.load(
                language,
                env,
                duration=args.warmup,
                result_name=(
                    f"capacity.{language.name}.{vus}vus."
                    f"attempt-{attempt}.warmup.json"
                ),
            )
        with DockerStatsSampler(
            project_name(language), args.cores, args.loadgen_cores
        ) as sampler:
            result = benchmark.load(
                language,
                env,
                duration=args.duration,
                result_name=(
                    f"capacity.{language.name}.{vus}vus.attempt-{attempt}.json"
                ),
            )
        result["cpu"] = sampler.summary()
    finally:
        benchmark.run(
            benchmark.compose_command(
                language, "down", "--volumes", "--remove-orphans"
            ),
            cwd=language.example,
            env=env,
            check=False,
        )

    reasons = failure_reasons(result, args)
    result.update(
        {
            "vus": vus,
            "attempt": attempt,
            "successful": not reasons,
            "failure_reasons": reasons,
        }
    )
    verdict = "PASS" if result["successful"] else "FAIL"
    suffix = "" if not reasons else "; " + ", ".join(reasons)
    print(
        f"{language.name}: VUs={vus} {verdict}, "
        f"rps={result['requests_per_second']:.1f}, "
        f"p95={result['latency_ms']['p95']:.2f}ms, "
        f"p99={result['latency_ms']['p99']:.2f}ms, "
        f"errors={result['error_rate'] * 100:.3f}%{suffix}",
        flush=True,
    )
    return result


def aggregate_attempts(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "vus": results[0]["vus"],
        "attempts": len(results),
        "request_count": int(statistics.median(
            result["request_count"] for result in results
        )),
        "requests_per_second": statistics.median(
            result["requests_per_second"] for result in results
        ),
        "error_rate": statistics.median(
            result["error_rate"] for result in results
        ),
        "latency_ms": {
            percentile: statistics.median(
                result["latency_ms"][percentile] for result in results
            )
            for percentile in ("p95", "p99")
        },
    }


def rps_gain_percent(current_rps: float, previous_rps: float) -> float:
    if previous_rps <= 0:
        return 0.0
    return (current_rps - previous_rps) / previous_rps * 100


def level_failure_reasons(
    result: dict[str, Any],
    args: argparse.Namespace,
    previous: dict[str, Any] | None,
) -> list[str]:
    reasons = failure_reasons(result, args)
    if previous is not None:
        gain = rps_gain_percent(
            result["requests_per_second"], previous["requests_per_second"]
        )
        result["rps_gain_percent"] = gain
        if gain < args.min_rps_gain_percent:
            reasons.append(
                f"RPS gain {gain:.2f}% < {args.min_rps_gain_percent:g}%"
            )
    return reasons


def find_capacity(
    language: benchmark.Language, args: argparse.Namespace
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    accepted_levels: list[dict[str, Any]] = []
    failed_level: dict[str, Any] | None = None
    stop_reasons: list[str] = []
    first_failed_vus: int | None = None
    vus = args.start_vus

    print(
        f"{language.name}: start={args.start_vus} VUs, "
        f"step={args.vus_step}, limit={args.max_vus}, "
        f"duration={args.duration}, attempts={args.attempts}, "
        f"minimum RPS gain={args.min_rps_gain_percent:g}%",
        flush=True,
    )

    while vus <= args.max_vus:
        attempts = [run_attempt(language, args, vus, 1)]
        observations.extend(attempts)
        level = aggregate_attempts(attempts)
        previous = accepted_levels[-1] if accepted_levels else None
        reasons = level_failure_reasons(level, args, previous)

        if reasons:
            for attempt in range(2, args.attempts + 1):
                result = run_attempt(language, args, vus, attempt)
                attempts.append(result)
                observations.append(result)
            level = aggregate_attempts(attempts)
            reasons = level_failure_reasons(level, args, previous)

        if reasons:
            first_failed_vus = vus
            failed_level = level
            stop_reasons = reasons
            print(
                f"{language.name}: STOP at {vus} VUs after "
                f"{len(attempts)} attempts: {', '.join(reasons)}",
                flush=True,
            )
            break
        accepted_levels.append(level)
        gain = level.get("rps_gain_percent")
        gain_text = "baseline" if gain is None else f"RPS gain={gain:.2f}%"
        print(
            f"{language.name}: ACCEPT {vus} VUs, "
            f"rps={level['requests_per_second']:.1f}, {gain_text}",
            flush=True,
        )
        vus += args.vus_step

    last_success = accepted_levels[-1] if accepted_levels else None
    return {
        "language": language.name,
        "maximum_unsaturated_vus": (
            last_success["vus"] if last_success is not None else None
        ),
        "rps_at_maximum_unsaturated_vus": (
            last_success["requests_per_second"]
            if last_success is not None
            else None
        ),
        "p95_at_maximum_unsaturated_vus": (
            last_success["latency_ms"]["p95"]
            if last_success is not None
            else None
        ),
        "p99_at_maximum_unsaturated_vus": (
            last_success["latency_ms"]["p99"]
            if last_success is not None
            else None
        ),
        "first_failed_vus": first_failed_vus,
        "rps_at_first_failed_vus": (
            failed_level["requests_per_second"]
            if failed_level is not None
            else None
        ),
        "stop_reasons": stop_reasons,
        "limit_reached": first_failed_vus is None,
        "accepted_levels": accepted_levels,
        "observations": observations,
    }


def write_results(results: list[dict[str, Any]], args: argparse.Namespace) -> None:
    document = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "architecture": platform.machine(),
            "os": platform.platform(),
        },
        "parameters": {
            "build": "reused" if args.skip_build else "release",
            "service_cores": args.cores,
            "loadgen_cores": args.loadgen_cores,
            "start_vus": args.start_vus,
            "vus_step": args.vus_step,
            "max_vus": args.max_vus,
            "attempts": args.attempts,
            "duration": args.duration,
            "warmup": args.warmup,
            "max_error_rate": args.max_error_rate,
            "max_p95_ms": args.max_p95_ms,
            "max_p99_ms": args.max_p99_ms,
            "min_rps_gain_percent": args.min_rps_gain_percent,
        },
        "results": results,
    }
    (benchmark.ARTIFACTS / "capacity.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )

    rows = [
        "| Language | Last unsaturated VUs | RPS | p95 | p99 | Stop VUs | Stop RPS | Reason |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in results:
        maximum_vus = result["maximum_unsaturated_vus"]
        rps = result["rps_at_maximum_unsaturated_vus"]
        p95 = result["p95_at_maximum_unsaturated_vus"]
        p99 = result["p99_at_maximum_unsaturated_vus"]
        first_failed = result["first_failed_vus"]
        failed_rps = result["rps_at_first_failed_vus"]
        reason = "; ".join(result["stop_reasons"]) or "limit reached"
        rows.append(
            f"| {result['language']} | "
            f"{maximum_vus if maximum_vus is not None else 'none'} | "
            f"{f'{rps:.1f}' if rps is not None else 'n/a'} | "
            f"{f'{p95:.2f} ms' if p95 is not None else 'n/a'} | "
            f"{f'{p99:.2f} ms' if p99 is not None else 'n/a'} | "
            f"{first_failed if first_failed is not None else 'not reached'} | "
            f"{f'{failed_rps:.1f}' if failed_rps is not None else 'n/a'} | "
            f"{reason} |"
        )
    markdown = (
        "# Virtual-user load ramp\n\n"
        f"- Start: `{args.start_vus}` VUs\n"
        f"- Step: `{args.vus_step}` VUs\n"
        f"- Maximum: `{args.max_vus}` VUs\n"
        f"- Measurement: `{args.duration}` per attempt\n"
        f"- Attempts after failure: `{args.attempts}` total\n"
        f"- Maximum error rate: `{args.max_error_rate * 100:.3f}%`\n"
        f"- Maximum p95 latency: `{args.max_p95_ms:g} ms`\n"
        f"- Maximum p99 latency: `{args.max_p99_ms:g} ms`\n"
        f"- Minimum RPS gain: `{args.min_rps_gain_percent:g}%`\n\n"
        + "\n".join(rows)
        + "\n"
    )
    (benchmark.ARTIFACTS / "capacity.md").write_text(markdown)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Increase virtual users until three attempts fail"
    )
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--loadgen-cores", type=int, default=6)
    parser.add_argument("--start-vus", type=int, default=32)
    parser.add_argument("--vus-step", type=int, default=32)
    parser.add_argument("--max-vus", type=int, default=4096)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--duration", default="20s")
    parser.add_argument("--warmup", default="5s")
    parser.add_argument("--max-error-rate", type=float, default=0.001)
    parser.add_argument("--max-p95-ms", type=float, default=100.0)
    parser.add_argument("--max-p99-ms", type=float, default=200.0)
    parser.add_argument("--min-rps-gain-percent", type=float, default=5.0)
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
        or args.start_vus <= 0
        or args.vus_step <= 0
        or args.max_vus < args.start_vus
        or args.attempts <= 0
        or args.max_error_rate < 0
        or args.max_p95_ms <= 0
        or args.max_p99_ms <= 0
        or args.min_rps_gain_percent < 0
        or args.max_map_count < 0
    ):
        parser.error("invalid virtual-user ramp parameters")
    for option, value in (("--duration", args.duration), ("--warmup", args.warmup)):
        try:
            benchmark.duration_seconds(value)
        except ValueError as error:
            parser.error(f"{option}: {error}")

    args.vus = args.start_vus
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
        print(f"\n=== VU ramp: {language.name} ===", flush=True)
        results.append(find_capacity(language, args))
        write_results(results, args)
    print("\n" + (benchmark.ARTIFACTS / "capacity.md").read_text(), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
