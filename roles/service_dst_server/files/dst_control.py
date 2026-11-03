#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""DST control: start/stop a Don't Starve Together cluster on request, and stop it when empty.

MANAGED BY ANSIBLE — roles/service_dst_server/files/dst_control.py. Edits here are overwritten.

The server is meant to run only while someone is playing on it. This small process is what
makes that hands-off:

  ENDPOINT   POST /start    start the cluster's systemd target (prepare, then every shard)
             POST /stop     stop it — every shard SAVES first (c_shutdown(true) via dst-shard)
             POST /restart  both
             GET  /healthz  what this process sees, as JSON: unit states, which shard ports are
                            listening, the player count and how it was obtained, the idle clock
           Allowed only from 127.0.0.1 and the --allow addresses (e.g. a client's reserved IPs).
           The allowlist IS the access control and the grant is deliberately narrow: start or
           stop one game server, nothing else, so a credential on the client would add little.

  IDLE       every --interval seconds, while the cluster is up and past its startup grace, count
             the players. Empty for --idle-minutes in a row -> stop the cluster (in enforce mode;
             observe mode only logs WOULD STOP).

THE PLAYER COUNT HAS THREE OUTCOMES, NOT TWO. `players` is an int when it was READ, and None when
it could not be — and None NEVER counts as empty. A count that fails silently to 0 would stop a
server with people on it; one that fails to "busy" merely leaves it running, which is the cheap
direction. Two sources, in order:

  console   print a nonce-tagged count into the master's console and read it back from the
            master's server_log.txt. The count is cluster-wide (the master holds every shard's
            client list). TheNet:GetClientTable() on a dedicated server includes an entry for the
            SERVER ITSELF, distinguished by having a `performance` field; that entry is excluded.
  pause     with pause_when_empty, every shard logs "Sim paused" when its last player leaves and
            "Sim unpaused" when one arrives. If EVERY enabled shard's latest such line is a pause,
            the cluster is empty. Every shard, not just the master: a player in the caves leaves
            the master paused, so the master alone would read as empty while someone is playing.

WHAT IT DELIBERATELY DOES NOT DO: stop the server because another game started. A game that is
running is not evidence nobody is on DST — the host may be playing DST itself, or a guest may be.
Idle means "no players", and it is measured, not inferred.

NO PRIVILEGES. It runs as the server's own user in a --user unit; `systemctl --user` and
`podman exec` are that user's own, and /proc/net/udp is world-readable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UDP_UNCONN = "07"  # /proc/net/udp `st` for a bound, unconnected socket (TCP_CLOSE's value)

# The console probe. One line — dst-console refuses newlines. `print` lands in server_log.txt.
# NONCE is substituted with str.replace, NEVER str.format: the Lua's own `{}` (an empty table)
# is a format placeholder, and .format() would raise on every probe.
_PLAYERS_LUA = (
    "local n=0 for _,c in ipairs(TheNet:GetClientTable() or {}) do "
    "if c.performance==nil then n=n+1 end end print('DSTCTL NONCE players='..n)"
)


def players_probe(nonce: str) -> str:
    """The console line that makes the master print the player count tagged with `nonce`."""
    if not re.fullmatch(r"[0-9a-f]{6,32}", nonce):
        raise ValueError("nonce must be lowercase hex: %r" % nonce)
    return _PLAYERS_LUA.replace("NONCE", nonce)


_PAUSE_RE = re.compile(r"\bSim (paused|unpaused)\b")


# --------------------------------------------------------------------------------------------
# PURE LOGIC (tested in test_dst_control.py)
# --------------------------------------------------------------------------------------------


@dataclass
class Shard:
    name: str
    unit: str
    container: str
    port: int
    log: str
    is_master: bool = False


@dataclass
class Config:
    instance: str
    target: str
    shards: list[Shard]
    allow: set[str]
    port: int = 8091
    interval: int = 60
    idle_minutes: int = 30
    grace_minutes: int = 15
    mode: str = "enforce"
    podman: str = "/usr/bin/podman"
    state_file: str = ""
    prepare_unit: str = ""

    @property
    def master(self) -> Shard:
        return next(s for s in self.shards if s.is_master)


@dataclass
class Observation:
    target_state: str  # systemd ActiveState of the target
    active_for: float | None  # seconds since the target became active; None = not active
    units: dict[str, str]  # unit -> ActiveState
    listening: dict[str, bool]  # shard name -> its UDP port is bound
    players: int | None
    players_source: str  # console | pause | none
    players_detail: str = ""


@dataclass
class State:
    idle_since: float | None = None
    last_action: dict = field(default_factory=dict)
    last_verdict: str = "starting"


