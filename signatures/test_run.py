from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path


RUN = runpy.run_path(str(Path(__file__).with_name("run.py")))


class SignatureParserTest(unittest.TestCase):
    def test_shift_expression_does_not_open_template_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "shift.hpp"
            header.write_text(
                "namespace example {\n"
                "class Value {\n"
                " private:\n"
                "  static constexpr unsigned high_bit = unsigned{1} << 63;\n"
                "};\n"
                "}\n"
            )
            _, errors = RUN["extract"](header)
        self.assertEqual(errors, [])

    def test_adjacent_template_closures_remain_separate_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "template.hpp"
            header.write_text(
                "namespace example {\n"
                "using Values = Outer<Inner<int>>;\n"
                "}\n"
            )
            _, errors = RUN["extract"](header)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
