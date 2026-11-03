#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install and configure CSS Loader themes declaratively.

CSS Loader stores each theme as a directory under ~/homebrew/themes/, and each theme's
enable-state and patch selections as a `config_USER.json` INSIDE that directory:

    {"active": true, "Main Color": "Black", "Disable Blur": "Yes"}

So a theme is fully described by (a) its unpacked files and (b) that one small JSON — which is
what makes this declarative rather than a sequence of UI taps.

---------------------------------------------------------------------------------------------
WHY PATCH VALUES ARE VALIDATED HERE RATHER THAN LEFT TO CSS LOADER.

ThemePatch.check_value() silently resets an unrecognised value to the patch default:

    def check_value(self):
        if (self.value not in self.options):
            self.value = self.default

There is no error, no log line, and the theme still loads — so "Disable Blur": "true" (instead
of the actual option "Yes") reads as configured and does nothing. That is exactly the silent
guard shape this repo exists to avoid, so every requested patch name AND value is checked
against the theme's own theme.json here, and an unknown one is a hard failure.

---------------------------------------------------------------------------------------------
WHY DECKY MUST BE STOPPED AROUND AN APPLY (the caller's job, not this script's).

Theme.save() rewrites config_USER.json from the loader's IN-MEMORY state:

    config = {"active": self.enabled}
    for x in self.patches:
        config[x.name] = x.get_value()

A write performed while CSS Loader is running is therefore discarded the moment anything makes
it save — the same false-success as editing shortcuts.vdf under a live Steam.

---------------------------------------------------------------------------------------------
IDEMPOTENCE AND THE `files_present` CHECK. A theme is re-downloaded only when its directory is
missing or its installed version differs from the requested one. Version comes from the
installed theme.json, not from a marker file this script writes, so a theme replaced by hand or
updated through the CSS Loader UI is detected rather than assumed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile

API = "https://api.deckthemes.com"

# CSS Loader's own manifest support level (CSS_LOADER_VER in css_theme.py). A theme declaring a
# higher manifest_version than the installed loader understands will not load correctly, so it
# is checked rather than discovered as a theme that silently does nothing.
SUPPORTED_MANIFEST = 9


class ThemeError(Exception):
    pass


# The API 403s a request carrying urllib's default User-Agent ("Python-urllib/3.x") while
# serving the identical URL to curl: no UA -> 403, any non-default UA -> 200. Identifying
# ourselves honestly is also the right thing to do against someone else's API.
_UA = "iaac-ansible-steamdeck/1.0 (+ansible automation)"


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https host
        return r.read()


def fetch_meta(theme_id: str) -> dict:
    """The theme's API record — used for the download blob id and the upstream version."""
    try:
        return json.loads(_get(f"{API}/themes/{theme_id}"))
    except Exception as exc:
        raise ThemeError(f"could not fetch metadata for {theme_id}: {exc}") from exc


def manifest_from_blob(blob: bytes) -> dict:
    """Read theme.json out of a downloaded zip WITHOUT unpacking it.

    This is what makes report mode meaningful. The API record does not carry the theme's
    `patches`, so on a first run — nothing installed yet — the only way to check that declared
    patch names and values are real is to look inside the archive. Doing it in memory keeps the
    report read-only: a dry run must not leave files behind.
    """
    zf = zipfile.ZipFile(io.BytesIO(blob))
    entries = [n for n in zf.namelist() if n.endswith("theme.json")]
    if not entries:
        raise ThemeError("archive contains no theme.json")
    return json.loads(zf.read(entries[0]))


