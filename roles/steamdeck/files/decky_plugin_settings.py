#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Set keys in a Decky plugin's settings file, idempotently.

WHY A SCRIPT. These files are plain JSON but their shape differs per plugin, and a value must be
written with its correct TYPE — a bool written as the string "false" is truthy to the plugin's
frontend, which is a silent misconfiguration rather than an error.

NOT SAFE WHILE THE PLUGIN RUNS. Decky plugins hold settings in memory and rewrite the whole file
on commit, so an edit made while plugin_loader is running is silently discarded. The caller must
stop the service first; this script does not, because stopping Decky is a decision about the
whole device rather than about one settings file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--path",
        required=True,
        help="settings file; JSON and INI are both handled, chosen by extension",
    )
    ap.add_argument("--set", required=True, help="JSON object of key -> value to enforce")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    wanted = json.loads(args.set)

    # PLUGINS DO NOT AGREE ON A FORMAT. Observed on one device: settings.json, config.json,
    # settings.ini, steamgriddb.json and a bare scid.txt across twelve plugins. Assuming
    # settings.json means silently doing nothing for most of them, so the format is chosen by
    # extension and an unsupported one is an explicit error rather than a no-op.
    is_ini = args.path.endswith(".ini")
    try:
        if is_ini:
            import configparser

            parser = configparser.ConfigParser()
            parser.read(args.path)
            # Flattened as "section.key" so the same --set shape works for both formats.
            current = {f"{sec}.{k}": v for sec in parser.sections() for k, v in parser[sec].items()}
        else:
            with open(args.path) as fh:
                current = json.load(fh)
    except (OSError, ValueError) as exc:
        print(json.dumps({"changed": False, "error": str(exc), "path": args.path}))
        return 1

    changes = {}
    for key, value in wanted.items():
        # Compared by value AND type: True != "True", and a plugin reading the latter as truthy
        # is precisely the silent misconfiguration this avoids.
        if key not in current or current[key] != value or type(current[key]) is not type(value):
            changes[key] = {"from": current.get(key, "(absent)"), "to": value}

    if args.apply and changes:
        tmp = args.path + ".iaac-tmp"
        if is_ini:
            import configparser

            parser = configparser.ConfigParser()
            parser.read(args.path)
            for key, value in wanted.items():
                section, _, option = key.partition(".")
                if not parser.has_section(section):
                    parser.add_section(section)
                parser.set(section, option, str(value))
            with open(tmp, "w") as fh:
                parser.write(fh)
        else:
            merged = dict(current)
            merged.update(wanted)
            with open(tmp, "w") as fh:
                json.dump(merged, fh, indent=4)
        # Replaced atomically: a crash mid-write would otherwise leave a plugin with a truncated
        # config, which it treats as corrupt rather than as absent.
        os.replace(tmp, args.path)

    print(
        json.dumps(
            {
                "changed": bool(changes and args.apply),
                "changes": changes,
                "applied": bool(args.apply),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
