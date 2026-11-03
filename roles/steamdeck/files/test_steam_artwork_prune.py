#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for steam_artwork_prune.py — the ONLY code in this role that deletes the user's files.

Run: python3 roles/steamdeck/files/test_steam_artwork_prune.py   (stdlib only)

Exercises the real script end to end against a fixture grid directory, through its command line
exactly as the role calls it: a preview must change nothing, an apply must delete ONLY the
previewed orphans, and every other file — the current tiles' art, real Steam games' art, names it
does not recognise — must be byte-identical afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "steam_artwork_prune.py")

CURRENT = 3795321282  # a protected shortcut appid (in the shortcut range, >= 2**31)
# In the shortcut range (>= 2**31), but no current shortcut has it.
ORPHAN = 4157280562
STEAM_GAME = 322330  # Don't Starve Together's real Steam appid — custom art for it must survive


def digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run(*args: str) -> tuple[int, dict]:
    p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, check=False)
    return p.returncode, json.loads(p.stdout)


class PruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.grid = self.tmp.name
        self.files = {
            # the current tile's full set — protected
            f"{CURRENT}.png": b"cur-capsule",
            f"{CURRENT}p.png": b"cur-portrait",
            f"{CURRENT}_hero.png": b"cur-hero",
            f"{CURRENT}_logo.png": b"cur-logo",
            f"{CURRENT}_icon.png": b"cur-icon",
            f"{CURRENT}.json": b'{"logo": 1}',
            # a removed tile's art — the orphans
            f"{ORPHAN}.png": b"old-capsule",
            f"{ORPHAN}_hero.png": b"old-hero",
            f"{ORPHAN}.json": b'{"logo": 2}',
            # a real Steam game's custom art — never touched
            f"{STEAM_GAME}_hero.png": b"steam-game-hero",
            # names it does not recognise — kept, not guessed at
            "12354063639852875776.png": b"64-bit-gameid",
            f"{ORPHAN}_notes.txt": b"something else",
        }
        for name, data in self.files.items():
            with open(os.path.join(self.grid, name), "wb") as fh:
                fh.write(data)
        self.before = {n: digest(os.path.join(self.grid, n)) for n in self.files}
        self.orphans = sorted([f"{ORPHAN}.png", f"{ORPHAN}_hero.png", f"{ORPHAN}.json"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def preview(self) -> dict:
        rc, out = run("--grid-dir", self.grid, "--protect", json.dumps([CURRENT]))
        self.assertEqual(rc, 0, out)
        return out

    def test_preview_classifies_and_changes_nothing(self) -> None:
        out = self.preview()
        self.assertEqual(sorted(o["name"] for o in out["orphans"]), self.orphans)
        self.assertEqual(out["counts"]["current"], 6)
        self.assertEqual(out["counts"]["steam_game"], 1)
        self.assertEqual(out["counts"]["unknown"], 2)
        self.assertEqual(out["steam_game_appids"], [STEAM_GAME])
        self.assertFalse(out["changed"])
        after = {n: digest(os.path.join(self.grid, n)) for n in os.listdir(self.grid)}
        self.assertEqual(after, self.before)

    def test_apply_deletes_exactly_the_orphans_and_nothing_else(self) -> None:
        rc, out = run(
            "--grid-dir", self.grid,
            "--protect", json.dumps([CURRENT]),
            "--expect", json.dumps(self.orphans),
            "--apply",
        )  # fmt: skip
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(out["deleted"]), self.orphans)
        remaining = sorted(os.listdir(self.grid))
        self.assertEqual(remaining, sorted(set(self.files) - set(self.orphans)))
        for n in remaining:
            self.assertEqual(digest(os.path.join(self.grid, n)), self.before[n], n)

    def test_apply_refuses_when_the_orphan_set_changed(self) -> None:
        # The play hands back the PREVIEW's list; if the grid changed meanwhile, delete nothing.
        rc, out = run(
            "--grid-dir", self.grid,
            "--protect", json.dumps([CURRENT]),
            "--expect", json.dumps(self.orphans[:1]),
            "--apply",
        )  # fmt: skip
        self.assertEqual(rc, 3)
        self.assertEqual(out["deleted"], [])
        self.assertEqual(sorted(os.listdir(self.grid)), sorted(self.files))

    def test_a_signed_appid_in_protect_is_refused(self) -> None:
        # Steam stores appids SIGNED in shortcuts.vdf; files carry the UNSIGNED form. A signed
        # value would protect nothing and every current tile's art would look orphaned.
        signed = CURRENT - 2**32
        rc, out = run("--grid-dir", self.grid, "--protect", json.dumps([signed]))
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_unprotected_current_tile_would_be_an_orphan(self) -> None:
        # Near-miss guard: with an EMPTY protect list the current tile's art classifies as orphan
        # — which is exactly why the role refuses to delete when no shortcut was read.
        rc, out = run("--grid-dir", self.grid, "--protect", "[]")
        self.assertEqual(rc, 0)
        self.assertIn(CURRENT, out["orphan_appids"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
