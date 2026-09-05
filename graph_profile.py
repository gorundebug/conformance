"""Shared assertions for generated and live ServiceLib call semantics."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path


PROFILE_COUNTS = {
    "function-call": {
        "FunctionCall": 19,
        "TaskPool": 0,
        "PriorityTaskPool": 0,
        "ParallelCall": 0,
    },
    "current": {
        "FunctionCall": 8,
        "TaskPool": 4,
        "PriorityTaskPool": 4,
        "ParallelCall": 3,
    },
}

SEMANTIC_TOKENS = {
    "FunctionCall": (
        "callSemantics: FunctionCall",
        "call_semantics: FunctionCall",
        "      functionCall:",
    ),
    "TaskPool": (
        "callSemantics: TaskPool",
        "call_semantics: TaskPool",
        "call_semantics: !TaskPool",
        "      taskPool:",
    ),
    "PriorityTaskPool": (
        "callSemantics: PriorityTaskPool",
        "call_semantics: PriorityTaskPool",
        "call_semantics: !PriorityTaskPool",
        "      priorityTaskPool:",
    ),
    "ParallelCall": (
        "callSemantics: ParallelCall",
        "call_semantics: ParallelCall",
        "      parallelCall:",
    ),
}


def counts(text: str) -> dict[str, int]:
    return {
        name: sum(text.count(token) for token in tokens)
        for name, tokens in SEMANTIC_TOKENS.items()
    }


def verify_generated_project(project: Path, profile: str) -> dict[str, int]:
    expected = PROFILE_COUNTS.get(profile)
    if expected is None:
        raise RuntimeError(f"unsupported graph profile {profile!r}")
    graph = project / "graph" / "example.generated.yaml"
    actual = counts(graph.read_text())
    if actual != expected:
        raise RuntimeError(
            f"{project.name} generated graph is not profile {profile!r}: "
            f"actual={actual}, expected={expected}"
        )
    return actual


def verify_live_service(
    project: Path,
    service: str,
    port: int,
    *,
    urlopen=urllib.request.urlopen,
) -> dict[str, int]:
    url = f"http://localhost:{port}/status/graph"
    try:
        with urlopen(url, timeout=5) as response:
            live_graph = response.read().decode("utf-8")
    except (OSError, UnicodeError, urllib.error.URLError) as error:
        raise RuntimeError(
            f"cannot verify {project.name} {service} live graph from {url}: {error}"
        ) from error
    return verify_live_service_text(project, service, live_graph)


def verify_live_service_text(
    project: Path, service: str, live_graph: str
) -> dict[str, int]:
    expected_graph = project / service / "graph" / f"{service}.generated.yaml"
    expected = counts(expected_graph.read_text())
    actual = counts(live_graph)
    if actual != expected:
        raise RuntimeError(
            f"{project.name} {service} live graph uses the wrong call semantics: "
            f"actual={actual}, generated={expected}; the runtime image is stale"
        )
    return actual
