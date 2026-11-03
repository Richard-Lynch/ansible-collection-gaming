#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify and unpack a Decky plugin release archive, on the CONTROLLER.

Prints one JSON object. On refusal it prints {"error": "..."} and exits 2, so the calling task
fails with the reason rather than with a traceback.

THE PINNED SHA256 IS THE TRUST ANCHOR. The archive is refused unless its digest equals the value
pinned in inventory. A sidecar .sha256 file (published next to the zip) is only a cross-check: it
comes from the same place as the zip, so agreeing with it proves the download is intact, not that
it is the build that was reviewed.

The archive must hold exactly ONE top-level directory, named as the caller expects, with a
plugin.json whose `name` is the expected display name. Members that are absolute, climb with
`..`, or are symlinks are refused (a zip can carry all three, and extracting any of them writes
outside the package).

Idempotent: the package is unpacked to <out>/<sha256>/<directory>/ once; a later run with the
same digest finds the manifest written beside it and reports changed=false. A partial unpack
cannot be mistaken for a complete one — extraction happens in a temporary directory that is
renamed into place only when complete.

Stdlib only.
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile

MANIFEST = "manifest.json"


class Refused(Exception):
    pass


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_members(zf, directory):
    """Return (files, dirs) relative to the top-level directory, or raise Refused."""
    files, dirs, tops = [], set(), set()
    for info in zf.infolist():
        name = info.filename
        if "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            raise Refused("archive member %r is an absolute or non-POSIX path" % name)
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts:
            continue
        if ".." in parts:
            raise Refused("archive member %r climbs out with '..'" % name)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise Refused("archive member %r is a symlink" % name)
        tops.add(parts[0])
        rel = "/".join(parts[1:])
        if not rel:
            continue
        if name.endswith("/"):
            dirs.add(rel)
        else:
            files.append(rel)
            for i in range(1, len(parts) - 1):
                dirs.add("/".join(parts[1 : i + 1]))
    if tops != {directory}:
        raise Refused(
            "archive must hold exactly one top-level directory %r; found %s"
            % (directory, sorted(tops) or "none")
        )
    if "plugin.json" not in files:
        raise Refused("archive has no %s/plugin.json" % directory)
    return sorted(files), sorted(dirs)


def extract(zf, directory, dest):
    """Extract the members under <dest>/<directory>/ with fixed modes (0644 files, 0755 dirs)."""
    root = os.path.join(dest, directory)
    os.makedirs(root, mode=0o755)
    for info in zf.infolist():
        parts = [p for p in info.filename.split("/") if p not in ("", ".")]
        if len(parts) < 2:
            continue
        target = os.path.join(root, *parts[1:])
        if info.filename.endswith("/"):
            os.makedirs(target, mode=0o755, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        os.chmod(target, 0o644)
    return root


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def run(args):
    if not os.path.isfile(args.archive):
        raise Refused("archive %s does not exist" % args.archive)
    actual = sha256_of(args.archive)
    if actual != args.sha256:
        raise Refused(
            "sha256 MISMATCH: %s is %s but the pinned value is %s — refusing it. If the release "
            "was deliberately rebuilt, review it and update the pin."
            % (os.path.basename(args.archive), actual, args.sha256)
        )
    if args.sidecar:
        with open(args.sidecar, "r", encoding="utf-8") as fh:
            tokens = fh.read().split()
        if not tokens or tokens[0].lower() != actual:
            raise Refused(
                "the release's own %s says %s, the archive is %s — the asset and its checksum "
                "disagree"
                % (
                    os.path.basename(args.sidecar),
                    tokens[0] if tokens else "nothing",
                    actual,
                )
            )

    with zipfile.ZipFile(args.archive) as zf:
        files, dirs = check_members(zf, args.directory)
        base = os.path.join(args.out, actual)
        package_dir = os.path.join(base, args.directory)
        manifest_path = os.path.join(base, MANIFEST)
        changed = False
        manifest = None
        if os.path.isfile(manifest_path) and os.path.isdir(package_dir):
            manifest = read_json(manifest_path)
            if manifest.get("files") != files:
                manifest = None
        if manifest is None:
            os.makedirs(args.out, exist_ok=True)
            tmp = tempfile.mkdtemp(prefix=".unpack-", dir=args.out)
            try:
                extract(zf, args.directory, tmp)
                manifest = {
                    "sha256": actual,
                    "directory": args.directory,
                    "files": files,
                    "dirs": dirs,
                }
                with open(os.path.join(tmp, MANIFEST), "w", encoding="utf-8") as fh:
                    json.dump(manifest, fh, indent=1, sort_keys=True)
                if os.path.isdir(base):
                    shutil.rmtree(base)
                os.replace(tmp, base)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            changed = True

    plugin = read_json(os.path.join(package_dir, "plugin.json"))
    if plugin.get("name") != args.name:
        raise Refused(
            "plugin.json name is %r, expected %r — wrong archive, or decky_plugin_name is stale"
            % (plugin.get("name"), args.name)
        )
    version = None
    package_json = os.path.join(package_dir, "package.json")
    if os.path.isfile(package_json):
        version = read_json(package_json).get("version")
    return {
        "changed": changed,
        "sha256": actual,
        "package_dir": package_dir,
        "files": files,
        "dirs": dirs,
        "name": plugin.get("name"),
        "flags": plugin.get("flags", []),
        "version": version,
        "has_backend": "main.py" in files,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--sidecar", default="")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Refused as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
