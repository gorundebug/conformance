"""Deterministic host-side TypeScript toolchain commands for conformance."""

from __future__ import annotations

import os
from pathlib import Path


CONFORMANCE_DIR = Path(__file__).resolve().parent
PNPM_STORE = CONFORMANCE_DIR / ".dependencies" / ".pnpm-store" / "v11"


def environment() -> dict[str, str]:
    """Keep every host-side package and install-script download proxied."""
    environment = {**os.environ, "CI": "true"}
    github_raw = environment.get("SERVICEGEN_GITHUB_RAW_URL", "").rstrip("/")
    if github_raw:
        # node-pre-gyp derives this unusual key from the package name and only
        # replaces its first hyphen. Keep it centralized so every suite gets
        # the same binary mirror without knowing which package triggers it.
        environment[
            "npm_config_confluent_kafka-javascript_binary_host_mirror"
        ] = (
            f"{github_raw}/confluentinc/confluent-kafka-javascript/"
            "releases/download/"
        )
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
