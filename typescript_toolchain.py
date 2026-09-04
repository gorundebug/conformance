"""Deterministic host-side TypeScript toolchain commands for conformance."""

from __future__ import annotations

import os
from pathlib import Path

import dependency_download_mirrors


CONFORMANCE_DIR = Path(__file__).resolve().parent
PNPM_STORE = CONFORMANCE_DIR / ".dependencies" / ".pnpm-store" / "v11"


def environment() -> dict[str, str]:
    """Keep every host-side package and install-script download proxied."""
    environment = {**os.environ, "CI": "true"}
    environment.update(dependency_download_mirrors.environment())
    return environment


def install_command() -> list[str]:
    command = [
        "corepack",
        "pnpm",
    ]
    registry = os.environ.get("NPM_CONFIG_REGISTRY", "").strip()
    if registry:
        # pnpm 11 does not honor NPM_CONFIG_REGISTRY when a workspace config
        # supplies its default registry. The explicit CLI setting is the
        # authoritative boundary for all current and future npm packages.
        command.append(f"--config.registry={registry}")
    command.extend([
        "install",
        "--frozen-lockfile",
        # Host-side conformance gates compile and exercise the TypeScript
        # implementation with injected transport clients. Native dependency
        # installation belongs to the Docker runtime/integration builds; do
        # not make these unit gates depend on a host C/C++ toolchain.
        "--ignore-scripts",
        "--store-dir",
        str(PNPM_STORE),
    ])
    return command


def tsc_command(tsconfig: str, *, force: bool = False) -> list[str]:
    command = [
        "node",
        "node_modules/typescript/bin/tsc",
        "--build",
        tsconfig,
    ]
    if force:
        command.append("--force")
    return command


def copy_runtime_assets_command() -> list[str]:
    """Copy non-TypeScript runtime assets required by compiled modules."""
    return ["node", "scripts/copy-status-assets.mjs", "dist"]
