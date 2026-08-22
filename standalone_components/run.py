#!/usr/bin/env python3
"""Build generated components from isolated local, non-Git repositories.

The production manifests intentionally retain their public package identities
and pinned Git fallbacks.  This runner materializes a development-only local
workspace and applies the normal local override mechanism of each language.
No generated component has to be published before this gate can run.
"""

from __future__ import annotations

import argparse
import atexit
from collections import deque
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


CONFORMANCE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CONFORMANCE))

import cpp_source_cache  # noqa: E402


DEFAULT_ROOT = Path(
    os.environ.get("CONFORMANCE_STANDALONE_COMPONENTS_ROOT")
    or os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "standalone-components"
SUMMARY = ARTIFACTS / "summary.json"
DIAGNOSTIC_SUMMARY = ARTIFACTS / "diagnostic-summary.json"
RUST_TOOLCHAIN_IMAGE = "servicelib-standalone-rust:1.97"
GO_TOOLCHAIN_IMAGE = "servicelib-standalone-go:1.25.4"
RUN_ID = f"servicegen-standalone-{os.getpid()}-{int(time.time())}"
ACTIVE_CONTAINERS: set[str] = set()

SERVICES = ("analyticsservice", "inventoryservice", "orderservice")
MODULES = ("inventory_service_api", "model", "order_service_api")
COMPONENTS = SERVICES + MODULES
DECLARED_MODULES: dict[str, tuple[str, ...]] = {
    "analyticsservice": ("model",),
    "inventoryservice": ("inventory_service_api", "model"),
    "orderservice": (
        "inventory_service_api",
        "model",
        "order_service_api",
    ),
    "inventory_service_api": (),
    "model": (),
    "order_service_api": (),
}


@dataclass(frozen=True)
class Language:
    name: str
    example: str
    framework: str


LANGUAGES: dict[str, Language] = {
    "go": Language("go", "goexample", "servicelib"),
    "cpp": Language("cpp", "cppexample", "cppservicelib"),
    "cppboost": Language("cppboost", "cppboostexample", "cppboostservicelib"),
    "python": Language("python", "pyexample", "pyservicelib"),
    "rust": Language("rust", "rustexample", "rustservicelib"),
    "typescript": Language("typescript", "tsexample", "tsservicelib"),
}

IGNORED_NAMES = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "dist-test",
    "node_modules",
    "target",
    "tools",
}


def ignore_copy(directory: str, names: list[str]) -> set[str]:
    del directory
    return {
        name
        for name in names
        if name in IGNORED_NAMES
        or name.endswith(".pyc")
        or name.endswith(".pyo")
        or name.endswith(".tsbuildinfo")
    }


def copy_source(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f"local source directory is missing: {source}")
    shutil.copytree(
        source,
        destination,
        ignore=ignore_copy,
        symlinks=False,
    )


def assert_plain_filesystem_tree(root: Path) -> None:
    git_entries = [path for path in root.rglob(".git")]
    if git_entries:
        rendered = ", ".join(str(path.relative_to(root)) for path in git_entries)
        raise RuntimeError(f"isolated local tree contains Git metadata: {rendered}")


def replace_exact(text: str, old: str, new: str, path: Path) -> str:
    if old not in text:
        raise RuntimeError(f"expected local-override source is absent in {path}: {old}")
    return text.replace(old, new)


def component_package_name(language: str, component_root: Path) -> str:
    if language == "rust":
        match = re.search(
            r'(?ms)^\[package\].*?^name\s*=\s*"([^"]+)"',
            (component_root / "Cargo.toml").read_text(),
        )
        if match:
            return match.group(1)
    if language == "typescript":
        return str(json.loads((component_root / "package.json").read_text())["name"])
    return component_root.name


def materialize_go(root: Path, language: Language, component: str, target: Path) -> None:
    example = root / language.example
    copy_source(example / component, target / component)
    for dependency in DECLARED_MODULES[component]:
        copy_source(example / dependency, target / dependency)
    copy_source(root / language.framework, target / language.framework)

    uses = [component, *DECLARED_MODULES[component], language.framework]
    go_version = "1.25.4"
    source_work = example / "go.work"
    if source_work.is_file():
        match = re.search(r"(?m)^go\s+(\S+)", source_work.read_text())
        if match:
            go_version = match.group(1)
    body = "\n".join(f"\t./{entry}" for entry in uses)
    (target / "go.work").write_text(f"go {go_version}\n\nuse (\n{body}\n)\n")


