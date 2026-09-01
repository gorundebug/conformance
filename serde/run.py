#!/usr/bin/env python3
"""Cross-language ServiceLib serde conformance gate."""

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
import dependency_environment
import go_toolchain
import typescript_toolchain


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "serde" / "summary.json"
CANONICAL = ROOT / "cppservicelib"
BOOST = ROOT / "cppboostservicelib"
PYTHON = ROOT / "pyservicelib"
PYTHON_EXAMPLE = ROOT / "pyexample"
RUST = ROOT / "rustservicelib"
TYPESCRIPT = ROOT / "tsservicelib"
PROBE = CONFORMANCE_DIR / "serde" / "cpp_probe.cpp"
USERVER_REMOTE_CONTEXT = (
    "https://github.com/userver-framework/userver.git"
    "#c9f77729c0edce7e423def2d4a4450aa7fc9d259"
)


def repository_mounts() -> list[str]:
    return [
        "-v", f"{ROOT}:/repo",
        "-v", f"{CONFORMANCE_DIR}:/repo/conformance:ro",
    ]


def boost_source_mount_args() -> list[str]:
    return ["-v", cpp_source_cache.source_mount(BOOST)]

CASES = (
    "PrimitiveWireFormatMatchesGo",
    "FloatingPointRoundTripPreservesBits",
    "StringBytesAndSerializeToUseLengthPrefixAndAppend",
    "FixedAndFramedArraysRoundTrip",
    "MapRoundTripAndMalformedCountMismatch",
    "StreamWrappersAndKeyValueSerde",
    "TypeErasureRejectsWrongObjectType",
    "LimitsAreEnforcedOnEncodeAndDecode",
    "TruncatedFramesReportTheReadOffset",
    "CompositeConstructorsRejectNullDependencies",
)


def execute(
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    print(f"[serde] START {name}", file=sys.stderr, flush=True)
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
    print(f"[serde] PASS  {name} ({duration:.1f}s)", file=sys.stderr, flush=True)
    return {
        "name": name,
        "command": command,
        "exit_code": return_code,
        "duration_seconds": duration,
        "output_tail": output[-12000:],
    }


def fixture_probe(
    name: str,
    command: list[str],
    cwd: Path,
    *,
    fixture_prefix: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, object]]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{name} failed:\n{completed.stdout}")
    fixtures: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        fixture_name, encoded = line.split("=", 1)
        if fixture_prefix is not None and not fixture_name.startswith(fixture_prefix):
            continue
        if fixture_name in fixtures:
            raise RuntimeError(f"{name} emitted duplicate fixture {fixture_name}")
        try:
            bytes.fromhex(encoded)
        except ValueError as error:
            raise RuntimeError(f"{name} emitted invalid hex for {fixture_name}") from error
        fixtures[fixture_name] = encoded
    if not fixtures:
        raise RuntimeError(f"{name} emitted no fixtures")
    return fixtures, {
        "name": name,
        "command": command,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "fixture_count": len(fixtures),
    }


def compare_wire_fixtures(
    reference: dict[str, str], candidate: dict[str, str], runtime_name: str
) -> None:
    if candidate == reference:
        return
    missing = sorted(set(reference) - set(candidate))
    extra = sorted(set(candidate) - set(reference))
    changed = sorted(
        key
        for key in set(reference) & set(candidate)
        if reference[key] != candidate[key]
    )
    raise RuntimeError(
        f"{runtime_name}/Go wire fixtures differ: "
        f"missing={missing}, extra={extra}, changed={changed}"
    )


