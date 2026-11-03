#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find, and optionally delete, ORPHANED non-Steam grid artwork. Stdlib only.

WHY. Steam names grid art after an appid, and a non-Steam shortcut's appid is derived from its
exe+name. Removing (or renaming) a tile therefore leaves its art on disk under an appid nothing
references. steam_artwork.py restore only ever ADDS files, so without this the grid directory
only grows.

THE ORPHAN DEFINITION IS STRICT, because the same directory holds custom art for real Steam
games, which nothing in the repo can restore. A file is an orphan only if ALL of these hold:

  1. its name is <appid><variant>.<ext> with a KNOWN variant/extension (KNOWN below) — anything
     else is "unknown" and kept;
  2. <appid> is in the NON-STEAM SHORTCUT range: Steam generates shortcut appids as
     crc32(exe+name) | 0x80000000, so they are >= 2**31 and < 2**32. Real Steam store appids are
     small. Anything below 2**31 is "steam_game" and kept; anything >= 2**32 is kept as "unknown";
  3. <appid> is NOT the unsigned appid of ANY shortcut currently in shortcuts.vdf (--protect),
     whoever owns it — Steam ROM Manager's tiles included.

Everything that fails any test is kept. The caller asserts on the classification before deleting.

--apply DELETES ONLY what --expect names, and only if a fresh classification produces EXACTLY that
set. The play previews, backs up what is not recoverable from the repo, and then hands the
preview's list back here: a grid that changed in between (a new tile, Steam writing art) makes the
sets differ and nothing is deleted.

Output is JSON: counts, bytes, file names, sizes, sha256 — never file contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

SHORTCUT_MIN = 2**31  # the high bit Steam sets on every generated shortcut appid
APPID_LIMIT = 2**32  # unsigned 32-bit; a larger number is not a shortcut appid

# The variants Steam's grid directory uses: capsule (""), portrait ("p"), hero, logo, icon, and
# the logo-position sidecar (<appid>.json), as found in a Deck's grid directory.
KNOWN = re.compile(
    r"\A(?P<appid>[0-9]+)(?P<variant>p|_hero|_logo|_icon)?\.(?P<ext>png|jpg|jpeg|webp|ico|json)\Z"
)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root: str) -> list[str]:
    """sha256 of every file under the vendored tree, skipping backup pulls (_device) and dotfiles."""
    out = set()
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != "_device" and not x.startswith(".")]
        for f in files:
            if not f.startswith(".") and f != "MANIFEST.json":
                out.add(sha256(os.path.join(d, f)))
    return sorted(out)


def classify(name: str, protect: set[int]) -> tuple[str, int | None, str]:
    m = KNOWN.match(name)
    if not m or (m.group("ext") == "json" and m.group("variant")):
        return "unknown", None, ""
    appid = int(m.group("appid"))
    suffix = name[len(m.group("appid")) :]
    if appid < SHORTCUT_MIN:
        return "steam_game", appid, suffix
    if appid >= APPID_LIMIT:
        return "unknown", appid, suffix
    if appid in protect:
        return "current", appid, suffix
    return "orphan", appid, suffix


def survey(grid: str, protect: set[int], vendored: set[str]) -> dict:
    entries = []
    for name in sorted(os.listdir(grid)):
        path = os.path.join(grid, name)
        if not os.path.isfile(path) or os.path.islink(path):
            entries.append({"name": name, "class": "unknown", "appid": None, "suffix": ""})
            continue
        cls, appid, suffix = classify(name, protect)
        st = os.stat(path)
        e = {
            "name": name,
            "class": cls,
            "appid": appid,
            "suffix": suffix,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "sha256": sha256(path),
        }
        if cls == "orphan":
            e["vendored"] = e["sha256"] in vendored
        entries.append(e)

    def of(cls: str) -> list[dict]:
        return [e for e in entries if e["class"] == cls]

    orphans = of("orphan")
    suffixes: dict[str, int] = {}
    for e in entries:
        if e["class"] != "unknown":
            suffixes[e["suffix"]] = suffixes.get(e["suffix"], 0) + 1
    return {
        "grid_dir": grid,
        "counts": {c: len(of(c)) for c in ("current", "steam_game", "orphan", "unknown")},
        "orphan_bytes": sum(e["size"] for e in orphans),
        "orphans": [
            {k: e[k] for k in ("name", "appid", "size", "sha256", "vendored")} for e in orphans
        ],
        "orphan_appids": sorted({e["appid"] for e in orphans}),
        "unvendored_orphans": [e["name"] for e in orphans if not e["vendored"]],
        "steam_game_appids": sorted({e["appid"] for e in of("steam_game")}),
        "current_appids_with_art": sorted({e["appid"] for e in of("current")}),
        "unknown_names": [e["name"] for e in of("unknown")],
        "suffixes_seen": suffixes,
        # Everything that is NOT deleted, fingerprinted, so a read-back can prove it untouched.
        "kept": {
            e["name"]: [e.get("size"), e.get("mtime_ns"), e.get("sha256")]
            for e in entries
            if e["class"] != "orphan"
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--hash-tree", help="print the sha256 set of a vendored tree and exit")
    ap.add_argument("--grid-dir", help="Steam's grid directory")
    ap.add_argument("--protect", help="JSON list of every current shortcut's UNSIGNED appid")
    ap.add_argument("--vendor-hashes", default="[]", help="JSON list of vendored sha256s")
    ap.add_argument("--expect", help="JSON list of file names --apply may delete")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.hash_tree:
        print(json.dumps(hash_tree(args.hash_tree)))
        return 0
    if not args.grid_dir or args.protect is None:
        ap.error("--grid-dir and --protect are required")

    protect_raw = json.loads(args.protect)
    # A SIGNED appid here would protect nothing (files carry the unsigned form) and every current
    # tile's art would classify as an orphan. Refuse rather than guess.
    bad = [p for p in protect_raw if not isinstance(p, int) or not 0 <= p < APPID_LIMIT]
    if bad:
        print(json.dumps({"error": "protect holds non-unsigned-32-bit values", "bad": bad}))
        return 2
    protect = set(protect_raw)
    vendored = set(json.loads(args.vendor_hashes))

    if not os.path.isdir(args.grid_dir):
        # No grid directory means no art at all — nothing to classify and nothing to delete.
        print(
            json.dumps(
                {
                    "grid_dir": args.grid_dir,
                    "absent": True,
                    "counts": {},
                    "orphans": [],
                    "kept": {},
                    "deleted": [],
                    "changed": False,
                }
            )
        )
        return 0

    plan = survey(args.grid_dir, protect, vendored)
    plan["deleted"] = []
    plan["changed"] = False
    plan["applied"] = bool(args.apply)

    if args.apply:
        expect = sorted(json.loads(args.expect or "null") or [])
        now = sorted(o["name"] for o in plan["orphans"])
        if expect != now:
            plan["error"] = "orphan set changed since the preview; nothing deleted"
            plan["expected"] = expect
            print(json.dumps(plan))
            return 3
        for name in now:
            os.remove(os.path.join(args.grid_dir, name))
            plan["deleted"].append(name)
        plan["changed"] = bool(plan["deleted"])

    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