def parse_show(text: str) -> dict[str, dict[str, str]]:
    """`systemctl show -p Id -p ActiveState ... u1 u2` -> {Id: {prop: value}}.

    systemctl separates units with a BLANK line and prints properties in its own order, so the
    blocks are keyed by their Id rather than by position.
    """
    out: dict[str, dict[str, str]] = {}
    for block in text.strip().split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        if "Id" in props:
            out[props["Id"]] = props
    return out


def udp_bound(port: int, tables: list[str]) -> bool:
    """Is any socket bound to local UDP `port`? `tables` are /proc/net/udp{,6} contents."""
    want = "%04X" % port
    for table in tables:
        for line in table.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 4:
                continue
            local, st = cols[1], cols[3]
            if local.rsplit(":", 1)[-1].upper() == want and st == UDP_UNCONN:
                return True
    return False


def players_from_log(lines: list[str], nonce: str) -> int | None:
    """The count printed for THIS nonce, or None. A stale answer to an earlier probe never counts."""
    pat = re.compile(r"DSTCTL %s players=(\d+)\b" % re.escape(nonce))
    for line in reversed(lines):
        m = pat.search(line)
        if m:
            return int(m.group(1))
    return None


def pause_state(lines: list[str]) -> str | None:
    """'paused' / 'unpaused' from the LATEST pause line in a shard log, or None if there is none.

    The control process's own probe line contains neither phrase, so it cannot match itself.
    """
    for line in reversed(lines):
        m = _PAUSE_RE.search(line)
        if m:
            return m.group(1)
    return None


def players_from_pause(states: dict[str, str | None]) -> int | None:
    """0 when EVERY shard's latest pause line is a pause; otherwise unknown (None).

    'unpaused' on any shard means someone is there, but not how many — so it is not a count, and
    None (busy, cannot tell) is the honest answer. A shard with no pause line at all is unknown.
    """
    if states and all(v == "paused" for v in states.values()):
        return 0
    return None


def decide(now: float, obs: Observation, st: State, cfg: Config) -> str:
    """One idle tick. Mutates st.idle_since; returns the verdict.

    Verdicts: disabled, not_running, starting, grace, cannot_tell, in_use, idle, stop, would_stop.
    """
    if cfg.idle_minutes <= 0:
        st.idle_since = None
        return "disabled"
    if obs.target_state != "active" or obs.active_for is None:
        st.idle_since = None
        return "not_running"
    if not all(obs.listening.values()):
        # A shard still loading (or crashed) has no players to count yet; do not start the clock.
        st.idle_since = None
        return "starting"
    if obs.active_for < cfg.grace_minutes * 60:
        st.idle_since = None
        return "grace"
    if obs.players is None:
        # NEVER treated as empty. Reset rather than pause the clock, so a server is stopped only
        # after idle_minutes of CONTINUOUS, POSITIVE evidence that it is empty.
        st.idle_since = None
        return "cannot_tell"
    if obs.players > 0:
        st.idle_since = None
        return "in_use"
    if st.idle_since is None:
        st.idle_since = now
    if now - st.idle_since >= cfg.idle_minutes * 60:
        return "stop" if cfg.mode == "enforce" else "would_stop"
    return "idle"


def peer_allowed(peer: str, allow: set[str]) -> bool:
    """Loopback always; otherwise an exact match. IPv4-mapped IPv6 peers are unwrapped."""
    if peer.startswith("::ffff:"):
        peer = peer[len("::ffff:") :]
    return peer in ("127.0.0.1", "::1") or peer in allow


def summarise(obs: Observation) -> str:
    """The one-word state a client shows: stopped, starting, running, stopping, failed.

    TRANSITIONS FIRST. During a start the target sits `inactive` with a queued job while prepare
    runs, and during a stop the target can go inactive while the shards are still saving — so
    judging by the target alone reports "stopped" mid-start and "running" mid-stop. Any unit (shards or prepare) in transition decides.
    """
    states = set(obs.units.values()) | {obs.target_state}
    if "deactivating" in states:
        return "stopping"
    if "activating" in states:
        return "starting"
    if obs.target_state == "active":
        return "running" if obs.listening and all(obs.listening.values()) else "starting"
    if "failed" in obs.units.values():
        return "failed"
    if "active" in obs.units.values():
        # Target inactive but a shard is up (started by hand, or left behind): not "stopped".
        return "starting"
    return "stopped"


# --------------------------------------------------------------------------------------------
# OBSERVING (I/O)
# --------------------------------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _tail(path: str, nbytes: int = 131072) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _run(argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def unit_states(cfg: Config) -> tuple[dict[str, dict[str, str]], str]:
    units = [cfg.target] + [s.unit for s in cfg.shards]
    if cfg.prepare_unit:
        units.append(cfg.prepare_unit)
    try:
        p = _run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "Id",
                "-p",
                "ActiveState",
                "-p",
                "ActiveEnterTimestampMonotonic",
            ]
            + units
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {}, "systemctl failed: %s" % e
    return parse_show(p.stdout), ""


