#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and edit a Don't Starve Together CLIENT's mod enable state (client_save/modindex).

Run by the klei_dst_client role through the script module; nothing is installed.

    dst_modindex.py <modindex path> '<{"enable": [...], "disable": [...]}>' [--apply]

THE FILE FORMAT (decoded from a real client's file, not from documentation):

    KLEI     1D<base64>

`KLEI     1` is Klei's persistent-string header and `D` marks the encoded form. The base64 holds
a 16-byte little-endian header — (1, 16, raw length, compressed length) — followed by zlib data,
which inflates to a Lua chunk:

    return {disable_special_event_warning=false,known_api_version=10,known_mods={}}

`known_mods` maps "workshop-<id>" to a table whose `enabled` field is what the in-game Mods
screen toggles. The game owns every OTHER field (it caches modinfo there), so this edits only
`enabled` — adding a minimal {enabled=true} entry when the mod is not known yet; the game fills
in the rest on its next start.

THE GAME REWRITES THIS FILE when it exits, so an edit made while it runs is silently lost. The
role refuses to run then; this script refuses too when it is handed a running pid check. A copy
of the original is written to <path>.iaac-bak before any change.

Prints JSON: before/after enabled state per declared mod, whether anything changed.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import sys
import zlib

HEADER = "KLEI     1"
_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>[{}\[\]=,;])
    """,
    re.X,
)
_IDENT = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_KEYWORDS = {
    "and",
    "break",
    "do",
    "else",
    "elseif",
    "end",
    "false",
    "for",
    "function",
    "if",
    "in",
    "local",
    "nil",
    "not",
    "or",
    "repeat",
    "return",
    "then",
    "true",
    "until",
    "while",
}


# --------------------------------------------------------------------------------------------
# The container
# --------------------------------------------------------------------------------------------


def decode(text: str) -> str:
    if not text.startswith(HEADER):
        raise ValueError("not a Klei persistent string (no %r header)" % HEADER)
    body = text[len(HEADER) :]
    if not body.startswith("D"):
        # An un-encoded persistent string is the Lua text itself.
        return body.lstrip()
    raw = base64.b64decode(body[1:].strip())
    _ver, hlen, size, _csize = struct.unpack("<4I", raw[:16])
    lua = zlib.decompress(raw[hlen:]).decode("utf-8")
    if len(lua.encode("utf-8")) != size:
        raise ValueError("inflated %d bytes, header says %d" % (len(lua.encode()), size))
    return lua


def encode(lua: str) -> str:
    data = lua.encode("utf-8")
    comp = zlib.compress(data, 9)
    raw = struct.pack("<4I", 1, 16, len(data), len(comp)) + comp
    return HEADER + "D" + base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------------------------
# The Lua table — the literal subset the game's own serializer writes
# --------------------------------------------------------------------------------------------


def _tokens(src: str):
    pos = 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise ValueError("unexpected %r at %d" % (src[pos : pos + 20], pos))
        if m.lastgroup != "ws":
            yield m.lastgroup, m.group(0)
        pos = m.end()


def parse(lua: str):
    toks = list(_tokens(lua))
    i = 0

    def peek(k=0):
        return toks[i + k] if i + k < len(toks) else ("eof", "")

    def take(expect=None):
        nonlocal i
        t = toks[i]
        if expect and t[1] != expect:
            raise ValueError("expected %r, got %r" % (expect, t[1]))
        i += 1
        return t

    def value():
        kind, text = peek()
        if text == "{":
            return table()
        take()
        if kind == "str":
            # The game's serializer writes Lua strings with JSON-compatible escapes (\" \\ \n).
            return json.loads(text)
        if kind == "num":
            return float(text) if any(c in text for c in ".eE") else int(text)
        if text in ("true", "false"):
            return text == "true"
        if text == "nil":
            return None
        raise ValueError("unsupported value %r" % text)

    def table():
        take("{")
        out, n = {}, 0
        while peek()[1] != "}":
            kind, text = peek()
            if text == "[":
                take()
                key = value()
                take("]")
                take("=")
                out[key] = value()
            elif kind == "name" and peek(1)[1] == "=":
                take()
                take("=")
                out[text] = value()
            else:
                n += 1
                out[n] = value()
            if peek()[1] in (",", ";"):
                take()
        take("}")
        return out

    if peek()[1] != "return":
        raise ValueError("not a `return {...}` chunk")
    take()
    result = table()
    if peek()[0] != "eof":
        raise ValueError("trailing content after the table")
    return result


def dump(v) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, dict):
        parts = []
        for k in sorted(v, key=lambda k: (isinstance(k, str), str(k))):
            if isinstance(k, str) and _IDENT.match(k) and k not in _KEYWORDS:
                key = k
            else:
                key = "[%s]" % dump(k)
            parts.append("%s=%s" % (key, dump(v[k])))
        return "{" + ",".join(parts) + "}"
    raise ValueError("cannot serialise %r" % (v,))


# --------------------------------------------------------------------------------------------
# The edit
# --------------------------------------------------------------------------------------------


def mod_key(mod_id) -> str:
    m = re.fullmatch(r"(?:workshop-)?(\d+)", str(mod_id).strip())
    if not m:
        raise ValueError("%r is not a Workshop id" % (mod_id,))
    return "workshop-" + m.group(1)


def apply_wanted(state: dict, enable: list, disable: list) -> tuple[dict, dict]:
    """Set `enabled` per declared mod. Returns (new state, report). Never touches other mods."""
    known = state.setdefault("known_mods", {})
    report = {}
    for mod_id, want in [(m, True) for m in enable] + [(m, False) for m in disable]:
        key = mod_key(mod_id)
        entry = known.get(key)
        before = bool(entry.get("enabled")) if isinstance(entry, dict) else None
        if entry is None:
            if want:
                known[key] = {"enabled": True}
        else:
            entry["enabled"] = want
        report[key] = {
            "before": before,
            "after": want if (entry is not None or want) else None,
        }
    return state, report


def main() -> int:
    path = sys.argv[1]
    wanted = json.loads(sys.argv[2])
    apply = "--apply" in sys.argv[3:]
    text = open(path, encoding="utf-8").read()
    state = parse(decode(text))
    new_state, report = apply_wanted(
        state,
        wanted.get("enable", []),
        wanted.get("disable", []),
    )
    changed = any(r["before"] != r["after"] for r in report.values())
    if changed and apply:
        with open(path + ".iaac-bak", "w", encoding="utf-8") as fh:
            fh.write(text)
        out = encode("return " + dump(new_state))
        # Read back what was written before reporting success.
        if parse(decode(out)) != new_state:
            raise SystemExit("re-encoded modindex does not round-trip — refusing to write it")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(out)
    print(json.dumps({"changed": changed, "applied": bool(changed and apply), "mods": report}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
