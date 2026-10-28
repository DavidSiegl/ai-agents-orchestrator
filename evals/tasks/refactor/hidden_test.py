"""The hidden acceptance test of the eval task `refactor`: money and item_line in invoice/render.py, its output
unchanged (task.md). evals/acceptance.py says how it is run."""

import ast
import importlib.util
import pathlib
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # evals/, for acceptance.py
from acceptance import Acceptance, main

ITEMS = [
    [],
    [("Widget", 3, Decimal("2.50")), ("Gadget", 1, Decimal(10))],
    [("A name longer than twenty characters", 12, Decimal("0.125")), ("Half cent", 3, Decimal("0.335"))],
    [("Bulk", 1000, Decimal("1234.5678")), ("Free", 2, Decimal(0))],
]
RATES = [Decimal(0), Decimal("0.2"), Decimal("0.075"), Decimal("0.19"), Decimal(1)]


class Refactor(Acceptance):
    """The renderers' output is unchanged, and money and item_line are the one home of rounding and item lines."""

    def original_render(self):
        """invoice/render.py as the task found it, imported as a module of its own."""
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d, "original_render.py")
            path.write_text(self.original("invoice/render.py"))
            spec = importlib.util.spec_from_file_location("original_render", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def check_output_unchanged(self):
        """Both renderers give what they gave, for every input."""
        from invoice import render_credit_note, render_invoice
        before = self.original_render()
        for items in ITEMS:
            for rate in RATES:
                with self.subTest(items=items, rate=rate):
                    self.assertEqual(render_invoice("ACME", items, rate), before.render_invoice("ACME", items, rate))
                    self.assertEqual(render_credit_note("ACME", items, rate),
                                     before.render_credit_note("ACME", items, rate))

    def check_money(self):
        """money rounds to the cent, half away from zero."""
        from invoice.render import money
        self.assertEqual(money(Decimal("2.675")), Decimal("2.68"))
        self.assertEqual(money(Decimal("-0.005")), Decimal("-0.01"))
        self.assertEqual(str(money(Decimal(10))), "10.00")

    def check_item_line(self):
        """item_line is an item's line without its line end, for a negative price too."""
        from invoice.render import item_line
        self.assertEqual(item_line("Widget", 3, Decimal("2.50")), "Widget                  3 x      2.50 =       7.50")
        self.assertEqual(item_line("Widget", 3, Decimal("-2.50")), "Widget                  3 x     -2.50 =      -7.50")

    def check_rounding_has_one_home(self):
        """render.py names quantize once."""
        tree = ast.parse(pathlib.Path(self.repo, "invoice", "render.py").read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "quantize"]
        self.assertEqual(len(calls), 1, "render.py should name quantize once, in money")

    def check_the_existing_tests_unchanged(self):
        """tests/test_render.py is as the task found it."""
        self.assertEqual(pathlib.Path(self.repo, "tests", "test_render.py").read_text(),
                         self.original("tests/test_render.py"))


if __name__ == "__main__":
    sys.exit(main(Refactor))
