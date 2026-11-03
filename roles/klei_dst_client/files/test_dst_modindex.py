#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for dst_modindex.py — the DST client modindex codec and editor. Stdlib only.

The fixture is a modindex as a real client writes it (no secrets in it). The tests pin the two
properties that matter: the codec round-trips the game's own bytes, and an edit changes ONLY the
declared mods' `enabled` field — a writer that dropped the game's cached modinfo for other mods
would silently disable everything else the player had switched on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dst_modindex import (  # noqa: E402
    apply_wanted,
    decode,
    dump,
    encode,
    mod_key,
    parse,
)

LIVE = (
    "KLEI     1DAQAAABAAAABPAAAAUAAAAHjaLcjBDYAgDAXQVRzAgw7ALE2Vr2nEQlqEA3F3L77jM9THdBpRnLcE8oJdOBEat"
    "FJnU9EzHJwc86W5K3ERajCXrGFd/rxz9DDe9wP1Rh+A"
)
LIVE_LUA = "return {disable_special_event_warning=false,known_api_version=10,known_mods={}}"


class Codec(unittest.TestCase):
    def test_decodes_the_live_file(self):
        self.assertEqual(decode(LIVE), LIVE_LUA)

    def test_encode_round_trips(self):
        self.assertEqual(decode(encode(LIVE_LUA)), LIVE_LUA)

    def test_refuses_a_non_klei_file(self):
        with self.assertRaises(ValueError):
            decode("return {}")


class Table(unittest.TestCase):
    def test_parse_the_live_table(self):
        self.assertEqual(
            parse(LIVE_LUA),
            {
                "disable_special_event_warning": False,
                "known_api_version": 10,
                "known_mods": {},
            },
        )

    def test_bracketed_keys_and_nesting_round_trip(self):
        src = (
            'return {known_mods={["workshop-376333686"]={enabled=true,modinfo_message="a\\"b",'
            'temp_disabled=false,version="1.9.8"}},known_api_version=10}'
        )
        state = parse(src)
        self.assertTrue(state["known_mods"]["workshop-376333686"]["enabled"])
        self.assertEqual(parse("return " + dump(state)), state)


class Edit(unittest.TestCase):
    def test_enables_an_unknown_mod_with_a_minimal_entry(self):
        state, report = apply_wanted(parse(LIVE_LUA), ["376333686"], [])
        self.assertEqual(state["known_mods"]["workshop-376333686"], {"enabled": True})
        self.assertEqual(report["workshop-376333686"], {"before": None, "after": True})

    def test_flips_only_enabled_and_keeps_the_games_other_fields_and_other_mods(self):
        state = {
            "known_mods": {
                "workshop-1": {
                    "enabled": False,
                    "modinfo_message": "cached",
                    "version": "2",
                },
                "workshop-999": {"enabled": True, "version": "x"},
            }
        }
        new, report = apply_wanted(state, ["workshop-1"], [])
        self.assertEqual(
            new["known_mods"]["workshop-1"],
            {"enabled": True, "modinfo_message": "cached", "version": "2"},
        )
        # Near-miss: a mod nobody declared is untouched.
        self.assertEqual(new["known_mods"]["workshop-999"], {"enabled": True, "version": "x"})
        self.assertEqual(report["workshop-1"], {"before": False, "after": True})

    def test_disabling_an_unknown_mod_does_not_invent_an_entry(self):
        new, report = apply_wanted(parse(LIVE_LUA), [], ["42"])
        self.assertEqual(new["known_mods"], {})
        self.assertEqual(report["workshop-42"], {"before": None, "after": None})

    def test_mod_key(self):
        self.assertEqual(mod_key(" 376333686"), "workshop-376333686")
        self.assertEqual(mod_key("workshop-1"), "workshop-1")
        with self.assertRaises(ValueError):
            mod_key("workshop-x")


class Cli(unittest.TestCase):
    def run_cli(self, path, wanted, *extra):
        p = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "dst_modindex.py"),
                path,
                json.dumps(wanted),
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_preview_writes_nothing_and_apply_writes_a_readable_file_and_a_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "modindex")
            with open(path, "w") as fh:
                fh.write(LIVE)
            r = self.run_cli(path, {"enable": ["376333686"]})
            self.assertTrue(r["changed"])
            self.assertFalse(r["applied"])
            self.assertEqual(open(path).read(), LIVE)

            r = self.run_cli(path, {"enable": ["376333686"]}, "--apply")
            self.assertTrue(r["applied"])
            self.assertEqual(open(path + ".iaac-bak").read(), LIVE)
            self.assertTrue(
                parse(decode(open(path).read()))["known_mods"]["workshop-376333686"]["enabled"]
            )

            # Idempotent: a second run changes nothing.
            r = self.run_cli(path, {"enable": ["376333686"]}, "--apply")
            self.assertFalse(r["changed"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
