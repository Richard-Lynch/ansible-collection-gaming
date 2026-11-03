#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Remove a non-Steam shortcut by NAME, preserving everything else.

BY NAME, NOT BY INDEX. shortcuts.vdf indices are positional and Steam renumbers them every time
it rewrites the file, so "delete entry 1" is a different entry depending on when you ask. Name
is what a human actually means.

Re-indexes the remaining entries contiguously from 0, because Steam expects the keys to be a
dense sequence and quietly ignores entries after a gap.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        print(json.dumps({"removed": 0, "reason": "no shortcuts file"}))
        return 0

    existing = load(args.path)
    keep, removed = [], []
    for _, entry in sorted(existing.items(), key=lambda kv: int(kv[0])):
        (removed if entry.get("AppName") == args.name else keep).append(entry)

    if removed and args.apply:
        # Dense renumbering: Steam stops reading at the first missing index, so a gap silently
        # truncates the library rather than erroring.
        dump(args.path, {str(i): e for i, e in enumerate(keep)})

    print(
        json.dumps(
            {
                "removed": len(removed),
                "remaining": len(keep),
                "applied": bool(args.apply and removed),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
