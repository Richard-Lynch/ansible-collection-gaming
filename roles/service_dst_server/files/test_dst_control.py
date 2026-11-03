#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for dst_control.py's decision logic. Stdlib only.

Each rule is tested in BOTH directions: a case that must
make it act, and a near-miss that must not. The expensive failure here is ASYMMETRIC — stopping a
server with people on it loses their session, while failing to stop an empty one costs some RAM —
so most near-misses below are "this must NOT read as empty".
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dst_control import (  # noqa: E402
    Config,
    Observation,
    Shard,
    State,
    decide,
    parse_show,
    pause_state,
    peer_allowed,
    players_from_log,
    players_from_pause,
    players_probe,
    summarise,
    udp_bound,
)

SHARDS = [
    Shard("Master", "dst-master.service", "dst-master", 10999, "/m.log", True),
    Shard("Caves", "dst-caves.service", "dst-caves", 11000, "/c.log"),
]
CFG = Config(
    instance="dst",
    target="dst.target",
    shards=SHARDS,
    allow={"192.0.2.4"},
    idle_minutes=30,
    grace_minutes=15,
    mode="enforce",
)


def obs(**kw) -> Observation:
    base = dict(
        target_state="active",
        active_for=3600.0,
        units={"dst-master.service": "active", "dst-caves.service": "active"},
        listening={"Master": True, "Caves": True},
        players=0,
        players_source="console",
    )
    base.update(kw)
    return Observation(**base)


class Idle(unittest.TestCase):
    def run_for(self, minutes: float, o: Observation, cfg: Config = CFG) -> str:
        st = State()
        verdict = decide(0.0, o, st, cfg)
        return decide(minutes * 60, o, st, cfg) if minutes else verdict

    def test_empty_for_the_whole_window_stops(self):
        self.assertEqual(self.run_for(30, obs()), "stop")

    def test_near_miss_empty_one_second_short_does_not(self):
        st = State()
        decide(0.0, obs(), st, CFG)
        self.assertEqual(decide(30 * 60 - 1, obs(), st, CFG), "idle")

    def test_observe_mode_never_stops(self):
        cfg = Config(**{**CFG.__dict__, "mode": "observe"})
        self.assertEqual(self.run_for(60, obs(), cfg), "would_stop")

    def test_a_player_resets_the_clock(self):
        st = State()
        decide(0.0, obs(), st, CFG)
        self.assertEqual(decide(20 * 60, obs(players=1), st, CFG), "in_use")
        # 20 more minutes empty: only 20 of the 30 since the player left.
        decide(21 * 60, obs(), st, CFG)
        self.assertEqual(decide(41 * 60, obs(), st, CFG), "idle")
        self.assertEqual(decide(51 * 60, obs(), st, CFG), "stop")

    def test_unknown_player_count_is_never_empty(self):
        # The load-bearing near-miss: a count that could not be read must NOT stop the server,
        # however long it lasts.
        st = State()
        for t in range(0, 4 * 3600, 60):
            self.assertEqual(
                decide(float(t), obs(players=None, players_source="none"), st, CFG),
                "cannot_tell",
            )

    def test_unknown_resets_rather_than_pauses_the_clock(self):
        st = State()
        decide(0.0, obs(), st, CFG)
        decide(25 * 60, obs(players=None), st, CFG)
        # Empty again: the 25 minutes before the unknown reading do not carry over.
        decide(26 * 60, obs(), st, CFG)
        self.assertEqual(decide(50 * 60, obs(), st, CFG), "idle")

    def test_grace_after_start(self):
        self.assertEqual(self.run_for(0, obs(active_for=60.0)), "grace")
        # Near-miss: just past the grace the clock starts.
        self.assertEqual(self.run_for(0, obs(active_for=15 * 60 + 1.0)), "idle")

    def test_a_shard_not_listening_is_starting_not_idle(self):
        o = obs(listening={"Master": True, "Caves": False})
        self.assertEqual(self.run_for(60, o), "starting")

    def test_not_running(self):
        self.assertEqual(
            self.run_for(0, obs(target_state="inactive", active_for=None)),
            "not_running",
        )

    def test_disabled(self):
        cfg = Config(**{**CFG.__dict__, "idle_minutes": 0})
        self.assertEqual(self.run_for(600, obs(), cfg), "disabled")


class PlayerCount(unittest.TestCase):
    LOG = [
        "[00:01:00]: DSTCTL aaaa players=3",
        "[00:02:00]: [Join Announcement] Someone",
        "[00:03:00]: DSTCTL bbbb players=0",
    ]

    def test_reads_the_answer_to_this_nonce(self):
        self.assertEqual(players_from_log(self.LOG, "aaaa"), 3)
        self.assertEqual(players_from_log(self.LOG, "bbbb"), 0)

    def test_a_stale_answer_to_another_probe_does_not_count(self):
        self.assertIsNone(players_from_log(self.LOG, "cccc"))

    def test_the_echoed_command_is_not_an_answer(self):
        # The console may echo the command it ran into the log. The echo contains the Lua
        # `'..n` and not a digit after `players=`, so it must not parse as a count.
        echoed = ["[00:04:00]: RemoteCommandInput: print('DSTCTL dddd players='..n)"]
        self.assertIsNone(players_from_log(echoed, "dddd"))

    def test_nonce_is_not_a_regex(self):
        self.assertIsNone(players_from_log(["DSTCTL aXaa players=1"], "a.aa"))


