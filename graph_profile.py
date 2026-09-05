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


def override_counts(
    text: str, default_semantics: str = "FunctionCall"
) -> dict[str, int]:
    """Count only link semantics that differ from the service default.

    Runtime graph exporters intentionally may omit an explicit semantic equal
    to ``defaultCallSemantics``. Generated YAML may retain it, so raw token
    counts cannot be compared across those two representations.
    """
    result = counts(text)
    if default_semantics in result:
        result[default_semantics] = 0
    return result


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
    expected = override_counts(expected_graph.read_text())
    actual = override_counts(live_graph)
    if actual != expected:
        raise RuntimeError(
            f"{project.name} {service} live graph uses the wrong call semantics: "
            f"actual overrides={actual}, generated overrides={expected}; "
            "the runtime image is stale"
        )
    return actual