def count_players(cfg: Config, listening: dict[str, bool]) -> tuple[int | None, str, str]:
    master = cfg.master
    if listening.get(master.name):
        nonce = secrets.token_hex(6)
        try:
            p = _run(
                [
                    cfg.podman,
                    "exec",
                    master.container,
                    "dst-console",
                    players_probe(nonce),
                ],
                timeout=10,
            )
            sent = p.returncode == 0
            detail = "" if sent else ("console write failed: " + (p.stderr or p.stdout).strip())
        except (OSError, subprocess.TimeoutExpired) as e:
            sent, detail = False, "console write failed: %s" % e
        if sent:
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                n = players_from_log(_tail(master.log), nonce)
                if n is not None:
                    return n, "console", ""
                time.sleep(0.5)
            detail = "console did not answer within 6s"
    else:
        detail = "master not listening"
    states = {s.name: pause_state(_tail(s.log)) for s in cfg.shards if s.name in listening}
    n = players_from_pause(states)
    if n is not None:
        return n, "pause", detail
    return None, "none", "%s; pause lines: %s" % (detail, states)


def observe(cfg: Config) -> Observation:
    shown, err = unit_states(cfg)
    target = shown.get(cfg.target, {})
    target_state = target.get("ActiveState", "unknown")
    active_for = None
    if target_state == "active":
        try:
            entered_us = int(target.get("ActiveEnterTimestampMonotonic", "0"))
            if entered_us > 0:
                active_for = max(0.0, time.monotonic() - entered_us / 1e6)
        except ValueError:
            pass
    units = {s.unit: shown.get(s.unit, {}).get("ActiveState", "unknown") for s in cfg.shards}
    if cfg.prepare_unit:
        # Reported (and used by summarise) so a start that is still updating reads "starting".
        units[cfg.prepare_unit] = shown.get(cfg.prepare_unit, {}).get("ActiveState", "unknown")
    tables = [_read("/proc/net/udp"), _read("/proc/net/udp6")]
    listening = {
        s.name: udp_bound(s.port, tables)
        for s in cfg.shards
        if units.get(s.unit) in ("active", "activating", "deactivating")
    }
    if target_state == "active" and listening and all(listening.values()):
        players, source, detail = count_players(cfg, listening)
    else:
        players, source, detail = None, "none", err or "cluster not fully up"
    return Observation(target_state, active_for, units, listening, players, source, detail)


# --------------------------------------------------------------------------------------------
# ACTING
# --------------------------------------------------------------------------------------------


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg, flush=True)


class Control:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.st = State()
        self.lock = threading.Lock()
        self.last_obs: Observation | None = None
        self.idle_verdict = "starting"

    def systemctl(self, verb: str, why: str) -> tuple[bool, str]:
        # --no-block: the HTTP answer must not wait for a world to load (minutes) or save.
        try:
            p = _run(["systemctl", "--user", verb, "--no-block", self.cfg.target])
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        ok = p.returncode == 0
        with self.lock:
            self.st.last_action = {
                "verb": verb,
                "why": why,
                "at": time.time(),
                "ok": ok,
                "error": "" if ok else (p.stderr or p.stdout).strip(),
            }
            self.st.idle_since = None
        log("%s %s (%s): %s" % (verb, self.cfg.target, why, "ok" if ok else p.stderr.strip()))
        self._save()
        return ok, "" if ok else (p.stderr or p.stdout).strip()

    def _save(self) -> None:
        if not self.cfg.state_file:
            return
        tmp = self.cfg.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cfg.state_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_action": self.st.last_action}, f)
            os.replace(tmp, self.cfg.state_file)
        except OSError as e:
            log("could not save state: %s" % e)

    def tick(self) -> None:
        obs = observe(self.cfg)
        with self.lock:
            verdict = decide(time.monotonic(), obs, self.st, self.cfg)
            self.last_obs, self.idle_verdict = obs, verdict
            changed = verdict != self.st.last_verdict
            self.st.last_verdict = verdict
        if changed or verdict in ("stop", "would_stop"):
            log(
                "idle: %s (players=%s via %s %s)"
                % (verdict, obs.players, obs.players_source, obs.players_detail)
            )
        if verdict == "stop":
            self.systemctl("stop", "idle for %d min" % self.cfg.idle_minutes)

    def health(self) -> dict:
        obs = self.last_obs
        if obs is None:
            # No tick has run (--once, or the first request after a restart): observe now and
            # judge it against a THROWAWAY state, so the verdict is real ("grace", "in_use",
            # "cannot_tell"...) rather than a placeholder — but can never be "stop", because a
            # fresh state has no idle history.
            obs = observe(self.cfg)
            with self.lock:
                self.idle_verdict = decide(time.monotonic(), obs, State(), self.cfg)
        with self.lock:
            idle_since = self.st.idle_since
            remaining = None
            if idle_since is not None and self.cfg.idle_minutes > 0:
                remaining = max(
                    0, int(self.cfg.idle_minutes * 60 - (time.monotonic() - idle_since))
                )
            return {
                "instance": self.cfg.instance,
                "state": summarise(obs),
                "target": self.cfg.target,
                "target_state": obs.target_state,
                "units": obs.units,
                "listening": obs.listening,
                "ready": bool(obs.listening)
                and all(obs.listening.values())
                and obs.target_state == "active",
                "players": obs.players,
                "players_source": obs.players_source,
                "players_detail": obs.players_detail,
                "idle": {
                    "verdict": self.idle_verdict,
                    "mode": self.cfg.mode,
                    "minutes": self.cfg.idle_minutes,
                    "grace_minutes": self.cfg.grace_minutes,
                    "stop_in_s": remaining,
                },
                "active_for_s": None if obs.active_for is None else int(obs.active_for),
                "last_action": self.st.last_action,
            }


