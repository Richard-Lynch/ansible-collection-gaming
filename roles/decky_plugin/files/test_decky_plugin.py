#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for decky_plugin_package.py and decky_plugin_probe.py (stdlib unittest, no network).

Each refusal is tested with a near-miss that must PASS, so a check that can never fire, or one
that fires on everything, is caught here rather than on the device.

    python3 roles/decky_plugin/files/test_decky_plugin.py
"""

import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True

import decky_plugin_package as pkg  # noqa: E402
import decky_plugin_probe as probe  # noqa: E402

PLUGIN_JSON = json.dumps({"name": "Fixture", "author": "t", "flags": [], "api_version": 1})
VALIDATOR = """
import json
def load(path):
    doc = json.load(open(path))
    if "boom" in doc:
        raise RuntimeError("validator exploded")
    if "bad" in doc:
        return None, "not configured: bad is set"
    errors = {k: k + " missing" for k in ("a", "b") if k not in doc}
    return {"errors": errors, "sections": {}}, None
"""


def make_zip(path, members, symlink=None):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


GOOD = {
    "fixture/": "",
    "fixture/plugin.json": PLUGIN_JSON,
    "fixture/package.json": json.dumps({"version": "1.2.3"}),
    "fixture/main.py": "class Plugin: pass\n",
    "fixture/py_modules/fixturemod/__init__.py": VALIDATOR,
    "fixture/dist/index.js": "export default 1;\n",
}


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.out = os.path.join(self.dir, "staging")

    def tearDown(self):
        self.tmp.cleanup()

    def run_pkg(self, archive, sha, directory="fixture", name="Fixture", sidecar=""):
        buf = io.StringIO()
        argv = [
            "--archive",
            archive,
            "--sha256",
            sha,
            "--directory",
            directory,
            "--name",
            name,
            "--out",
            self.out,
        ]
        if sidecar:
            argv += ["--sidecar", sidecar]
        with redirect_stdout(buf):
            rc = pkg.main(argv)
        return rc, json.loads(buf.getvalue())

    def good(self):
        path = os.path.join(self.dir, "fixture-v1.2.3.zip")
        return path, make_zip(path, GOOD)

    def test_good_archive_unpacks_then_is_idempotent(self):
        path, sha = self.good()
        rc, res = self.run_pkg(path, sha)
        self.assertEqual(rc, 0, res)
        self.assertTrue(res["changed"])
        self.assertEqual(res["version"], "1.2.3")
        self.assertIn("py_modules/fixturemod/__init__.py", res["files"])
        self.assertIn("py_modules/fixturemod", res["dirs"])
        self.assertIn("py_modules", res["dirs"])
        mode = os.stat(os.path.join(res["package_dir"], "main.py")).st_mode & 0o777
        self.assertEqual(mode, 0o644)
        rc, again = self.run_pkg(path, sha)
        self.assertEqual(rc, 0)
        self.assertFalse(again["changed"])
        self.assertEqual(again["files"], res["files"])

    def test_wrong_pin_is_refused(self):
        path, sha = self.good()
        rc, res = self.run_pkg(path, "0" * 64)
        self.assertEqual(rc, 2)
        self.assertIn("MISMATCH", res["error"])
        self.assertFalse(os.path.isdir(os.path.join(self.out, "0" * 64)))

    def test_sidecar_cross_check(self):
        path, sha = self.good()
        side = os.path.join(self.dir, "fixture.zip.sha256")
        with open(side, "w") as fh:
            fh.write("%s  fixture-v1.2.3.zip\n" % sha)
        self.assertEqual(self.run_pkg(path, sha, sidecar=side)[0], 0)
        with open(side, "w") as fh:
            fh.write("%s  fixture-v1.2.3.zip\n" % ("f" * 64))
        rc, res = self.run_pkg(path, sha, sidecar=side)
        self.assertEqual(rc, 2)
        self.assertIn("disagree", res["error"])

    def test_wrong_top_level_directory(self):
        path, sha = self.good()
        rc, res = self.run_pkg(path, sha, directory="other")
        self.assertEqual(rc, 2)
        self.assertIn("top-level", res["error"])

    def test_two_top_level_directories(self):
        path = os.path.join(self.dir, "two.zip")
        sha = make_zip(path, dict(GOOD, **{"stray/file": "x"}))
        rc, res = self.run_pkg(path, sha)
        self.assertEqual(rc, 2)
        self.assertIn("stray", res["error"])

    def test_climbing_member(self):
        path = os.path.join(self.dir, "slip.zip")
        sha = make_zip(path, dict(GOOD, **{"fixture/../../evil": "x"}))
        rc, res = self.run_pkg(path, sha)
        self.assertEqual(rc, 2)
        self.assertIn("..", res["error"])

    def test_symlink_member(self):
        path = os.path.join(self.dir, "link.zip")
        sha = make_zip(path, GOOD, symlink="fixture/link")
        rc, res = self.run_pkg(path, sha)
        self.assertEqual(rc, 2)
        self.assertIn("symlink", res["error"])

    def test_wrong_display_name(self):
        path, sha = self.good()
        rc, res = self.run_pkg(path, sha, name="Some Other Plugin")
        self.assertEqual(rc, 2)
        self.assertIn("plugin.json name", res["error"])

    def test_no_plugin_json(self):
        path = os.path.join(self.dir, "nojson.zip")
        sha = make_zip(path, {k: v for k, v in GOOD.items() if not k.endswith("plugin.json")})
        rc, res = self.run_pkg(path, sha)
        self.assertEqual(rc, 2)
        self.assertIn("plugin.json", res["error"])


class ConfigCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = os.path.join(self.tmp.name, "plugin")
        os.makedirs(os.path.join(self.plugin, "py_modules", "fixturemod"))
        with open(os.path.join(self.plugin, "py_modules", "fixturemod", "__init__.py"), "w") as fh:
            fh.write(VALIDATOR)
        self.cfg = os.path.join(self.tmp.name, "config.json")
        sys.modules.pop("fixturemod", None)

    def tearDown(self):
        self.tmp.cleanup()
        sys.modules.pop("fixturemod", None)

    def judge(self, doc, check="fixturemod:load"):
        if doc is not None:
            with open(self.cfg, "w") as fh:
                fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        return probe.check_config(self.plugin, self.cfg, check)

    def test_parsed(self):
        self.assertEqual(self.judge({"a": 1, "b": 2})["verdict"], "parsed")

    def test_section_errors_are_invalid(self):
        res = self.judge({"a": 1})
        self.assertEqual(res["verdict"], "invalid")
        self.assertIn("b missing", res["detail"])

    def test_validator_error_is_invalid(self):
        self.assertEqual(self.judge({"a": 1, "b": 2, "bad": 1})["verdict"], "invalid")

    def test_validator_raising_is_cannot_tell(self):
        self.assertEqual(self.judge({"boom": 1})["verdict"], "cannot tell")

    def test_bad_json_is_invalid(self):
        self.assertEqual(self.judge("{not json")["verdict"], "invalid")

    def test_absent(self):
        self.assertEqual(self.judge(None)["verdict"], "absent")

    def test_json_only_when_no_validator(self):
        res = self.judge({"x": 1}, check="")
        self.assertEqual(res["verdict"], "parsed")
        self.assertIn("no plugin validator", res["detail"])


class ProbeVerdictTests(unittest.TestCase):
    """The verdict on a home with no Decky: never 'loaded'."""

    def test_no_install_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as home:
            args = probe.argparse.Namespace(
                home=home,
                directory="fixture",
                name="Fixture",
                service="iaac-no-such-unit",
                ready_pattern="ready",
                config_file="config.json",
                config_check="",
            )
            res = probe.probe(args)
            self.assertNotEqual(res["verdict"], "loaded")
            self.assertFalse(res["installed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
