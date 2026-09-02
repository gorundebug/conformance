from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("idle_cpu_run", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
RUN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUN
SPEC.loader.exec_module(RUN)


class IdleCpuTest(unittest.TestCase):
    def test_temporal_languages_are_complete(self) -> None:
        self.assertEqual(set(RUN.LANGUAGES), {"go", "python", "typescript"})

    def test_cpu_percent_and_summary(self) -> None:
        self.assertEqual(RUN.parse_cpu_percent("1.25%"), 1.25)
        self.assertEqual(RUN.parse_cpu_percent("1,25%"), 1.25)
        summary = RUN.summarize([0.1, 0.3, 0.2])
        self.assertEqual(summary["sampleCount"], 3)
        self.assertAlmostEqual(summary["averagePercent"], 0.2)
        self.assertEqual(summary["maximumPercent"], 0.3)


if __name__ == "__main__":
    unittest.main()
