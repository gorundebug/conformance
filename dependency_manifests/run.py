#!/usr/bin/env python3
"""Fail early when checked-in dependency manifests and lockfiles are stale."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
IGNORED_PARTS = {
    ".artifacts", ".git", ".venv", "build", "dist", "node_modules", "target",
}


def module_directories(project: Path) -> list[Path]:
    return sorted(
        path.parent
        for path in project.rglob("go.mod")
        if not any(part in IGNORED_PARTS for part in path.relative_to(project).parts)
    )


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise RuntimeError(
            f"dependency manifest check failed in {cwd.relative_to(ROOT)}\n"
            f"command: {' '.join(command)}\n{result.stdout}"
        )
    return result.stdout


def check_go_project(
    project: Path, module_paths: tuple[str, ...], framework: Path, cache: Path,
) -> dict[str, object]:
    modules = [project / relative for relative in module_paths]
    missing = [path / "go.mod" for path in modules if not (path / "go.mod").is_file()]
    if missing:
        rendered = ", ".join(str(path.relative_to(ROOT)) for path in missing)
        raise RuntimeError(f"required Go manifests are missing: {rendered}")
    workspace_modules = list(modules)
    if framework.is_dir() and framework not in workspace_modules:
        workspace_modules.append(framework)
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
                ["go", "list", "-mod=readonly", "./..."], cwd=module, env=env,
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
    summary = {
        "status": "pass",
        "checks": {
            "goReadonlyResolution": checked,
            "rustLockedMetadata": rust_checked,
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
