#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Report declared DST mods against what is downloaded, and each mod's REAL option names.

MANAGED BY ANSIBLE — roles/service_dst_server/files/dst_mods_report.py (run by
tasks/mods_report.yml via the script module; nothing is installed).

    dst_mods_report.py <root> '<declared mods JSON>'

WHY. A mod's configuration_options are keyed by NAMES only its modinfo.lua defines, and DST
ignores an unknown key silently: a typo in inventory is a setting that "applied" and did nothing.
So for each declared mod this reads the downloaded modinfo.lua and prints its option names,
defaults and allowed values — what to put in inventory — and flags every declared config key
the mod does not define.

modinfo.lua IS LUA, NOT DATA, and this is a deliberately small parser of its table literals:
strings, numbers, booleans, nil and nested tables. Anything else (a variable, a function call,
string concatenation) is reported as `<expr: ...>` with its source text rather than guessed at —
so an option whose allowed values are built in a loop shows as an expression, never as an empty
or wrong list. Stdlib only; tests in test_dst_mods_report.py.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

APP_CLIENT = "322330"  # DST's CLIENT app id: Workshop content is filed under it, not 343050

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lcomment>--\[(?P<leq>=*)\[.*?\](?P=leq)\])
  | (?P<comment>--[^\n]*)
  | (?P<lstr>\[(?P<seq>=*)\[.*?\](?P=seq)\])
  | (?P<str>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
  | (?P<num>0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\.\.\.|\.\.|==|~=|<=|>=|[{}\[\]()=,;.:+\-*/%^#<>])
    """,
    re.S | re.X,
)


class Expr(str):
    """Source text of a value this parser does not evaluate."""


def tokenize(src: str) -> list[tuple[str, str, int]]:
    out, pos = [], 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            out.append(("op", src[pos], pos))  # unknown char: keep going, never crash
            pos += 1
            continue
        kind = m.lastgroup
        if kind in ("leq", "seq"):  # inner groups: resolve to their outer group
            kind = "lcomment" if m.group("lcomment") else "lstr"
        if kind not in ("ws", "comment", "lcomment"):
            out.append((kind, m.group(0), m.start()))
        pos = m.end()
    return out


def _unquote(tok: str) -> str:
    if tok.startswith("["):
        body = tok[tok.index("[", 1) + 1 : tok.rindex("]", 0, len(tok) - 1)]
        return body[1:] if body.startswith("\n") else body
    body = tok[1:-1]
    esc = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n"}
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            out.append(esc.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0

    def peek(self, k: int = 0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else ("eof", "", len(self.src))

    def consumed_end(self) -> int:
        """Source offset just past the last CONSUMED token — so an Expr's text stops at its own
        last token, never running on into the comments or blank lines that follow it."""
        if self.i == 0:
            return 0
        _, text, pos = self.toks[self.i - 1]
        return pos + len(text)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def value(self):
        """Parse one expression. Literal -> Python value; anything else -> Expr(source)."""
        start = self.peek()[2]
        kind, text, _ = self.peek()
        simple = None
        if kind in ("str", "lstr"):
            self.take()
            simple = _unquote(text)
        elif kind == "num":
            self.take()
            simple = (
                int(text, 16)
                if text.lower().startswith("0x")
                else (float(text) if any(c in text for c in ".eE") else int(text))
            )
        elif kind == "name" and text in ("true", "false", "nil"):
            self.take()
            simple = {"true": True, "false": False, "nil": None}[text]
        elif kind == "op" and text == "-" and self.peek(1)[0] == "num":
            self.take()
            n = self.value()
            simple = -n if isinstance(n, (int, float)) else Expr("-" + str(n))
        elif kind == "op" and text == "{":
            simple = self.table()
        else:
            self.skip_expr()
            return Expr(self.src[start : self.consumed_end()].strip())
        # A literal followed by an operator (`"a" .. b`, `1 + x`) is an expression after all.
        nk, nt, _ = self.peek()
        if nk == "op" and nt not in (",", ";", "}", ")", "]"):
            self.skip_expr()
            return Expr(self.src[start : self.consumed_end()].strip())
        return simple

    def skip_function(self):
        """Skip `function ... end` whole, nested blocks included.

        A function body contains `return` and `end`, which skip_expr treats as the end of an
        expression — so without this, `{ function() return 1 end }` stopped at `return` having
        consumed nothing, and table() looped on it forever — mods_report would hang on a real
        Workshop mod's modinfo.lua.
        """
        assert self.take()[1] == "function"
        depth = 1
        while self.peek()[0] != "eof" and depth > 0:
            kind, text, _ = self.take()
            if kind == "name" and text in ("function", "do", "if", "repeat"):
                depth += 1
            elif kind == "name" and text in ("end", "until"):
                depth -= 1

    def skip_expr(self):
        depth = 0
        while True:
            kind, text, _ = self.peek()
            if kind == "eof":
                return
            if kind == "name" and text == "function":
                self.skip_function()
                continue
            if kind == "op" and text in "{([":
                depth += 1
            elif kind == "op" and text in "})]":
                if depth == 0:
                    return
                depth -= 1
            elif kind == "op" and text in ",;" and depth == 0:
                return
            elif kind == "name" and depth == 0 and text in ("local", "return", "end"):
                return
            elif (
                kind == "name"
                and depth == 0
                and self.peek(1)[0] == "op"
                and self.peek(1)[1] == "="
                and self.i > 0
                and (
                    self.toks[self.i - 1][0] != "op" or self.toks[self.i - 1][1] in (")", "]", "}")
                )
            ):
                # `x = f("a")` NEWLINE `y = ...`: Lua needs no separator between statements, so
                # a name followed by `=` right after a complete operand starts the NEXT one.
                # Without this, a mod's `name = en_zh("...", "...")` swallows the rest of its
                # modinfo.lua as one value. (After an operator — `a .. b = ` cannot
                # occur — the name is an operand, hence the previous-token check.)
                return
            self.take()

    def skip_expr_from_open(self):
        """Skip a bracketed group starting at the current `{`, `(` or `[`, nesting included."""
        depth = 0
        while self.peek()[0] != "eof":
            kind, text, _ = self.take()
            if kind == "op" and text in "{([":
                depth += 1
            elif kind == "op" and text in "})]":
                depth -= 1
                if depth == 0:
                    return

    def table(self):
        assert self.take()[1] == "{"
        out: dict = {}
        n = 0
        while True:
            kind, text, _ = self.peek()
            if kind == "eof":
                return out
            if kind == "op" and text == "}":
                self.take()
                break
            if kind == "op" and text in ",;":
                self.take()
                continue
            if kind == "name" and self.peek(1) == ("op", "=", self.peek(1)[2]):
                self.take()
                self.take()
                out[text] = self.value()
            elif kind == "op" and text == "[":
                self.take()
                key = self.value()
                if self.peek()[1] == "]":
                    self.take()
                if self.peek()[1] == "=":
                    self.take()
                out[key if not isinstance(key, Expr) else str(key)] = self.value()
            else:
                before = self.i
                n += 1
                out[n] = self.value()
                if self.i == before:
                    # PROGRESS GUARANTEE: a token no rule consumes (a stray `return`, `end`,
                    # `)`) is skipped rather than re-read forever. An unparseable fragment
                    # costs one bogus Expr entry, never a hang.
                    out[n] = Expr(self.take()[1])
        # An empty constructor is an empty LIST: every table modinfo leaves empty
        # (configuration_options = {}) is one that would otherwise hold a sequence.
        if not out:
            return []
        if all(isinstance(k, int) for k in out) and sorted(out) == list(range(1, len(out) + 1)):
            return [out[k] for k in sorted(out)]
        return out


def top_level_assignments(src: str) -> dict:
    """Every `name = <value>` at statement level, outside any block or table (modinfo's globals).

    Block depth follows Lua's keywords — function/do/if/repeat open, end/until close (`for` and
    `while` open through their `do`, `elseif`/`then` open nothing) — so an assignment inside a loop
    body or a table constructor (`speeds[i] = {description = ...}`) is never mistaken for a global.
    """
    p = Parser(src)
    out: dict = {}
    block = 0
    while p.peek()[0] != "eof":
        kind, text, _ = p.peek()
        if kind == "name" and text in ("function", "do", "if", "repeat"):
            block += 1
            p.take()
            continue
        if kind == "name" and text in ("end", "until"):
            block = max(0, block - 1)
            p.take()
            continue
        if kind == "op" and text in "{([":
            p.skip_expr_from_open()
            continue
        prev = p.toks[p.i - 1][1] if p.i > 0 else ""
        if (
            block == 0
            and kind == "name"
            and p.peek(1)[0] == "op"
            and p.peek(1)[1] == "="
            and prev not in (".", ":", "]")
        ):
            p.take()
            p.take()
            out[text] = p.value()
        else:
            p.take()
    return out


def describe_options(opts) -> list[dict]:
    if not isinstance(opts, list):
        return [{"error": "configuration_options is not a literal list: %s" % (opts,)}]
    out = []
    for o in opts:
        if not isinstance(o, dict) or "name" not in o:
            continue
        allowed = o.get("options")
        if isinstance(allowed, list):
            values = [a.get("data") if isinstance(a, dict) else a for a in allowed]
            labels = [a.get("description") if isinstance(a, dict) else None for a in allowed]
        else:
            values, labels = allowed, None
        out.append(
            {
                "name": o.get("name"),
                "label": o.get("label") or o.get("hover") or "",
                "default": o.get("default"),
                "values": values,
                "labels": labels,
            }
        )
    return out


def report_mod(root: str, mod: dict) -> dict:
    num = str(mod["key"]).split("-", 1)[1]
    candidates = glob.glob(os.path.join(root, "ugc", "content", APP_CLIENT, num, "modinfo.lua"))
    candidates += glob.glob(os.path.join(root, "server", "mods", "workshop-" + num, "modinfo.lua"))
    entry = {
        "key": mod["key"],
        "declared_name": mod.get("name", ""),
        "enabled": mod.get("enabled", True),
    }
    if not candidates:
        entry["downloaded"] = False
        return entry
    path = candidates[0]
    entry["downloaded"] = True
    entry["path"] = path
    with open(path, encoding="utf-8", errors="replace") as f:
        g = top_level_assignments(f.read())
    for k in (
        "name",
        "version",
        "author",
        "server_only_mod",
        "client_only_mod",
        "all_clients_require_mod",
        "dst_compatible",
    ):
        v = g.get(k)
        entry[k] = str(v) if isinstance(v, Expr) else v
    desc = g.get("description")
    entry["description"] = (
        desc.strip().splitlines()[0][:160] if isinstance(desc, str) and desc.strip() else ""
    )
    options = describe_options(g.get("configuration_options", []))
    entry["options"] = options
    known = {o["name"] for o in options if "name" in o}
    entry["unknown_config_keys"] = sorted(k for k in (mod.get("config") or {}) if k not in known)
    bad_values = []
    for o in options:
        k = o.get("name")
        if k in (mod.get("config") or {}) and isinstance(o.get("values"), list):
            if mod["config"][k] not in o["values"]:
                bad_values.append(
                    {"option": k, "declared": mod["config"][k], "allowed": o["values"]}
                )
    entry["config_values_not_offered"] = bad_values
    if entry.get("client_only_mod") is True:
        entry["warning"] = "client_only_mod: a server cannot run it; remove it from dst_mods"
    return entry


def main() -> int:
    root, mods = sys.argv[1], json.loads(sys.argv[2])
    print(json.dumps([report_mod(root, m) for m in mods], default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