class Probe(unittest.TestCase):
    def test_probe_carries_the_nonce_and_its_lua_survives(self):
        # Regression: built with str.format, the Lua's `{}` raised IndexError on every probe.
        line = players_probe("abc123")
        self.assertIn("print('DSTCTL abc123 players='..n)", line)
        self.assertIn("GetClientTable() or {}", line)
        self.assertNotIn("\n", line)

    def test_the_answer_to_the_probe_parses_back(self):
        nonce = "deadbeef01"
        self.assertEqual(players_from_log(["[00:00:01]: DSTCTL %s players=2" % nonce], nonce), 2)

    def test_a_non_hex_nonce_is_refused(self):
        for bad in ("", "abc", "ABCDEF", "abc12'3", "a" * 40):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                players_probe(bad)


class Pause(unittest.TestCase):
    def test_latest_pause_line_wins(self):
        self.assertEqual(pause_state(["Sim paused", "x", "Sim unpaused"]), "unpaused")
        self.assertEqual(pause_state(["Sim unpaused", "Sim paused"]), "paused")
        self.assertIsNone(pause_state(["nothing here"]))

    def test_every_shard_paused_is_empty(self):
        self.assertEqual(players_from_pause({"Master": "paused", "Caves": "paused"}), 0)

    def test_master_paused_while_someone_is_in_the_caves_is_not_empty(self):
        self.assertIsNone(players_from_pause({"Master": "paused", "Caves": "unpaused"}))

    def test_a_shard_with_no_pause_line_is_unknown(self):
        self.assertIsNone(players_from_pause({"Master": "paused", "Caves": None}))
        self.assertIsNone(players_from_pause({}))


class Parsing(unittest.TestCase):
    def test_parse_show_keys_by_id(self):
        text = (
            "ActiveState=active\nId=dst.target\nActiveEnterTimestampMonotonic=123\n\n"
            "Id=dst-master.service\nActiveState=activating\n"
        )
        got = parse_show(text)
        self.assertEqual(got["dst.target"]["ActiveState"], "active")
        self.assertEqual(got["dst-master.service"]["ActiveState"], "activating")

    UDP = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
        "  1: 00000000:2AF7 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000\n"
        "  2: 0100007F:2A78 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000\n"
    )

    def test_udp_bound(self):
        self.assertTrue(udp_bound(10999, [self.UDP]))  # 0x2AF7
        self.assertTrue(udp_bound(10872, [self.UDP]))  # 0x2A78 on loopback still counts
        # Near-miss: 11000 is 0x2AF8, one off.
        self.assertFalse(udp_bound(11000, [self.UDP]))

    def test_udp_bound_ignores_a_connected_socket(self):
        connected = (
            self.UDP.splitlines()[0] + "\n  1: 00000000:2AF8 0A000004:1234 01 0 0 0 0 0 1000\n"
        )
        self.assertFalse(udp_bound(11000, [connected]))


class Access(unittest.TestCase):
    def test_loopback_and_listed_addresses(self):
        for peer in ("127.0.0.1", "::1", "192.0.2.4", "::ffff:192.0.2.4"):
            with self.subTest(peer=peer):
                self.assertTrue(peer_allowed(peer, {"192.0.2.4"}))

    def test_near_misses_are_refused(self):
        for peer in ("192.0.2.40", "192.0.2.", "::ffff:192.0.2.5", "127.0.0.2", ""):
            with self.subTest(peer=peer):
                self.assertFalse(peer_allowed(peer, {"192.0.2.4"}))


class Summary(unittest.TestCase):
    def test_running_needs_every_shard_listening(self):
        self.assertEqual(summarise(obs()), "running")
        self.assertEqual(summarise(obs(listening={"Master": True, "Caves": False})), "starting")

    def test_mid_start_with_prepare_running_is_starting_not_stopped(self):
        # As a client sees it mid-start: target inactive with a queued job, prepare activating.
        units = {
            "dst-master.service": "inactive",
            "dst-caves.service": "inactive",
            "dst-prepare.service": "activating",
        }
        self.assertEqual(
            summarise(obs(target_state="inactive", units=units, listening={})), "starting"
        )

    def test_mid_stop_with_shards_saving_is_stopping_not_running(self):
        units = {"dst-master.service": "deactivating", "dst-caves.service": "active"}
        self.assertEqual(summarise(obs(units=units)), "stopping")
        self.assertEqual(summarise(obs(target_state="inactive", units=units)), "stopping")

    def test_prepare_finished_and_inactive_does_not_block_stopped(self):
        # Near-miss: prepare is a oneshot and sits inactive after every successful start.
        units = {
            "dst-master.service": "inactive",
            "dst-caves.service": "inactive",
            "dst-prepare.service": "inactive",
        }
        self.assertEqual(
            summarise(obs(target_state="inactive", units=units, listening={})), "stopped"
        )

    def test_stopped_and_failed(self):
        down = {"dst-master.service": "inactive", "dst-caves.service": "inactive"}
        self.assertEqual(
            summarise(obs(target_state="inactive", units=down, listening={})), "stopped"
        )
        failed = {"dst-master.service": "failed", "dst-caves.service": "inactive"}
        self.assertEqual(
            summarise(obs(target_state="inactive", units=failed, listening={})),
            "failed",
        )


if __name__ == "__main__":
    unittest.main(verbosity=1)
