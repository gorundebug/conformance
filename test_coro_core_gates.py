"""The core conformance gates must exercise the supported coroutine runtime."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parent


class CoroCoreGatesTest(unittest.TestCase):
    def load_gate(self, name: str) -> dict:
        with patch.dict(os.environ, {"DEPENDENCIES_DIR": str(ROOT)}):
            return runpy.run_path(str(DIRECTORY / name / "run.py"))

    def test_operator_sources_keep_all_coro_test_groups(self) -> None:
        gate = self.load_gate("operators")
        self.assertEqual(gate["CORO"], ROOT / "cppcoroservicelib")
        sources = gate["SOURCE_CASES"]
        coro_sources = {path for path in sources if gate["CORO"] in path.parents}
        self.assertEqual(
            {path.name for path in coro_sources},
            {"operators_compile_test.cpp", "operators_topology_test.cpp",
             "serviceapp_test.cpp", "status_test.cpp", "join_topology_test.cpp"},
        )
        for path in coro_sources:
            source = path.read_text(encoding="utf-8")
            for marker in sources[path]:
                with self.subTest(file=path.name, case=marker):
                    self.assertIn(marker, source)
        for path in (*sources, *gate["FUNCTION_CONTRACTS"]):
            self.assertNotIn("cppboostservicelib", path.parts)
            self.assertNotIn("cppboostexample", path.parts)
            self.assertNotIn("cppboost", path.parts)
        self.assertIn(
            ROOT / "cppcoroexample/orderservice/internal/functions/order/process_order_items.hpp",
            gate["FUNCTION_CONTRACTS"],
        )

    def test_pool_sources_keep_all_cancellation_and_lifecycle_cases(self) -> None:
        gate = self.load_gate("pools")
        self.assertEqual(gate["CORO"], ROOT / "cppcoroservicelib")
        sources = gate["REQUIRED_SOURCE_CASES"]
        coro_sources = {path for path in sources if gate["CORO"] in path.parents}
        self.assertEqual({path.name for path in coro_sources},
                         {"taskpool_test.cpp", "other_pools_test.cpp"})
        self.assertEqual(sum(len(sources[path]) for path in coro_sources), 25)
        for path in sources:
            self.assertNotIn("cppboostservicelib", path.parts)
        for path in coro_sources:
            source = path.read_text(encoding="utf-8")
            for marker in sources[path]:
                with self.subTest(file=path.name, case=marker):
                    self.assertIn(marker, source)

    def test_pool_build_keeps_install_and_consumer_checks(self) -> None:
        gate = self.load_gate("pools")
        with patch.object(gate["cpp_source_cache"], "cmake_args", return_value="-DTEST_CACHE=ON ") as cache:
            script = gate["coro_framework_build_script"]()
        cache.assert_called_once_with(ROOT / "cppcoroservicelib")
        self.assertIn("ctest --test-dir build/docker --output-on-failure", script)
        self.assertIn("cmake --install build/docker", script)
        self.assertIn("-S tests/consumer", script)
        self.assertIn("/workspace/build/consumer/", script)


if __name__ == "__main__":
    unittest.main()
