#!/usr/bin/env python3
"""Cross-language structured logging contract gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import typescript_toolchain


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE_DIR))
import cpp_source_cache
import cpp_userver

ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "logging" / "summary.json"
GO = ROOT / "servicelib"
CANONICAL = ROOT / "cppservicelib"
BOOST = ROOT / "cppboostservicelib"
PYTHON = ROOT / "pyservicelib"
PYTHON_EXAMPLE = ROOT / "pyexample"
RUST = ROOT / "rustservicelib"
TYPESCRIPT = ROOT / "tsservicelib"

SOURCE_CASES = {
    GO / "runtime/testlog/testlog_test.go": (
        "TestStructuredLogLevelAndTypedFieldContract",
        "FieldTypeString", "FieldTypeInt64", "FieldTypeFloat64",
        "FieldTypeBool", "FieldTypeError",
    ),
    CANONICAL / "tests/telemetry_test.cpp": (
        "LogCapturesStructuredEntriesAndSupportsReset",
        "Field::Float64", "Field::Bool", "Field::Err",
    ),
    BOOST / "tests/telemetry_test.cpp": (
        "LogCapturesStructuredEntriesAndSupportsReset",
        "Field::Float64", "Field::Bool", "Field::Err",
    ),
    PYTHON / "tests/test_structured_log_contract.py": (
        "test_structured_log_level_and_typed_field_contract",
        "FieldType.STRING", "FieldType.INT64", "FieldType.FLOAT64",
        "FieldType.BOOL", "FieldType.ERROR",
    ),
    RUST / "tests/telemetry.rs": (
        "structured_log_level_and_typed_field_contract",
        "tracing::Level::DEBUG", "tracing::Level::INFO",
        "tracing::Level::WARN", "tracing::Level::ERROR",
    ),
    TYPESCRIPT / "test/structured-log.test.ts": (
        "structured logging preserves the canonical levels and typed fields",
        "LogLevel.Debug", "LogLevel.Info", "LogLevel.Warn", "LogLevel.Error",
        "int64", "float64", "bool", "err", "engine.reset()",
    ),
}


def verify_sources() -> dict[str, object]:
    files: dict[str, object] = {}
    failures: list[str] = []
    total = 0
    for path, markers in SOURCE_CASES.items():
        relative = str(path.relative_to(ROOT))
        if not path.is_file():
            failures.append(f"missing source: {relative}")
            continue
        source = path.read_text()
        missing = [marker for marker in markers if marker not in source]
        files[relative] = {"required_markers": len(markers), "missing": missing}
        total += len(markers)
        failures.extend(f"{relative}: missing {marker}" for marker in missing)
    if failures:
        raise RuntimeError("logging source matrix failed:\n" + "\n".join(failures))
    return {"files": files, "required_markers": total}


def execute(
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    print(f"[logging] START {name}", file=sys.stderr, flush=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, bufsize=1,
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
    print(f"[logging] PASS  {name} ({duration:.1f}s)", file=sys.stderr, flush=True)
    return {
        "name": name,
        "command": command,
        "exit_code": 0,
        "duration_seconds": duration,
        "output_tail": output[-8000:],
    }


def python_image_build() -> tuple[list[str], dict[str, str]]:
    env = {**os.environ, "PYSERVICELIB_SOURCE_CONTEXT": str(PYTHON)}
    command = [
        "docker", "compose",
        "--project-name", "servicelib-logging-conformance-python",
        "--project-directory", str(PYTHON_EXAMPLE),
        "--file", str(PYTHON_EXAMPLE / "docker-compose.yml"),
        "build", "inventoryservice",
    ]
    return command, env


def python_test_command() -> list[str]:
    return [
        "docker", "run", "--rm",
        "--volume", f"{PYTHON}:/workspace/.pyservicelib:ro",
        "--workdir", "/workspace/.pyservicelib",
        "--env", "PYTHONPATH=/workspace/.pyservicelib/src",
        "example-python:latest",
        "/workspace/.venv/bin/python", "-m", "pytest", "-q",
        "-p", "no:cacheprovider",
        "tests/test_structured_log_contract.py",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    source_matrix = verify_sources()

    canonical_script = "./build/servicelib_telemetry_test"
    boost_script = (
        "ctest --test-dir build/docker --output-on-failure "
        "-R '^cppboostservicelib_telemetry_test$'"
    )
    if not args.skip_build:
        boost_source_mount = [
            "-v",
            cpp_source_cache.source_mount(BOOST),
        ]
        cpp_source_cache.ensure(BOOST)
        canonical_script = (
            cpp_userver.configure_script() +
            " && cmake --build --preset docker --parallel "
            "--target servicelib_telemetry_test && " + canonical_script
        )
        boost_script = (
            "cmake -S . -B build/docker -G Ninja "
            "-DCMAKE_BUILD_TYPE=Debug "
            "-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON "
            f"{cpp_source_cache.cmake_args(BOOST)}&& "
            "cmake --build build/docker --parallel --target "
            "cppboostservicelib_telemetry_test && " + boost_script
        )
    else:
        boost_source_mount = []

    python_build_run: dict[str, object] | None = None
    rust_build_run: dict[str, object] | None = None
    if not args.skip_build:
        python_build_command, python_build_env = python_image_build()
        python_build_run = execute(
            "python-structured-logging-image",
            python_build_command,
            PYTHON_EXAMPLE,
            python_build_env,
        )
        rust_build_run = execute(
            "rust-structured-logging-toolchain",
            [
                "docker", "build", "--target", "toolchain", "--tag",
                "rustservicelib-toolchain:latest", ".",
            ],
            RUST,
        )

    typescript_build_run: dict[str, object] | None = None
    if not args.skip_build:
        execute(
            "typescript-structured-logging-dependencies",
            typescript_toolchain.install_command(),
            TYPESCRIPT,
            typescript_toolchain.environment(),
        )
        typescript_build_run = execute(
            "typescript-structured-logging-build",
            typescript_toolchain.tsc_command("tsconfig.test.json", force=True),
            TYPESCRIPT,
        )
        execute(
            "typescript-structured-logging-runtime-assets",
            typescript_toolchain.copy_runtime_assets_command(),
            TYPESCRIPT,
        )

    runs = [
        execute(
            "go-structured-logging",
            ["go", "test", "./runtime/testlog", "-run",
             "^TestStructuredLogLevelAndTypedFieldContract$", "-count=1", "-v"],
            GO,
            {**os.environ, "GOWORK": "off"},
        ),
        execute(
            "canonical-cpp-structured-logging",
            ["docker", "compose", "-f", "docker-compose.cmake.yml", "run",
             "--rm", "test", "/bin/bash", "-lc", canonical_script],
            CANONICAL,
        ),
        execute(
            "boost-cpp-structured-logging",
            ["docker", "run", "--rm", *boost_source_mount,
             "-v", f"{BOOST}:/workspace",
             "-v", cpp_source_cache.build_volume_mount(
                 BOOST, "cppboostservicelib-logging"
             ),
             "-w", "/workspace", "cppboostservicelib-build:latest", "/bin/bash",
             "-lc", boost_script],
            BOOST,
        ),
        execute(
            "python-structured-logging",
            python_test_command(),
            PYTHON,
        ),
        execute(
            "rust-structured-logging",
            ["docker", "run", "--rm", "-v", f"{RUST}:/workspace",
             "-v", "servicelib-conformance-rust-cargo-registry:/usr/local/cargo/registry",
             "-v", "servicelib-conformance-rust-logging-target:/workspace/target",
             "-w",
             "/workspace", "rustservicelib-toolchain:latest", "cargo", "test",
             "--test", "telemetry",
             "structured_log_level_and_typed_field_contract", "--", "--nocapture"],
            RUST,
        ),
        execute(
            "typescript-structured-logging",
            ["node", "--test", "--enable-source-maps",
             "dist-test/test/structured-log.test.js"],
            TYPESCRIPT,
        ),
    ]
    summary = {
        "status": "pass",
        "languages": ["go", "cpp", "cppboost", "python", "rust", "typescript"],
        "contract": {
            "levels": ["debug", "info", "warn", "error"],
            "typed_fields": ["string", "int64", "float64", "bool", "error"],
            "preserves": ["level", "message", "field_name", "field_value"],
            "reset_or_clear": True,
        },
        "source_matrix": source_matrix,
        "runs": runs,
        "unrestricted_build_parallelism": True,
    }
    if python_build_run is not None:
        summary["runs"].insert(3, python_build_run)
    if rust_build_run is not None:
        summary["runs"].insert(-1, rust_build_run)
    if typescript_build_run is not None:
        summary["runs"].insert(-1, typescript_build_run)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        "Structured logging conformance passed: "
        "go, cpp, cppboost, python, rust, typescript"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"Structured logging conformance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