def docker_image_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def python_fixture_probe() -> tuple[dict[str, str], dict[str, object], list[dict[str, object]]]:
    setup_runs: list[dict[str, object]] = []
    if not docker_image_exists("inventoryservice-python:local"):
        setup_runs.append(
            execute(
                "python-serde-runtime-image",
                [
                    "docker", "compose",
                    "--project-directory", str(PYTHON_EXAMPLE),
                    "--file", str(PYTHON_EXAMPLE / "docker-compose.yml"),
                    "build", "inventoryservice",
                ],
                PYTHON_EXAMPLE,
                env={**os.environ, "PYSERVICELIB_SOURCE_CONTEXT": str(PYTHON)},
            )
        )
    fixtures, run = fixture_probe(
        "python-serde-wire-probe",
        [
            "docker", "run", "--rm",
            "--volume", f"{PYTHON}:/workspace/.pyservicelib:ro",
            "--volume", f"{CONFORMANCE_DIR}:/workspace/conformance:ro",
            "--workdir", "/workspace/.pyservicelib",
            "--env", "PYTHONPATH=/workspace/.pyservicelib/src",
            "--entrypoint", "",
            "inventoryservice-python:local",
            "/workspace/.venv/bin/python",
            "/workspace/conformance/serde/python_probe.py",
        ],
        ROOT,
    )
    return fixtures, run, setup_runs


def rust_fixture_probe() -> tuple[dict[str, str], dict[str, object], list[dict[str, object]]]:
    setup_runs: list[dict[str, object]] = []
    image = "rustservicelib-toolchain:local"
    if not docker_image_exists(image):
        setup_runs.append(
            execute(
                "rust-serde-toolchain-image",
                ["docker", "build", "--target", "toolchain", "--tag", image, "."],
                RUST,
            )
        )
    fixtures, run = fixture_probe(
        "rust-serde-wire-probe",
        [
            "docker", "run", "--rm",
            "--volume", f"{RUST}:/workspace",
            "--volume", "servicelib-conformance-rust-cargo-registry:/usr/local/cargo/registry",
            "--volume", "servicelib-conformance-rust-serde-target:/workspace/target",
            "--workdir", "/workspace",
            image,
            "cargo", "run", "--quiet", "--example", "serde_wire_probe",
        ],
        RUST,
    )
    return fixtures, run, setup_runs


