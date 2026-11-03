#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Remove a plugin's references from Decky's plugin_loader_settings.json.

Decky records plugins by NAME in several lists — pluginOrder, hiddenPlugins, frozenPlugins and
similar. Deleting a plugin's directory leaves those entries behind, referring to something that
no longer exists; Decky's own uninstaller clears them, so a manual removal must too.

Written as a script rather than a Jinja edit because the shape of this file is not fixed: keys
have been added across Decky versions, so this walks EVERY list-valued key and every dict key
rather than hardcoding the ones that exist today.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def strip(obj, names: set[str]) -> tuple[object, int]:
    """Recursively drop `names` from any list or dict key. Returns (new, removals)."""
    removed = 0
    if isinstance(obj, list):
        out = []
        for item in obj:
            if isinstance(item, str) and item in names:
                removed += 1
                continue
            new_item, n = strip(item, names)
            removed += n
            out.append(new_item)
        return out, removed
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in names:
                removed += 1
                continue
            new_value, n = strip(value, names)
            removed += n
            out[key] = new_value
        return out, removed
    return obj, removed


def display_names(plugins_dir: str, dirs: list[str]) -> set[str]:
    """Resolve plugin DIRECTORY names to the DISPLAY names Decky records.

    THIS IS THE BUG THIS FUNCTION EXISTS FOR. loader.json's pluginOrder holds display names —
    "CSS Loader", "AutoFlatpaks" — while the filesystem uses directory names — SDH-CssLoader,
    decky-autoflatpaks. Searching for the directory name finds nothing and reports zero
    references to clean, which reads as "already clean" rather than "looked for the wrong
    string" — a removal can even report refs=0 correctly, but by accident.

    The mapping is authoritative in each plugin's own plugin.json.
    """
    out = set(dirs)
    for name in dirs:
        meta = os.path.join(plugins_dir, name, "plugin.json")
        try:
            with open(meta) as fh:
                display = json.load(fh).get("name")
            if display:
                out.add(display)
        except (OSError, ValueError):
            # The directory may already be gone — the caller removes files before cleaning
            # references — so a missing plugin.json is expected, not an error.
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--settings", required=True)
    ap.add_argument("--remove", required=True, help="JSON list of plugin DIRECTORY names")
    ap.add_argument(
        "--plugins-dir",
        default="",
        help="plugin root, used to resolve directory names to display names",
    )
    ap.add_argument(
        "--also",
        default="[]",
        help="JSON list of additional names (e.g. display names captured before "
        "the plugin directory was deleted, when plugin.json is no longer "
        "readable)",
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    dirs = json.loads(args.remove) + json.loads(args.also)
    names = display_names(args.plugins_dir, dirs) if args.plugins_dir else set(dirs)
    try:
        with open(args.settings) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(json.dumps({"changed": False, "error": str(exc)}))
        return 0

    cleaned, removed = strip(data, names)

    # DEDUPLICATE pluginOrder. Decky appends on install without checking, so the list drifts: a
    # plugin can appear twice while another installed one is missing entirely. Duplicates are harmless to Decky but make the file
    # unreadable as a record of what is installed, and they mask a genuinely missing entry.
    order = cleaned.get("pluginOrder")
    if isinstance(order, list):
        seen, deduped = set(), []
        for entry in order:
            if entry in seen:
                removed += 1
                continue
            seen.add(entry)
            deduped.append(entry)
        cleaned["pluginOrder"] = deduped
    if args.apply and removed:
        # Write via a temp file so a crash mid-write cannot leave Decky with a truncated
        # settings file, which it would treat as corrupt.
        tmp = args.settings + ".iaac-tmp"
        with open(tmp, "w") as fh:
            json.dump(cleaned, fh, indent=4)
        import os

        os.replace(tmp, args.settings)

    print(
        json.dumps(
            {
                "changed": bool(removed and args.apply),
                "references_removed": removed,
                "searched_for": sorted(names),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
