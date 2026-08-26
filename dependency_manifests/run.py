#!/usr/bin/env python3
"""Fail early when checked-in dependency manifests and lockfiles are stale."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path


CONFORMANCE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_ROOT.parent)
).expanduser().resolve()
ARTIFACT = CONFORMANCE_ROOT / ".artifacts" / "dependency-manifests" / "summary.json"
GO_PROJECT_MODULES = {
    "goexample": (
        "analyticsservice", "automationservice", "inventory_service_api",
        "inventoryservice", "model", "order_service_api", "orderservice",
    ),
    "gonativeexample": (".",),
    "cppexample": ("automationservice", "inventory_service_api", "model", "order_service_api"),
    "cppboostexample": ("automationservice", "inventory_service_api", "model", "order_service_api"),
    "rustexample": ("automationservice", "inventory_service_api", "model", "order_service_api"),
    "servicegen": (".",),
    "servicelib": (".",),
    "tsservicelib": ("test/interop",),
}
GO_GENERATED_SOURCE_PROBES = {
    "goexample": (
        "inventory_service_api/pkg/generated/proto/inventoryserviceapi/"
        "inventoryserviceapi.generated.pb.go",
        "order_service_api/pkg/generated/openapi/orderserviceapi/"
        "orderserviceapi_http.openapi.go",
    ),
}
RUST_PROJECTS = {
    "rustservicelib": (),
    "rustnativeexample": (),
    "rustexample": (
        "--config",
        'patch."https://github.com/gorundebug/rustservicelib.git".'
        'servicelib-gorundebug.path="../rustservicelib"',
    ),
}
RUST_TOOLCHAIN_IMAGE = os.environ.get(
    "SERVICEGEN_RUST_TOOLCHAIN_IMAGE", "rust:1.97-bookworm"
)
PYTHON_PROJECTS = ("pyservicelib", "pyexample")
TYPESCRIPT_PROJECTS = ("tsservicelib", "tsnativeexample", "tsexample")
IGNORED_PARTS = {
    ".artifacts", ".git", ".venv", "build", "dist", "node_modules", "target",
}


def module_directories(project: Path) -> list[Path]:
    return sorted(
        path.parent
        for path in project.rglob("go.mod")
        if not any(part in IGNORED_PARTS for part in path.relative_to(project).parts)
    )


def path_for_diagnostic(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise RuntimeError(
            f"dependency manifest check failed in {path_for_diagnostic(cwd, ROOT)}\n"
            f"command: {' '.join(command)}\n{result.stdout}"
        )
    return result.stdout


def missing_generated_source_probes(project: Path) -> list[Path]:
    return [
        project / relative
        for relative in GO_GENERATED_SOURCE_PROBES.get(project.name, ())
        if not (project / relative).is_file()
    ]


def ensure_go_generated_sources(project: Path, cache: Path) -> None:
    missing = missing_generated_source_probes(project)
    if not missing:
        return
    print(
        f"[dependency-manifests] generating missing transport sources for "
        f"{project.name}",
        flush=True,
    )
    env = os.environ.copy()
    env["GOCACHE"] = str(cache)
    run(
        ["make", "golang-gen-proto", "golang-gen-openapi"],
        cwd=project, env=env,
    )
    missing = missing_generated_source_probes(project)
    if missing:
        rendered = ", ".join(str(path.relative_to(ROOT)) for path in missing)
        raise RuntimeError(f"Go source generation did not create: {rendered}")


def resolved_workspace_modules(
    modules: list[Path], framework: Path,
) -> list[Path]:
    """Return physical module paths for a disposable Go workspace.

    The ``current`` profile symlinks unchanged repositories into its temporary
    dependency root. Go resolves the command working directory through those
    symlinks, so a ``go.work`` containing the lexical symlink path does not
    consider that same module part of the workspace. Always writing physical
    paths keeps the workspace and ``go list`` identity identical.
    """
    result = [path.resolve() for path in modules]
    resolved_framework = framework.resolve()
    if framework.is_dir() and resolved_framework not in result:
        result.append(resolved_framework)
    return result


def check_go_project(
    project: Path, module_paths: tuple[str, ...], framework: Path, cache: Path,
) -> dict[str, object]:
    ensure_go_generated_sources(project, cache)
    modules = [project / relative for relative in module_paths]
    missing = [path / "go.mod" for path in modules if not (path / "go.mod").is_file()]
    if missing:
        rendered = ", ".join(str(path.relative_to(ROOT)) for path in missing)
        raise RuntimeError(f"required Go manifests are missing: {rendered}")
    workspace_modules = resolved_workspace_modules(modules, framework)
    with tempfile.TemporaryDirectory(prefix="servicegen-manifest-work-") as temporary:
        work_root = Path(temporary)
        base_env = os.environ.copy()
        base_env["GOWORK"] = "off"
        run(
            ["go", "work", "init", *(str(path) for path in workspace_modules)],
            cwd=work_root, env=base_env,
        )
        env = os.environ.copy()
        env["GOWORK"] = str(work_root / "go.work")
        env["GOCACHE"] = str(cache)
        packages = 0
        for module in modules:
            output = run(
                ["go", "list", "-mod=readonly", "./..."],
                cwd=module.resolve(), env=env,
            )
            packages += len([line for line in output.splitlines() if line.strip()])
    return {
        "project": project.name,
        "modules": [str(path.relative_to(project)) or "." for path in modules],
        "packages": packages,
    }


def check_rust_project(project: Path, cargo_options: tuple[str, ...]) -> dict[str, str]:
    for name in ("Cargo.toml", "Cargo.lock"):
        manifest = project / name
        if not manifest.is_file():
            raise RuntimeError(
                f"required Rust manifest is missing: {manifest.relative_to(ROOT)}"
            )
    command = [
        "docker", "run", "--rm",
        "-v", f"{ROOT}:/repo:ro",
        "-w", f"/repo/{project.relative_to(ROOT)}",
        RUST_TOOLCHAIN_IMAGE,
        "cargo", *cargo_options,
        "metadata", "--locked", "--offline", "--no-deps", "--format-version", "1",
    ]
    run(command, cwd=CONFORMANCE_ROOT, env=os.environ.copy())
    return {"project": project.name, "lockfile": "Cargo.lock"}


def normalize_python_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def python_requirement(value: str) -> tuple[str, str]:
    requirement = value.split(";", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?(.*)$", requirement)
    if match is None:
        raise RuntimeError(f"unsupported Python requirement: {value!r}")
    return normalize_python_name(match.group(1)), match.group(2).replace(" ", "")


def python_manifest_dependencies(manifest: Path, data: dict[str, object]) -> set[tuple[str, str]]:
    project = data.get("project")
    if not isinstance(project, dict):
        return set()
    raw = project.get("dependencies", [])
    dependencies = list(raw) if isinstance(raw, list) else []
    dynamic = project.get("dynamic", [])
    if isinstance(dynamic, list) and "dependencies" in dynamic:
        requirements = manifest.parent / "requirements.txt"
        if not requirements.is_file():
            raise RuntimeError(f"dynamic Python dependencies have no {requirements}")
        dependencies.extend(
            line.strip() for line in requirements.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for values in optional.values():
            if isinstance(values, list):
                dependencies.extend(values)
    return {python_requirement(value) for value in dependencies if isinstance(value, str)}


def check_python_project(project: Path) -> dict[str, object]:
    lock_path = project / "uv.lock"
    if not lock_path.is_file():
        raise RuntimeError(f"required Python lockfile is missing: {lock_path.relative_to(ROOT)}")
    lock = tomllib.loads(lock_path.read_text())
    packages = lock.get("package", [])
    locked: dict[str, set[tuple[str, str]]] = {}
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, dict) or not isinstance(package.get("name"), str):
                continue
            metadata = package.get("metadata", {})
            values = metadata.get("requires-dist", []) if isinstance(metadata, dict) else []
            requirements: set[tuple[str, str]] = set()
            if isinstance(values, list):
                for value in values:
                    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
                        continue
                    requirements.add((
                        normalize_python_name(value["name"]),
                        str(value.get("specifier", "")).replace(" ", ""),
                    ))
            locked[normalize_python_name(package["name"])] = requirements
    checked: list[str] = []
    for manifest in sorted(project.rglob("pyproject.toml")):
        relative = manifest.relative_to(project)
        if any(part.startswith(".") or part in IGNORED_PARTS for part in relative.parts):
            continue
        data = tomllib.loads(manifest.read_text())
        package = data.get("project")
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            continue
        name = normalize_python_name(package["name"])
        expected = python_manifest_dependencies(manifest, data)
        if name not in locked:
            raise RuntimeError(f"{manifest.relative_to(ROOT)} is absent from {lock_path.relative_to(ROOT)}")
        if expected != locked[name]:
            raise RuntimeError(
                f"{lock_path.relative_to(ROOT)} is stale for {manifest.relative_to(ROOT)}: "
                f"expected={sorted(expected)!r}, locked={sorted(locked[name])!r}"
            )
        checked.append(str(relative))
    return {"project": project.name, "manifests": checked, "lockfile": "uv.lock"}


def yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return json.loads(value)
    return value


def pnpm_importer_specifiers(text: str) -> dict[str, dict[str, str]]:
    importers: dict[str, dict[str, str]] = {}
    in_importers = False
    importer: str | None = None
    dependency: str | None = None
    for line in text.splitlines():
        if line == "importers:":
            in_importers = True
            continue
        if not in_importers:
            continue
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([^ ].*):$", line)
        if match:
            importer = yaml_scalar(match.group(1))
            importers[importer] = {}
            dependency = None
            continue
        match = re.match(r"^      ([^ ].*):$", line)
        if match and importer is not None:
            dependency = yaml_scalar(match.group(1))
            continue
        match = re.match(r"^        specifier: (.+)$", line)
        if match and importer is not None and dependency is not None:
            importers[importer][dependency] = yaml_scalar(match.group(1))
    return importers


def check_typescript_project(project: Path) -> dict[str, object]:
    lock_path = project / "pnpm-lock.yaml"
    if not lock_path.is_file():
        raise RuntimeError(f"required TypeScript lockfile is missing: {lock_path.relative_to(ROOT)}")
    locked = pnpm_importer_specifiers(lock_path.read_text())
    checked: list[str] = []
    for manifest in sorted(project.rglob("package.json")):
        relative = manifest.relative_to(project)
        if any(part.startswith(".") or part in IGNORED_PARTS for part in relative.parts):
            continue
        package = json.loads(manifest.read_text())
        expected: dict[str, str] = {}
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            values = package.get(key, {})
            if isinstance(values, dict):
                expected.update({str(name): str(specifier) for name, specifier in values.items()})
        importer = str(relative.parent) if relative.parent != Path(".") else "."
        if importer not in locked:
            raise RuntimeError(f"{manifest.relative_to(ROOT)} has no pnpm lock importer")
        if expected != locked[importer]:
            raise RuntimeError(
                f"{lock_path.relative_to(ROOT)} is stale for {manifest.relative_to(ROOT)}: "
                f"expected={expected!r}, locked={locked[importer]!r}"
            )
        checked.append(str(relative))
    return {"project": project.name, "manifests": checked, "lockfile": "pnpm-lock.yaml"}


def main() -> int:
    framework = ROOT / "servicelib"
    cache = CONFORMANCE_ROOT / ".artifacts" / "dependency-manifests" / "go-build-cache"
    cache.mkdir(parents=True, exist_ok=True)
    checked: list[dict[str, object]] = []
    for name, module_paths in GO_PROJECT_MODULES.items():
        project = ROOT / name
        if not project.is_dir():
            raise RuntimeError(f"required dependency project is missing: {project}")
        result = check_go_project(project, module_paths, framework, cache)
        if result["modules"]:
            checked.append(result)
            print(
                f"[dependency-manifests] PASS {name}: "
                f"{len(result['modules'])} modules, {result['packages']} packages",
                flush=True,
            )
    rust_checked: list[dict[str, str]] = []
    for name, cargo_options in RUST_PROJECTS.items():
        project = ROOT / name
        if not project.is_dir():
            raise RuntimeError(f"required dependency project is missing: {project}")
        rust_checked.append(check_rust_project(project, cargo_options))
        print(f"[dependency-manifests] PASS {name}: Cargo.lock is current", flush=True)
    python_checked: list[dict[str, object]] = []
    for name in PYTHON_PROJECTS:
        result = check_python_project(ROOT / name)
        python_checked.append(result)
        print(f"[dependency-manifests] PASS {name}: uv.lock is current", flush=True)
    typescript_checked: list[dict[str, object]] = []
    for name in TYPESCRIPT_PROJECTS:
        result = check_typescript_project(ROOT / name)
        typescript_checked.append(result)
        print(f"[dependency-manifests] PASS {name}: pnpm-lock.yaml is current", flush=True)
    summary = {
        "status": "pass",
        "checks": {
            "goReadonlyResolution": checked,
            "rustLockedMetadata": rust_checked,
            "pythonLockedRequirements": python_checked,
            "typescriptLockedImporters": typescript_checked,
        },
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        raise SystemExit(1)