def installed_manifest(theme_dir: str) -> dict | None:
    path = os.path.join(theme_dir, "theme.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        # A corrupt or half-written theme.json means the install is not trustworthy; treating it
        # as absent makes the next run repair it rather than reporting a version it cannot read.
        return None


def unpack(blob: bytes, themes_root: str, expect_folder: str) -> str:
    """Unpack a theme zip, replacing any previous copy of that theme.

    The archive contains a single top-level directory named for the theme. That name is taken
    from the ARCHIVE rather than assumed from the display name — "Clean Gameview" is one theme
    whose folder happens to match, but the two are not guaranteed to agree.
    """
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()

    tops = {n.split("/")[0] for n in names if "/" in n}
    if len(tops) != 1:
        raise ThemeError(
            f"expected exactly one top-level folder in the archive, found {sorted(tops)}"
        )
    folder = tops.pop()

    # Refuse anything that would write outside the themes directory. These archives come from a
    # third-party API over the network, and zip entries can contain .. or absolute paths.
    #
    # BOTH SIDES MUST BE ABSOLUTE. Comparing a normpath'd relative join against an abspath'd
    # root makes every entry look like an escape when themes_root is relative — a check that
    # fires on everything is as useless as one that fires on nothing, just louder.
    root = os.path.abspath(themes_root)
    for n in names:
        target = os.path.abspath(os.path.join(root, n))
        if target != root and not target.startswith(root + os.sep):
            raise ThemeError(f"archive entry escapes the themes directory: {n}")

    dest = os.path.join(themes_root, folder)

    # PRESERVE the existing config across a reinstall. Removing the directory would take
    # config_USER.json with it, silently resetting enable-state and every patch — which reads as
    # the theme "forgetting its settings" rather than as this tool's doing. It is written back
    # by the caller anyway, but only for themes declared with settings; keeping it means an
    # upgrade never loses a selection made in the UI.
    saved: dict[str, bytes] = {}
    if os.path.isdir(dest):
        for cfg in ("config_USER.json", "config_ROOT.json"):
            p = os.path.join(dest, cfg)
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    saved[cfg] = fh.read()
        shutil.rmtree(dest)

    zf.extractall(themes_root)

    for cfg, data in saved.items():
        with open(os.path.join(dest, cfg), "wb") as fh:
            fh.write(data)

    if expect_folder and folder != expect_folder:
        raise ThemeError(f"archive folder {folder!r} does not match expected {expect_folder!r}")
    return folder


def validate_patches(manifest: dict, wanted: dict) -> list[str]:
    """Check requested patch names and values against the theme's own declared options.

    Returns a list of human-readable problems rather than raising, so every problem across every
    theme is reported in one pass instead of one per run.
    """
    problems: list[str] = []
    patches = manifest.get("patches") or {}
    for name, value in wanted.items():
        if name not in patches:
            problems.append(
                f"unknown patch {name!r} (available: {', '.join(sorted(patches)) or 'none'})"
            )
            continue
        options = list((patches[name].get("values") or {}).keys())
        # A "Custom" option typically carries free-form components (a colour picker), so its
        # value is not constrained to the option list.
        if options and str(value) not in options and "Custom" not in options:
            problems.append(f"patch {name!r} value {value!r} is not one of: {', '.join(options)}")
    return problems


def desired_config(manifest: dict, enabled: bool, patches: dict) -> dict:
    """Build the config_USER.json content CSS Loader itself would write.

    Every patch the theme declares is written, not only the ones being overridden — that is what
    save() does, and matching it means a re-read compares equal instead of reporting a
    difference on every unmentioned patch.
    """
    config: dict = {"active": bool(enabled)}
    for name, patch in (manifest.get("patches") or {}).items():
        if name in patches:
            config[name] = patches[name]
            continue
        # Fall back to the theme's OWN declared default — but only when it actually declares
        # one. A patch with no default has no reasonable value to invent: writing None (or a
        # guessed first option) would put the theme in a state its author never described, and
        # CSS Loader would then silently correct it, hiding the problem. Surface it instead.
        if "default" not in patch:
            raise ThemeError(
                f"patch {name!r} declares no default and none was given — "
                f"set it explicitly to one of: "
                f"{', '.join((patch.get('values') or {}).keys()) or '(no options declared)'}"
            )
        config[name] = patch["default"]
    return config


def _patch_value(entry):
    """The SELECTED option, whether stored plainly or in CSS Loader's expanded form.

    A patch with sub-components round-trips through the plugin as
    {"value": "Black", "components": {...}} rather than as "Black" — see ThemePatch.get_value().
    Both spellings mean the same selection, so both must compare equal or every run reports a
    difference that is not one.
    """
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return entry


def read_config(theme_dir: str) -> dict | None:
    path = os.path.join(theme_dir, "config_USER.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_config(theme_dir: str, config: dict) -> None:
    path = os.path.join(theme_dir, "config_USER.json")
    tmp = path + ".iaac-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    os.replace(tmp, path)


def remove_theme(name: str, themes_root: str, apply: bool) -> dict:
    """Delete an installed theme's directory outright.

    WHY THIS EXISTS AS A SEPARATE PATH, rather than "just delete the inventory entry".

    process() only ever iterates the themes that ARE declared — there is no reconciliation pass
    over themes_root — so an undeclared theme is not disabled, it is merely unwatched. Removing
    a theme's inventory lines therefore leaves it installed and, crucially, STILL ACTIVE: its
    config_USER.json keeps whatever `active` it last had, CSS Loader keeps loading it, and the
    report no longer mentions it. The theme goes on styling the UI with nothing in the repo
    saying why.

    So removal is an explicit, declared act. The two-step it supports is: set `enabled: false`,
    apply, confirm the device wrote {"active": false} — THEN name the theme here to delete it,
    and only then drop the inventory lines.

    The directory name is the FOLDER under themes_root, which is a theme's `folder` if it
    declares one and its name otherwise — the same value process() installs to.
    """
    out: dict = {"name": name, "actions": [], "problems": []}

    target = os.path.realpath(os.path.join(themes_root, name))
    root = os.path.realpath(themes_root)
    # A name like "../../.ssh" would otherwise delete outside the themes directory entirely.
    # Checked rather than trusted because this value comes from inventory and the operation is
    # irreversible.
    if target != root and not target.startswith(root + os.sep):
        out["problems"].append(f"{name!r} escapes the themes directory")
        return out
    if target == root:
        out["problems"].append("refusing to remove the themes directory itself")
        return out

    if not os.path.isdir(target):
        # Already gone is the desired end state, so this is success — and reporting it as
        # "absent" rather than silence is what makes the play safe to run twice.
        out["state"] = "absent"
        return out

    out["state"] = "present"
    out["actions"].append("remove")
    if apply:
        shutil.rmtree(target)
        # Read back rather than assuming rmtree did it. A remove path that reports success
        # without checking is the silent-guard shape this repo exists to prevent.
        if os.path.isdir(target):
            out["problems"].append(f"{name!r} still present after removal")
        else:
            out["state"] = "absent"
    return out


def process(theme: dict, themes_root: str, apply: bool) -> dict:
    name = theme["name"]
    theme_id = theme["id"]
    folder = theme.get("folder") or name
    # Defaults to ON, and that IS a reasonable default: declaring a theme in inventory is a
    # statement that you want it. `enabled: false` keeps a theme installed and configured but
    # inactive, which is how you park one without losing its settings.
    enabled = theme.get("enabled", True)
    patches = theme.get("patches") or {}

    out: dict = {"name": name, "id": theme_id, "actions": [], "problems": []}

    theme_dir = os.path.join(themes_root, folder)
    manifest = installed_manifest(theme_dir)

    meta = fetch_meta(theme_id)
    upstream_version = meta.get("version")
    out["upstream_version"] = upstream_version
    out["installed_version"] = manifest.get("version") if manifest else None

    needs_download = manifest is None or manifest.get("version") != upstream_version
    if needs_download:
        out["actions"].append("install" if manifest is None else "upgrade")

    if needs_download:
        blob_id = (meta.get("download") or {}).get("id")
        if not blob_id:
            out["problems"].append("API record has no download blob")
            return out
        blob = _get(f"{API}/blobs/{blob_id}")
        if apply:
            os.makedirs(themes_root, exist_ok=True)
            folder = unpack(blob, themes_root, theme.get("folder"))
            theme_dir = os.path.join(themes_root, folder)
            manifest = installed_manifest(theme_dir)
            out["installed_version"] = manifest.get("version") if manifest else None
        else:
            # Report mode: validate against the archive in memory. Nothing is written.
            manifest = manifest_from_blob(blob)

    if manifest is None:
        out["problems"].append("theme directory exists but has no readable theme.json")
        return out

    # No assumed manifest version. Defaulting a missing one to 1 would quietly pass the
    # compatibility check below for a theme.json that is malformed — the check would run, look
    # like it had verified something, and have verified nothing.
    if "manifest_version" not in manifest:
        out["problems"].append(
            "theme.json declares no manifest_version — cannot check compatibility"
        )
    else:
        required = int(manifest["manifest_version"])
        if required > SUPPORTED_MANIFEST:
            out["problems"].append(
                f"needs manifest_version {required}, CSS Loader here supports {SUPPORTED_MANIFEST}"
            )

    out["problems"].extend(validate_patches(manifest, patches))
    if out["problems"]:
        return out

    want = desired_config(manifest, enabled, patches)
    have = read_config(theme_dir)

    # COMPARE THROUGH _patch_value. CSS Loader EXPANDS a patch that has sub-components into
    # {"value": "Black", "components": {"Background Color": "#000"}} when it next saves, so the
    # file it writes never equals the plain string we asked for. A naive dict comparison then
    # reports "configure" on every single run, and each apply would overwrite the components
    # CSS Loader had filled in — a permanent, pointless fight with the plugin over a theme that
    # is already in exactly the requested state.
    #
    # The selection is the `value`; the components are the plugin's business.
    changed_keys = [
        k for k, v in want.items() if _patch_value((have or {}).get(k)) != _patch_value(v)
    ]

    if changed_keys:
        out["actions"].append("configure")
        out["diff"] = {
            k: {"from": _patch_value((have or {}).get(k)), "to": _patch_value(want[k])}
            for k in changed_keys
        }
        if apply:
            # MERGE rather than replace: preserve the components CSS Loader expanded for any
            # patch that is not itself changing, so applying one option does not discard the
            # colour pickers attached to another.
            merged = dict(have or {})
            for key, value in want.items():
                if key in changed_keys or key not in merged:
                    merged[key] = value
            write_config(theme_dir, merged)

    out["config"] = want
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--themes-root", required=True)
    ap.add_argument("--desired", required=True, help="JSON list of themes to manage")
    ap.add_argument(
        "--remove", default="[]", help="JSON list of theme FOLDER names to delete from themes-root"
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    results = []
    for theme in json.loads(args.desired):
        try:
            results.append(process(theme, args.themes_root, args.apply))
        except ThemeError as exc:
            results.append({"name": theme.get("name"), "actions": [], "problems": [str(exc)]})

    removals = []
    remove_names = json.loads(args.remove)

    # A theme cannot be both declared and removed: the two paths would fight, and which one won
    # would depend on nothing more than their order here. Caught as a configuration error rather
    # than resolved by precedence, because either reading is a plausible thing to have meant.
    declared = {t.get("folder") or t.get("name") for t in json.loads(args.desired)}
    for name in remove_names:
        if name in declared:
            removals.append(
                {
                    "name": name,
                    "actions": [],
                    "problems": [
                        f"{name!r} is in both steamdeck_css_themes and steamdeck_css_remove — "
                        f"drop it from one"
                    ],
                }
            )
            continue
        try:
            removals.append(remove_theme(name, args.themes_root, args.apply))
        except OSError as exc:
            removals.append({"name": name, "actions": [], "problems": [f"remove failed: {exc}"]})

    problems = [f"{r['name']}: {p}" for r in results + removals for p in r["problems"]]
    changed = any(r["actions"] for r in results + removals)

    print(
        json.dumps(
            {
                "changed": changed,
                "applied": bool(args.apply and changed),
                "themes": results,
                "removed": removals,
                "problems": problems,
            },
            indent=2,
        )
    )
    # Problems are a hard failure: a theme that cannot be validated has silently done nothing,
    # which is the outcome this script exists to make impossible.
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
