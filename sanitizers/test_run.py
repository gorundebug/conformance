from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("sanitizer_conformance_run", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
RUN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUN
SPEC.loader.exec_module(RUN)


class SanitizerConformanceTest(unittest.TestCase):
    def test_matrix_covers_every_runtime_and_native_detector(self) -> None:
        self.assertEqual(
            set(RUN.IMPLEMENTATIONS),
            {"go", "cpp", "cppboost", "python", "rust", "typescript"},
        )
        self.assertEqual(set(RUN.SANITIZERS), {"race", "asan", "tsan", "runtime"})
        self.assertEqual(
            RUN.IMPLEMENTATION_SANITIZERS["cpp"],
            ("runtime", "asan", "tsan"),
        )
        self.assertEqual(
            RUN.IMPLEMENTATION_SANITIZERS["cppboost"],
            ("runtime", "asan", "tsan"),
        )
        for language, sanitizers in RUN.IMPLEMENTATION_SANITIZERS.items():
            if "runtime" in sanitizers:
                self.assertIn(language, RUN.RUNTIME_FAILURE_MARKERS)

    def test_runtime_graph_requires_positive_calls(self) -> None:
        self.assertFalse(RUN.edge_has_calls({"edges": [{"label": "calls: 0"}]}))
        self.assertTrue(
            RUN.edge_has_calls(
                {"edges": [{"label": "FunctionCall\ncalls: 12 (L)"}]}
            )
        )

    def test_load_uses_canonical_out_of_stock_scenario(self) -> None:
        payload = json.loads(RUN.REQUEST_BODY)
        self.assertEqual(payload["items"][0]["sku"], "UNKNOWN")

    def test_lifecycle_windows_are_fixed(self) -> None:
        previous = sys.argv
        try:
            sys.argv = [str(MODULE_PATH)]
            args = RUN.parse_args()
        finally:
            sys.argv = previous
        self.assertEqual(args.single_request_runs, 3)
        self.assertEqual(args.shutdown_load_duration, 20.0)
        self.assertEqual(args.shutdown_after, 10.0)
        self.assertEqual(args.shutdown_timeout, 7.0)

    def test_local_framework_context_is_explicit(self) -> None:
        previous = os.environ.pop("USE_LOCAL_MODULES", None)
        previous_source = os.environ.get("SERVICELIB_SOURCE_CONTEXT")
        try:
            os.environ["SERVICELIB_SOURCE_CONTEXT"] = "published-context"
            self.assertEqual(
                RUN.implementation_env("cppboost")["SERVICELIB_SOURCE_CONTEXT"],
                str(RUN.ROOT / "cppboostservicelib"),
            )
            cpp_env = RUN.implementation_env("cpp")
            self.assertEqual(cpp_env["USE_LOCAL_MODULES"], "1")
            self.assertEqual(
                cpp_env["SERVICELIB_SOURCE_CONTEXT"],
                str(RUN.ROOT / "cppservicelib"),
            )
            self.assertNotIn("CPPSERVICELIB_CONAN_LOCKFILE", cpp_env)
            env = RUN.implementation_env("cppboost")
            self.assertEqual(
                env["SERVICELIB_SOURCE_CONTEXT"],
                str(RUN.ROOT / "cppboostservicelib"),
            )
        finally:
            if previous is None:
                os.environ.pop("USE_LOCAL_MODULES", None)
            else:
                os.environ["USE_LOCAL_MODULES"] = previous
            if previous_source is None:
                os.environ.pop("SERVICELIB_SOURCE_CONTEXT", None)
            else:
                os.environ["SERVICELIB_SOURCE_CONTEXT"] = previous_source

    def test_lifecycle_grace_covers_the_canonical_soft_deadline(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            env = RUN.implementation_env("typescript")
        self.assertEqual(env["LIFECYCLE_STOP_TIMEOUT"], "7")
        self.assertEqual(env["SANITIZER_STOP_TIMEOUT"], "7")
        self.assertEqual(env["RACE_STOP_TIMEOUT"], "7")

    def test_concurrent_shutdown_waits_on_one_shared_container_set(self) -> None:
        observations = [
            mock.Mock(returncode=0, stdout="true\nfalse\n"),
            mock.Mock(returncode=0, stdout="false\n"),
        ]
        with (
            mock.patch.object(RUN.subprocess, "run", side_effect=observations) as inspect,
            mock.patch.object(RUN.time, "sleep"),
        ):
            self.assertEqual(
                RUN.wait_for_container_exit(("project-a-1", "project-b-1"), 5, env={}),
                (),
            )
        self.assertEqual(
            inspect.call_args_list[0].args[0],
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                "project-a-1",
                "project-b-1",
            ],
        )
        self.assertEqual(
            inspect.call_args_list[1].args[0],
            ["docker", "inspect", "-f", "{{.State.Running}}", "project-a-1"],
        )


if __name__ == "__main__":
    unittest.main()
