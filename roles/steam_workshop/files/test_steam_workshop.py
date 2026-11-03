#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for steam_workshop.py. Stdlib only — the API is a local fake HTTP server.

Both directions per rule: a case that must fail and a near-miss that must pass. The expensive failures are
UNSUBSCRIBING something nobody declared (the account's subscriptions span every game), and
reporting success for a subscribe the API accepted but never applied — so the fake can be told
to accept a subscribe and silently drop it, and the run must then FAIL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from steam_workshop import plan, subscribed_ids, validate  # noqa: E402

STEAMID = "76561197960265729"


class Pure(unittest.TestCase):
    def test_subscribed_ids(self):
        resp = {
            "response": {
                "total": 2,
                "publishedfiledetails": [
                    {"publishedfileid": "1"},
                    {"publishedfileid": "2"},
                ],
            }
        }
        self.assertEqual(subscribed_ids(resp), {"1", "2"})

    def test_empty_response_shape(self):
        # Steam omits the list entirely when there is nothing to return.
        self.assertEqual(subscribed_ids({"response": {"total": 0}}), set())

    def test_plan_never_touches_undeclared(self):
        actions = plan({"1", "99"}, [{"id": "1"}, {"id": "2"}, {"id": "3", "state": "absent"}])
        self.assertEqual(actions, {"1": "ok", "2": "subscribe", "3": "ok"})
        self.assertNotIn("99", actions)

    def test_plan_unsubscribes_only_when_declared_absent(self):
        self.assertEqual(plan({"3"}, [{"id": "3", "state": "absent"}]), {"3": "unsubscribe"})

    def test_validate(self):
        ok = {"steamid": STEAMID, "items": [{"id": "1", "app_id": 322330}]}
        self.assertEqual(validate(ok), [])
        # An invented 32-bit ACCOUNT id: the option wants the 17-digit SteamID64.
        self.assertTrue(validate({"steamid": "12345678", "items": []}))
        self.assertTrue(validate({"steamid": STEAMID, "items": [{"id": "x", "app_id": 1}]}))


class FakeSteam:
    def __init__(self, subscribed: set[str], drop_subscribes: bool = False):
        self.subscribed = set(subscribed)
        self.drop = drop_subscribes
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                fake.calls.append(("GET", self.path.split("?")[0], q.get("key", [""])[0]))
                details = [{"publishedfileid": i} for i in sorted(fake.subscribed)]
                self._send(
                    {
                        "response": {
                            "total": len(details),
                            "publishedfiledetails": details,
                        }
                    }
                )

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                q = urllib.parse.parse_qs(self.rfile.read(n).decode())
                fake.calls.append(("POST", self.path, q.get("key", [""])[0]))
                fid = q["publishedfileid"][0]
                if "Unsubscribe" in self.path:
                    fake.subscribed.discard(fid)
                elif not fake.drop:
                    fake.subscribed.add(fid)
                self._send({"response": {}})

            def log_message(self, *a):
                return

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_port

    def close(self):
        self.srv.shutdown()


def run(fake, items, *extra, key="k3y"):
    cfg = {"api_base": fake.base, "steamid": STEAMID, "items": items}
    env = dict(os.environ, STEAM_WEBAPI_KEY=key)
    p = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "steam_workshop.py"),
            json.dumps(cfg),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return p.returncode, json.loads(p.stdout)


class EndToEnd(unittest.TestCase):
    ITEMS = [
        {"id": "376333686", "app_id": 322330, "name": "Combined Status"},
        {"id": "351325790", "app_id": 322330, "name": "Geometric Placement"},
    ]

    def test_preview_changes_nothing(self):
        f = FakeSteam({"376333686"})
        try:
            rc, out = run(f, self.ITEMS)
            self.assertEqual(rc, 0)
            self.assertTrue(out["changed"])
            self.assertFalse(out["applied"])
            self.assertEqual(out["items"]["351325790"]["action"], "subscribe")
            self.assertFalse(any(c[0] == "POST" for c in f.calls))
        finally:
            f.close()

    def test_apply_subscribes_and_proves_it(self):
        f = FakeSteam({"376333686", "999"})
        try:
            rc, out = run(f, self.ITEMS, "--apply")
            self.assertEqual(rc, 0)
            self.assertTrue(out["items"]["351325790"]["subscribed_after"])
            self.assertIn("999", f.subscribed)  # undeclared: untouched
            posts = [c for c in f.calls if c[0] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0][2], "k3y")
        finally:
            f.close()

    def test_a_subscribe_the_api_accepted_but_dropped_fails_the_run(self):
        f = FakeSteam(set(), drop_subscribes=True)
        try:
            rc, out = run(f, self.ITEMS, "--apply")
            self.assertEqual(rc, 1)
            self.assertEqual(sorted(out["unproven"]), ["351325790", "376333686"])
        finally:
            f.close()

    def test_idempotent(self):
        f = FakeSteam({"376333686", "351325790"})
        try:
            rc, out = run(f, self.ITEMS, "--apply")
            self.assertEqual(rc, 0)
            self.assertFalse(out["changed"])
        finally:
            f.close()

    def test_no_key_is_refused(self):
        f = FakeSteam(set())
        try:
            rc, out = run(f, self.ITEMS, key="")
            self.assertEqual(rc, 2)
            self.assertIn("STEAM_WEBAPI_KEY is not set", out["error"])
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
