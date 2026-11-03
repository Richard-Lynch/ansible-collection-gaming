#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cases for css_themes.py's REMOVAL path.

WHY THIS FILE EXISTS. Removal is the one operation here that is irreversible and that runs
against a path built from inventory, so both of its failure modes are expensive:

  * a remove that silently does NOTHING reads as success and leaves the theme styling the UI —
    the exact shape that made removal necessary in the first place (deleting a theme's inventory
    lines never uninstalled it);
  * a remove that deletes the WRONG PATH cannot be undone.

So every rule is tested in both directions: the guard fires on the case it exists for, and stays
quiet on the near-miss that must keep working.

Run: venv/bin/python roles/steamdeck/files/test_css_themes.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import re
import tempfile

import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from css_themes import remove_theme  # noqa: E402

HELPER = Path(__file__).resolve().parent / "css_themes.py"
FAILURES = 0


def check(label: str, got, want) -> None:
    global FAILURES
    if got == want:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")


def themes_root() -> str:
    """A themes dir holding one installed theme, shaped like the real thing."""
    root = tempfile.mkdtemp(prefix="css-themes-test-")
    d = os.path.join(root, "Colored Toggles")
    os.makedirs(d)
    with open(os.path.join(d, "theme.json"), "w") as fh:
        json.dump({"name": "Colored Toggles", "version": "v1.6"}, fh)
    with open(os.path.join(d, "config_USER.json"), "w") as fh:
        json.dump({"active": False, "Theme Color": "Blue"}, fh)
    return root


# --- report mode must not delete -------------------------------------------------------------
# The whole generate-don't-apply premise: a dry run that removes things is worse than useless.
root = themes_root()
r = remove_theme("Colored Toggles", root, apply=False)
check("report: reports the theme present", r["state"], "present")
check("report: plans a removal", r["actions"], ["remove"])
check("report: leaves it on disk", os.path.isdir(os.path.join(root, "Colored Toggles")), True)
check("report: no problems", r["problems"], [])

# --- apply removes, and says so ---------------------------------------------------------------
r = remove_theme("Colored Toggles", root, apply=True)
check("apply: directory is gone", os.path.isdir(os.path.join(root, "Colored Toggles")), False)
check("apply: reports absent", r["state"], "absent")
check("apply: no problems", r["problems"], [])

# --- idempotence: safe to run twice ------------------------------------------------------------
# A play must be re-runnable, and a second removal must not be a failure or a phantom change.
r = remove_theme("Colored Toggles", root, apply=True)
check("second run: still absent", r["state"], "absent")
check("second run: reports NO action (not a fake change)", r["actions"], [])
check("second run: no problems", r["problems"], [])
shutil.rmtree(root, ignore_errors=True)

# --- the traversal guard FIRES ------------------------------------------------------------------
# The case it exists for: an inventory value that climbs out of themes_root.
root = themes_root()
outside = os.path.join(root, "..", "sentinel")
os.makedirs(os.path.realpath(outside), exist_ok=True)
r = remove_theme("../sentinel", root, apply=True)
check("escape: is refused", bool(r["problems"]), True)
check("escape: names the reason", "escapes" in r["problems"][0], True)
check("escape: the outside path SURVIVES", os.path.isdir(os.path.realpath(outside)), True)
check("escape: performs no action", r["actions"], [])
shutil.rmtree(os.path.realpath(outside), ignore_errors=True)

r = remove_theme(".", root, apply=True)
check("root: refuses to remove themes-root itself", bool(r["problems"]), True)
check("root: themes-root SURVIVES", os.path.isdir(root), True)

# --- ...and stays QUIET on the near-miss ---------------------------------------------------------
# A guard that also rejects legitimate names would simply block removal. These must pass.
for name in ("Colored Toggles", "Clean Gameview", "theme..name", "..leading"):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    r = remove_theme(name, root, apply=True)
    check(f"near-miss {name!r}: allowed", r["problems"], [])
    check(f"near-miss {name!r}: actually removed", os.path.isdir(d), False)

# --- absent theme is success, not an error --------------------------------------------------------
r = remove_theme("Never Installed", root, apply=True)
check("absent theme: no problems", r["problems"], [])
check("absent theme: no phantom change", r["actions"], [])
shutil.rmtree(root, ignore_errors=True)


# --- CLI: declared AND removed is a hard error -----------------------------------------------------
# Ambiguity resolved by failing, not by precedence — either reading is plausible.
def run_cli(desired, remove, root_dir, apply=False):
    argv = [
        sys.executable,
        str(HELPER),
        "--themes-root",
        root_dir,
        "--desired",
        json.dumps(desired),
        "--remove",
        json.dumps(remove),
    ]
    if apply:
        argv.append("--apply")
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout[p.stdout.find("{") :])


root = themes_root()
rc, out = run_cli([{"name": "Colored Toggles", "id": "x"}], ["Colored Toggles"], root)
check("conflict: exits non-zero", rc, 1)
check("conflict: is reported", any("both" in p for p in out["problems"]), True)
check("conflict: theme NOT deleted", os.path.isdir(os.path.join(root, "Colored Toggles")), True)

# The near-miss: removing something that is NOT declared is the normal case and must work.
rc, out = run_cli([], ["Colored Toggles"], root, apply=True)
check("no conflict: exits zero", rc, 0)
check("no conflict: removed", os.path.isdir(os.path.join(root, "Colored Toggles")), False)
check("no conflict: reports changed", out["changed"], True)
shutil.rmtree(root, ignore_errors=True)

# --- report mode via the CLI leaves disk untouched ----------------------------------------------
root = themes_root()
rc, out = run_cli([], ["Colored Toggles"], root, apply=False)
check("CLI report: exits zero", rc, 0)
check("CLI report: changed is true (there IS work)", out["changed"], True)
check("CLI report: applied is false", out["applied"], False)
check("CLI report: theme survives", os.path.isdir(os.path.join(root, "Colored Toggles")), True)
shutil.rmtree(root, ignore_errors=True)

# --- every argspec option is actually RESOLVED -------------------------------------------------
# setup_vars.yml is HAND-maintained while defaults/main.yml is GENERATED from argument_specs.py,
# so the two drift silently: adding an option gives you a `<name>__default` that nothing maps to
# the plain name tasks use. The failure surfaces only at RUN time, on a real host, as
# "'<name>' is undefined" — and it reads as a broken role rather than a stale list.
#
# For example, an option added to the argspec, generated into defaults and referenced by a task
# file but omitted from setup_vars is otherwise caught only when the role runs against a Deck.
# This check makes it a test failure instead.
ROLE = Path(__file__).resolve().parent.parent
_spec = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())
_declared = set(_spec["argument_specs"]["main"]["options"])
_resolved = set(
    re.findall(r"^\s{4}(steamdeck_\w+):", (ROLE / "tasks" / "setup_vars.yml").read_text(), re.M)
)
check("every argspec option is resolved in setup_vars.yml", sorted(_declared - _resolved), [])
check("setup_vars.yml resolves nothing undeclared", sorted(_resolved - _declared), [])

print(f"\n{'ALL PASS' if not FAILURES else str(FAILURES) + ' FAILED'}")
sys.exit(1 if FAILURES else 0)