def make_handler(ctl: Control):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = (json.dumps(body, sort_keys=True) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _gate(self) -> bool:
            peer = self.client_address[0]
            if peer_allowed(peer, ctl.cfg.allow):
                return True
            log("refused %s %s from %s" % (self.command, self.path, peer))
            self._send(403, {"error": "address %s is not allowed" % peer})
            return False

        def do_GET(self) -> None:  # noqa: N802 — http.server's naming
            if not self._gate():
                return
            if self.path in ("/healthz", "/"):
                self._send(200, ctl.health())
            else:
                self._send(404, {"error": "unknown path"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._gate():
                return
            verb = {"/start": "start", "/stop": "stop", "/restart": "restart"}.get(self.path)
            if verb is None:
                self._send(404, {"error": "unknown path"})
                return
            obs = observe(ctl.cfg)
            state = summarise(obs)
            if verb == "start" and state in ("running", "starting"):
                self._send(200, {"result": "already %s" % state, "state": state})
                return
            if verb == "stop" and state == "stopped":
                self._send(200, {"result": "already stopped", "state": state})
                return
            ok, err = ctl.systemctl(verb, "requested by %s" % self.client_address[0])
            if ok:
                self._send(202, {"result": "%s accepted" % verb, "state_before": state})
            else:
                self._send(500, {"error": err})

        def log_message(self, fmt: str, *args) -> None:  # per-request access logging is noise
            return

    return Handler


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    shards = [Shard(**s) for s in raw.pop("shards")]
    allow = set(raw.pop("allow", []))
    cfg = Config(shards=shards, allow=allow, **raw)
    if sum(1 for s in shards if s.is_master) != 1:
        raise SystemExit("config: exactly one shard must be is_master")
    if cfg.mode not in ("observe", "enforce"):
        raise SystemExit("config: mode must be observe or enforce")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, help="JSON written by the role")
    ap.add_argument("--once", action="store_true", help="print one health report and exit")
    ap.add_argument(
        "--fire-test",
        type=int,
        metavar="SECONDS",
        help=(
            "run the idle loop WITHOUT the HTTP server until it stops the cluster (exit 0) or "
            "SECONDS pass (exit 1) — proves idle shutdown can fire, through the same tick() "
            "and systemctl path the endpoint uses"
        ),
    )
    args = ap.parse_args()
    cfg = load_config(args.config)
    ctl = Control(cfg)
    if args.once:
        print(json.dumps(ctl.health(), indent=2, sort_keys=True))
        return 0
    if args.fire_test:
        deadline = time.monotonic() + args.fire_test
        while time.monotonic() < deadline:
            ctl.tick()
            if ctl.st.last_action.get("verb") == "stop":
                ok = bool(ctl.st.last_action.get("ok"))
                log("fire-test: idle shutdown FIRED (%s)" % ("ok" if ok else "systemctl FAILED"))
                return 0 if ok else 1
            time.sleep(cfg.interval)
        log(
            "fire-test: idle shutdown did NOT fire within %ds (last verdict %s)"
            % (args.fire_test, ctl.idle_verdict)
        )
        return 1

    def loop() -> None:
        while True:
            try:
                ctl.tick()
            except Exception as e:  # noqa: BLE001 — the loop must survive one bad observation
                log("tick failed: %r" % e)
            time.sleep(cfg.interval)

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(ctl))
    log(
        "listening on :%d for %s (allow %s; idle %d min, %s)"
        % (
            cfg.port,
            cfg.target,
            sorted(cfg.allow) or "loopback only",
            cfg.idle_minutes,
            cfg.mode,
        )
    )
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
