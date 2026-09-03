from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("config_conformance_run", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
RUN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUN
SPEC.loader.exec_module(RUN)


class OverrideCoverageTest(unittest.TestCase):
    def test_normalizes_typed_go_schedule_policies(self) -> None:
        self.assertEqual(
            RUN.normalize_snapshot(
                "api.ScheduleOverlapPolicySkip", "overlapPolicy"
            ),
            "Skip",
        )
        self.assertEqual(
            RUN.normalize_snapshot(
                "api.ScheduleMissedRunPolicyFireOnce", "missedRunPolicy"
            ),
            "FireOnce",
        )

    def test_reports_only_generated_variables_missing_from_override(self) -> None:
        config = """\
pools:
  defaultPool:
    executorsCount: $defaultPoolExecutorsCount
streams:
  pause:
    duration: $pauseDuration
"""
        override = """\
pools:
  defaultPool:
    executorsCount: 2
"""
        self.assertEqual(
            RUN.unresolved_override_paths(config, override),
            ["streams.pause.duration"],
        )

    def test_empty_quoted_scalar_is_a_concrete_override(self) -> None:
        config = """\
services:
  service:
    environment: $serviceEnvironment
"""
        override = """\
services:
  service:
    environment: ""
"""
        self.assertEqual(RUN.unresolved_override_paths(config, override), [])

    def test_temporal_endpoint_capacity_requires_matching_override(self) -> None:
        config = """\
endpoints:
  activity:
    maxConcurrentActivities: $activityMaxConcurrentActivities
  workflow:
    maxConcurrentWorkflowTasks: $workflowMaxConcurrentWorkflowTasks
"""
        override = """\
endpoints:
  activity:
    maxConcurrentActivities: 2
"""
        self.assertEqual(
            RUN.unresolved_override_paths(config, override),
            ["endpoints.workflow.maxConcurrentWorkflowTasks"],
        )


if __name__ == "__main__":
    unittest.main()
