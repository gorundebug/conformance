#!/usr/bin/env python3
"""Verify the generated hybrid repository contract against a local Git mirror.

The fixture snapshots the current sources under the exact repository URLs and
revisions declared by generated files.  Each component is then checked out in
an empty directory.  Service builds use ``USE_LOCAL_MODULES=0`` and therefore
cannot see sibling workspace modules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


CONFORMANCE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CONFORMANCE))

from standalone_components import run as standalone  # noqa: E402


DEFAULT_ROOT = Path(
    os.environ.get("CONFORMANCE_PUBLISHED_COMPONENTS_ROOT")
    or os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE / ".artifacts" / "published-components"
SUMMARY = ARTIFACTS / "summary.json"
DIAGNOSTIC_SUMMARY = ARTIFACTS / "diagnostic-summary.json"
WORKSPACE_PREFIX = "servicegen-published-components-"
CONTAINER_LABEL = "servicegen.conformance=published-components"
INTERNAL_ORGANIZATION = "gorundebug"
GITHUB_PREFIX = "https://github.com/"
REPOSITORY_REFERENCE_PATTERNS = (
    re.compile(
        r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
        r"(?P<repo>[A-Za-z0-9_.-]+?)\.git(?:\\?#|#)(?P<tag>v[0-9][A-Za-z0-9_.+-]*)"
    ),
    re.compile(
        r"git\s*=\s*\"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
        r"(?P<repo>[A-Za-z0-9_.-]+?)\.git\"\s*,\s*tag\s*=\s*\"(?P<tag>v[^\"]+)\""
    ),
    re.compile(
        r"clone_if_missing\s+\"[^\"]+\"\s+"
        r"\"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
        r"(?P<repo>[A-Za-z0-9_.-]+?)\.git\"\s+\"(?P<tag>v[^\"]+)\""
    ),
)


@dataclass(frozen=True)
class RepositorySpec:
    owner: str
    name: str
    source: Path
    tags: tuple[str, ...]
    package_script: str | None = None

    @property
    def relative_path(self) -> Path:
        return Path("github.com") / self.owner / f"{self.name}.git"

    @property
    def canonical_url(self) -> str:
        return f"{GITHUB_PREFIX}{self.owner}/{self.name}.git"


SERVICE_REPOSITORIES: dict[str, str] = {
    "go": "{service}",
    "cpp": "cppexample-{service}",
    "cppboost": "cppboostexample-{service}",
    "python": "pyexample-{service}",
    "rust": "rustexample-{service}",
    "typescript": "tsexample-{service}",
}

SERVICE_PACKAGE_SCRIPTS: dict[str, str] = {
    "cpp": "scripts/package-cpp-service.generated.sh",
    "cppboost": "scripts/package-cpp-service.generated.sh",
    "python": "scripts/package-python-service.generated.sh",
    "rust": "scripts/package-rust-service.generated.sh",
    "typescript": "scripts/package-typescript-service.generated.sh",
}


def service_package_script(language_name: str, service: str) -> str | None:
    # Automation Service falls back to generated Go in languages without the
    # Temporal SDK. Its repository is already autonomous and does not use the
    # native language packager.
    if service == "automationservice" and language_name in {
        "cpp", "cppboost", "rust",
    }:
        return None
    return SERVICE_PACKAGE_SCRIPTS.get(language_name)


def command(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> str:
    print(f"- ({cwd}) {' '.join(args)}", flush=True)
    result = subprocess.run(
        list(args),
        cwd=cwd,
        env={**os.environ, **(env or {})},
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if result.returncode != 0:
        output = result.stdout or ""
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{output}"
        )
    return result.stdout or ""


def latest_tag(repository: Path) -> str:
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    tag = result.stdout.strip()
    return tag if result.returncode == 0 and tag else "v0.0.0-local"


def tracked_files(repository: Path) -> Iterable[Path]:
    """Yield files that belong to the repository's published HEAD shape."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    for encoded in result.stdout.split(b"\0"):
        if encoded:
            yield repository / os.fsdecode(encoded)


