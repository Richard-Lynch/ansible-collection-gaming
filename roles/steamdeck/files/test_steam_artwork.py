#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that every vendored tile's art is FINDABLE by restore. Stdlib only.

steam_artwork.py finds a shortcut's art in <artwork dir>/<slug(NAME)>/. A directory under any
other name composes, commits and lints fine, and then restores NOTHING: the run reports
"no vendored art" and the tile stays blank — for example art for "DST: play on host" placed under
"dst-play", which slug() never produces. MANIFEST.json (the record of which shortcut NAME each
set belongs to) is excluded from the rsync to the device, so the only link from name to art on
the Deck is slug() itself.

So these tests use the role's OWN slug() and its OWN restore lookup (run as a script, exactly as
the play runs it), never a re-implementation that could agree with a wrong slug. Both directions:
a real tree must pass, and a mis-slugged fixture must FAIL.

THE REAL TREE is the caller's data (steamdeck_artwork_dir), not the role's, so it is named by the
STEAMDECK_ARTWORK_DIR environment variable. Unset, the RealTree cases report SKIPPED by name, so a
green run without a tree cannot be mistaken for one with it; the fixtures always run.
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

from steam_artwork import slug  # noqa: E402

GRID = os.environ.get("STEAMDECK_ARTWORK_DIR", "")
SCRIPT = os.path.join(HERE, "steam_artwork.py")
NOT_TILES = {"_device"}  # backup pulls land here; not a shortcut's set


def manifest_names(grid: str) -> list[str]:
    with open(os.path.join(grid, "MANIFEST.json")) as fh:
        return list(json.load(fh)["shortcuts"])


def tile_dirs(grid: str) -> set[str]:
    return {
        d
        for d in os.listdir(grid)
        if os.path.isdir(os.path.join(grid, d)) and d not in NOT_TILES and not d.startswith(".")
    }


def restore_dry_run(grid: str, names: list[str]) -> dict:
    """Run the REAL restore lookup (no --apply) with a fake appid per name."""
    with tempfile.TemporaryDirectory() as dest:
        out = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--vendor-dir",
                grid,
                "--grid-dir",
                dest,
                "--shortcuts",
                json.dumps({n: 1000 + i for i, n in enumerate(names)}),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    return json.loads(out)


def problems(grid: str) -> list[str]:
    names = manifest_names(grid)
    found = []
    result = restore_dry_run(grid, names)
    for name in result["no_vendored_art"]:
        found.append("%r: restore finds no art (looks in %s/)" % (name, slug(name)))
    expected = {slug(n) for n in names}
    for d in sorted(tile_dirs(grid) - expected):
        found.append("%s/: matches slug() of no shortcut in MANIFEST.json, so never restored" % d)
    with open(os.path.join(grid, "MANIFEST.json")) as fh:
        listed = json.load(fh)["shortcuts"]
    for name, files in listed.items():
        for f in files:
            if not os.path.isfile(os.path.join(grid, slug(name), f)):
                found.append("%r: MANIFEST lists %s, absent from %s/" % (name, f, slug(name)))
    return found


def fixture(root: str, dirs: dict[str, list[str]], manifest: dict[str, list[str]]) -> str:
    for d, files in dirs.items():
        os.makedirs(os.path.join(root, d))
        for f in files:
            open(os.path.join(root, d, f), "wb").close()
    with open(os.path.join(root, "MANIFEST.json"), "w") as fh:
        json.dump({"shortcuts": manifest}, fh)
    return root


@unittest.skipUnless(GRID, "STEAMDECK_ARTWORK_DIR is not set: no real artwork tree to check")
class RealTree(unittest.TestCase):
    def test_every_vendored_set_is_restorable(self):
        self.assertEqual(problems(GRID), [])

    def test_the_tree_is_not_empty(self):
        # A tree with no shortcuts passes the check above vacuously; require a population.
        self.assertTrue(manifest_names(GRID), "MANIFEST.json lists no shortcuts")
        self.assertTrue(tile_dirs(GRID), "no tile directories under %s" % GRID)


class MisSlugged(unittest.TestCase):
    def test_draft_slug_fails(self):
        # Art under "dst-play" for "DST: play on host": a plausible slug slug() never produces.
        with tempfile.TemporaryDirectory() as root:
            fixture(root, {"dst-play": ["art.png"]}, {"DST: play on host": ["art.png"]})
            found = problems(root)
        self.assertTrue(any("restore finds no art" in p for p in found), found)
        self.assertTrue(any(p.startswith("dst-play/") for p in found), found)

    def test_correct_slug_passes(self):
        # Near-miss: the same tile under slug(name) is clean.
        with tempfile.TemporaryDirectory() as root:
            fixture(
                root,
                {"dst-play-on-host": ["art.png"], "_device": []},
                {"DST: play on host": ["art.png"]},
            )
            self.assertEqual(problems(root), [])

    def test_manifest_listing_a_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(
                root,
                {"wake-host": ["art.png"]},
                {"Wake: host": ["art.png", "art_hero.png"]},
            )
            self.assertEqual(len(problems(root)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
