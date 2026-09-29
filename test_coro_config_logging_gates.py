"""Keep configuration and logging gates on the supported Coro runtime."""

import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


class CoroConfigLoggingGatesTest(unittest.TestCase):
    def test_reload_shutdown_check_requires_both_calls_in_order(self):
        gate = runpy.run_path(str(HERE / "config" / "runtime.py"))
        lifecycle = gate["generated_lifecycle"]
        self.assertTrue(lifecycle(
            "loader.Stop(); co_spawn(executor, service.stop(context), use_future).get();"
        )["stop_before_service"])
        for source in (
            "service.stop(context);", "loader.Stop();", "",
            "service.stop(context); loader.Stop();",
        ):
            with self.subTest(source=source):
                self.assertFalse(lifecycle(source)["stop_before_service"])

    def test_generated_coro_main_preserves_all_reload_lifecycle_checks(self):
        gate = runpy.run_path(str(HERE / "config" / "runtime.py"))
        source = (gate["DEFAULT_ROOT"] / "cppcoroexample" / "orderservice"
                  / "cmd" / "service" / "main.cpp").read_text()
        checks = gate["generated_lifecycle"](source)
        self.assertEqual(len(checks), 4)
        self.assertTrue(all(checks.values()), checks)

    def test_logging_keeps_contracts_and_runs_coro_once(self):
        gate = runpy.run_path(str(HERE / "logging" / "run.py"))
        self.assertEqual(len(gate["SOURCE_CASES"]), 6)
        for source in gate["SOURCE_CASES"]:
            self.assertNotIn("cppboostservicelib", source.parts)
        sources = gate["verify_sources"]()
        self.assertEqual(len(sources["files"]), 6)
        calls = []

        def execute(name, command, cwd, env=None):
            calls.append((name, command, cwd))
            return {"name": name, "exit_code": 0}

        main = gate["main"]
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "summary.json"
            with patch.dict(main.__globals__, execute=execute, ARTIFACT=artifact), \
                 patch.object(sys, "argv", ["logging", "--skip-build"]), \
                 patch.object(gate["cpp_source_cache"], "build_volume_mount_args", return_value=[]), \
                 patch.object(gate["dependency_environment"], "from_framework", return_value={}):
                self.assertEqual(main(), 0)
            summary = json.loads(artifact.read_text())
        self.assertEqual(summary["languages"],
                         ["go", "cpp", "cppcoro", "python", "rust", "typescript"])
        self.assertEqual(len(calls), 6)
        coro = [call for call in calls if call[0] == "coro-cpp-structured-logging"]
        self.assertEqual(len(coro), 1)
        self.assertEqual(coro[0][2], gate["CORO"])
        self.assertIn("cppcoroservicelib_telemetry_test", coro[0][1][-1])
        self.assertIn("cppcoroservicelib-build:local", coro[0][1])


if __name__ == "__main__":
    unittest.main()