def materialize_cpp(root: Path, language: Language, component: str, target: Path) -> None:
    example = root / language.example
    copy_source(example / component, target / component)
    for dependency in DECLARED_MODULES[component]:
        copy_source(example / dependency, target / dependency)


def materialize_python(
    root: Path,
    language: Language,
    component: str,
    target: Path,
) -> None:
    example = root / language.example
    component_root = target / component
    copy_source(example / component, component_root)
    dependencies = component_root / ".servicegen" / "dependencies"
    dependencies.mkdir(parents=True)
    for dependency in DECLARED_MODULES[component]:
        copy_source(example / dependency, dependencies / dependency)

    if component in SERVICES:
        standalone = component_root / "pyproject.standalone.generated.toml"
        if not standalone.is_file():
            raise RuntimeError(f"standalone Python manifest is missing: {standalone}")
        manifest = standalone.read_text()
        framework_target = dependencies / language.framework
        copy_source(root / language.framework, framework_target)
        manifest = re.sub(
            r'pyservicelib-gorundebug\s*=\s*\{\s*git\s*=\s*"[^"]+",\s*tag\s*=\s*"[^"]+"\s*\}',
            'pyservicelib-gorundebug = { path = ".servicegen/dependencies/pyservicelib" }',
            manifest,
            count=1,
        )
        if 'path = ".servicegen/dependencies/pyservicelib"' not in manifest:
            raise RuntimeError("Python framework Git source was not replaced locally")
        (component_root / "pyproject.toml").write_text(manifest)


def materialize_rust(root: Path, language: Language, component: str, target: Path) -> None:
    example = root / language.example
    copy_source(example / component, target / component)
    for dependency in DECLARED_MODULES[component]:
        copy_source(example / dependency, target / dependency)
    if component in SERVICES:
        copy_source(root / language.framework, target / language.framework)
        manifest_path = target / component / "Cargo.toml"
        manifest = manifest_path.read_text()
        manifest, replacements = re.subn(
            r'servicelib-gorundebug\s*=\s*\{\s*git\s*=\s*"[^"]+",\s*tag\s*=\s*"[^"]+"\s*\}',
            'servicelib-gorundebug = { path = "../rustservicelib" }',
            manifest,
            count=1,
        )
        if replacements != 1:
            raise RuntimeError("Rust framework Git source was not replaced locally")
        manifest_path.write_text(manifest)

    workspace_source = (example / "Cargo.toml").read_text()
    members = [component, *DECLARED_MODULES[component]]
    workspace_source = re.sub(
        r'(?ms)^members\s*=\s*\[.*?^\]',
        "members = [\n" + "".join(f'    "{item}",\n' for item in members) + "]",
        workspace_source,
        count=1,
    )
    (target / "Cargo.toml").write_text(workspace_source)
    lock = example / "Cargo.lock"
    if lock.is_file():
        shutil.copy2(lock, target / "Cargo.lock")


def materialize_typescript(
    root: Path,
    language: Language,
    component: str,
    target: Path,
) -> None:
    example = root / language.example
    component_root = target / component
    copy_source(example / component, component_root)
    for dependency in DECLARED_MODULES[component]:
        copy_source(example / dependency, target / dependency)
    if component in SERVICES:
        copy_source(root / language.framework, target / language.framework)
        standalone = component_root / "package.standalone.generated.json"
        if not standalone.is_file():
            raise RuntimeError(f"standalone TypeScript manifest is missing: {standalone}")
        package = json.loads(standalone.read_text())
        local_names: dict[str, str] = {}
        for directory in (*DECLARED_MODULES[component], language.framework):
            dependency_package = json.loads((target / directory / "package.json").read_text())
            local_names[str(dependency_package["name"])] = "workspace:*"
        for name, version in local_names.items():
            if name not in package.get("dependencies", {}):
                raise RuntimeError(
                    f"TypeScript component {component} does not declare {name}"
                )
            package["dependencies"][name] = version
        (component_root / "package.json").write_text(
            json.dumps(package, indent=2, sort_keys=True) + "\n"
        )

    root_package = example / "package.json"
    if root_package.is_file():
        shutil.copy2(root_package, target / "package.json")
    workspace = [component, *DECLARED_MODULES[component]]
    if component in SERVICES:
        workspace.append(language.framework)
    (target / "pnpm-workspace.yaml").write_text(
        "packages:\n" + "".join(f'  - "{item}"\n' for item in workspace)
        + "\nallowBuilds:\n"
        + "  '@confluentinc/kafka-javascript': true\n"
        + "  esbuild: true\n"
        + "  protobufjs: false\n"
    )


