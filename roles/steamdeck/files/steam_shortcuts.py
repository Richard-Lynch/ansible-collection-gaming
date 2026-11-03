#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write Steam's shortcuts.vdf — the non-Steam games in a user's library.

WHY A REAL PARSER RATHER THAN grep/strings. shortcuts.vdf is Valve's BINARY VDF: typed,
length-prefixed, nested. `strings` recovers the names and little else — launch options, icons,
tags, the Proton compat flags and the generated appid all vanish. Writing it back from scraped
text would silently drop whatever it failed to see, and the damage shows up as shortcuts that
launch wrong or lose their controller layout.

FORMAT, as observed and as documented by the community (Valve publishes no spec):

    0x00 <key\\0> <nested map...> 0x08     map
    0x01 <key\\0> <value\\0>               string
    0x02 <key\\0> <4-byte LE int>          int32
    0x08                                  end of map (two in a row ends the file)

Top level is a map named "shortcuts" whose keys are "0", "1", "2"... in order.

SAFETY. Steam REWRITES this file when it exits, so any edit made while Steam is running is
discarded — silently, and long after a play reports success. Callers must stop Steam first. This
script refuses to write if it cannot re-parse what it just produced, because a corrupt
shortcuts.vdf presents as an empty library rather than an error.
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
    ap.add_argument("--mode", choices=["read", "write"], default="read")
    ap.add_argument(
        "--shortcuts", default="", help="write mode: JSON map of index -> shortcut fields"
    )
    args = ap.parse_args()

    if args.mode == "read":
        if not os.path.exists(args.path):
            print(json.dumps({"exists": False, "shortcuts": {}}))
            return 0
        sc = load(args.path)
        print(
            json.dumps(
                {
                    "exists": True,
                    "count": len(sc),
                    "shortcuts": sc,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    dump(args.path, json.loads(args.shortcuts))
    print(json.dumps({"written": args.path, "count": len(json.loads(args.shortcuts))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
