# SPDX-License-Identifier: GPL-3.0-or-later
"""Render Don't Starve Together's two config formats from plain data: INI and Lua tables.

Used by the service_dst_server role. A collection filter rather than the role's own
filter_plugins/ because a role inside a collection never loads its own plugin
directories. One file exposes nine filters, so each has a sidecar .yml with its documentation.

WHY FILTERS AND NOT TEMPLATES. Every DST config file is either an INI (cluster.ini, server.ini)
or a Lua file that returns a table (modoverrides.lua, worldgenoverride.lua,
leveldataoverride.lua). The values come from inventory as YAML, and the failure modes are all in
the TYPE translation: a YAML `true` must become Lua `true` and not the string "True", a mod
option named "Starting Items" must be written `["Starting Items"]` because it is not a Lua
identifier, a `workshop-378160973` key must be bracketed because of the hyphen, and a key that
happens to be a Lua keyword (`end`, `nil`) must be bracketed too. A Jinja macro doing that
recursively is unreadable and untestable; these are pure functions with a stdlib test suite
(tests/unit/test_dst_formats.py), so every one of those cases is pinned.

Both renderers are DETERMINISTIC (keys sorted where the format allows it) so an unchanged
configuration renders byte-identically and the deploy reports `changed` only when something did.
"""

from __future__ import annotations

import math
import re

try:  # Ansible is absent when the test suite imports this file directly.
    from ansible.errors import AnsibleFilterError as _FilterError
except ImportError:  # pragma: no cover - exercised only outside Ansible

    class _FilterError(Exception):  # type: ignore[no-redef]
        pass


_LUA_IDENT = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
# Lua 5.1 reserved words (DST embeds 5.1). A key spelled like one of these is a syntax error as a
# bare identifier, so it must be bracketed.
_LUA_KEYWORDS = frozenset(
    "and break do else elseif end false for function if in local nil not or repeat return "
    "then true until while".split()
)