def declared_internal_tags(root: Path) -> dict[str, set[str]]:
    """Return exact gorundebug repository tags referenced by generated files."""
    result: dict[str, set[str]] = {}
    for language in standalone.LANGUAGES.values():
        example = root / language.example
        if not example.is_dir():
            continue
        for path in tracked_files(example):
            if not path.is_file() or path.name in {"go.sum", "Cargo.lock", "pnpm-lock.yaml"}:
                continue
            if path.suffix not in {"", ".mk", ".toml", ".sh", ".yml", ".yaml"}:
                continue
            try:
                body = path.read_text()
            except UnicodeDecodeError:
                continue
            for pattern in REPOSITORY_REFERENCE_PATTERNS:
                for match in pattern.finditer(body):
                    if match.group("owner") != INTERNAL_ORGANIZATION:
                        continue
                    result.setdefault(match.group("repo"), set()).add(match.group("tag"))
    return result


def source_for_repository(root: Path, name: str) -> Path | None:
    direct = root / name
    if direct.is_dir():
        return direct
    go_component = root / "goexample" / name
    if go_component.is_dir():
        return go_component
    return None


def repository_specs(root: Path) -> list[RepositorySpec]:
    references = declared_internal_tags(root)
    specs: dict[tuple[str, str], RepositorySpec] = {}
    for name, tags in references.items():
        source = source_for_repository(root, name)
        if source is None:
            raise RuntimeError(
                f"declared internal repository has no local source: {name}"
            )
        specs[(INTERNAL_ORGANIZATION, name)] = RepositorySpec(
            INTERNAL_ORGANIZATION, name, source, tuple(sorted(tags))
        )

    # Every non-Go service can be packaged into its own repository even though
    # the canonical examples keep it in the project checkout.  Snapshot that
    # autonomous publication shape as well; Go service repositories are already
    # discovered from clone.generated.sh.
    for language_name, language in standalone.LANGUAGES.items():
        example = root / language.example
        if not example.is_dir():
            continue
        example_tag = latest_tag(example)
        for service in standalone.SERVICES:
            source = example / service
            if not source.is_dir():
                continue
            repository_name = SERVICE_REPOSITORIES[language_name].format(
                service=service
            )
            key = (INTERNAL_ORGANIZATION, repository_name)
            existing = specs.get(key)
            tags = set(existing.tags if existing else ())
            tags.add(example_tag)
            specs[key] = RepositorySpec(
                INTERNAL_ORGANIZATION,
                repository_name,
                source,
                tuple(sorted(tags)),
                service_package_script(language_name, service),
            )
    return sorted(specs.values(), key=lambda item: (item.owner, item.name))


def run_git(args: Sequence[str], cwd: Path) -> None:
    command(["git", *args], cwd=cwd)


def copy_tracked_source(source: Path, destination: Path) -> None:
    """Materialize exactly the tracked HEAD subtree that publication would see."""
    repository = Path(
        command(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=source,
            capture=True,
        ).strip()
    ).resolve()
    relative = source.resolve().relative_to(repository)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", *(() if relative == Path(".") else (str(relative),))],
        cwd=repository,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if archive.returncode != 0:
        raise RuntimeError(
            f"git archive failed for {source}: "
            f"{archive.stderr.decode(errors='replace')}"
        )
    extracted = destination.parent / f".{destination.name}-archive"
    extracted.mkdir(parents=True)
    archive_path = extracted / "source.tar"
    archive_path.write_bytes(archive.stdout)
    tree = extracted / "tree"
    tree.mkdir()
    with tarfile.open(archive_path) as bundle:
        bundle.extractall(tree, filter="data")
    materialized = tree / relative if relative != Path(".") else tree
    if not materialized.is_dir():
        raise RuntimeError(f"tracked source subtree is empty: {source}")
    shutil.copytree(materialized, destination, symlinks=True)
    shutil.rmtree(extracted)


