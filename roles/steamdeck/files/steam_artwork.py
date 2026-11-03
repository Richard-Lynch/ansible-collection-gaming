#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Restore vendored Steam library artwork, keyed to each shortcut's CURRENT appid.

WHY NAME-KEYED STORAGE. Steam names artwork files after a shortcut's appid, and that appid is
derived from exe+name — so renaming a shortcut or changing its command orphans every artwork
file for it at once. The files stay on disk under an appid nothing references and the tile goes
blank, with no warning. Vendoring by NAME and resolving the appid at restore time means the art
survives edits that would otherwise silently break it.

SIGNED VS UNSIGNED. shortcuts.vdf stores the appid as a SIGNED int32; artwork filenames use the
UNSIGNED form. Mixing them up finds nothing and reports every shortcut as art-less.

NEVER OVERWRITES. A file already on the device is left alone: restore fills gaps after a reset
or a rename, and clobbering something newer than the repo is exactly the loss this prevents.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys


def slug(name: str) -> str:
    return name.lower().replace(": ", "-").replace(" ", "-")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vendor-dir", required=True, help="grid_artwork/ staged on the target")
    ap.add_argument("--grid-dir", required=True, help="Steam's grid directory")
    ap.add_argument(
        "--shortcuts", required=True, help="JSON map of shortcut name -> UNSIGNED appid"
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    names = json.loads(args.shortcuts)
    os.makedirs(args.grid_dir, exist_ok=True)

    planned, skipped, missing = [], [], []
    for name, appid in names.items():
        src_dir = os.path.join(args.vendor_dir, slug(name))
        if not os.path.isdir(src_dir):
            missing.append(name)
            continue
        for fname in sorted(os.listdir(src_dir)):
            if not fname.startswith("art"):
                continue
            suffix = fname[len("art") :]  # e.g. ".png", "_hero.png", "p.png"
            dest = os.path.join(args.grid_dir, f"{appid}{suffix}")
            if os.path.exists(dest):
                skipped.append(os.path.basename(dest))
                continue
            planned.append({"name": name, "src": os.path.join(src_dir, fname), "dest": dest})

    if args.apply:
        for item in planned:
            shutil.copy2(item["src"], item["dest"])

    print(
        json.dumps(
            {
                "restored": [os.path.basename(p["dest"]) for p in planned],
                "already_present": skipped,
                "no_vendored_art": missing,
                "changed": bool(planned and args.apply),
                "applied": bool(args.apply),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
