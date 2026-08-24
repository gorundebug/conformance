#!/usr/bin/env python3
"""Validate and combine every repository conformance result."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / ".artifacts"
OUTPUT = ARTIFACTS / "summary.json"
SUITES = (
    "tooling", "structure", "signatures", "config", "config-schema",
    "config-runtime", "config-runtime-go", "config-runtime-typescript",
    "dependencies", "standalone-components", "pools",
    "operators", "serde", "transports", "kafka", "temporal", "tracing", "metrics",
    "dashboards", "logging",
    "scenarios", "call-semantics", "generation", "kubernetes", "profiling",
)
LANGUAGE_SUITES = {
    "standalone-components": {
        "go", "cpp", "cppboost", "python", "rust", "typescript",
    },
    "pools": {"go", "canonical-cpp", "cppboost", "typescript"},
    "operators": {"go", "canonical-cpp", "cppboost", "typescript"},
    "serde": {
        "go", "canonical-cpp", "cppboost", "python", "rust", "typescript",
    },
    "transports": {
        "go", "canonical-cpp", "cppboost", "python", "rust", "typescript",
    },
    "kafka": {"go", "cpp", "cppboost", "python", "rust", "typescript"},
    "temporal": {"go", "python", "typescript"},
    "tracing": {"go", "cpp", "cppboost", "python", "rust", "typescript"},
    "metrics": {"go", "cpp", "cppboost", "python", "rust", "typescript"},
    "dashboards": {"go", "cpp", "cppboost", "python", "rust", "typescript"},
    "logging": {"go", "cpp", "cppboost", "python", "rust", "typescript"},
    "scenarios": {
        "go", "go-native",
        "cpp", "cpp-native", "cppboost", "cppboost-native",
        "python", "python-native", "rust", "rust-native",
        "typescript", "typescript-native",
    },
    "call-semantics": {
        "go", "cpp", "cppboost", "python", "rust", "typescript",
    },
    "kubernetes": {
        "go", "cpp", "cppboost", "python", "rust", "typescript",
    },
}


def passed(name: str, summary: dict[str, object]) -> tuple[bool, str]:
    status = summary.get("status")
    if status in {"pass", "passed"}:
        result = True
    elif isinstance(summary.get("passed"), list) and summary.get("failed") == {}:
        result = bool(summary["passed"])
    else:
        return False, f"unrecognized or failing status: {status!r}"
    expected = LANGUAGE_SUITES.get(name)
    if expected is not None:
        implementations = summary.get("implementations")
        if isinstance(implementations, dict):
            actual = set(implementations)
        else:
            actual_value = summary.get("languages", summary.get("passed", []))
            actual = set(actual_value) if isinstance(actual_value, list) else set()
        if actual != expected:
            return False, (
                f"language matrix differs: actual={sorted(actual)}, "
                f"expected={sorted(expected)}"
            )
    return result, "pass"


def print_summary(matrix: dict[str, object], failures: list[str]) -> None:
    print("\nConformance summary:")
    for name in SUITES:
        result = matrix.get(name, {})
        status = result.get("status", "fail") if isinstance(result, dict) else "fail"
        detail = result.get("detail", "missing result") if isinstance(result, dict) else "missing result"
        suffix = "" if status == "pass" else f" — {detail}"
        print(f"  {status.upper():4}  {name}{suffix}")
    passed_count = sum(
        1
        for result in matrix.values()
        if isinstance(result, dict) and result.get("status") == "pass"
    )
    overall = "PASS" if not failures else "FAIL"
    print(f"\nResult: {overall} — {passed_count}/{len(SUITES)} suites passed")
    print(f"Full report: {OUTPUT.relative_to(ROOT)}")


def main() -> int:
    matrix: dict[str, object] = {}
    failures: list[str] = []
    for name in SUITES:
        path = ARTIFACTS / name / "summary.json"
        if not path.is_file():
            detail = f"missing {path.relative_to(ROOT)}"
            matrix[name] = {
                "status": "fail",
                "detail": detail,
                "artifact": str(path.relative_to(ROOT)),
            }
            failures.append(f"{name}: {detail}")
            continue
        try:
            summary = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            detail = f"invalid summary: {error}"
            matrix[name] = {
                "status": "fail",
                "detail": detail,
                "artifact": str(path.relative_to(ROOT)),
            }
            failures.append(f"{name}: {detail}")
            continue
        ok, detail = passed(name, summary)
        diagnostics = summary.get("diagnostics")
        if not ok and isinstance(diagnostics, list) and diagnostics:
            first = diagnostics[0]
            if isinstance(first, dict):
                code = first.get("code")
                message = first.get("message")
                if isinstance(code, str) and isinstance(message, str):
                    detail = f"[{code}] {message}"
        matrix[name] = {
            "status": "pass" if ok else "fail",
            "detail": detail,
            "artifact": str(path.relative_to(ROOT)),
        }
        if isinstance(diagnostics, list):
            matrix[name]["diagnostics"] = diagnostics
        if not ok:
            failures.append(f"{name}: {detail}")

    aggregate = {
        "status": "pass" if not failures else "fail",
        "example_profile": os.environ.get(
            "CONFORMANCE_EXAMPLE_PROFILE", "function-call"
        ),
        "suite_count": len(SUITES),
        "matrix": matrix,
        "failures": failures,
        "unrestricted_build_parallelism": True,
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n")
    print_summary(matrix, failures)
    if failures:
        raise RuntimeError("aggregate conformance failed:\n" + "\n".join(failures))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(error, file=sys.stderr)
        raise SystemExit(1)