def snapshot_repository(spec: RepositorySpec, mirror_root: Path, scratch: Path) -> None:
    destination = mirror_root / spec.relative_path
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_repository = Path(
        command(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=spec.source,
            capture=True,
        ).strip()
    ).resolve()
    if spec.package_script is None and spec.source.resolve() == source_repository:
        # A real standalone repository may be referenced by lockfiles through
        # an exact commit SHA (uv and Cargo both do this). Rebuilding its
        # history around a synthetic fixture commit would leave the tag usable
        # while making that locked commit impossible to fetch. Preserve the
        # published repository identity; synthetic histories are only for
        # project subtrees that are being tested as independently publishable.
        for tag in spec.tags:
            command(
                ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
                cwd=spec.source,
                capture=True,
            )
        command(
            ["git", "clone", "--mirror", str(spec.source), str(destination)],
            cwd=scratch,
        )
        run_git(["update-server-info"], destination)
        return

    worktree = scratch / f"{spec.owner}-{spec.name}"
    if worktree.exists():
        shutil.rmtree(worktree)
    if spec.package_script is None:
        copy_tracked_source(spec.source, worktree)
    else:
        project = scratch / f".{spec.owner}-{spec.name}-project"
        copy_tracked_source(spec.source.parent, project)
        package_script = project / spec.package_script
        if not package_script.is_file():
            raise RuntimeError(
                f"service package script is missing: {package_script}"
            )
        command(
            [str(package_script), str(project / spec.source.name), str(worktree)],
            cwd=project,
        )
        shutil.rmtree(project)
    run_git(["init", "--initial-branch=main"], worktree)
    run_git(["config", "user.name", "ServiceGen Conformance"], worktree)
    run_git(["config", "user.email", "conformance@localhost"], worktree)
    # The source archive already contains exactly tracked HEAD files. Preserve
    # files that were intentionally force-added in the source repository even
    # when its published .gitignore also matches them.
    run_git(["add", "--force", "--all"], worktree)
    run_git(["commit", "--quiet", "-m", "Published component fixture"], worktree)
    for tag in spec.tags:
        run_git(["tag", tag], worktree)
    command(["git", "clone", "--mirror", str(worktree), str(destination)], cwd=scratch)
    run_git(["update-server-info"], destination)


def copy_cached_external_mirrors(cache_root: Path, mirror_root: Path) -> int:
    """Copy only previously cached third-party mirrors into the offline fixture."""
    if not cache_root.is_dir():
        raise RuntimeError(f"shared Git mirror cache is missing: {cache_root}")
    copied = 0
    for host in ("github.com", "gitlab.com"):
        source_host = cache_root / host
        if not source_host.is_dir():
            continue
        for owner in source_host.iterdir():
            if not owner.is_dir() or (
                host == "github.com" and owner.name == INTERNAL_ORGANIZATION
            ):
                continue
            destination = mirror_root / host / owner.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            def link_or_copy(source: str, target: str) -> str:
                try:
                    os.link(source, target)
                    return target
                except OSError:
                    return shutil.copy2(source, target)

            def ignore_transient(_directory: str, names: list[str]) -> set[str]:
                return {
                    name
                    for name in names
                    if name.endswith(".lock") or name == "servicegen-last-refresh"
                }

            shutil.copytree(
                owner,
                destination,
                copy_function=link_or_copy,
                ignore=ignore_transient,
            )
            copied += sum(1 for path in destination.rglob("*.git") if path.is_dir())
    return copied


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def mirror_environment(
    port: int,
    *,
    docker: bool = False,
    include_external: bool = True,
) -> dict[str, str]:
    host = "host.docker.internal" if docker else "localhost"
    base = f"http://{host}:{port}/cgi-bin/git"
    if not include_external:
        return {
            "DEPENDENCY_GIT_MIRROR_PORT": str(port),
            "DEPENDENCY_GIT_MIRROR_URL": base,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": (
                f"url.{base}/github.com/{INTERNAL_ORGANIZATION}/.insteadOf"
            ),
            "GIT_CONFIG_VALUE_0": (
                f"https://github.com/{INTERNAL_ORGANIZATION}/"
            ),
        }
    return {
        "DEPENDENCY_GIT_MIRROR_PORT": str(port),
        "DEPENDENCY_GIT_MIRROR_URL": base,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"url.{base}/github.com/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
        "GIT_CONFIG_KEY_1": f"url.{base}/gitlab.com/.insteadOf",
        "GIT_CONFIG_VALUE_1": "https://gitlab.com/",
    }


