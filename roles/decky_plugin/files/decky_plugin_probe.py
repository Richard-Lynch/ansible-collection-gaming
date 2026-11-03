#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only probe: is a Decky Loader plugin loaded and healthy, judged from the RUNNING system.

Two modes, both print one JSON object and write nothing:

  probe   on the DEVICE. Decky's unit state and start time, the plugin's backend process, the
          newest log the backend wrote, Decky's disabled list, and the config. Verdict
          loaded / failed / cannot tell.
  config  anywhere (the controller uses it BEFORE writing). Judge a config file with the
          plugin's own validator, imported from <plugin dir>/py_modules.

WHERE THE EVIDENCE COMES FROM (Decky Loader v3.2.9, backend/decky_loader/):
  * plugin/sandboxed_plugin.py: the backend process retitles itself
    setproctitle(f"{name} ({file})"), runs as the device user unless plugin.json has the `root`
    flag, and sets DECKY_PLUGIN_LOG_DIR = <homebrew>/logs/<directory>.
  * plugin/imports/decky.py: decky.logger writes to <log dir>/<"%Y-%m-%d %H.%M.%S">.log, one file
    per backend START, so the newest file whose name is not older than the process belongs to
    the running backend. A start failure is logged as "Failed to start <name>".
  * loader.py: "Loaded <name>" / "Could not load <file>" go to the plugin_loader journal. On a
    Steam Deck that journal is heavily rotated (a few dozen lines for a whole boot), so it
    is supporting evidence when present and never required.
  * The disabled list is `disabled_plugins` in <homebrew>/settings/loader.json, by display name.

The frontend panel runs inside Steam's CEF and is not visible from here; the verdict is about
the backend, and the report says so.

