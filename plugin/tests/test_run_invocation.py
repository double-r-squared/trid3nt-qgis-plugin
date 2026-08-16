"""Unit tests for the ``!run`` chat-invocation parser (ADR 0114).

Pure parser -- no QGIS, no network. Run with:

    cd plugin
    ../venvs/agent/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from plugin.net.run_invocation import (  # noqa: E402
    RunInvocation,
    is_run_prefix,
    parse_run_invocation,
)


class TestPrefixAnchoring(unittest.TestCase):
    def test_non_run_returns_none(self):
        self.assertIsNone(parse_run_invocation("model the flood in Boulder"))

    def test_mid_sentence_mention_flows_to_chat(self):
        # A message MENTIONING !run mid-sentence is NOT an invocation.
        self.assertIsNone(parse_run_invocation("you can use !run to call a tool"))
        self.assertIsNone(parse_run_invocation("what does !run do?"))

    def test_prefix_must_be_exact_token(self):
        # !running / !runx are not the !run token.
        self.assertIsNone(parse_run_invocation("!running fetch_dem()"))
        self.assertIsNone(parse_run_invocation("!runx"))

    def test_leading_whitespace_still_anchored(self):
        # The dock strips before calling; the parser also strips defensively.
        inv = parse_run_invocation("   !run fetch_dem()")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.name, "fetch_dem")


class TestHelp(unittest.TestCase):
    def test_bare_run_is_help(self):
        inv = parse_run_invocation("!run")
        self.assertTrue(inv.help)
        self.assertIsNone(inv.name)

    def test_run_help_is_help(self):
        inv = parse_run_invocation("!run help")
        self.assertTrue(inv.help)

    def test_run_help_case_insensitive(self):
        self.assertTrue(parse_run_invocation("!run HELP").help)


class TestKwargsForm(unittest.TestCase):
    def test_string_kwarg(self):
        inv = parse_run_invocation('!run geocode_location(query="Boulder, Colorado")')
        self.assertEqual(inv.name, "geocode_location")
        self.assertEqual(inv.args, {"query": "Boulder, Colorado"})
        self.assertIsNone(inv.error)

    def test_list_and_string_kwargs(self):
        inv = parse_run_invocation(
            '!run fetch_dem(bbox=[-85.4, 29.9, -85.3, 30.0], source="3dep")'
        )
        self.assertEqual(inv.name, "fetch_dem")
        self.assertEqual(
            inv.args, {"bbox": [-85.4, 29.9, -85.3, 30.0], "source": "3dep"}
        )

    def test_numeric_bool_null_kwargs(self):
        inv = parse_run_invocation(
            "!run t(n=5, f=1.5, flag=True, off=False, empty=None)"
        )
        self.assertEqual(
            inv.args, {"n": 5, "f": 1.5, "flag": True, "off": False, "empty": None}
        )

    def test_nested_dict_kwarg(self):
        inv = parse_run_invocation('!run t(opts={"a": [1, 2], "b": "x"})')
        self.assertEqual(inv.args, {"opts": {"a": [1, 2], "b": "x"}})

    def test_bare_name_no_parens(self):
        inv = parse_run_invocation("!run list_categories")
        self.assertEqual(inv.name, "list_categories")
        self.assertEqual(inv.args, {})

    def test_empty_parens(self):
        inv = parse_run_invocation("!run list_categories()")
        self.assertEqual(inv.name, "list_categories")
        self.assertEqual(inv.args, {})


class TestJsonForm(unittest.TestCase):
    def test_json_object_args(self):
        inv = parse_run_invocation('!run fetch_dem {"bbox": [-85.4, 29.9, -85.3, 30.0]}')
        self.assertEqual(inv.name, "fetch_dem")
        self.assertEqual(inv.args, {"bbox": [-85.4, 29.9, -85.3, 30.0]})

    def test_json_non_object_is_error(self):
        inv = parse_run_invocation("!run fetch_dem [1, 2, 3]")
        # Not a JSON-object form (no leading name+brace) and not valid python
        # kwargs -> error.
        self.assertIsNotNone(inv.error)

    def test_malformed_json_is_error(self):
        inv = parse_run_invocation('!run fetch_dem {"bbox": [1, 2,}')
        self.assertIsNotNone(inv.error)
        self.assertIsNone(inv.name)


class TestMalformed(unittest.TestCase):
    def test_positional_arg_rejected(self):
        inv = parse_run_invocation("!run fetch_dem([1, 2, 3])")
        self.assertIsNotNone(inv.error)
        self.assertIn("keyword", inv.error.lower())

    def test_non_literal_value_rejected(self):
        # A name reference (not a literal) must be rejected -- never eval'd.
        inv = parse_run_invocation("!run t(x=some_variable)")
        self.assertIsNotNone(inv.error)

    def test_expression_value_rejected(self):
        inv = parse_run_invocation("!run t(x=1+2)")
        self.assertIsNotNone(inv.error)

    def test_garbage_is_error_not_crash(self):
        inv = parse_run_invocation("!run (((")
        self.assertIsNotNone(inv.error)

    def test_error_includes_usage(self):
        inv = parse_run_invocation("!run fetch_dem([1,2])")
        self.assertIn("!run", inv.error)


class TestSharedPredicate(unittest.TestCase):
    """The blue-``!run`` highlight (is_run_prefix) must fire on EXACTLY the
    inputs that route direct (parse_run_invocation is not None). One shared
    predicate -> the visual signal can never disagree with the routing."""

    CASES = [
        "!run",
        "!run help",
        "!run fetch_dem(bbox=[1,2,3,4])",
        '!run geocode_location(query="Boulder")',
        "!run fetch_dem([1,2])",          # anchored but malformed -> still direct
        "!run (((",                        # anchored garbage -> still direct
        "   !run fetch_dem()",            # leading ws, anchored
        "model the flood",               # chat
        "use !run to invoke a tool",      # mid-sentence mention -> chat
        "!running fetch_dem()",           # not the token -> chat
        "!runx",                          # not the token -> chat
        "what does !run do?",            # mid-sentence -> chat
    ]

    def test_highlight_on_iff_routes_direct(self):
        for text in self.CASES:
            highlight_on = is_run_prefix(text)
            routes_direct = parse_run_invocation(text) is not None
            self.assertEqual(
                highlight_on,
                routes_direct,
                msg=f"disagreement for {text!r}: highlight={highlight_on} "
                f"route={routes_direct}",
            )


if __name__ == "__main__":
    unittest.main()
