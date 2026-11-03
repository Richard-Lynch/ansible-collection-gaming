#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reconcile a Steam account's Workshop SUBSCRIPTIONS against a declared list, over the Web API.

Run on the controller by the steam_workshop role; stdlib only.

    STEAM_WEBAPI_KEY=... steam_workshop.py '<config JSON>' [--apply]

    config: {"api_base": "https://api.steampowered.com", "steamid": "7656...",
             "items": [{"id": "376333686", "app_id": 322330, "name": "...", "state": "present"}]}

WHY THE API AND NOT THE STEAM CLIENT. A subscription belongs to the ACCOUNT, not to a device:
once subscribed, every Steam client signed in to it downloads the item for that game (a mod
subscribed on the website appears in a signed-in device's workshop/content/<appid> within
minutes, before the game is even launched). So the declaration
is account-level and is applied once, from the controller — not per device.

ENDPOINTS (IPublishedFileService, authenticated by the account owner's own Web API key, minted
at steamcommunity.com/dev/apikey — the same key type NexusMods uses for this):
    GET  /IPublishedFileService/GetUserFiles/v1   type=mysubscriptions  -> what is subscribed
    POST /IPublishedFileService/Subscribe/v1      publishedfileid, appid, list_type=1, notify_client
    POST /IPublishedFileService/Unsubscribe/v1    same
Every subscribe is followed by a READ-BACK through GetUserFiles: a 200 from Subscribe is a
request accepted, not a subscription proven.

NEVER UNSUBSCRIBES what it was not told to. An item absent from the declaration is left alone —
the account has subscriptions for other games and other reasons; only `state: absent` removes.

THE KEY IS READ FROM THE ENVIRONMENT, never argv, so it cannot appear in a process listing or a
failed task's arguments. Prints JSON: per item before/after, and whether anything changed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

LIST_SUBSCRIBED = 1  # EUserUGCList k_EUserUGCList_Subscribed


# --------------------------------------------------------------------------------------------
# PURE LOGIC (tested in test_steam_workshop.py)
# --------------------------------------------------------------------------------------------


def subscribed_ids(response: dict) -> set[str]:
    """publishedfileids from a GetUserFiles response; tolerant of the empty-result shape."""
    body = response.get("response", {})
    return {
        str(d.get("publishedfileid"))
        for d in body.get("publishedfiledetails") or []
        if d.get("publishedfileid") is not None
    }


def plan(current: set[str], items: list[dict]) -> dict[str, str]:
    """item id -> 'subscribe' | 'unsubscribe' | 'ok'. Undeclared subscriptions are not listed."""
    out = {}
    for item in items:
        iid = str(item["id"])
        want = item.get("state", "present") == "present"
        have = iid in current
        out[iid] = "ok" if want == have else ("subscribe" if want else "unsubscribe")
    return out


def validate(cfg: dict) -> list[str]:
    errors = []
    if not str(cfg.get("steamid", "")).isdigit() or len(str(cfg.get("steamid", ""))) != 17:
        errors.append("steamid must be the 17-digit SteamID64")
    for item in cfg.get("items") or []:
        if not str(item.get("id", "")).isdigit():
            errors.append("item %r: id must be digits" % (item,))
        if not str(item.get("app_id", "")).isdigit():
            errors.append("item %r: app_id must be digits" % (item,))
        if item.get("state", "present") not in ("present", "absent"):
            errors.append("item %r: state must be present or absent" % (item,))
    return errors


# --------------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------------


def _get(base: str, path: str, params: dict) -> dict:
    url = "%s%s?%s" % (base.rstrip("/"), path, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def _post(base: str, path: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def current_subscriptions(base: str, key: str, steamid: str, app_id: int) -> set[str]:
    ids, page = set(), 1
    while True:
        resp = _get(
            base,
            "/IPublishedFileService/GetUserFiles/v1/",
            {
                "key": key,
                "steamid": steamid,
                "appid": app_id,
                "type": "mysubscriptions",
                "numperpage": 100,
                "page": page,
                "return_short_description": 0,
            },
        )
        got = subscribed_ids(resp)
        ids |= got
        total = int(resp.get("response", {}).get("total", 0) or 0)
        if not got or len(ids) >= total:
            return ids
        page += 1


def main() -> int:
    cfg = json.loads(sys.argv[1])
    apply = "--apply" in sys.argv[2:]
    key = os.environ.get("STEAM_WEBAPI_KEY", "")
    errors = validate(cfg) + ([] if key else ["STEAM_WEBAPI_KEY is not set"])
    if errors:
        print(json.dumps({"error": errors}))
        return 2
    base = cfg.get("api_base", "https://api.steampowered.com")
    items = cfg.get("items") or []
    report = {}
    for app_id in sorted({int(i["app_id"]) for i in items}):
        app_items = [i for i in items if int(i["app_id"]) == app_id]
        before = current_subscriptions(base, key, cfg["steamid"], app_id)
        actions = plan(before, app_items)
        for item in app_items:
            iid, action = str(item["id"]), actions[str(item["id"])]
            if apply and action != "ok":
                _post(
                    base,
                    "/IPublishedFileService/%s/v1/"
                    % ("Subscribe" if action == "subscribe" else "Unsubscribe"),
                    {
                        "key": key,
                        "publishedfileid": iid,
                        "appid": app_id,
                        "list_type": LIST_SUBSCRIBED,
                        "notify_client": 1,
                    },
                )
            report[iid] = {
                "name": item.get("name", ""),
                "app_id": app_id,
                "action": action,
                "subscribed_before": iid in before,
            }
        if apply and any(a != "ok" for a in actions.values()):
            after = current_subscriptions(base, key, cfg["steamid"], app_id)
            for item in app_items:
                iid = str(item["id"])
                report[iid]["subscribed_after"] = iid in after
    changed = any(r["action"] != "ok" for r in report.values())
    unproven = [
        i
        for i, r in report.items()
        if apply
        and r["action"] != "ok"
        and r.get("subscribed_after") != (r["action"] == "subscribe")
    ]
    print(
        json.dumps(
            {
                "changed": changed,
                "applied": apply and changed,
                "unproven": unproven,
                "items": report,
            }
        )
    )
    return 1 if unproven else 0


if __name__ == "__main__":
    sys.exit(main())
