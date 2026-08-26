"""Workflow used by conformance to prove the official Python sandbox is active."""

from __future__ import annotations

import os

from temporalio import workflow


@workflow.defn(name="servicelib.conformance.python-sandbox-probe.v1")
class PythonSandboxProbeWorkflow:
    @workflow.run
    async def run(self) -> str:
        # Process filesystem state is intentionally unavailable to a Workflow.
        # The default SandboxedWorkflowRunner must reject this at execution
        # time; returning from this function is a conformance failure.
        return os.getcwd()
