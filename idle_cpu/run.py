#!/usr/bin/env python3
"""Measure configured-idle automationservice CPU for Temporal runtimes."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import graph_profile


CONFORMANCE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE.parent)).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "idle-cpu"
SERVICES = {
    "analyticsservice": 9093,
    "automationservice": 9094,
    "inventoryservice": 9092,
    "orderservice": 9091,
}
PROXY_ENVIRONMENT_KEYS = (
    "DEPENDENCY_PROXY_DIR",
    "DEPENDENCY_DOCKER_REGISTRY",
    "DEPENDENCY_GIT_MIRROR_URL",
    "DEPENDENCY_CONAN_REMOTE_URL",
    "GOPROXY",
    "NPM_CONFIG_REGISTRY",
    "PIP_INDEX_URL",
    "UV_INDEX_URL",
    "CARGO_REGISTRIES_CRATES_IO_INDEX",
)


@dataclass(frozen=True)
class Language:
    name: str
    example: Path


LANGUAGES = {
    language.name: language
    for language in (
        Language("go", ROOT / "goexample"),
        Language("python", ROOT / "pyexample"),
        Language("typescript", ROOT / "tsexample"),
    )
}


class LanguageLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("")

    def write(self, text: str) -> None:
        with self.path.open("a") as output:
            output.write(text)
            if text and not text.endswith("\n"):
                output.write("\n")

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool = True,
        capture: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.write(f"$ {' '.join(command)}")
        if capture:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )
            self.write(result.stdout)
        else:
            with self.path.open("a") as output:
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    text=True,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=timeout,
                )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"command failed ({result.returncode}): {' '.join(command)}"
            )
        return result


def wait_for_services(timeout: float, log: LanguageLog) -> None:
    pending = dict(SERVICES)
    deadline = time.monotonic() + timeout
    errors: dict[str, str] = {}
    while pending and time.monotonic() < deadline:
        for service, port in tuple(pending.items()):
            url = f"http://localhost:{port}/health/ready"
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        pending.pop(service)
                        errors.pop(service, None)
            except (OSError, urllib.error.URLError) as error:
                errors[service] = str(error)
        if pending:
            time.sleep(0.5)
    if pending:
        log.write("readiness errors: " + json.dumps(errors, sort_keys=True))
        raise RuntimeError("services did not become ready: " + ", ".join(pending))


def parse_cpu_percent(value: str) -> float:
    normalized = value.strip().removesuffix("%").replace(",", ".")
    return float(normalized)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def summarize(values: list[float]) -> dict[str, float | int | list[float]]:
    if not values:
        raise ValueError("at least one CPU sample is required")
    return {
        "samples": values,
        "sampleCount": len(values),
        "minimumPercent": min(values),
        "medianPercent": statistics.median(values),
        "averagePercent": statistics.fmean(values),
        "p95Percent": percentile(values, 0.95),
        "maximumPercent": max(values),
    }


def compose(
    log: LanguageLog,
    language: Language,
    env: dict[str, str],
    *arguments: str,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return log.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        cwd=language.example,
        env=env,
        check=check,
        capture=capture,
    )


def measure_language(
    language: Language,
    *,
    samples: int,
    interval: float,
    stabilization: float,
    readiness_timeout: float,
) -> dict[str, object]:
    log_path = ARTIFACTS / "logs" / f"{language.name}.log"
    log = LanguageLog(log_path)
    env = os.environ.copy()
    env["USE_LOCAL_MODULES"] = "1"
    graph_profile.verify_generated_project(
        language.example, os.environ.get("EXAMPLE_PROFILE", "function-call")
    )
    log.write(
        "proxy environment:\n"
        + "\n".join(
            f"  {key}={env[key]}" for key in PROXY_ENVIRONMENT_KEYS if env.get(key)
        )
    )
    started = time.monotonic()
    result: dict[str, object] = {
        "language": language.name,
        "example": str(language.example),
        "log": str(log_path),
    }
    try:
        log.run(["make", "docker-up"], cwd=language.example, env=env)
        wait_for_services(readiness_timeout, log)
        for service, port in SERVICES.items():
            semantics = graph_profile.verify_live_service(
                language.example, service, port
            )
            log.write(f"verified live {service} call semantics: {semantics}")
        compose(log, language, env, "ps")
        required_containers = (*SERVICES, "redpanda", "temporal")
        for service in required_containers:
            service_id = compose(
                log,
                language,
                env,
                "ps",
                "-q",
                service,
                capture=True,
            ).stdout.strip()
            if not service_id:
                raise RuntimeError(f"required container is absent: {service}")
        if stabilization > 0:
            log.write(f"stabilizing configured-idle runtime for {stabilization:.1f}s")
            time.sleep(stabilization)

        container = compose(
            log,
            language,
            env,
            "ps",
            "-q",
            "automationservice",
            capture=True,
        ).stdout.strip()
        if not container:
            raise RuntimeError("automationservice container is absent")

        cpu_samples: list[float] = []
        for index in range(samples):
            output = log.run(
                [
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.CPUPerc}}",
                    container,
                ],
                cwd=language.example,
                env=env,
                capture=True,
            ).stdout.strip()
            value = parse_cpu_percent(output)
            cpu_samples.append(value)
            log.write(f"cpu sample {index + 1}/{samples}: {value:.3f}%")
            if index + 1 < samples and interval > 0:
                time.sleep(interval)

        result.update(summarize(cpu_samples))
        result["status"] = "pass"
        return result
    except Exception as error:
        result["status"] = "fail"
        result["error"] = str(error)
        raise
    finally:
        compose(
            log,
            language,
            env,
            "logs",
            "--no-color",
            "--tail",
            "300",
            "automationservice",
            check=False,
        )
        compose(log, language, env, "down", "--remove-orphans", check=False)
        result["elapsedSeconds"] = time.monotonic() - started


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--language",
        action="append",
        choices=tuple(LANGUAGES),
        dest="languages",
    )
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--stabilization", type=float, default=15.0)
    parser.add_argument("--readiness-timeout", type=float, default=180.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    if args.interval < 0 or args.stabilization < 0 or args.readiness_timeout <= 0:
        raise SystemExit("durations must be non-negative and readiness timeout positive")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    selected = args.languages or list(LANGUAGES)
    summary: dict[str, object] = {
        "schemaVersion": "1.0",
        "operation": "configured-idle-cpu",
        "graphProfile": os.environ.get("EXAMPLE_PROFILE", "function-call"),
        "proxyEnabled": bool(os.environ.get("DEPENDENCY_PROXY_DIR")),
        "status": "pass",
        "languages": {},
    }
    summary_path = ARTIFACTS / "summary.json"
    for name in selected:
        print(f"==> [idle-cpu:{name}] START", flush=True)
        language_result: dict[str, object] = {
            "language": name,
            "status": "fail",
            "log": str(ARTIFACTS / "logs" / f"{name}.log"),
        }
        summary["languages"][name] = language_result  # type: ignore[index]
        try:
            language_result = measure_language(
                LANGUAGES[name],
                samples=args.samples,
                interval=args.interval,
                stabilization=args.stabilization,
                readiness_timeout=args.readiness_timeout,
            )
            summary["languages"][name] = language_result  # type: ignore[index]
            print(
                f"==> [idle-cpu:{name}] PASS "
                f"avg={language_result['averagePercent']:.3f}% "
                f"p95={language_result['p95Percent']:.3f}% "
                f"max={language_result['maximumPercent']:.3f}%",
                flush=True,
            )
        except Exception as error:
            language_result["error"] = str(error)
            summary["status"] = "fail"
            print(f"==> [idle-cpu:{name}] FAIL: {error}", file=sys.stderr, flush=True)
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
            return 1
        finally:
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
