import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import capacity
import run as benchmark


class VirtualUserRampTest(unittest.TestCase):
    def test_latency_and_errors_fail_the_run(self) -> None:
        args = Namespace(
            max_error_rate=0.001,
            max_p95_ms=100.0,
            max_p99_ms=200.0,
        )
        high_latency = {
            "request_count": 100,
            "error_rate": 0.0,
            "latency_ms": {"p95": 10_000.0, "p99": 10_000.0},
        }
        errors = {
            "request_count": 100,
            "error_rate": 0.01,
            "latency_ms": {"p95": 1.0, "p99": 1.0},
        }

        self.assertIn("p95", capacity.failure_reasons(high_latency, args)[0])
        self.assertIn("p99", capacity.failure_reasons(high_latency, args)[1])
        self.assertIn("errors", capacity.failure_reasons(errors, args)[0])

    def test_confirms_rps_plateau_with_three_attempts(self) -> None:
        language = benchmark.Language(
            "test", Path("/tmp/test-example"), Path("/tmp/test-overlay.yml")
        )
        args = Namespace(
            start_vus=2,
            vus_step=2,
            max_vus=10,
            attempts=3,
            duration="20s",
            max_error_rate=0.001,
            max_p95_ms=100.0,
            max_p99_ms=200.0,
            min_rps_gain_percent=5.0,
        )
        rps_values = iter([200.0, 202.0, 204.0, 203.0])

        def fake_attempt(
            _language: benchmark.Language,
            _args: Namespace,
            vus: int,
            attempt: int,
        ) -> dict[str, object]:
            rps = next(rps_values)
            return {
                "vus": vus,
                "attempt": attempt,
                "successful": True,
                "request_count": 100,
                "requests_per_second": rps,
                "error_rate": 0.0,
                "latency_ms": {"p95": float(vus), "p99": float(vus * 2)},
            }

        with patch.object(capacity, "run_attempt", side_effect=fake_attempt) as run:
            result = capacity.find_capacity(language, args)

        self.assertEqual(result["maximum_unsaturated_vus"], 2)
        self.assertEqual(result["rps_at_maximum_unsaturated_vus"], 200.0)
        self.assertEqual(result["first_failed_vus"], 4)
        self.assertIn("RPS gain", result["stop_reasons"][0])
        self.assertEqual(
            [(call.args[2], call.args[3]) for call in run.call_args_list],
            [(2, 1), (4, 1), (4, 2), (4, 3)],
        )

    def test_median_rps_recovery_continues_the_ramp(self) -> None:
        language = benchmark.Language(
            "test", Path("/tmp/test-example"), Path("/tmp/test-overlay.yml")
        )
        args = Namespace(
            start_vus=2,
            vus_step=2,
            max_vus=4,
            attempts=3,
            duration="20s",
            max_error_rate=0.001,
            max_p95_ms=100.0,
            max_p99_ms=200.0,
            min_rps_gain_percent=5.0,
        )
        rps_values = iter([100.0, 102.0, 110.0, 112.0])

        def fake_attempt(
            _language: benchmark.Language,
            _args: Namespace,
            vus: int,
            attempt: int,
        ) -> dict[str, object]:
            return {
                "vus": vus,
                "attempt": attempt,
                "successful": True,
                "request_count": 100,
                "requests_per_second": next(rps_values),
                "error_rate": 0.0,
                "latency_ms": {"p95": 1.0, "p99": 2.0},
            }

        with patch.object(capacity, "run_attempt", side_effect=fake_attempt):
            result = capacity.find_capacity(language, args)

        self.assertEqual(result["maximum_unsaturated_vus"], 4)
        self.assertEqual(result["rps_at_maximum_unsaturated_vus"], 110.0)
        self.assertIsNone(result["first_failed_vus"])


if __name__ == "__main__":
    unittest.main()
