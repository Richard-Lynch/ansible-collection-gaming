#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for dst_mods_report.py — the modinfo.lua reader. Stdlib only.

The parser's contract is honesty, not completeness: a literal is read exactly, and anything it
does not evaluate comes back as an Expr carrying its source — never as a plausible wrong value.
Both directions are tested: literals that must parse, and expressions that must NOT be guessed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dst_mods_report import (  # noqa: E402
    Expr,
    describe_options,
    report_mod,
    top_level_assignments,
)

MODINFO = r"""
-- A representative modinfo.lua, with the shapes real mods use.
name = "Global Positions"
description = [[Shows players on the map.
Second line.]]
author = "rezecib"
version = "1.7.5"
dst_compatible = true
all_clients_require_mod = true
client_only_mod = false
server_only_mod = false
--[[ a long
comment with name = "not a global" ]]
local speeds = {}
for i = 1, 5 do speeds[i] = {description = ""..i, data = i} end

configuration_options =
{
    {
        name = "SHOWPLAYERICONS",
        label = "Player Icons",
        options = {
            {description = "Show", data = true},
            {description = "Hide", data = false},
        },
        default = true,
    },
    {
        name = "Starting Items",
        options = { {description = "Default", data = "default"}, {description = "None", data = "none"} },
        default = "default",
    },
    {
        name = "SPEED",
        options = speeds,
        default = 3,
    },
    {
        name = "OFFSET",
        options = { {description = "-1", data = -1}, {description = "0.5", data = 0.5} },
        default = -1,
    },
    {
        name = "LABEL",
        default = "a" .. "b",
    },
}
"""


class Parsing(unittest.TestCase):
    def setUp(self):
        self.g = top_level_assignments(MODINFO)

    def test_globals(self):
        self.assertEqual(self.g["name"], "Global Positions")
        self.assertEqual(self.g["version"], "1.7.5")
        self.assertIs(self.g["all_clients_require_mod"], True)
        self.assertIs(self.g["client_only_mod"], False)
        self.assertTrue(self.g["description"].startswith("Shows players on the map."))

    def test_a_name_inside_a_long_comment_is_not_a_global(self):
        self.assertEqual(self.g["name"], "Global Positions")

    def test_option_names_defaults_and_values(self):
        opts = {o["name"]: o for o in describe_options(self.g["configuration_options"])}
        self.assertEqual(
            set(opts), {"SHOWPLAYERICONS", "Starting Items", "SPEED", "OFFSET", "LABEL"}
        )
        self.assertEqual(opts["SHOWPLAYERICONS"]["values"], [True, False])
        self.assertIs(opts["SHOWPLAYERICONS"]["default"], True)
        self.assertEqual(opts["Starting Items"]["values"], ["default", "none"])
        self.assertEqual(opts["OFFSET"]["values"], [-1, 0.5])

    def test_values_built_elsewhere_are_an_expression_not_a_guess(self):
        opts = {o["name"]: o for o in describe_options(self.g["configuration_options"])}
        self.assertIsInstance(opts["SPEED"]["values"], Expr)
        self.assertEqual(opts["SPEED"]["values"], "speeds")
        # A literal followed by an operator is an expression, not its first literal.
        self.assertIsInstance(opts["LABEL"]["default"], Expr)
        self.assertEqual(opts["LABEL"]["default"], '"a" .. "b"')


class NoHang(unittest.TestCase):
    """Regression: a function inside a table hung the parser forever (found on a real mod).

    Each case runs in a subprocess with a timeout, so a regression FAILS instead of hanging the
    suite — a hang is exactly the failure mode under test.
    """

    CASES = [
        "x = { function() return 1 end, 2 }\ny = 3",
        "t = { return }\nz = 4",
        "c = { {name='A', hover=function(v) if v then return 'x' end end, default=1} }",
        "d = { ) , ] }\ne = 5",
    ]

    def test_pathological_tables_terminate(self):
        import subprocess

        here = os.path.dirname(os.path.abspath(__file__))
        for src in self.CASES:
            with self.subTest(src=src):
                p = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]);"
                        "from dst_mods_report import top_level_assignments as t;"
                        "print(sorted(t(sys.argv[2])))",
                        here,
                        src,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_function_valued_field_does_not_hide_its_siblings(self):
        g = top_level_assignments(self.CASES[2])
        self.assertEqual(g["c"][0]["name"], "A")
        self.assertEqual(g["c"][0]["default"], 1)
        self.assertIsInstance(g["c"][0]["hover"], Expr)


class CallValues(unittest.TestCase):
    """Regression: a function-call value swallowed every later global (as in a real modinfo.lua)."""

    SRC = (
        'name = en_zh("Combined Status", "zh")\n'
        'description = en_zh("Displays Health.", "zh")\n'
        'version = "1.9.8"\n'
        "all_clients_require_mod = false\n"
        "client_only_mod = true\n"
    )

    def test_globals_after_a_call_are_still_read(self):
        g = top_level_assignments(self.SRC)
        self.assertIsInstance(g["name"], Expr)
        self.assertEqual(g["name"], 'en_zh("Combined Status", "zh")')
        self.assertEqual(g["version"], "1.9.8")
        self.assertIs(g["client_only_mod"], True)
        self.assertIs(g["all_clients_require_mod"], False)

    def test_expression_text_stops_at_its_last_token(self):
        g = top_level_assignments('name = f("x")\n\n-- a comment\nversion = "1"\n')
        self.assertEqual(g["name"], 'f("x")')

    def test_near_miss_concatenation_is_one_value(self):
        g = top_level_assignments('label = "a" .. b\nnext = 1\n')
        self.assertEqual(g["label"], '"a" .. b')
        self.assertEqual(g["next"], 1)


class Report(unittest.TestCase):
    def test_downloaded_mod_and_config_checks(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "ugc", "content", "322330", "378160973")
            os.makedirs(d)
            with open(os.path.join(d, "modinfo.lua"), "w") as f:
                f.write(MODINFO)
            r = report_mod(
                root,
                {
                    "key": "workshop-378160973",
                    "name": "GP",
                    "config": {
                        "SHOWPLAYERICONS": "yes",
                        "Starting Items": "none",
                        "TYPO": 1,
                    },
                },
            )
        self.assertTrue(r["downloaded"])
        self.assertEqual(r["name"], "Global Positions")
        self.assertEqual(r["unknown_config_keys"], ["TYPO"])
        # "yes" is not one of the offered values [true, false] — the YAML-string trap.
        self.assertEqual(
            r["config_values_not_offered"],
            [
                {
                    "option": "SHOWPLAYERICONS",
                    "declared": "yes",
                    "allowed": [True, False],
                }
            ],
        )
        json.dumps(r, default=str)  # serialisable, as main() prints it

    def test_not_downloaded(self):
        with tempfile.TemporaryDirectory() as root:
            r = report_mod(root, {"key": "workshop-1", "config": {}})
        self.assertFalse(r["downloaded"])

    def test_legacy_mods_directory_is_found_too(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "server", "mods", "workshop-42")
            os.makedirs(d)
            with open(os.path.join(d, "modinfo.lua"), "w") as f:
                f.write('name = "Old"\nconfiguration_options = {}\n')
            r = report_mod(root, {"key": "workshop-42"})
        self.assertTrue(r["downloaded"])
        self.assertEqual(r["options"], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
