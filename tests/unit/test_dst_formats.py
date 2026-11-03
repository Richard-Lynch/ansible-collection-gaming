#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugins/filter/dst_formats.py — the INI and Lua renderers. Stdlib only.

Each rule is tested in BOTH directions: an input that must
be transformed or refused, and a near-miss that must pass through untouched. The renderers' whole
job is type translation, and a translation bug is silent — DST does not reject a mod option set to
the string "True", it just ignores it.

THE ROUND-TRIP. When a Lua 5.1 interpreter is on PATH (luajit, lua5.1, or lua), the rendered Lua
is EXECUTED and its result serialised back and compared with the input: the only proof that the
text is not merely plausible-looking but parses to the same value. DST embeds Lua 5.1, so luajit
is the faithful interpreter. Where none is installed (a stock CI runner) those cases report
SKIPPED by name rather than passing, so a green run without an interpreter cannot be mistaken for
one with it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

# tests/unit/ -> plugins/filter/: two levels up from here, in any collection checkout.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins", "filter")
)

from dst_formats import (  # noqa: E402
    _FilterError,
    dst_ini_get,
    dst_ini_parse,
    dst_lua_return,
    dst_mod_key,
    dst_modoverrides,
    dst_mods_setup,
    to_dst_ini,
    to_dst_lua,
)

LUA = next((p for p in (shutil.which(n) for n in ("luajit", "lua5.1", "lua")) if p), None)

# A tiny Lua serialiser: walk the value and print JSON. Arrays (1..n with no other keys) become
# JSON lists, so the comparison distinguishes a list from a dict the way the game does.
_LUA_DUMP = r"""
local function is_array(t)
  local n = 0
  for _ in pairs(t) do n = n + 1 end
  for i = 1, n do if t[i] == nil then return false end end
  return n > 0
end
local function q(s)
  return '"' .. s:gsub('[%c"\\]', function(c)
    if c == '"' then return '\\"' elseif c == '\\' then return '\\\\' end
    return string.format('\\u%04x', c:byte())
  end) .. '"'
end
local function dump(v)
  local t = type(v)
  if t == "table" then
    if next(v) == nil then return "{}" end
    if is_array(v) then
      local out = {}
      for i = 1, #v do out[#out + 1] = dump(v[i]) end
      return "[" .. table.concat(out, ",") .. "]"
    end
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = k end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    local out = {}
    for _, k in ipairs(keys) do out[#out + 1] = q(tostring(k)) .. ":" .. dump(v[k]) end
    return "{" .. table.concat(out, ",") .. "}"
  elseif t == "string" then return q(v)
  elseif t == "boolean" then return tostring(v)
  elseif t == "number" then
    if v == math.floor(v) then return string.format("%d", v) end
    return string.format("%.17g", v)
  end
  error("cannot dump " .. t)
end
local chunk = assert(loadstring(io.read("*a")))
io.write(dump(chunk()))
"""


