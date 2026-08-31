from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


SPEC = importlib.util.spec_from_file_location("metrics_run", Path(__file__).with_name("run.py"))
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MetricsNormalizationTests(unittest.TestCase):
    def test_cpp_uses_exact_workspace_binary_overlay(self) -> None:
        example = Path("/tmp/cppexample")
        language = SimpleNamespace(
            name="cpp", example=example, compose=Path("/tmp/metrics-cpp.yml")
        )
        command = MODULE.compose_command(language, "up")
        self.assertIn(str(example / "docker-compose.integration.generated.yml"), command)
        self.assertNotIn(
            str(example / "docker-compose.cpp-runtime.generated.yml"), command
        )

    def test_runtime_graph_normalizes_integral_json_numbers(self) -> None:
        result = MODULE.normalize_runtime_graphs(
            {
                "service": """{
  "nodes": [{"id": 1, "label": "Input", "opacity": 1.0, "x": 2.0}],
  "edges": []
}"""
            }
        )
        self.assertEqual(result["service"]["nodes"][0]["opacity"], 1)
        self.assertEqual(result["service"]["nodes"][0]["x"], 2)

    def test_observed_zero_duration_keeps_histogram_sum_shape(self) -> None:
        result = MODULE.normalize({
            "service": """# TYPE datasource_endpoint_request_duration_seconds histogram
datasource_endpoint_request_duration_seconds_sum{endpoint=\"input\"} 0
datasource_endpoint_request_duration_seconds_count{endpoint=\"input\"} 1
"""
        })
        self.assertEqual(
            result["service"],
            [
                {
                    "name": "datasource_endpoint_request_duration_seconds_count",
                    "labels": {"endpoint": "input"},
                    "value": 1,
                },
                {
                    "name": "datasource_endpoint_request_duration_seconds_sum",
                    "labels": {"endpoint": "input"},
                    "value": None,
                },
            ],
        )

    def test_unobserved_histogram_remains_omitted(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exported no ServiceLib metrics"):
            MODULE.normalize({
                "service": """# TYPE datasource_endpoint_request_duration_seconds histogram
datasource_endpoint_request_duration_seconds_sum{endpoint=\"input\"} 0
datasource_endpoint_request_duration_seconds_count{endpoint=\"input\"} 0
"""
            })


if __name__ == "__main__":
    unittest.main()