def wait_for_mirror(port: int) -> None:
    url = f"http://localhost:{port}/cgi-bin/git/__servicegen_health"
    for _ in range(60):
        result = subprocess.run(
            ["curl", "--fail", "--silent", "--output", "/dev/null", url],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError(f"ephemeral Git mirror did not become ready: {url}")


def start_mirror(root: Path, mirror_root: Path, port: int, name: str) -> None:
    example = root / "goexample"
    bind_host = "0.0.0.0" if sys.platform.startswith("linux") else "127.0.0.1"
    command(
        [
            "docker", "run", "--detach", "--rm", "--name", name,
            "--label", CONTAINER_LABEL,
            "--publish", f"{bind_host}:{port}:8080",
            "--env", "DEPENDENCY_GIT_MIRROR_OFFLINE=1",
            # git-http-backend serializes requests with a short-lived adjacent
            # lock. The directory is ephemeral and upstream access is disabled,
            # so allow only that local coordination write.
            "--volume", f"{mirror_root}:/mirrors",
            "--volume",
            f"{example / 'scripts/git_mirror.generated.cgi'}:/www/cgi-bin/git:ro",
            "servicegen-git-mirror:2.49.1",
        ],
        cwd=example,
    )
    wait_for_mirror(port)


def clone_repository(url: str, tag: str, destination: Path, port: int) -> None:
    command(
        ["git", "clone", "--branch", tag, "--depth", "1", url, str(destination)],
        cwd=destination.parent,
        env=mirror_environment(port),
    )


def verify_independent_checkouts(
    root: Path,
    specs: Iterable[RepositorySpec],
    checkout_root: Path,
    port: int,
) -> list[str]:
    checked: list[str] = []
    for spec in specs:
        for tag in spec.tags:
            destination = checkout_root / spec.owner / spec.name / tag
            destination.parent.mkdir(parents=True, exist_ok=True)
            clone_repository(spec.canonical_url, tag, destination, port)
            if any(destination.parent.glob(f"{spec.name}-sibling-*")):
                raise RuntimeError(f"checkout is not isolated: {destination}")
            checked.append(f"{spec.owner}/{spec.name}@{tag}")
    return checked


def service_repository_name(language_name: str, service: str) -> str:
    return SERVICE_REPOSITORIES[language_name].format(service=service)


def build_environment(port: int) -> dict[str, str]:
    proxy_dir = os.environ.get("DEPENDENCY_PROXY_DIR")
    environment = mirror_environment(port, include_external=bool(proxy_dir))
    environment.update(
        {
            "DEPENDENCY_GIT_MIRROR_PORT": str(port),
            "GONOPROXY": "github.com/gorundebug/*",
            "GONOSUMDB": "github.com/gorundebug/*",
        }
    )
    if proxy_dir:
        environment["DEPENDENCY_PROXY_DIR"] = proxy_dir
    return environment


def build_services(
    root: Path,
    specs: Sequence[RepositorySpec],
    checkout_root: Path,
    port: int,
    languages: Sequence[str],
    services: Sequence[str],
) -> list[str]:
    by_name = {spec.name: spec for spec in specs}
    environment = build_environment(port)
    passed: list[str] = []
    for language_name in languages:
        for service in services:
            repository_name = service_repository_name(language_name, service)
            spec = by_name.get(repository_name)
            if spec is None:
                raise RuntimeError(
                    f"service repository fixture is missing: {repository_name}"
                )
            tag = latest_tag(root / standalone.LANGUAGES[language_name].example)
            if tag not in spec.tags:
                raise RuntimeError(
                    f"service fixture {repository_name} has no release tag {tag}"
                )
            checkout = checkout_root / spec.owner / spec.name / tag
            if not checkout.is_dir():
                raise RuntimeError(f"service checkout is missing: {checkout}")
            print(f"[published] BUILD {language_name}:{service}@{tag}", flush=True)
            command(
                ["make", "docker-build", "USE_LOCAL_MODULES=0"],
                cwd=checkout,
                env=environment,
            )
            passed.append(f"{language_name}:{service}@{tag}")
    return passed


def docker_environment_arguments(port: int) -> list[str]:
    environment = standalone.docker_process_environment()
    environment.update(
        mirror_environment(
            port,
            docker=True,
            include_external=bool(os.environ.get("DEPENDENCY_PROXY_DIR")),
        )
    )
    environment.update(
        {
            "GONOPROXY": "github.com/gorundebug/*",
            "GONOSUMDB": "github.com/gorundebug/*",
        }
    )
    result = ["--add-host", "host.docker.internal:host-gateway"]
    for name, value in sorted(environment.items()):
        result.extend(["--env", f"{name}={value}"])
    return result


def materialize_module_checkouts(
    root: Path,
    specs: Sequence[RepositorySpec],
    repository_checkouts: Path,
    destination_root: Path,
    languages: Sequence[str],
    modules: Sequence[str],
) -> dict[tuple[str, str], Path]:
    by_name = {spec.name: spec for spec in specs}
    result: dict[tuple[str, str], Path] = {}
    for language_name in languages:
        language = standalone.LANGUAGES[language_name]
        for module in modules:
            physical = standalone.component_directory(language_name, module)
            destination = destination_root / language_name / physical
            destination.parent.mkdir(parents=True, exist_ok=True)
            if language_name == "go":
                spec = by_name.get(physical)
                if spec is None:
                    raise RuntimeError(f"Go module repository is missing: {physical}")
                source = (
                    repository_checkouts
                    / spec.owner
                    / spec.name
                    / sorted(spec.tags)[-1]
                )
            else:
                spec = by_name.get(language.example)
                if spec is None:
                    raise RuntimeError(
                        f"module owner repository is missing: {language.example}"
                    )
                project_checkout = (
                    repository_checkouts
                    / spec.owner
                    / spec.name
                    / sorted(spec.tags)[-1]
                )
                source = project_checkout / physical
            copy_tracked_source(source, destination)
            standalone.assert_plain_filesystem_tree(destination)
            result[(language_name, module)] = destination
    return result


def build_go_module(module: Path, port: int, name: str) -> None:
    standalone.ensure_go_image()
    container = f"published-go-{name}-{os.getpid()}".replace("_", "-")
    command(
        [
            "docker", "run", "--rm", "--name", container,
            *docker_environment_arguments(port),
            "--env", "GOWORK=off",
            "--env", "GOCACHE=/root/.cache/go-build",
            "--env", "GOMODCACHE=/go/pkg/mod",
            "--volume", f"{module}:/workspace",
            "--volume", "standalone-components-go-build:/root/.cache/go-build",
            "--volume", "standalone-components-go-modules:/go/pkg/mod",
            "--workdir", "/workspace",
            standalone.GO_TOOLCHAIN_IMAGE,
            "make", "test",
        ],
        cwd=module,
    )


def build_python_module(module: Path, port: int, name: str) -> None:
    standalone.ensure_python_image()
    container = f"published-python-{name}-{os.getpid()}".replace("_", "-")
    command(
        [
            "docker", "run", "--rm", "--name", container,
            *docker_environment_arguments(port),
            "--env", "UV_CACHE_DIR=/root/.cache/uv",
            "--env", "UV_LINK_MODE=copy",
            "--volume", f"{module}:/workspace",
            "--volume", "standalone-components-uv:/root/.cache/uv",
            "--workdir", "/workspace",
            standalone.PYTHON_TOOLCHAIN_IMAGE,
            "/bin/bash", "-lc",
            "uv sync --all-extras && "
            "if [[ -f generate.generated.sh ]]; then "
            "PYTHON=.venv/bin/python ./generate.generated.sh; fi && "
            "uv run python -m compileall -q src",
        ],
        cwd=module,
    )


def build_rust_module(module: Path, port: int, name: str) -> None:
    standalone.ensure_rust_image()
    container = f"published-rust-{name}-{os.getpid()}".replace("_", "-")
    make_contract = (module / "make.generated.mk").read_text()
    if "cargo $(DEPENDENCY_CARGO_CONFIG_ARGS)" in make_contract:
        build_command = ["make", "test"]
    else:
        # API modules are shared repository identities. Their repository-level
        # Makefile may be owned by the Go scaffold while a Rust binding lives
        # beside it; compile that binding with its native toolchain.
        script = (
            "if [[ -x generate-openapi.generated.sh ]]; then "
            "./generate-openapi.generated.sh; fi && "
            "cargo --config 'source.crates-io.replace-with=\"dependency-proxy\"' "
            "--config \"source.dependency-proxy.registry="
            "\\\"${CARGO_REGISTRIES_CRATES_IO_INDEX}\\\"\" "
            "test --all-targets"
        )
        build_command = ["/bin/bash", "-c", script]
    command(
        [
            "docker", "run", "--rm", "--name", container,
            *docker_environment_arguments(port),
            "--volume", f"{module}:/workspace",
            "--volume", "standalone-components-cargo-registry:/usr/local/cargo/registry",
            "--volume", "standalone-components-cargo-git:/usr/local/cargo/git",
            "--volume", f"published-components-rust-{name}:/workspace/target",
            "--workdir", "/workspace",
            standalone.RUST_TOOLCHAIN_IMAGE,
            *build_command,
        ],
        cwd=module,
    )


def build_typescript_module(module: Path, port: int) -> None:
    command(
        ["make", "docker-build"],
        cwd=module,
        env=build_environment(port),
    )


def build_cpp_module(
    root: Path,
    module: Path,
    language_name: str,
    logical_name: str,
    context: standalone.CppContext,
    parent: Path,
) -> None:
    physical = standalone.component_directory(language_name, logical_name)
    target = parent / f"{language_name}-{physical}"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(module, target / physical, symlinks=True)
    standalone.build_cpp(root, target, language_name, logical_name, context)


def build_modules(
    root: Path,
    module_checkouts: dict[tuple[str, str], Path],
    port: int,
    languages: Sequence[str],
    modules: Sequence[str],
    workspace: Path,
) -> list[str]:
    standalone.GO_VERSION = standalone.go_toolchain.example_version(root)
    standalone.GO_TOOLCHAIN_IMAGE = (
        f"servicelib-standalone-go:{standalone.GO_VERSION}"
    )
    cpp_contexts: dict[str, standalone.CppContext] = {}
    passed: list[str] = []
    for language_name in languages:
        if language_name in {"cpp", "cppboost"}:
            cpp_contexts[language_name] = standalone.ensure_cpp_image(
                root, language_name
            )
        for module_name in modules:
            module = module_checkouts[(language_name, module_name)]
            print(f"[published] BUILD {language_name}:{module_name}", flush=True)
            if language_name == "go":
                build_go_module(module, port, module_name)
            elif language_name == "python":
                build_python_module(module, port, module_name)
            elif language_name == "rust":
                build_rust_module(module, port, module_name)
            elif language_name == "typescript":
                build_typescript_module(module, port)
            else:
                build_cpp_module(
                    root,
                    module,
                    language_name,
                    module_name,
                    cpp_contexts[language_name],
                    workspace / "cpp-module-builds",
                )
            passed.append(f"{language_name}:{module_name}")
    return passed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument(
        "--language", action="append", choices=sorted(standalone.LANGUAGES)
    )
    parser.add_argument(
        "--component", action="append", choices=standalone.COMPONENTS
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="seed the offline mirror and verify clean independent clones",
    )
    return parser.parse_args()


def write_summary(value: dict[str, object], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def cleanup_stale_workspaces(*, keep: Path | None = None) -> None:
    temporary_root = Path(tempfile.gettempdir()).resolve()
    for candidate in temporary_root.glob(f"{WORKSPACE_PREFIX}*"):
        resolved = candidate.resolve()
        if keep is not None and resolved == keep.resolve():
            continue
        if resolved.parent != temporary_root or not resolved.name.startswith(
            WORKSPACE_PREFIX
        ):
            raise RuntimeError(f"refusing unsafe stale workspace cleanup: {resolved}")
        shutil.rmtree(resolved, ignore_errors=False)


def cleanup_stale_containers() -> None:
    result = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"label={CONTAINER_LABEL}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot enumerate stale mirror containers: {result.stderr}")
    containers = result.stdout.split()
    if containers:
        command(["docker", "rm", "--force", *containers], cwd=CONFORMANCE)


def main() -> int:
    args = parse_args()
    root = args.local_root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"local repository root is missing: {root}")
    cleanup_stale_containers()
    cleanup_stale_workspaces()
    workspace = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX)).resolve()
    mirror_root = workspace / "mirrors"
    scratch = workspace / "snapshots"
    checkouts = workspace / "checkouts"
    module_checkouts_root = workspace / "module-checkouts"
    mirror_root.mkdir(parents=True)
    if any(mirror_root.iterdir()):
        raise RuntimeError(f"ephemeral mirror root is not empty: {mirror_root}")
    scratch.mkdir()
    checkouts.mkdir()
    module_checkouts_root.mkdir()
    container = f"servicegen-published-mirror-{os.getpid()}"
    port = free_port()
    started = time.monotonic()
    try:
        external_mirrors = 0
        if proxy_dir := os.environ.get("DEPENDENCY_PROXY_DIR"):
            external_mirrors = copy_cached_external_mirrors(
                Path(proxy_dir).expanduser().resolve() / "git-mirror",
                mirror_root,
            )
        specs = repository_specs(root)
        for spec in specs:
            print(
                f"[published] SNAPSHOT {spec.owner}/{spec.name} "
                f"tags={','.join(spec.tags)}",
                flush=True,
            )
            snapshot_repository(spec, mirror_root, scratch)
        start_mirror(root, mirror_root, port, container)
        checked = verify_independent_checkouts(root, specs, checkouts, port)
        selected_languages = args.language or list(standalone.LANGUAGES)
        requested_components = args.component or list(standalone.COMPONENTS)
        selected_services = [
            component
            for component in requested_components
            if component in standalone.SERVICES
        ]
        selected_modules = [
            component
            for component in requested_components
            if component in standalone.MODULES
        ]
        built_services: list[str] = []
        built_modules: list[str] = []
        if not args.prepare_only and selected_services:
            built_services = build_services(
                root,
                specs,
                checkouts,
                port,
                selected_languages,
                selected_services,
            )
        if not args.prepare_only and selected_modules:
            module_checkouts = materialize_module_checkouts(
                root,
                specs,
                checkouts,
                module_checkouts_root,
                selected_languages,
                selected_modules,
            )
            built_modules = build_modules(
                root,
                module_checkouts,
                port,
                selected_languages,
                selected_modules,
                workspace,
            )
        summary = {
            "status": "pass",
            "mode": "prepare-only" if args.prepare_only else "build",
            "offline_git_mirror": True,
            "cached_external_git_repositories": external_mirrors,
            "repositories": len(specs),
            "languages": selected_languages,
            "checkouts": checked,
            "built_services": built_services,
            "built_modules": built_modules,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        authoritative = (
            args.language is None
            and args.component is None
            and not args.prepare_only
        )
        write_summary(
            summary,
            SUMMARY if authoritative else DIAGNOSTIC_SUMMARY,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not args.keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
