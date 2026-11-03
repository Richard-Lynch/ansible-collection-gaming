#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Merge managed shortcuts into an existing shortcuts.vdf without disturbing the rest.

THE CORE RULE: only entries this tool created are ever changed or removed. Everything else — shortcuts
another tool wrote (EmuDeck's Steam ROM Manager) or a person added on the device, with their
controller layouts, artwork and play history — is passed through untouched.

Managed entries are identified by a TAG, not by name or position. Names get edited, positions
shift whenever Steam rewrites the file, and either would make this delete the wrong thing. The
tag travels with the entry and survives both.

APPID STABILITY MATTERS MORE THAN IT LOOKS. Steam derives a shortcut's appid from its exe and
name, and uses it to key artwork, controller configuration and play time. Regenerating an appid
for an unchanged shortcut silently orphans all three — the tile loses its art and its controller
layout, which reads as Steam misbehaving rather than as this tool's doing. So an existing
managed entry keeps its appid unless its exe or name actually changed.
"""

from __future__ import annotations

import argparse
import binascii
import json
import os
import struct
import sys

# ---------------------------------------------------------------------------------------------
# VDF core, INLINED from steam_shortcuts.py.
#
# `ansible.builtin.script` copies exactly ONE file to the target and runs it there, so an import
# of a sibling module fails with a bare non-zero exit and no useful message. Duplicating this is
# the lesser evil against shipping a tarball or requiring the role to stage a directory — but it
# IS duplication, so the two must be changed together. The round-trip assertion in dump() is
# what catches them drifting apart.
# ---------------------------------------------------------------------------------------------

TYPE_MAP, TYPE_STR, TYPE_INT, TYPE_END = 0x00, 0x01, 0x02, 0x08


class VdfError(Exception):
    pass


def _read_cstr(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.index(b"\x00", pos)
    # utf-8 with surrogateescape: shortcut names and paths can contain bytes that are not valid
    # UTF-8, and refusing to read the file because one game has an odd character would be worse
    # than round-tripping the bytes unchanged.
    return buf[pos:end].decode("utf-8", "surrogateescape"), end + 1


def parse(buf: bytes, pos: int = 0) -> tuple[dict, int]:
    out: dict = {}
    while pos < len(buf):
        vtype = buf[pos]
        pos += 1
        if vtype == TYPE_END:
            return out, pos
        key, pos = _read_cstr(buf, pos)
        if vtype == TYPE_MAP:
            out[key], pos = parse(buf, pos)
        elif vtype == TYPE_STR:
            out[key], pos = _read_cstr(buf, pos)
        elif vtype == TYPE_INT:
            out[key] = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
        else:
            raise VdfError(f"unknown value type {vtype:#x} at offset {pos - 1}")
    return out, pos


def serialise(data: dict) -> bytes:
    chunks = []
    for key, value in data.items():
        k = key.encode("utf-8", "surrogateescape") + b"\x00"
        if isinstance(value, dict):
            chunks.append(bytes([TYPE_MAP]) + k + serialise(value) + bytes([TYPE_END]))
        elif isinstance(value, bool):
            # Steam stores these flags as int32, and json round-trips them as bool — so this
            # must come BEFORE the int branch, since bool is a subclass of int in Python.
            chunks.append(bytes([TYPE_INT]) + k + struct.pack("<i", int(value)))
        elif isinstance(value, int):
            chunks.append(bytes([TYPE_INT]) + k + struct.pack("<i", value))
        else:
            chunks.append(
                bytes([TYPE_STR]) + k + str(value).encode("utf-8", "surrogateescape") + b"\x00"
            )
    return b"".join(chunks)


def load(path: str) -> dict:
    with open(path, "rb") as fh:
        data, _ = parse(fh.read())
    return data.get("shortcuts", {})


def dump(path: str, shortcuts: dict) -> None:
    payload = (
        bytes([TYPE_MAP])
        + b"shortcuts\x00"
        + serialise(shortcuts)
        + bytes([TYPE_END])
        + bytes([TYPE_END])
    )

    # Prove the output is readable BEFORE it replaces anything. A corrupt shortcuts.vdf does not
    # raise an error in Steam — it presents as a library with no non-Steam games, which reads as
    # data loss rather than a bad write.
    check, _ = parse(payload)
    if set(check.get("shortcuts", {})) != set(shortcuts):
        raise VdfError("serialised output did not round-trip; refusing to write")

    tmp = path + ".iaac-tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, path)


MANAGED_TAG = "iaac"


def generate_appid(exe: str, name: str) -> int:
    """Steam's own shortcut appid derivation.

    crc32 of exe+name, with the high bit set, expressed as a signed 32-bit int — which is how
    Steam stores it and why these values look like large negatives in the file.
    """
    crc = binascii.crc32((exe + name).encode("utf-8", "surrogateescape")) & 0xFFFFFFFF
    unsigned = crc | 0x80000000
    return unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned


def is_managed(entry: dict) -> bool:
    tags = entry.get("tags", {})
    return isinstance(tags, dict) and MANAGED_TAG in tags.values()


# Fields whose change means the shortcut genuinely differs. Compared through _fields() so an
# ABSENT key and an empty one are the same thing: Steam omits keys it has no value for, while
# this tool always writes them, and a raw dict comparison would report an endless "update" for
# entries that are in fact identical.
_COMPARED = ("Exe", "LaunchOptions", "StartDir", "icon")


def _fields(entry: dict) -> dict:
    return {k: (entry.get(k) or "") for k in _COMPARED}


def build(desired: list[dict], existing: dict) -> tuple[dict, list[str]]:
    """Return the merged shortcut map and a human-readable list of what changed.

    ADOPTION. A shortcut added on the device carries no tag, so it is invisible to this tool and
    would be duplicated rather than updated. An entry declared with `adopt: true` claims the
    EXISTING one by name — tagging it in place and keeping its appid, because Steam keys
    artwork, controller layout and play time off that value. Creating a fresh entry instead
    would silently reset all three, which reads as Steam losing your settings.
    """
    ordered = sorted(existing.items(), key=lambda kv: int(kv[0]))
    adopt_names = {d["name"] for d in desired if d.get("adopt")}

    # Unmanaged entries are kept as found — EXCEPT any being adopted, which move into the
    # managed set rather than being duplicated there.
    kept = [e for _, e in ordered if not is_managed(e) and e.get("AppName") not in adopt_names]
    adopted = {
        e.get("AppName"): e
        for _, e in ordered
        if not is_managed(e) and e.get("AppName") in adopt_names
    }
    previously_managed = {e.get("AppName"): e for _, e in existing.items() if is_managed(e)}
    previously_managed.update(adopted)

    changes: list[str] = []
    built: list[dict] = []
    for want in desired:
        name = want["name"]
        exe = want["exe"]
        prev = previously_managed.pop(name, None)

        entry = dict(prev) if prev else {}
        entry.update(
            {
                "AppName": name,
                "Exe": exe,
                "StartDir": want.get("start_dir", ""),
                "LaunchOptions": want.get("launch_options", ""),
                "icon": want.get("icon", ""),
                "ShortcutPath": want.get("shortcut_path", ""),
                "IsHidden": 0,
                "AllowDesktopConfig": 1,
                "AllowOverlay": 1,
                "OpenVR": 0,
                "Devkit": 0,
                "DevkitGameID": "",
                "DevkitOverrideAppID": 0,
                "FlatpakAppID": want.get("flatpak_app_id", ""),
                "sortas": want.get("sort_as", ""),
                "tags": {"0": MANAGED_TAG},
            }
        )
        entry.setdefault("LastPlayTime", 0)

        # Preserve the appid unless the inputs it is derived from changed — see the module note.
        if prev and prev.get("Exe") == exe and prev.get("AppName") == name:
            entry["appid"] = prev.get("appid", generate_appid(exe, name))
        else:
            entry["appid"] = generate_appid(exe, name)

        if not prev:
            changes.append(f"add {name}")
        elif name in adopted:
            # Report adoption distinctly: "update" would imply the entry was already ours.
            changes.append(f"adopt {name}")
        elif _fields(prev) != _fields(entry):
            changes.append(f"update {name}")

        built.append(entry)

    for gone in previously_managed:
        changes.append(f"remove {gone}")

    merged = {str(i): e for i, e in enumerate(kept + built)}
    return merged, changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--desired", required=True, help="JSON list of shortcuts to manage")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    existing = load(args.path) if os.path.exists(args.path) else {}
    merged, changes = build(json.loads(args.desired), existing)

    if args.apply and changes:
        dump(args.path, merged)

    print(
        json.dumps(
            {
                "changed": bool(changes),
                "changes": changes,
                "total": len(merged),
                "managed": sum(1 for e in merged.values() if is_managed(e)),
                "unmanaged": sum(1 for e in merged.values() if not is_managed(e)),
                "applied": bool(args.apply and changes),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
