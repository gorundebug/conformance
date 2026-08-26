from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("metrics_run", Path(__file__).with_name("run.py"))
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MetricsNormalizationTests(unittest.TestCase):
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
