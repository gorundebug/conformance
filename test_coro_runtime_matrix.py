from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import cppcoro_runtime as gate


class CoroRuntimeMatrixTest(unittest.TestCase):
    def run_matrix(self, omitted: str = "") -> tuple[int, list[tuple[str, str]], dict]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "cppcoroservicelib/scripts/test-conan.sh"
            driver.parent.mkdir(parents=True)
            driver.write_text("# fixture driver; execution is mocked\n")
            artifacts = root / "results"
            calls = []

            def execute(command, **kwargs):
                backend = kwargs["env"]["CPP_CORO_IO_BACKEND"]
                calls.append((backend, command[-1]))
                names = set().union(*gate.required_stage_tests(backend).values())
                names.discard(omitted)
                output = kwargs["stdout"]
                for index, name in enumerate(sorted(names), 1):
                    output.write(f"Test #{index}: {name} ... Passed 0.01 sec\n")
                output.write(f"100% tests passed, 0 tests failed out of {len(names)}\n")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(gate, "ROOT", root), mock.patch.object(
                gate, "ARTIFACTS", artifacts
            ), mock.patch.dict(gate.os.environ, {}, clear=True), mock.patch.object(
                gate.subprocess, "run", side_effect=execute
            ):
                result = gate.main()
            return result, calls, json.loads((artifacts / "summary.json").read_text())

    def test_all_backend_build_combinations_are_mandatory(self):
        result, calls, summary = self.run_matrix()
        self.assertEqual(result, 0)
        self.assertEqual(calls, [
            ("epoll", "Debug"), ("epoll", "Release"),
            ("uring", "Debug"), ("uring", "Release"),
        ])
        self.assertEqual(len({run["log"] for run in summary["runs"]}), 4)
        self.assertTrue(all(not run["missing_by_stage"] for run in summary["runs"]))

    def test_green_ctest_without_selected_backend_test_is_not_success(self):
        result, calls, summary = self.run_matrix("uring_backend_tests")
        self.assertEqual(result, 1)
        self.assertEqual(calls[-1], ("uring", "Debug"))
        self.assertEqual(summary["runs"][-1]["missing_by_stage"], {"io": ["uring_backend_tests"]})

    def test_callback_suite_is_required_for_both_backends(self):
        for backend in gate.BACKENDS:
            self.assertIn("coro_callback_transport_tests", gate.required_stage_tests(backend)["io"])
        result, _, summary = self.run_matrix("coro_callback_transport_tests")
        self.assertEqual(result, 1)
        self.assertEqual(summary["status"], "fail")

    def test_backend_requirements_are_independent(self):
        epoll = gate.required_stage_tests("epoll")
        uring = gate.required_stage_tests("uring")
        self.assertNotIn("uring_backend_tests", epoll["io"])
        self.assertNotIn("epoll_backend_tests", uring["io"])
        self.assertNotIn("epoll_backend_tests", gate.STAGE_TESTS["io"])
        with self.assertRaises(ValueError):
            gate.required_stage_tests("invalid")
