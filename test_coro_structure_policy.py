import importlib.util
import json
from pathlib import Path
import unittest


HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("coro_structure_gate", HERE / "structure" / "run.py")
assert SPEC and SPEC.loader
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class CoroStructurePolicyTests(unittest.TestCase):
    def test_mapping_preserves_all_artifacts(self):
        result = GATE.mapped_example_paths(
            {"model_cppcoro/a.hpp", "model_cppcoro/new.hpp", "service/app.cpp"},
            {"model_cppcoro/": "model_cpp/"},
        )
        self.assertEqual(result, {"model_cpp/a.hpp", "model_cpp/new.hpp", "service/app.cpp"})

    def test_mapping_rejects_duplicate_logical_paths(self):
        with self.assertRaisesRegex(ValueError, "collision"):
            GATE.mapped_example_paths({"model_cppcoro/a.hpp", "model_cpp/a.hpp"}, {"model_cppcoro/": "model_cpp/"})

    def test_unexpected_and_stale_boundaries_remain_errors(self):
        result = GATE.exact_difference(left={"shared"}, right={"shared", "new"}, allowed_left_only=set(), allowed_right_only={"old"})
        self.assertEqual(result["unexpected_right_only"], ["new"])
        self.assertEqual(result["stale_right_only_allowance"], ["old"])

    def test_endpoint_tokens_are_runtime_specific(self):
        required = {"shared": ["State"], "canonical": ["void consume("], "coro": ["awaitable<void> consume("]}
        self.assertEqual(GATE.interface_tokens(required, "coro"), ["State", "awaitable<void> consume("])
        self.assertEqual(GATE.interface_tokens(required, "canonical"), ["State", "void consume("])

    def test_policy_has_no_retired_runtime_boundaries(self):
        policy = json.loads((HERE / "structure" / "deviations.json").read_text())
        self.assertIn("cppcoroexample", policy["required_example_paths"])
        self.assertNotIn("cppboostexample", policy["required_example_paths"])
        for section in (policy["public_headers"], policy["example_layout"]):
            self.assertNotIn("boost_boundary_replacements", section)
            for name in section["coro_boundary_replacements"]:
                self.assertNotIn("cppboost", name)
                self.assertNotIn("cooperative_execution", name)
        self.assertIn("kCppBoost", policy["public_headers"]["forbidden_coro_tokens"]["api/serviceapi.hpp"])


if __name__ == "__main__":
    unittest.main()
