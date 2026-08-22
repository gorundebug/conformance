import argparse
import unittest

import graph_only


class GraphOnlyTest(unittest.TestCase):
    def test_duration_requires_positive_seconds(self) -> None:
        self.assertEqual(graph_only.duration_seconds("20s"), 20)
        for invalid in ("0s", "1.5s", "20", "-1s"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(argparse.ArgumentTypeError):
                    graph_only.duration_seconds(invalid)

    def test_result_parser_ignores_compose_output(self) -> None:
        output = """Container graph Created
{"scenario":"typescript_graph_without_grpc","requests":42,"requests_per_second":21.0}
"""
        self.assertEqual(graph_only.parse_result(output)["requests"], 42)

    def test_result_parser_rejects_missing_or_empty_result(self) -> None:
        with self.assertRaises(RuntimeError):
            graph_only.parse_result("Container graph Created\n")
        with self.assertRaises(RuntimeError):
            graph_only.parse_result(
                '{"scenario":"typescript_graph_without_grpc",'
                '"requests":0,"requests_per_second":0}\n'
            )


if __name__ == "__main__":
    unittest.main()
