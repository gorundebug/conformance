from __future__ import annotations

import ast
from pathlib import Path
import runpy
import unittest


DIRECTORY = Path(__file__).resolve().parent


class CoroTransportRetirementTest(unittest.TestCase):
    def setUp(self):
        self.gate = runpy.run_path(str(DIRECTORY / "transports/run.py"))

    def test_coro_source_contract_markers_still_exist(self):
        roots = (self.gate["CORO"], self.gate["SERVICEGEN"])
        for path, markers in self.gate["SOURCE_CASES"].items():
            if not any(root in path.parents for root in roots):
                continue
            with self.subTest(path=str(path)):
                text = path.read_text()
                for marker in markers:
                    self.assertTrue(marker in text, f"Missing contract case {marker} in {path}")

    def test_only_one_coro_transport_setup_and_http_run(self):
        source = (DIRECTORY / "transports/run.py").read_text()
        tree = ast.parse(source)
        names = [
            node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "execute"
            and node.args and isinstance(node.args[0], ast.Constant)
        ]
        self.assertEqual(len(names), len(set(names)))
        for name in (
            "coro-build-image", "coro-source-cache", "coro-grpc-release",
            "coro-http-and-custom-lifecycle", "coro-grpc-asan-ubsan",
            "coro-kafka-application-and-broker-wire", "coro-kafka-asan-ubsan",
        ):
            self.assertEqual(names.count(name), 1)
        self.assertNotIn("cppboost", source)

    def test_sanitizers_do_not_select_stackful_context_backend(self):
        flags = self.gate["coro_sanitizer_cmake_flags"]()
        self.assertIn("-DCPPCOROSERVICELIB_ASAN=ON", flags)
        self.assertIn("-DCPPCOROSERVICELIB_UBSAN=ON", flags)
        self.assertNotIn("BOOST_CONTEXT", flags)
        self.assertNotIn("BOOST_USE_ASAN", flags)
        grpc = self.gate["coro_command"]("build/grpc-test", True, False)[-1]
        for name in ("runtime", "endpoints", "unary", "streaming"):
            self.assertIn(f"cppcoroservicelib_grpc_{name}_test", grpc)