def json_probe(
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{name} failed:\n{completed.stdout}")
    values: dict[str, object] = {}
    type_erasure_checks: int | None = None
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        value_name, encoded = line.split("=", 1)
        if value_name == "generated_type_erasure_checks":
            try:
                type_erasure_checks = int(encoded)
            except ValueError as error:
                raise RuntimeError(
                    f"{name} emitted invalid type-erasure check count"
                ) from error
            continue
        if value_name not in {"order_item", "order_item_result", "order", "order_state"}:
            continue
        if value_name in values:
            raise RuntimeError(f"{name} emitted duplicate value {value_name}")
        try:
            values[value_name] = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{name} emitted invalid JSON for {value_name}") from error
    if set(values) != {"order_item", "order_item_result", "order", "order_state"}:
        raise RuntimeError(f"{name} emitted incomplete custom serde matrix: {sorted(values)}")
    if type_erasure_checks != 4:
        raise RuntimeError(
            f"{name} did not execute all generated type-erasure checks: "
            f"{type_erasure_checks}"
        )
    return values, {
        "name": name,
        "command": command,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "value_count": len(values),
        "generated_type_erasure_checks": type_erasure_checks,
    }


def verify_sources() -> dict[str, object]:
    canonical_test = CANONICAL / "tests" / "serde_test.cpp"
    boost_test = BOOST / "tests" / "serde_test.cpp"
    missing: dict[str, list[str]] = {}
    for path in (canonical_test, boost_test):
        source = path.read_text()
        absent = [case for case in CASES if case not in source]
        if absent:
            missing[str(path.relative_to(ROOT))] = absent
    if missing:
        raise RuntimeError(f"serde source matrix failed: {missing}")

    shared_headers = (
        Path("include/servicelib/runtime/serde/serde.hpp"),
        Path("include/servicelib/runtime/serde/serdeimpl.hpp"),
    )
    unequal = [
        str(path)
        for path in shared_headers
        if (CANONICAL / path).read_bytes() != (BOOST / path).read_bytes()
    ]
    if unequal:
        raise RuntimeError(f"canonical/Boost serde headers differ: {unequal}")
    typescript_cases = {
        TYPESCRIPT / "test/serde.test.ts": (
            "primitive serde is byte-compatible with the Go wire format",
            "string and bytes serde use uint64 framing and append semantics",
            "serde reports exact frame offsets and enforces configured limits",
            "stream serde preserves value and KeyValue framing semantics",
            "fixed-size arrays preserve the canonical packed element wire format",
            "map serde frames parallel key/value arrays and validates their cardinality",
        ),
        TYPESCRIPT / "test/schema-serde.test.ts": (
            "JSON serde validates generated runtime shape",
            "protobuf serde uses Protobuf-ES binary codecs and exact bigint fields",
            "serde registry uses runtime-validated typed keys without erased public casts",
        ),
        TYPESCRIPT / "test/stream-serde.test.ts": (
            "same-type operators retain the exact source StreamSerde instance",
            "type-changing and KeyValue operators resolve the declared output serde",
            "cycle, split, process result and virtual error outputs expose their own serde",
        ),
    }
    typescript_missing: dict[str, list[str]] = {}
    for path, cases in typescript_cases.items():
        if not path.is_file():
            typescript_missing[str(path.relative_to(ROOT))] = list(cases)
            continue
        source = path.read_text(encoding="utf-8")
        absent = [case for case in cases if case not in source]
        if absent:
            typescript_missing[str(path.relative_to(ROOT))] = absent
    if typescript_missing:
        raise RuntimeError(f"TypeScript serde source matrix failed: {typescript_missing}")
    return {
        "required_case_markers_per_cpp_runtime": len(CASES),
        "canonical_boost_headers_byte_identical": True,
        "headers": [str(path) for path in shared_headers],
        "typescript_required_case_markers": sum(
            len(cases) for cases in typescript_cases.values()
        ),
    }


def boost_serde_script(skip_build: bool) -> str:
    if skip_build:
        return (
            "ctest --test-dir build/debug --output-on-failure "
            "-R cppboostservicelib_serde_test"
        )
    source_args = cpp_source_cache.cmake_args(BOOST)
    return (
        "cmake -S . -B build/debug -G Ninja -DCMAKE_BUILD_TYPE=Debug "
        f"-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON {source_args}&& "
        "cmake --build build/debug --parallel --target "
        "cppboostservicelib_serde_test && "
        "ctest --test-dir build/debug --output-on-failure "
        "-R cppboostservicelib_serde_test && "
        "cmake -S . -B build/release -G Ninja -DCMAKE_BUILD_TYPE=Release "
        f"-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON {source_args}&& "
        "cmake --build build/release --parallel --target "
        "cppboostservicelib_serde_test && "
        "ctest --test-dir build/release --output-on-failure "
        "-R cppboostservicelib_serde_test"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    cpp_source_cache.ensure(BOOST)
    source_matrix = verify_sources()
    canonical_script = (
        "/workspace/build/servicelib_serde_test"
        if args.skip_build
        else cpp_userver.configure_script() + " && "
        "cmake --build --preset docker --parallel --target "
        "servicelib_serde_test && /workspace/build/servicelib_serde_test"
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

    boost_script = boost_serde_script(args.skip_build)
    boost_command = [
        "docker",
        "run",
        "--rm",
        *boost_source_mount_args(),
        "-v",
        f"{BOOST}:/workspace",
        *cpp_source_cache.build_volume_mount_args(
            BOOST, "cppboostservicelib-serde"
        ),
        "-w",
        "/workspace",
        "cppboostservicelib-build:local",
        "/bin/bash",
        "-lc",
        boost_script,
    ]

    canonical_env = dependency_environment.from_framework(CANONICAL)
    runs = [
        execute("canonical-cpp-serde", canonical_command, CANONICAL, canonical_env),
        execute("boost-cpp-serde", boost_command, BOOST),
    ]
    if not args.skip_build:
        runs.append(
            execute(
                "typescript-serde-dependencies",
                typescript_toolchain.install_command(),
                TYPESCRIPT,
                env=typescript_toolchain.environment(),
            )
        )
        runs.append(
            execute(
                "typescript-serde-build",
                typescript_toolchain.tsc_command(
                    "tsconfig.test.json", force=True
                ),
                TYPESCRIPT,
            )
        )
        runs.append(
            execute(
                "typescript-serde-runtime-assets",
                typescript_toolchain.copy_runtime_assets_command(),
                TYPESCRIPT,
            )
        )
    runs.append(
        execute(
            "typescript-serde",
            [
                "node", "--test", "--enable-source-maps",
                "dist-test/test/serde.test.js",
                "dist-test/test/schema-serde.test.js",
                "dist-test/test/stream-serde.test.js",
            ],
            TYPESCRIPT,
        )
    )

    go_fixtures, go_probe = fixture_probe(
        "go-serde-wire-probe",
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "GOWORK=off",
            "-e",
            "GOCACHE=/tmp/go-cache",
            *repository_mounts(),
            "-w",
            "/repo/conformance/serde/go_probe",
            go_toolchain.docker_image(ROOT),
            "go",
            "run",
            ".",
        ],
        ROOT,
    )

    python_fixtures, python_probe, python_setup_runs = python_fixture_probe()
    compare_wire_fixtures(go_fixtures, python_fixtures, "python")
    rust_fixtures, rust_probe, rust_setup_runs = rust_fixture_probe()
    compare_wire_fixtures(go_fixtures, rust_fixtures, "rust")
    typescript_fixtures, typescript_probe = fixture_probe(
        "typescript-serde-wire-probe",
        ["node", str(CONFORMANCE_DIR / "serde/typescript_probe.mjs")],
        ROOT,
        env={**os.environ, "TSSERVICELIB_ROOT": str(TYPESCRIPT)},
    )
    compare_wire_fixtures(go_fixtures, typescript_fixtures, "typescript")

    cpp_fixtures: dict[str, dict[str, str]] = {}
    probe_runs: list[dict[str, object]] = [
        *python_setup_runs,
        *rust_setup_runs,
        go_probe,
        python_probe,
        rust_probe,
        typescript_probe,
    ]
    for runtime_name, include_dir in (
        ("canonical-cpp", "/repo/cppservicelib/include"),
        ("boost-cpp", "/repo/cppboostservicelib/include"),
    ):
        binary = f"/tmp/{runtime_name}-serde-probe"
        fixtures, probe_run = fixture_probe(
            f"{runtime_name}-serde-wire-probe",
            [
                "docker",
                "run",
                "--rm",
                *repository_mounts(),
                "cppboostservicelib-build:local",
                "/bin/bash",
                "-lc",
                f"c++ -std=c++20 -I{include_dir} /repo/conformance/serde/cpp_probe.cpp -o {binary} && {binary}",
            ],
            ROOT,
        )
        cpp_fixtures[runtime_name] = fixtures
        probe_runs.append(probe_run)
        compare_wire_fixtures(go_fixtures, fixtures, runtime_name)

    canonical_custom_command = [
        "docker",
        "compose",
        "-f",
        "docker-compose.cmake.yml",
        "run",
        "--rm",
        *repository_mounts(),
        "test",
        "/bin/bash",
        "-lc",
        cpp_userver.configure_script(
            extra_args=(
                "-DCMAKE_PROJECT_INCLUDE="
                "/repo/conformance/serde/canonical_probe.cmake"
            )
        ) + " && "
        "cmake --build --preset docker --parallel --target "
        "servicelib_custom_serde_probe && /workspace/build/servicelib_custom_serde_probe",
    ]
    boost_custom_command = [
        "docker",
        "run",
        "--rm",
        *repository_mounts(),
        "cppboostservicelib-build:local",
        "/bin/bash",
        "-lc",
        "c++ -std=c++20 -DSERVICELIB_CUSTOM_SERDE_BOOST=1 "
        "-I/repo/cppboostservicelib/include -I/repo/cppboostexample "
        "/repo/conformance/serde/custom_cpp_probe.cpp -lboost_json "
        "-o /tmp/custom-serde-boost && /tmp/custom-serde-boost",
    ]
    canonical_custom, canonical_custom_run = json_probe(
        "canonical-cpp-custom-json-serde", canonical_custom_command, CANONICAL,
        canonical_env,
    )
    boost_custom, boost_custom_run = json_probe(
        "boost-cpp-custom-json-serde", boost_custom_command, BOOST
    )
    if boost_custom != canonical_custom:
        changed = sorted(
            key
            for key in set(canonical_custom) | set(boost_custom)
            if canonical_custom.get(key) != boost_custom.get(key)
        )
        raise RuntimeError(
            f"canonical/Boost custom JSON serde differs field-for-field: {changed}"
        )

    go_protobuf_setup_run: dict[str, object] | None = None
    go_protobuf_file = (
        ROOT / "goexample/inventory_service_api/pkg/generated/proto/"
        "inventoryserviceapi/processorderitem/processorderitem.pb.go"
    )
    if not go_protobuf_file.is_file():
        go_protobuf_setup_run = execute(
            "go-generated-protobuf-codegen",
            ["make", "gen-proto"],
            ROOT / "goexample",
            env={**os.environ, "GOWORK": "off"},
        )

    go_protobuf, go_protobuf_run = fixture_probe(
        "go-generated-protobuf-wire",
        [
            "docker", "run", "--rm",
            "-e", "GOWORK=off",
            "-e", "GOCACHE=/tmp/go-cache",
            *repository_mounts(),
            "-w", "/repo/conformance/serde/protobuf_go_probe",
            go_toolchain.docker_image(ROOT),
            "go", "run", ".",
        ],
        ROOT,
        fixture_prefix="protobuf_",
    )
    canonical_protobuf_compose = [
        "env", f"SERVICELIB_SOURCE_CONTEXT={CANONICAL}",
        f"USERVER_SOURCE_CONTEXT={ROOT / 'userver' if (ROOT / 'userver').is_dir() else USERVER_REMOTE_CONTEXT}",
        "docker", "compose",
        "-f", "docker-compose.cmake.generated.yml",
        "-f", str(CONFORMANCE_DIR / "serde/compose.canonical.yml"),
        "run",
    ]
    if not args.skip_build:
        canonical_protobuf_compose.append("--build")
    canonical_protobuf_compose.extend([
        "--rm", *repository_mounts(), "cpp-build",
        "/bin/bash", "-lc",
        "./scripts/conan-install.generated.sh Release /workspace/build/conan-release && "
        "conan_toolchain=$(cat /workspace/build/conan-release/toolchain.path) && "
        "trap 'cmake --preset docker-release -U CMAKE_PROJECT_INCLUDE >/dev/null' EXIT; "
        "cmake --fresh --preset docker-release -DCMAKE_TOOLCHAIN_FILE=$conan_toolchain "
        "-DCMAKE_PROJECT_INCLUDE=/repo/conformance/serde/protobuf_probe.cmake && "
        "cmake --build --preset docker-release --parallel --target "
        "servicelib_protobuf_wire_probe && "
        "/workspace/build/servicelib_protobuf_wire_probe",
    ])
    canonical_protobuf, canonical_protobuf_run = fixture_probe(
        "canonical-cpp-generated-protobuf-wire", canonical_protobuf_compose,
        ROOT / "cppexample",
        fixture_prefix="protobuf_",
        env=canonical_env,
    )

    boost_protobuf_compose = [
        "env", f"SERVICELIB_SOURCE_CONTEXT={BOOST}",
        "docker", "compose",
        "-f", "docker-compose.cmake.generated.yml",
        "-f", str(CONFORMANCE_DIR / "serde/compose.boost.yml"),
        "run",
    ]
    if not args.skip_build:
        boost_protobuf_compose.append("--build")
    boost_protobuf_compose.extend([
        "--rm", *boost_source_mount_args(), *repository_mounts(), "cpp-build",
        "/bin/bash", "-lc",
        "./scripts/conan-install.generated.sh Release /workspace/build/conan-release && "
        "conan_toolchain=$(cat /workspace/build/conan-release/toolchain.path) && "
        "trap 'cmake --preset docker-release -U CMAKE_PROJECT_INCLUDE >/dev/null' EXIT; "
        "cmake --fresh --preset docker-release -DCMAKE_TOOLCHAIN_FILE=$conan_toolchain "
        "-DCMAKE_PROJECT_INCLUDE=/repo/conformance/serde/protobuf_probe.cmake && "
        "cmake --build --preset docker-release --parallel --target "
        "servicelib_protobuf_wire_probe && "
        "/workspace/build/servicelib_protobuf_wire_probe",
    ])
    boost_protobuf, boost_protobuf_run = fixture_probe(
        "boost-cpp-generated-protobuf-wire", boost_protobuf_compose,
        ROOT / "cppboostexample",
        fixture_prefix="protobuf_",
    )
    for runtime_name, fixtures in (
        ("canonical-cpp", canonical_protobuf),
        ("boost-cpp", boost_protobuf),
    ):
        if fixtures != go_protobuf:
            missing = sorted(set(go_protobuf) - set(fixtures))
            extra = sorted(set(fixtures) - set(go_protobuf))
            changed = sorted(
                key for key in set(go_protobuf) & set(fixtures)
                if go_protobuf[key] != fixtures[key]
            )
            raise RuntimeError(
                f"generated {runtime_name}/Go deterministic protobuf fixtures differ: "
                f"missing={missing}, extra={extra}, changed={changed}"
            )

    summary = {
        "status": "pass",
        "languages": [
            "go", "canonical-cpp", "cppboost", "python", "rust", "typescript",
        ],
        "source_matrix": source_matrix,
        "runs": runs,
        "wire_fixture_runs": probe_runs,
        "wire_fixtures": go_fixtures,
        "wire_fixture_languages": [
            "go", "canonical-cpp", "boost-cpp", "python", "rust", "typescript",
        ],
        "custom_json_serde_runs": [canonical_custom_run, boost_custom_run],
        "custom_json_values": canonical_custom,
        "custom_json_comparison": "field-for-field",
        "custom_json_generated_factory": "MakeDefaultSerde<T>",
        "custom_json_type_erasure": (
            "Serializer SerializeObj/DeserializeObj, deterministic round-trip, "
            "wrong std::any type rejection"
        ),
        "generated_protobuf_runs": [
            *([go_protobuf_setup_run] if go_protobuf_setup_run else []),
            go_protobuf_run,
            canonical_protobuf_run,
            boost_protobuf_run,
        ],
        "generated_protobuf_languages": [
            "go", "canonical-cpp", "boost-cpp",
        ],
        "generated_protobuf_fixtures": go_protobuf,
        "generated_protobuf_comparison": "deterministic-byte-for-byte",
        "unrestricted_build_parallelism": True,
        "scope": (
            "cross-language deterministic primitive/container wire contract, "
            "generated JSON factory/type-erasure/values and generated protobuf "
            "wire contract"
        ),
        "boost_build_profiles": ["debug"] if args.skip_build else ["debug", "release"],
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"Serde conformance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
