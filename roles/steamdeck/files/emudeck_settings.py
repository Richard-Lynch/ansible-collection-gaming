#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bring EmuDeck's settings.json under declarative control WITHOUT vendoring it.

WHY NOT A TEMPLATE. The obvious approach — a settings.json.j2 rendering all 41 keys — is wrong
here, for two reasons found by reading the real file:

  1. IT CONTAINS LIVE CREDENTIALS. sudoPass is the device's sudo password in plaintext, and
     achievements.{pass,token} are a RetroAchievements password and API token, so a vendored
     copy would put them in version control. A template would keep them out of git, but every
     OTHER key would then be pinned by us.

  2. EACH ENTRY CARRIES UPSTREAM METADATA, not just a value:

         "ares": {"id": "ares", "status": false, "name": "ares",
                  "platforms": "Retro Systems"}

     `name`, `platforms` and `desc` belong to EmuDeck and change as it adds emulators and edits
     its own descriptions. A template that reproduced them would silently pin this device to a
     2.5.0-era view of the emulator catalogue, and every EmuDeck update would either fight it or
     quietly drift from it. Worse, an emulator ADDED upstream would simply vanish from the file
     the next time we wrote it.

So this MERGES instead: it reads the file that is on the device, applies only the keys actually
declared, and leaves everything else exactly as EmuDeck wrote it. What we own is a small,
explicit set of decisions; what EmuDeck owns stays EmuDeck's.

---------------------------------------------------------------------------------------------
THE KEY THAT MATTERS MOST IS overwriteConfigEmus.

For each emulator it decides whether a setup.sh run REPLACES that emulator's config (keeping a
.bak) or leaves it alone. EmuDeck's default is TRUE for every installed emulator, which means
any EmuDeck rerun — including one triggered from the GUI — silently reverts hand-tuning. That
is the single most valuable thing to have declared, and the reason this exists.

---------------------------------------------------------------------------------------------
SECRETS ARE WRITTEN, NEVER READ BACK INTO A REPORT. Values supplied via --secrets are merged in
but are replaced with "***" everywhere this script reports, so a -vvv run or a failed task
cannot leak them into a log. The file mode is tightened to 0600 on write: EmuDeck creates it
0644, world-readable, holding a sudo password.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

# Keys whose values must never appear in output. `achievements` is a dict; its whole subtree is
# masked rather than individual fields, so a future EmuDeck release that adds another credential
# field inside it is covered without this list needing to change.
SECRET_KEYS = {"sudoPass", "achievements", "yuzuEAtoken", "patreonToken", "cloudSync", "SDID"}

MASK = "***"


class SettingsError(Exception):
    pass


def mask(value, key: str = ""):
    """Recursively replace anything secret-bearing with MASK, for reporting only."""
    if key in SECRET_KEYS:
        if isinstance(value, dict):
            return {k: MASK for k in value}
        return MASK
    if isinstance(value, dict):
        return {k: mask(v, k) for k, v in value.items()}
    return value


def deep_merge(base: dict, overrides: dict) -> dict:
    """Merge overrides into base, recursing into dicts.

    A dict override merges KEY BY KEY rather than replacing the whole dict — that is what keeps
    EmuDeck's per-emulator metadata (`id`, `name`, `platforms`, `desc`) intact while letting a
    declaration set just `status`. Replacing the dict wholesale would strip those fields, and
    EmuDeck's own UI reads them.
    """
    out = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def expand_status(overrides: dict) -> dict:
    """Allow `{"installEmus": {"dolphin": true}}` as shorthand for `{"status": true}`.

    The real file nests every emulator's boolean under `status` alongside metadata. Declaring
    that in inventory as a bare boolean is much easier to read and to review, and the shape is
    unambiguous: within these two maps a plain bool can only mean the status flag.
    """
    out = dict(overrides)
    for group in ("installEmus", "overwriteConfigEmus", "installFrontends"):
        if group not in out or not isinstance(out[group], dict):
            continue
        out[group] = {
            name: ({"status": value} if isinstance(value, bool) else value)
            for name, value in out[group].items()
        }
    return out


def diff(before: dict, after: dict, path: str = "") -> list[str]:
    """Human-readable list of what changed, with secret values masked."""
    changes: list[str] = []
    for key in sorted(set(before) | set(after)):
        here = f"{path}.{key}" if path else key
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(diff(old, new, here))
        elif old != new:
            root = here.split(".")[0]
            if root in SECRET_KEYS:
                # Report THAT a secret changed, never the values on either side.
                changes.append(f"{here}: {MASK} -> {MASK}")
            else:
                changes.append(f"{here}: {old!r} -> {new!r}")
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True, help="the device's settings.json")
    ap.add_argument("--settings", default="{}", help="JSON of non-secret keys to enforce")
    ap.add_argument("--secrets", default="{}", help="JSON of secret keys to enforce")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.path):
        raise SettingsError(
            f"{args.path} does not exist — EmuDeck has not run on this device. This manages an "
            f"existing install rather than bootstrapping one."
        )

    with open(args.path, encoding="utf-8") as fh:
        current = json.load(fh)

    overrides = expand_status(json.loads(args.settings))
    secrets = json.loads(args.secrets)

    # Secrets last, so a non-secret declaration can never shadow one.
    desired = deep_merge(deep_merge(current, overrides), secrets)

    changes = diff(current, desired)
    mode = os.stat(args.path).st_mode & 0o777
    # 0644 as EmuDeck writes it: world-readable, containing a plaintext sudo password.
    mode_wrong = mode != 0o600

    if args.apply and (changes or mode_wrong):
        # Back up before the first write only. A .bak that gets overwritten on every run is not a
        # backup — it would hold the output of the previous run rather than the original file.
        backup = args.path + ".iaac-orig"
        if not os.path.exists(backup):
            shutil.copy2(args.path, backup)
            # 0600 ON THE BACKUP TOO. copy2 preserves the source's mode, which is the 0644 this
            # is here to fix — so tightening only the live file would leave an identical
            # world-readable copy of the sudo password sitting next to it. Caught on the device:
            # the first apply produced settings.json 0600 and settings.json.iaac-orig 0644.
            os.chmod(backup, 0o600)

        tmp = args.path + ".iaac-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(desired, fh, indent=2)
        os.replace(tmp, args.path)
        os.chmod(args.path, 0o600)

    print(
        json.dumps(
            {
                "changed": bool(changes or mode_wrong),
                "applied": bool(args.apply and (changes or mode_wrong)),
                "changes": changes,
                "mode": {
                    "found": oct(mode),
                    "wanted": "0o600",
                    "tightened": bool(args.apply and mode_wrong),
                },
                # The full resulting document, masked — so a report shows what the device WILL hold
                # without ever printing a credential.
                "result": mask(desired),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SettingsError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        sys.exit(1)
