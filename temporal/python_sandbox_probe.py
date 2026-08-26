"""Execute the negative Temporal Python Workflow sandbox conformance probe."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import RestrictedWorkflowAccessError

from python_sandbox_probe_workflow import PythonSandboxProbeWorkflow


async def main() -> None:
    client = await Client.connect("temporal:7233", namespace="default")
    task_queue = f"servicelib-python-sandbox-probe-{time.time_ns()}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[PythonSandboxProbeWorkflow],
        # This changes only whether the deliberate sandbox violation fails the
        # Workflow or leaves its Workflow Task retrying. The runner itself is
        # intentionally omitted so Worker uses the official default
        # SandboxedWorkflowRunner.
        workflow_failure_exception_types=[RestrictedWorkflowAccessError],
    )
    async with worker:
        try:
            await client.execute_workflow(
                PythonSandboxProbeWorkflow.run,
                id=f"servicelib-python-sandbox-probe-{time.time_ns()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=15),
            )
        except WorkflowFailureError as error:
            detail = str(error)
            current: BaseException | None = error
            while current is not None:
                detail += f"\n{type(current).__name__}: {current}"
                current = current.__cause__
            if "os.getcwd" not in detail and "RestrictedWorkflowAccessError" not in detail:
                raise RuntimeError(
                    "Python sandbox probe failed for an unrelated reason:\n" + detail
                ) from error
            print("Python Temporal default sandbox rejected os.getcwd: PASS")
            return
    raise RuntimeError("Python Temporal Workflow executed forbidden os.getcwd")


if __name__ == "__main__":
    asyncio.run(main())