MATERIALIZERS = {
    "go": materialize_go,
    "cpp": materialize_cpp,
    "cppboost": materialize_cpp,
    "python": materialize_python,
    "rust": materialize_rust,
    "typescript": materialize_typescript,
}


def materialize_component(
    root: Path,
    language_name: str,
    component: str,
    target: Path,
) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    language = LANGUAGES[language_name]
    MATERIALIZERS[language_name](root, language, component, target)
    assert_plain_filesystem_tree(target)


def print_command(command: Sequence[str], cwd: Path) -> None:
    rendered = " ".join(command)
    print(f"- ({cwd}) {rendered}", flush=True)


def run_command(
    command: Sequence[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    cleanup_container: str | None = None,
    log_name: str | None = None,
) -> None:
    print_command(command, cwd)
    safe_log_name = re.sub(
        r"[^A-Za-z0-9_.-]", "-",
        log_name or cleanup_container or f"command-{time.monotonic_ns()}",
    )
    log_dir = ARTIFACTS / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{safe_log_name}.log"
    tail: deque[str] = deque(maxlen=80)
    if cleanup_container is not None:
        ACTIVE_CONTAINERS.add(cleanup_container)
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout is None:
            raise RuntimeError("command output pipe was not created")
        with log_path.open("w") as log:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
                tail.append(line)
        return_code = process.wait()
    finally:
        if cleanup_container is not None:
            remove_container(cleanup_container)
    if return_code != 0:
        raise RuntimeError(
            f"command failed ({return_code}): {' '.join(command)}\n"
            f"full log: {log_path}\n"
            f"last output:\n{''.join(tail).rstrip()}"
        )


def container_name(language: str, component: str, phase: str = "build") -> str:
    return f"{RUN_ID}-{language}-{component}-{phase}".replace("_", "-")


def remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "--force", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    ACTIVE_CONTAINERS.discard(name)


def cleanup_containers() -> None:
    for name in tuple(ACTIVE_CONTAINERS):
        remove_container(name)


atexit.register(cleanup_containers)


def docker_mount(path: Path, destination: str, *, read_only: bool = False) -> str:
    suffix = ":ro" if read_only else ""
    return f"{path.resolve()}:{destination}{suffix}"


def build_go(target: Path, component: str) -> None:
    generation_targets = [*DECLARED_MODULES[component]]
    if component in MODULES:
        generation_targets.append(component)
    generation = " && ".join(
        f"make -C /workspace/{item} all "
        "TOOLS_DIR=/usr/local/bin PROTOC=/usr/local/bin/protoc "
        "OAPI_CODEGEN=/usr/local/bin/oapi-codegen"
        for item in generation_targets
    )
    service_generation = ""
    if component in SERVICES:
        service_generation = (
            f"make -C /workspace/{component} gen-proto "
            "TOOLS_DIR=/usr/local/bin PROTOC=/usr/local/bin/protoc"
        )
    phases = [value for value in (generation, service_generation, "go test ./...") if value]
    script = " && ".join(phases)
    name = container_name("go", component)
    run_command(
        [
            "docker", "run", "--rm",
            "--name", name,
            "-e", "GOWORK=/workspace/go.work",
            "-e", "GOCACHE=/root/.cache/go-build",
            "-e", "GOMODCACHE=/go/pkg/mod",
            "-v", docker_mount(target, "/workspace"),
            "-v", "standalone-components-go-build:/root/.cache/go-build",
            "-v", "standalone-components-go-modules:/go/pkg/mod",
            "-w", f"/workspace/{component}",
            GO_TOOLCHAIN_IMAGE, "/bin/bash", "-c", script,
        ],
        target,
        cleanup_container=name,
    )


def build_python(target: Path, component: str) -> None:
    component_root = target / component

    sync_name = container_name("python", component, "sync")
    prefix = [
        "docker", "run", "--rm",
        "--name", sync_name,
        "-e", "UV_CACHE_DIR=/root/.cache/uv",
        "-e", "UV_LINK_MODE=copy",
        "-v", docker_mount(component_root, "/workspace"),
        "-v", "standalone-components-uv:/root/.cache/uv",
        "-w", "/workspace",
        "ghcr.io/astral-sh/uv:python3.12-bookworm-slim",
    ]
    if component in SERVICES:
        run_command(
            [
                *prefix,
                "/bin/bash", "-lc",
                "SERVICEGEN_LOCAL_DEPENDENCIES_DIR="
                "/workspace/.servicegen/dependencies "
                "./scripts/fetch-dependencies.generated.sh && "
                "uv sync --all-extras",
            ],
            component_root,
            cleanup_container=sync_name,
        )
    else:
        run_command(
            [*prefix, "uv", "sync", "--all-extras"],
            component_root,
            cleanup_container=sync_name,
        )
    own_generator = component_root / "generate.generated.sh"
    if own_generator.is_file():
        own_generate_name = container_name("python", component, "generate")
        own_prefix = list(prefix)
        own_prefix[4] = own_generate_name
        run_command(
            [
                *own_prefix,
                "/bin/bash", "-lc",
                "PYTHON=.venv/bin/python ./generate.generated.sh",
            ],
            component_root,
            cleanup_container=own_generate_name,
        )
    test_name = container_name("python", component, "test")
    prefix[4] = test_name
    if component in SERVICES:
        run_command(
            [*prefix, "uv", "run", "pytest", "tests"],
            component_root,
            cleanup_container=test_name,
        )
    else:
        run_command(
            [
                *prefix, "uv", "run", "python", "-m", "compileall", "-q",
                "src",
            ],
            component_root,
            cleanup_container=test_name,
        )


def build_rust(target: Path, component: str) -> None:
    package = component_package_name("rust", target / component)
    generation_targets = [*DECLARED_MODULES[component], component]
    generation = " && ".join(
        f"make -C /workspace/{item} generate"
        for item in generation_targets
    )
    script = " && ".join([
        generation,
        " ".join(["cargo", "test", "-p", package, "--all-targets"]),
    ])
    name = container_name("rust", component)
    run_command(
        [
            "docker", "run", "--rm",
            "--name", name,
            "-v", docker_mount(target, "/workspace"),
            "-v", "standalone-components-cargo-registry:/usr/local/cargo/registry",
            "-v", "standalone-components-cargo-git:/usr/local/cargo/git",
            "-v", "standalone-components-rust-target:/workspace/target",
            "-w", "/workspace",
            RUST_TOOLCHAIN_IMAGE, "/bin/bash", "-c", script,
        ],
        target,
        cleanup_container=name,
    )


def build_typescript(target: Path, component: str) -> None:
    package = component_package_name("typescript", target / component)
    script = (
        "corepack enable && "
        "corepack pnpm config set store-dir /pnpm/store && "
        "corepack pnpm install --no-frozen-lockfile && "
        f"corepack pnpm --filter {package}... build && "
        f"corepack pnpm --filter {package} test"
    )
    name = container_name("typescript", component)
    run_command(
        [
            "docker", "run", "--rm",
            "--name", name,
            "-e", "CI=true",
            "-v", docker_mount(target, "/workspace"),
            "-v", "standalone-components-pnpm:/pnpm/store",
            "-w", "/workspace",
            "node:24.19.0-bookworm-slim",
            "/bin/bash", "-lc", script,
        ],
        target,
        cleanup_container=name,
    )


CppContext = tuple[Path, list[str], dict[str, str], Path | None]


def configure_userver_source_context(
    environment: dict[str, str], root: Path,
) -> None:
    userver = root / "userver"
    if userver.is_dir():
        environment["USERVER_SOURCE_CONTEXT"] = str(userver)
    else:
        environment.pop("USERVER_SOURCE_CONTEXT", None)


def ensure_cpp_image(root: Path, language_name: str) -> CppContext:
    language = LANGUAGES[language_name]
    example = root / language.example
    compose = ["docker", "compose", "-f", "docker-compose.cmake.generated.yml"]
    env = {
        "SERVICELIB_SOURCE_CONTEXT": str(root / language.framework),
        "SERVICEGEN_FETCH_CPP_DEPENDENCIES": "OFF",
    }
    source_cache: Path | None = None
    if language_name == "cppboost":
        source_cache = cpp_source_cache.configure_environment(
            env, root / language.framework, "cppboostexample",
        )
    else:
        configure_userver_source_context(env, root)
    run_command(
        [*compose, "build", "cpp-build"], example, env=env,
        log_name=f"{language_name}-toolchain",
    )
    return example, compose, env, source_cache


def ensure_rust_image() -> None:
    run_command(
        [
            "docker", "build",
            "-f", str(Path(__file__).with_name("Dockerfile.rust")),
            "-t", RUST_TOOLCHAIN_IMAGE,
            str(Path(__file__).parent),
        ],
        CONFORMANCE,
        log_name="rust-toolchain",
    )


def ensure_go_image() -> None:
    run_command(
        [
            "docker", "build",
            "-f", str(Path(__file__).with_name("Dockerfile.go")),
            "-t", GO_TOOLCHAIN_IMAGE,
            str(Path(__file__).parent),
        ],
        CONFORMANCE,
        log_name="go-toolchain",
    )


def build_cpp(
    root: Path,
    target: Path,
    language_name: str,
    component: str,
    cpp_context: CppContext,
) -> None:
    example, compose, env, source_cache = cpp_context
    build_dir = f"/workspace/build/standalone-components/{component}"
    definitions = [
        "-DSERVICEGEN_MODULES_ROOT=/standalone",
        "-DSERVICEGEN_FETCH_CPP_DEPENDENCIES=OFF",
    ]
    if language_name == "cpp":
        definitions.extend([
            "-DSERVICELIB_SOURCE_DIR=/opt/servicelib",
            "-DUSERVER_SOURCE_DIR=/opt/userver",
        ])
    else:
        definitions.extend([
            "-DCPPBOOSTSERVICELIB_SOURCE_DIR=/opt/servicelib",
            "-DCPPBOOSTSERVICELIB_DEPENDENCY_MODE=FETCH",
        ])
        definitions.append(cpp_source_cache.cmake_args(
            root / LANGUAGES[language_name].framework
        ).strip())
    service_targets = {
        "analyticsservice": (
            "example_analytics_service",
            "example_analytics_service_typed_config",
            "example_analytics_service_unit_tests",
        ),
        "inventoryservice": (
            "example_inventory_service",
            "example_inventory_service_typed_config",
            "example_inventory_service_unit_tests",
        ),
        "orderservice": (
            "example_order_service",
            "example_order_service_typed_config",
            "example_order_service_unit_tests",
        ),
    }
    build_targets = service_targets.get(component)
    if component == "inventory_service_api":
        build_targets = ("example_inventory_service_api",)
    build_command = f"cmake --build {build_dir} --parallel"
    if build_targets:
        build_command = (
            f"cmake --build {build_dir} --target "
            f"{' '.join(build_targets)} --parallel"
        )
    commands = [
        f"cmake -S /standalone/{component} -B {build_dir} -G Ninja",
        "-DCMAKE_BUILD_TYPE=Debug",
        *definitions,
        "&&", build_command,
    ]
    if component in SERVICES:
        commands.extend([
            "&&", f"ctest --test-dir {build_dir} --output-on-failure",
        ])
    script = " ".join(commands)
    command = [
        *compose,
        "run", "--rm", "--no-deps",
        "--name", container_name(language_name, component),
        "-v", docker_mount(target, "/standalone", read_only=True),
    ]
    if source_cache is not None:
        command.extend([
            "-v", f"{source_cache}:{cpp_source_cache.CONTAINER_SOURCE_DIR}:ro",
        ])
    command.extend(["cpp-build", "/bin/bash", "-lc", script])
    name = container_name(language_name, component)
    run_command(
        command,
        example,
        env=env,
        cleanup_container=name,
    )


BUILDERS = {
    "go": build_go,
    "python": build_python,
    "rust": build_rust,
    "typescript": build_typescript,
}


def selected(values: Iterable[str] | None, available: Iterable[str]) -> list[str]:
    return list(values) if values else list(available)


def write_summary(value: dict[str, object], destination: Path = SUMMARY) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--language", action="append", choices=sorted(LANGUAGES))
    parser.add_argument("--component", action="append", choices=COMPONENTS)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="materialize and validate isolated no-Git trees without compiling",
    )
    parser.add_argument("--keep-workspaces", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.local_root.expanduser().resolve()
    languages = selected(args.language, LANGUAGES)
    components = selected(args.component, COMPONENTS)
    if not root.is_dir():
        raise RuntimeError(f"local component repository root is missing: {root}")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    workspace_parent: Path | None = None
    if args.keep_workspaces:
        workspace_parent = ARTIFACTS / "workspaces"
        workspace_parent.mkdir(parents=True, exist_ok=True)
    else:
        # macOS exposes the same temporary directory through both /var and
        # /private/var. Go compares the absolute GOWORK module roots, so keep
        # one canonical spelling for both cwd and the workspace file.
        workspace_parent = Path(
            tempfile.mkdtemp(prefix="servicegen-standalone-")
        ).resolve()

    started = time.monotonic()
    matrix: dict[str, dict[str, object]] = {}
    failures: list[str] = []
    cpp_contexts: dict[str, CppContext] = {}
    try:
        if not args.prepare_only and "go" in languages:
            try:
                ensure_go_image()
            except Exception as error:  # noqa: BLE001
                failures.append(f"go:toolchain: {error}")
                print(f"[standalone] FAIL go:toolchain: {error}", file=sys.stderr)
                languages = [name for name in languages if name != "go"]
        if not args.prepare_only and "rust" in languages:
            try:
                ensure_rust_image()
            except Exception as error:  # noqa: BLE001
                failures.append(f"rust:toolchain: {error}")
                print(f"[standalone] FAIL rust:toolchain: {error}", file=sys.stderr)
                languages = [name for name in languages if name != "rust"]
        for language_name in languages:
            language_results: dict[str, object] = {}
            matrix[language_name] = language_results
            if not args.prepare_only and language_name in {"cpp", "cppboost"}:
                try:
                    cpp_contexts[language_name] = ensure_cpp_image(root, language_name)
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{language_name}:toolchain: {error}")
                    print(f"[standalone] FAIL {language_name}:toolchain: {error}", file=sys.stderr)
                    continue
            for component in components:
                label = f"{language_name}:{component}"
                target = workspace_parent / language_name / component
                print(f"[standalone] START {label}", flush=True)
                item: dict[str, object] = {
                    "declared_local_modules": list(DECLARED_MODULES[component]),
                    "git_required": False,
                    "workspace": str(target),
                }
                language_results[component] = item
                item_started = time.monotonic()
                try:
                    materialize_component(root, language_name, component, target)
                    item["materialized"] = True
                    if not args.prepare_only:
                        if language_name in {"cpp", "cppboost"}:
                            build_cpp(
                                root,
                                target,
                                language_name,
                                component,
                                cpp_contexts[language_name],
                            )
                        else:
                            BUILDERS[language_name](target, component)
                        item["build_test"] = "pass"
                    else:
                        item["build_test"] = "not-run"
                    item["status"] = "pass"
                    print(f"[standalone] PASS  {label}", flush=True)
                except Exception as error:  # noqa: BLE001
                    item["status"] = "fail"
                    item["error"] = str(error)
                    failures.append(f"{label}: {error}")
                    print(f"[standalone] FAIL  {label}: {error}", file=sys.stderr, flush=True)
                item["elapsed_seconds"] = round(time.monotonic() - item_started, 3)
    finally:
        if not args.keep_workspaces and workspace_parent is not None:
            shutil.rmtree(workspace_parent, ignore_errors=True)

    authoritative = (
        not args.prepare_only
        and set(languages) == set(LANGUAGES)
        and set(components) == set(COMPONENTS)
    )
    summary: dict[str, object] = {
        "status": (
            "fail" if failures else "pass" if authoritative else "diagnostic"
        ),
        "authoritative": authoritative,
        "local_root": str(root),
        "git_required": False,
        "prepare_only": bool(args.prepare_only),
        "languages": languages,
        "components": components,
        "matrix": matrix,
        "failures": failures,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    write_summary(summary, SUMMARY if authoritative else DIAGNOSTIC_SUMMARY)
    if failures:
        raise RuntimeError("standalone component conformance failed:\n" + "\n".join(failures))
    result_kind = "conformance" if authoritative else "diagnostic"
    print(
        f"Standalone component {result_kind} passed: {len(languages)} languages, "
        f"{len(components)} components each; local Git repositories were not required",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