def _lua_string(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            out.append("\\%03d" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _lua_key(k) -> str:
    if isinstance(k, bool):
        return "[" + ("true" if k else "false") + "]"
    if isinstance(k, int):
        return "[%d]" % k
    if isinstance(k, str):
        if _LUA_IDENT.match(k) and k not in _LUA_KEYWORDS:
            return k
        return "[" + _lua_string(k) + "]"
    raise _FilterError("to_dst_lua: unsupported table key %r (%s)" % (k, type(k).__name__))


def _lua_number(v) -> str:
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise _FilterError("to_dst_lua: %r has no Lua literal" % v)
        if v.is_integer():
            return "%d" % int(v)
        return repr(v)
    return "%d" % v


def _sort_key(k):
    # Integer keys first in numeric order, then everything else by its text — a total order
    # that never compares an int to a str directly.
    return (0, k, "") if isinstance(k, int) and not isinstance(k, bool) else (1, 0, str(k))


def to_dst_lua(value, indent: int = 2, _level: int = 0) -> str:
    """Render a YAML/JSON value as a Lua literal (tables nested, keys sorted)."""
    pad = " " * (indent * (_level + 1))
    end_pad = " " * (indent * _level)
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _lua_number(value)
    if isinstance(value, str):
        return _lua_string(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = []
        for k in sorted(value, key=_sort_key):
            v = value[k]
            if v is None:
                # A nil-valued table field does not exist in Lua; writing `k = nil` is legal but
                # reads as if the key were being set. Drop it so the file says what it means.
                continue
            items.append("%s%s = %s," % (pad, _lua_key(k), to_dst_lua(v, indent, _level + 1)))
        if not items:
            return "{}"
        return "{\n" + "\n".join(items) + "\n" + end_pad + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "{}"
        if any(v is None for v in value):
            # A nil inside a Lua array truncates it (# stops at the first hole), silently dropping
            # every element after it. Refuse rather than render a shorter list than was declared.
            raise _FilterError("to_dst_lua: a list cannot contain null: %r" % (value,))
        items = ["%s%s," % (pad, to_dst_lua(v, indent, _level + 1)) for v in value]
        return "{\n" + "\n".join(items) + "\n" + end_pad + "}"
    raise _FilterError("to_dst_lua: unsupported value %r (%s)" % (value, type(value).__name__))


def dst_lua_return(value, header: str = "") -> str:
    """A complete Lua file that returns `value`, with an optional comment header."""
    lines = []
    for line in header.splitlines():
        lines.append(("-- " + line).rstrip())
    lines.append("return " + to_dst_lua(value))
    return "\n".join(lines) + "\n"


def _ini_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return _lua_number(v)
    if isinstance(v, (list, tuple, dict)):
        raise _FilterError("to_dst_ini: value %r is not a scalar" % (v,))
    s = str(v)
    if "\n" in s or "\r" in s:
        # DST's INI reader is line-based: a newline would end the value and start a bogus key.
        raise _FilterError("to_dst_ini: value %r contains a newline" % (s,))
    return s


def deep_merge_sections(base: dict, extra: dict) -> dict:
    """Merge {SECTION: {key: value}} dicts; extra wins per key. Neither input is modified."""
    out = {s: dict(kv) for s, kv in (base or {}).items()}
    for section, kv in (extra or {}).items():
        if not isinstance(kv, dict):
            raise _FilterError(
                "INI extra: section %r must map keys to values, got %r" % (section, kv)
            )
        out.setdefault(section, {}).update(kv)
    return out


def to_dst_ini(sections: dict, header: str = "", extra: dict | None = None) -> str:
    """Render {SECTION: {key: value}} as DST's INI. Sections keep their given order; keys too.

    Section order is preserved rather than sorted because Klei's own files and documentation use
    a fixed order ([GAMEPLAY], [NETWORK], [MISC], [SHARD]) and a reader comparing against them
    should not have to re-sort. A section whose every value is None is omitted entirely.
    """
    merged = deep_merge_sections(sections, extra or {})
    out = []
    for line in header.splitlines():
        out.append(("; " + line).rstrip())
    for section, kv in merged.items():
        if not re.match(r"\A[A-Z][A-Z0-9_]*\Z", str(section)):
            raise _FilterError("to_dst_ini: bad section name %r" % (section,))
        rows = [(k, v) for k, v in kv.items() if v is not None]
        if not rows:
            continue
        if out:
            out.append("")
        out.append("[%s]" % section)
        for k, v in rows:
            if not re.match(r"\A[a-z][a-z0-9_]*\Z", str(k)):
                raise _FilterError("to_dst_ini: bad key %r in [%s]" % (k, section))
            out.append("%s = %s" % (k, _ini_value(v)))
    return "\n".join(out) + "\n"


def dst_mod_key(mod_id) -> str:
    """'378160973' or 'workshop-378160973' (or an int) -> 'workshop-378160973'."""
    s = str(mod_id).strip()
    m = re.match(r"\A(?:workshop-)?(\d+)\Z", s)
    if not m:
        raise _FilterError(
            "dst_mod_key: %r is not a Workshop id (digits, optionally 'workshop-' first)"
            % (mod_id,)
        )
    return "workshop-" + m.group(1)


def dst_mods_setup(mods: list, collections: list | None = None, header: str = "") -> str:
    """dedicated_server_mods_setup.lua: one ServerModSetup per ENABLED mod, then collections."""
    lines = [("-- " + h).rstrip() for h in header.splitlines()]
    seen = set()
    for m in mods or []:
        if not m.get("enabled", True):
            continue
        num = dst_mod_key(m["id"])[len("workshop-") :]
        if num in seen:
            continue
        seen.add(num)
        label = m.get("name") or ""
        lines.append('ServerModSetup("%s")%s' % (num, ("  -- " + label) if label else ""))
    for c in collections or []:
        num = dst_mod_key(c)[len("workshop-") :]
        lines.append('ServerModCollectionSetup("%s")' % num)
    return "\n".join(lines) + "\n"


def dst_modoverrides(mods: list, shard: str) -> dict:
    """The modoverrides.lua table for ONE shard: enabled mods that apply to it, with config."""
    out: dict = {}
    for m in mods or []:
        if not m.get("enabled", True):
            continue
        shards = m.get("shards") or []
        if shards and shard not in shards:
            continue
        key = dst_mod_key(m["id"])
        if key in out:
            raise _FilterError("dst_modoverrides: mod %s is declared twice" % key)
        entry: dict = {"enabled": True}
        cfg = m.get("config") or {}
        if cfg:
            entry["configuration_options"] = cfg
        out[key] = entry
    return out


def dst_ini_parse(text: str) -> dict:
    """INI text -> {SECTION: {key: value-as-text}}, ignoring comments, blank lines and layout.

    The game REWRITES a shard's server.ini in its own layout (two blank lines between
    sections, and an appended [SHARD] id), so comparing bytes would make every deploy after a run
    rewrite the file — fighting the game, and reporting `changed` for nothing. Comparing the
    parsed MEANING is what tells "the game reformatted it" from "the config changed".
    """
    out: dict = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            out.setdefault(current, {})
            continue
        if current is not None and "=" in line:
            k, v = line.split("=", 1)
            out[current][k.strip()] = v.strip()
    return out


def dst_ini_get(text: str, section: str, key: str):
    """Read ONE value from an INI file's text; None when the section or key is absent.

    Used to PRESERVE what the game writes back into its own config: a secondary shard started
    without a [SHARD] id logs "No shard id specified, generating a random one..." and persists
    that id into its server.ini. A deploy that re-rendered server.ini without it would strip the
    id on every run — non-idempotent, and a shard whose id changes is a different shard to the
    master, which is what its cave entrances are linked by.
    """
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current == section and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return None


def dst_control_shard(shard: dict, cluster_path: str) -> dict:
    """One normalised shard -> the record dst_control.py's Shard dataclass takes."""
    return {
        "name": shard["name"],
        "unit": shard["unit"],
        "container": shard["container"],
        "port": int(shard["server_port"]),
        "log": "%s/%s/server_log.txt" % (cluster_path.rstrip("/"), shard["name"]),
        "is_master": bool(shard.get("is_master", False)),
    }


class FilterModule:
    def filters(self):
        return {
            "to_dst_lua": to_dst_lua,
            "dst_lua_return": dst_lua_return,
            "to_dst_ini": to_dst_ini,
            "dst_mod_key": dst_mod_key,
            "dst_mods_setup": dst_mods_setup,
            "dst_modoverrides": dst_modoverrides,
            "dst_control_shard": dst_control_shard,
            "dst_ini_get": dst_ini_get,
            "dst_ini_parse": dst_ini_parse,
        }