def lua_eval(src: str):
    """Execute a `return ...` Lua file and return its value as Python data."""
    assert LUA is not None
    dumper = _LUA_DUMP
    if os.path.basename(LUA) == "lua":
        # Lua 5.2+ removed loadstring; `load` takes a string there. luajit/5.1 have loadstring.
        dumper = "loadstring = loadstring or load\n" + dumper
    p = subprocess.run(
        [LUA, "-e", dumper],
        input=src,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if p.returncode != 0:
        raise AssertionError("Lua rejected the rendered file:\n%s\n---\n%s" % (p.stderr, src))
    return json.loads(p.stdout)


def normalise(v):
    """The value Lua would hand back: dict keys become strings, nil-valued keys vanish."""
    if isinstance(v, dict):
        return {str(k): normalise(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [normalise(x) for x in v]
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


class LuaRendering(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(to_dst_lua(True), "true")
        self.assertEqual(to_dst_lua(False), "false")
        self.assertEqual(to_dst_lua(None), "nil")
        self.assertEqual(to_dst_lua(3), "3")
        self.assertEqual(to_dst_lua(2.5), "2.5")
        # A YAML float that is integral renders as an integer, not "3.0".
        self.assertEqual(to_dst_lua(3.0), "3")

    def test_bool_is_not_rendered_as_a_number(self):
        # bool is a subclass of int in Python; checked FIRST or True would render as 1 — which
        # Lua treats as truthy, so a mod would read `1 == true` as false and ignore the option.
        self.assertEqual(to_dst_lua({"a": True}), "{\n  a = true,\n}")

    def test_strings_are_escaped(self):
        self.assertEqual(to_dst_lua('say "hi"\n'), '"say \\"hi\\"\\n"')
        self.assertEqual(to_dst_lua("back\\slash"), '"back\\\\slash"')

    def test_identifier_keys_are_bare_and_others_bracketed(self):
        out = to_dst_lua({"plain": 1, "Starting Items": 2, "workshop-378160973": 3, "end": 4, 7: 5})
        self.assertIn("plain = 1,", out)
        self.assertIn('["Starting Items"] = 2,', out)
        self.assertIn('["workshop-378160973"] = 3,', out)
        # A keyword as a bare key is a syntax error.
        self.assertIn('["end"] = 4,', out)
        self.assertIn("[7] = 5,", out)

    def test_near_miss_identifier_with_digits_stays_bare(self):
        self.assertIn("option_2 = 1,", to_dst_lua({"option_2": 1}))

    def test_nil_values_are_dropped_from_tables(self):
        self.assertEqual(to_dst_lua({"a": None}), "{}")

    def test_null_in_a_list_is_refused(self):
        with self.assertRaises(_FilterError):
            to_dst_lua([1, None, 3])

    def test_unsupported_and_non_finite_values_are_refused(self):
        with self.assertRaises(_FilterError):
            to_dst_lua(float("nan"))
        with self.assertRaises(_FilterError):
            to_dst_lua({"a": object()})

    def test_output_is_deterministic(self):
        a = to_dst_lua({"b": 1, "a": 2, 3: "x", 1: "y"})
        b = to_dst_lua({1: "y", 3: "x", "a": 2, "b": 1})
        self.assertEqual(a, b)

    def test_return_file_has_header_comments(self):
        out = dst_lua_return({"a": 1}, header="MANAGED BY ANSIBLE\nsecond")
        self.assertTrue(out.startswith("-- MANAGED BY ANSIBLE\n-- second\nreturn {"))
        self.assertTrue(out.endswith("}\n"))


@unittest.skipIf(LUA is None, "no Lua interpreter on PATH — round-trip NOT checked")
class LuaRoundTrip(unittest.TestCase):
    CASES = [
        {"override_enabled": True, "preset": "DST_CAVE", "overrides": {}},
        {
            "workshop-378160973": {
                "enabled": True,
                "configuration_options": {
                    "Starting Items": "default",
                    "SHOWPLAYERICONS": True,
                    "radius": 1.5,
                    "count": 12,
                    "nested": {"list": [1, "two", False], "end": "kw"},
                },
            },
        },
        {"quote": 'a "b" \\ c\nd\te', "unicode": "café — ok"},
    ]

    def test_rendered_lua_evaluates_to_the_input(self):
        for case in self.CASES:
            with self.subTest(case=case):
                self.assertEqual(lua_eval(dst_lua_return(case)), normalise(case))

    def test_integer_keys_one_to_n_are_a_lua_array(self):
        # Lua semantics, not a renderer quirk: {[1]=..,[2]=..} IS a sequence. Pinned so nobody
        # "fixes" the renderer into emitting something the game reads differently.
        self.assertEqual(lua_eval(dst_lua_return({1: "one", 2: "two"})), ["one", "two"])


class IniRendering(unittest.TestCase):
    def test_sections_and_types(self):
        out = to_dst_ini(
            {
                "GAMEPLAY": {"pvp": False, "max_players": 6},
                "NETWORK": {"cluster_name": "Den", "cluster_password": ""},
            }
        )
        self.assertEqual(
            out,
            "[GAMEPLAY]\npvp = false\nmax_players = 6\n\n"
            "[NETWORK]\ncluster_name = Den\ncluster_password = \n",
        )

    def test_none_omits_the_key_and_an_all_none_section(self):
        out = to_dst_ini({"STEAM": {"steam_group_id": None}, "MISC": {"a": 1, "b": None}})
        self.assertNotIn("[STEAM]", out)
        self.assertNotIn("b =", out)
        self.assertIn("a = 1", out)

    def test_extra_overrides_and_adds(self):
        out = to_dst_ini(
            {"NETWORK": {"tick_rate": 15}},
            extra={"NETWORK": {"tick_rate": 30}, "MISC": {"x": True}},
        )
        self.assertIn("tick_rate = 30", out)
        self.assertNotIn("tick_rate = 15", out)
        self.assertIn("[MISC]\nx = true", out)

    def test_newline_in_a_value_is_refused(self):
        # A newline would end the value and inject a key of the attacker's (or typo's) choosing.
        with self.assertRaises(_FilterError):
            to_dst_ini({"NETWORK": {"cluster_description": "a\nb = c"}})

    def test_bad_names_are_refused(self):
        with self.assertRaises(_FilterError):
            to_dst_ini({"network": {"a": 1}})
        with self.assertRaises(_FilterError):
            to_dst_ini({"NETWORK": {"Bad Key": 1}})

    def test_non_scalar_value_is_refused(self):
        with self.assertRaises(_FilterError):
            to_dst_ini({"NETWORK": {"a": [1]}})

    def test_header_is_a_comment(self):
        self.assertTrue(to_dst_ini({"A": {"b": 1}}, header="hi").startswith("; hi\n\n[A]"))


class IniReading(unittest.TestCase):
    # As the game rewrites a secondary shard's server.ini after assigning it an id.
    GAME_WRITTEN = (
        "; MANAGED BY ANSIBLE\n\n[NETWORK]\nserver_port = 11000\n\n"
        "[SHARD]\nis_master = false\nname = Caves\nid = 1484813796\n"
    )

    def test_reads_the_game_assigned_shard_id(self):
        self.assertEqual(dst_ini_get(self.GAME_WRITTEN, "SHARD", "id"), "1484813796")

    def test_same_key_in_another_section_is_not_it(self):
        text = "[NETWORK]\nid = 9\n[SHARD]\nname = Caves\n"
        self.assertIsNone(dst_ini_get(text, "SHARD", "id"))

    def test_absent_file_or_key(self):
        self.assertIsNone(dst_ini_get("", "SHARD", "id"))
        self.assertIsNone(dst_ini_get(None, "SHARD", "id"))
        self.assertIsNone(dst_ini_get("[SHARD]\n; id = 3\n", "SHARD", "id"))

    # Byte-for-byte as the game rewrites it: its own layout.
    GAME_LAYOUT = (
        "; MANAGED BY ANSIBLE (roles/service_dst_server)\n\n\n[NETWORK]\nserver_port = 11000\n\n\n"
        "[SHARD]\nis_master = false\nname = Caves\nid = 452527704\n\n\n"
        "[STEAM]\nmaster_server_port = 27017\nauthentication_port = 8767\n\n\n"
        "[ACCOUNT]\nencode_user_path = true\n"
    )

    def test_game_layout_parses_equal_to_our_rendering(self):
        ours = to_dst_ini(
            {
                "NETWORK": {"server_port": 11000},
                "SHARD": {"is_master": False, "name": "Caves", "id": 452527704},
                "STEAM": {"master_server_port": 27017, "authentication_port": 8767},
                "ACCOUNT": {"encode_user_path": True},
            },
            header="different header",
        )
        self.assertNotEqual(ours, self.GAME_LAYOUT)  # the bytes differ...
        self.assertEqual(dst_ini_parse(ours), dst_ini_parse(self.GAME_LAYOUT))  # ...the meaning not

    def test_a_real_change_is_not_equal(self):
        changed = self.GAME_LAYOUT.replace("server_port = 11000", "server_port = 11001")
        self.assertNotEqual(dst_ini_parse(changed), dst_ini_parse(self.GAME_LAYOUT))

    def test_a_dropped_id_is_not_equal(self):
        # The failure this exists to prevent: losing the game-assigned id must register.
        dropped = self.GAME_LAYOUT.replace("id = 452527704\n", "")
        self.assertNotEqual(dst_ini_parse(dropped), dst_ini_parse(self.GAME_LAYOUT))

    def test_round_trip_with_the_renderer(self):
        out = to_dst_ini({"SHARD": {"is_master": False, "id": 1484813796}})
        self.assertEqual(dst_ini_get(out, "SHARD", "id"), "1484813796")


class Mods(unittest.TestCase):
    MODS = [
        {
            "id": "378160973",
            "name": "Global Positions",
            "config": {"SHOWPLAYERICONS": True},
        },
        {"id": "workshop-666155465", "name": "Show Me"},
        {"id": "1111", "enabled": False, "config": {"x": 1}},
        {"id": "2222", "shards": ["Master"]},
    ]

    def test_mod_key_normalises_both_spellings(self):
        self.assertEqual(dst_mod_key("378160973"), "workshop-378160973")
        self.assertEqual(dst_mod_key("workshop-378160973"), "workshop-378160973")
        self.assertEqual(dst_mod_key(378160973), "workshop-378160973")

    def test_mod_key_refuses_a_non_id(self):
        for bad in ("workshop-", "abc", "workshop-12a", "12 34", ""):
            with self.subTest(bad=bad), self.assertRaises(_FilterError):
                dst_mod_key(bad)

    def test_setup_lists_only_enabled_mods_once(self):
        out = dst_mods_setup(self.MODS + [{"id": "378160973"}], collections=["9999"])
        self.assertIn('ServerModSetup("378160973")  -- Global Positions', out)
        self.assertIn('ServerModSetup("666155465")', out)
        self.assertNotIn("1111", out)
        self.assertEqual(out.count('"378160973"'), 1)
        self.assertIn('ServerModCollectionSetup("9999")', out)

    def test_overrides_respect_enabled_and_shards(self):
        master = dst_modoverrides(self.MODS, "Master")
        caves = dst_modoverrides(self.MODS, "Caves")
        self.assertEqual(
            master["workshop-378160973"],
            {"enabled": True, "configuration_options": {"SHOWPLAYERICONS": True}},
        )
        # No config -> no configuration_options key at all (the mod's own defaults apply).
        self.assertEqual(master["workshop-666155465"], {"enabled": True})
        self.assertNotIn("workshop-1111", master)
        self.assertIn("workshop-2222", master)
        self.assertNotIn("workshop-2222", caves)
        self.assertIn("workshop-378160973", caves)

    def test_duplicate_mod_is_refused(self):
        with self.assertRaises(_FilterError):
            dst_modoverrides([{"id": "1"}, {"id": "workshop-1"}], "Master")

    def test_empty_mod_list_renders_an_empty_table(self):
        self.assertEqual(dst_lua_return(dst_modoverrides([], "Master")), "return {}\n")


if __name__ == "__main__":
    if LUA is None:
        print("NOTE: no Lua interpreter on PATH — the Lua ROUND-TRIP cases are SKIPPED.")
    else:
        print("Lua round-trip interpreter: %s" % LUA)
    unittest.main(verbosity=1)