Stdlib only, Python 3.6+ (whatever the device ships).
"""

import argparse
import glob
import importlib
import json
import os
import pwd
import re
import subprocess
import sys
import time

ERROR_RE = re.compile(
    r"Traceback \(most recent call last\)|\bERROR\b|Failed to start|Could not load"
)
MAX_LINE = 300


def run(argv):
    """(rc, stdout) or (None, '') when the binary is absent."""
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError) as exc:
        return None, str(exc)


def check_config(plugin_dir, config_path, check):
    """Judge a config file: 'parsed' / 'invalid' / 'absent' / 'cannot tell', plus detail."""
    out = {
        "path": config_path,
        "verdict": "cannot tell",
        "detail": "",
        "check": check or "json",
    }
    try:
        st = os.stat(config_path)
    except FileNotFoundError:
        out.update(verdict="absent", detail="no config file")
        return out
    except OSError as exc:
        out["detail"] = "cannot stat: %s" % exc
        return out
    out["mode"] = "%04o" % (st.st_mode & 0o7777)
    try:
        out["owner"] = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        out["owner"] = str(st.st_uid)
    doc, err = read_json(config_path)
    if err is not None:
        out.update(verdict="invalid", detail="not valid JSON or unreadable: %s" % err)
        return out
    if not check:
        out.update(verdict="parsed", detail="valid JSON (no plugin validator configured)")
        return out
    module_name, _, func_name = check.partition(":")
    sys.path.insert(0, os.path.join(plugin_dir, "py_modules"))
    sys.dont_write_bytecode = True
    try:
        func = getattr(importlib.import_module(module_name), func_name)
        result = func(config_path)
    except Exception as exc:  # the plugin's code, so any failure is "cannot tell", not a crash
        out["detail"] = "validator %s raised %s: %s" % (check, type(exc).__name__, exc)
        return out
    finally:
        sys.path.pop(0)
    if not (isinstance(result, tuple) and len(result) == 2):
        out["detail"] = "validator %s did not return (config, error)" % check
        return out
    cfg, error = result
    if error is not None or cfg is None:
        out.update(verdict="invalid", detail=str(error))
        return out
    section_errors = cfg.get("errors") if isinstance(cfg, dict) else None
    if section_errors:
        out.update(
            verdict="invalid",
            detail="; ".join("%s" % v for v in section_errors.values()),
        )
        out["section_errors"] = section_errors
        return out
    out.update(verdict="parsed", detail="the plugin's own validator accepted it")
    return out


def boot_time():
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def backend_processes(name, main_py):
    """Processes Decky titled '<name> (<main.py>)'. Start times in epoch seconds."""
    want = "%s (%s)" % (name, main_py)
    btime, hz = boot_time(), os.sysconf("SC_CLK_TCK")
    found = []
    for pid_dir in glob.glob("/proc/[0-9]*"):
        try:
            with open(pid_dir + "/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if not cmd.startswith(want):
                continue
            st = os.stat(pid_dir)
            with open(pid_dir + "/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            start = btime + int(fields[19]) / hz if btime is not None else None
        except (OSError, ValueError, IndexError):
            continue
        try:
            user = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            user = str(st.st_uid)
        found.append({"pid": int(os.path.basename(pid_dir)), "user": user, "started": start})
    return sorted(found, key=lambda p: p["pid"])


def newest_log(log_dir):
    """(path, start epoch parsed from its name) of the newest *.log, or (None, None)."""
    logs = []
    for path in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            started = time.mktime(time.strptime(os.path.basename(path)[:-4], "%Y-%m-%d %H.%M.%S"))
        except ValueError:
            started = os.path.getmtime(path)
        logs.append((started, path))
    if not logs:
        return None, None
    started, path = max(logs)
    return path, started


def probe(args):
    home = args.home
    plugin_dir = os.path.join(home, "homebrew", "plugins", args.directory)
    main_py = os.path.join(plugin_dir, "main.py")
    out = {"plugin_dir": plugin_dir, "reasons": []}

    rc, state = run(["systemctl", "is-active", args.service])
    out["decky_state"] = state.strip() if rc is not None else None
    rc, since = run(
        [
            "systemctl",
            "show",
            args.service,
            "-p",
            "ActiveEnterTimestamp",
            "--timestamp=unix",
            "--value",
        ]
    )
    m = re.match(r"@(\d+)", since.strip()) if rc == 0 else None
    out["decky_started"] = int(m.group(1)) if m else None
    loader_version = os.path.join(home, "homebrew", "services", ".loader.version")
    out["decky_version"] = (
        open(loader_version).read().strip() if os.path.isfile(loader_version) else None
    )

    plugin, err = read_json(os.path.join(plugin_dir, "plugin.json"))
    out["installed"] = plugin is not None
    out["installed_name"] = plugin.get("name") if plugin else None
    package, _ = read_json(os.path.join(plugin_dir, "package.json"))
    out["installed_version"] = package.get("version") if package else None
    out["has_backend"] = os.path.isfile(main_py)

    loader, _ = read_json(os.path.join(home, "homebrew", "settings", "loader.json"))
    out["disabled"] = bool(loader and args.name in (loader.get("disabled_plugins") or []))

    # No /proc (not Linux): the process table cannot be read, which is "cannot tell", never "absent".
    procs = backend_processes(args.name, main_py) if os.path.isdir("/proc/self") else None
    out["processes"] = procs
    proc_start = min([p["started"] for p in procs or [] if p["started"]] or [0]) or None

    log_dir = os.path.join(home, "homebrew", "logs", args.directory)
    log_path, log_started = newest_log(log_dir)
    out["log"] = log_path
    out["log_started"] = log_started
    # Belongs to the running backend if it was opened no earlier than the process started (the
    # filename has 1 s resolution and is taken at import time, after the process exists).
    out["log_is_current"] = bool(log_path and proc_start and log_started >= proc_start - 2)
    errors, ready = [], False
    if log_path:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if args.ready_pattern and re.search(args.ready_pattern, line):
                        ready = True
                    if ERROR_RE.search(line):
                        errors.append(line.rstrip()[:MAX_LINE])
            out["log_readable"] = True
        except OSError as exc:
            out["log_readable"] = False
            out["reasons"].append("cannot read %s: %s" % (log_path, exc))
    out["log_ready_line"] = ready
    out["log_errors"] = errors[:5]

    journal = []
    if out["decky_started"]:
        rc, text = run(
            [
                "journalctl",
                "-u",
                args.service,
                "--since",
                "@%d" % out["decky_started"],
                "--no-pager",
                "-o",
                "cat",
            ]
        )
        out["journal_readable"] = rc == 0
        needles = (
            "Loaded %s" % args.name,
            "Failed to start %s" % args.name,
            "Could not load %s" % main_py,
        )
        journal = [line[:MAX_LINE] for line in text.splitlines() if any(n in line for n in needles)]
    out["journal"] = journal[-5:]
    journal_failed = any("Failed to start" in line or "Could not load" in line for line in journal)

    config_path = os.path.join(home, "homebrew", "settings", args.directory, args.config_file)
    out["config"] = (
        check_config(plugin_dir, config_path, args.config_check) if out["installed"] else None
    )

    # ---- verdict ---------------------------------------------------------------------------
    reasons = out["reasons"]
    if out["decky_state"] is None:
        verdict = "cannot tell"
        reasons.append("systemctl unavailable: Decky's state cannot be read")
    elif out["decky_state"] != "active":
        verdict = "failed"
        reasons.append("Decky (%s) is %s" % (args.service, out["decky_state"]))
    elif not out["installed"]:
        verdict = "failed"
        reasons.append("not installed: %s/plugin.json absent" % plugin_dir)
    elif out["disabled"]:
        verdict = "failed"
        reasons.append("installed but DISABLED in Decky (loader.json disabled_plugins)")
    elif not out["has_backend"]:
        verdict = "cannot tell"
        reasons.append("frontend-only plugin (no main.py): no backend to observe")
    elif journal_failed:
        verdict = "failed"
        reasons.append("Decky's journal reports a load failure since it started")
    elif procs is None:
        verdict = "cannot tell"
        reasons.append("no /proc here: the backend process cannot be looked for")
    elif not procs:
        verdict = "failed"
        reasons.append("no backend process titled '%s (%s)'" % (args.name, main_py))
    elif out["log_is_current"] and errors:
        verdict = "failed"
        reasons.append("the running backend's log has error lines")
    elif not args.ready_pattern:
        verdict = "loaded"
        reasons.append("backend process running (no ready pattern configured: process only)")
    elif out["log_is_current"] and ready:
        verdict = "loaded"
        reasons.append("backend process running and its current log has the ready line")
    else:
        verdict = "cannot tell"
        reasons.append("backend process running but no ready line in a log opened since it started")
    out["verdict"] = verdict
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode")
    p = sub.add_parser("probe")
    p.add_argument("--home", required=True)
    p.add_argument("--directory", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--service", default="plugin_loader")
    p.add_argument("--ready-pattern", default="")
    p.add_argument("--config-file", default="config.json")
    p.add_argument("--config-check", default="")
    c = sub.add_parser("config")
    c.add_argument("--plugin-dir", required=True)
    c.add_argument("--config", required=True)
    c.add_argument("--config-check", default="")
    args = parser.parse_args(argv)
    if args.mode == "probe":
        print(json.dumps(probe(args)))
    elif args.mode == "config":
        print(json.dumps(check_config(args.plugin_dir, args.config, args.config_check)))
    else:
        parser.error("mode required: probe | config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
