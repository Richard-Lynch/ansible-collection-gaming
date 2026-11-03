#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Why does this device keep waking up? Read-only.

WHY A SCRIPT. "What woke it" has no single source. The evidence is spread across the ACPI
wakeup table, every `power/wakeup` node in sysfs, logind's idle/lid policy, the RTC alarm,
systemd timers with WakeSystem, and the journal's own suspend/resume records — each with its
own format. Collecting it in one tested place beats a different ad-hoc SSH command per question.

WHAT ACTUALLY WAKES A SUSPENDED MACHINE, and therefore what this reads:

  1. An ARMED WAKEUP SOURCE fires    — sysfs power/wakeup=enabled on a device that then sees
                                        activity (a magic packet, a USB device, a PCIe link
                                        event). `wakeup_count`/`wakeup_active_count` record
                                        how many times each one has ACTUALLY fired, which is
                                        the difference between "could wake it" and "did".
  2. An RTC ALARM fires              — /sys/class/rtc/rtc0/wakealarm, set by a systemd timer
                                        with WakeSystem=true, or by anything calling rtcwake.
  3. SOMETHING NEVER SUSPENDED       — an inhibitor lock held by an application means the
                                        machine came back because it never really went down.

A COUNT IS THE EVIDENCE, NOT THE FLAG. A device with wakeup=enabled is merely PERMITTED to wake
the machine; nearly every device on a Deck is. The one that is doing it is the one whose
wakeup_count has gone UP, which is why this reports counts next to flags rather than listing
armed devices and calling them suspects.

EVERYTHING HERE IS A READ. No sysfs writes, no unit changes, no journal rotation.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess


def _read(path: str, default: str = "") -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _run(cmd: list[str]) -> str:
    """Run a read-only command, returning '' rather than raising when it is absent.

    Missing tools are normal here — this same report should work on a Deck where systemd-analyze
    or journalctl has been trimmed — and an absent tool is a gap in evidence, not a failure.
    """
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout


def acpi_wakeup() -> list[dict]:
    """/proc/acpi/wakeup — the ACPI-level enable/disable table.

    This is a DIFFERENT layer from sysfs power/wakeup and the two disagree routinely: a device
    can be enabled here and disabled in sysfs or the reverse. Both are reported because a wake
    needs the whole path, and looking at only one explains nothing.

    Format is fixed-width-ish and version-dependent, so this splits on whitespace and reads by
    position from the LEFT, where the layout is stable: Device, S-state, Status, then a
    sysfs/PCI reference that may be absent.
    """
    out: list[dict] = []
    for line in _read("/proc/acpi/wakeup").splitlines():
        parts = line.split()
        # Skip the header ("Device S-state Status Sysfs node") and anything short.
        if len(parts) < 3 or parts[0] == "Device":
            continue
        out.append(
            {
                "device": parts[0],
                "sstate": parts[1],
                "status": parts[2].strip("*"),
                "node": " ".join(parts[3:]) if len(parts) > 3 else "",
            }
        )
    return out


def _find_wakeup_nodes() -> list[str]:
    """Every /sys/devices/**/power/wakeup, found with a BOUNDED walk.

    NOT glob(recursive=True). /sys/devices is dense with symlinks that point back up the tree,
    and `**` follows them — the walk does not terminate in any useful time. On a Deck, `find`
    locates every node in well under a second while the glob runs for many minutes without
    returning, which reads as a hung play rather than a slow one.

    os.walk does not follow directory symlinks by default, which is exactly the behaviour
    wanted: every real device is reachable through its physical path, and the symlinks are
    alternate names for devices already visited.
    """
    found: list[str] = []
    for root, dirs, files in os.walk("/sys/devices", followlinks=False):
        if os.path.basename(root) == "power" and "wakeup" in files:
            found.append(os.path.join(root, "wakeup"))
            # Nothing below a power/ directory contains another one; not descending saves
            # walking each device's attribute tree.
            dirs[:] = []
    return found


def sysfs_wakeup() -> list[dict]:
    """Every device permitted to wake the machine, WITH the count of times it has done so.

    wakeup_count is the number of wakeup EVENTS the device has signalled; wakeup_active_count
    is how many of those actually aborted or ended a suspend. A non-zero active count on one
    device, with zeroes everywhere else, names the culprit outright — which no amount of
    reading `enabled` flags can do.
    """
    out: list[dict] = []
    for wk in sorted(_find_wakeup_nodes()):
        state = _read(wk)
        dev = os.path.dirname(os.path.dirname(wk))
        power = os.path.dirname(wk)
        entry = {
            "device": os.path.basename(dev),
            "path": dev,
            "wakeup": state,
            # A device that has never fired reports 0; one with no counter reports ''.
            "count": _read(os.path.join(power, "wakeup_count"), ""),
            "active_count": _read(os.path.join(power, "wakeup_active_count"), ""),
            # What the thing actually IS. A bare "0000:04:00.3" names nothing to a human.
            "product": _read(os.path.join(dev, "product"))
            or _read(os.path.join(dev, "name"))
            or _read(os.path.join(dev, "device_name")),
            "driver": (
                os.path.basename(os.path.realpath(os.path.join(dev, "driver")))
                if os.path.exists(os.path.join(dev, "driver"))
                else ""
            ),
        }
        out.append(entry)
    return out


def rtc_alarm() -> dict:
    """A scheduled wake. The single most common cause of "it turns on by itself overnight".

    /sys/class/rtc/rtc0/wakealarm holds a UNIX timestamp when armed and is EMPTY when not, so
    an empty value here is a real answer ("nothing scheduled"), not missing data.
    """
    out: dict = {}
    for rtc in sorted(glob.glob("/sys/class/rtc/rtc*")):
        name = os.path.basename(rtc)
        alarm = _read(os.path.join(rtc, "wakealarm"))
        entry = {"wakealarm": alarm, "armed": bool(alarm)}
        # The driver's own view, which also shows an alarm set through a path that does not
        # write the sysfs node.
        for line in _read(os.path.join(rtc, "../../proc/driver/rtc")).splitlines():
            if line.startswith("alrm_"):
                k, _, v = line.partition(":")
                entry[k.strip()] = v.strip()
        out[name] = entry
    return out


def wake_timers() -> list[dict]:
    """systemd timers that can WAKE the machine to run, not merely run while it is awake.

    WakeSystem=true is what turns a timer into a wake source; a timer without it is irrelevant
    here no matter how often it fires. Read from `systemctl show` rather than the unit file,
    because a drop-in can set it and the on-disk unit would not show it.
    """
    out: list[dict] = []
    listing = _run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
    names = []
    for line in listing.splitlines():
        for tok in line.split():
            if tok.endswith(".timer"):
                names.append(tok)
                break
    for name in sorted(set(names)):
        props = _run(
            [
                "systemctl",
                "show",
                name,
                "-p",
                "WakeSystem",
                "-p",
                "NextElapseUSecRealtime",
                "-p",
                "Persistent",
                "-p",
                "Unit",
            ]
        )
        entry = {"timer": name}
        for line in props.splitlines():
            k, _, v = line.partition("=")
            entry[k] = v
        out.append(entry)
    # Only the ones that can actually wake the machine; the rest are noise in this context.
    return [t for t in out if t.get("WakeSystem") == "yes"]


def suspend_history() -> dict:
    """What the journal recorded about each sleep and each wake.

    THE WAKE REASON IS IN THE KERNEL LOG, not in logind's. systemd-sleep logs "Entering sleep
    state" and "System returned from sleep state"; the kernel logs which device signalled the
    resume ("PM: Wakeup" / "ACPI: Waking up from" / a driver's own line). Both are collected,
    over several boots, because a pattern across nights is what distinguishes a recurring
    trigger from a one-off.
    """
    out: dict = {}
    # -b is deliberately NOT used: the interesting history spans reboots, and the device may
    # have rebooted since the wake being investigated.
    out["sleep_events"] = _run(
        [
            "journalctl",
            "--no-pager",
            "--since",
            "-7 days",
            "-o",
            "short-iso",
            "-u",
            "systemd-suspend.service",
            "-u",
            "systemd-hibernate.service",
            "-u",
            "systemd-logind.service",
        ]
    ).splitlines()[-200:]

    # Kernel-side resume records. _TRANSPORT=kernel pins this to real kernel messages — without
    # it, a grep for "wakeup" also matches the journalctl command line this very report logged,
    # which reads as a phantom event.
    kern = _run(
        [
            "journalctl",
            "--no-pager",
            "--since",
            "-7 days",
            "-o",
            "short-iso",
            "_TRANSPORT=kernel",
            "--grep",
            "PM: |ACPI: Waking|Wakeup|wakeup|suspend|resume|Restarting tasks",
        ]
    ).splitlines()
    out["kernel_pm"] = kern[-300:]

    # Boot list: an entry per boot distinguishes "woke from suspend" from "powered on cold",
    # which look identical from the outside and have completely different causes.
    out["boots"] = _run(["journalctl", "--list-boots", "--no-pager"]).splitlines()[-15:]
    return out


def inhibitors() -> list[str]:
    """Locks that BLOCK sleep. A machine that never suspended cannot have been woken.

    Reported because "it keeps turning on" and "it never went to sleep" present identically to
    the owner, and the fix for each is the opposite of the other.
    """
    return _run(["systemd-inhibit", "--list", "--no-pager"]).splitlines()


def logind_policy() -> dict:
    """logind's idle and lid policy — the settings that decide whether it sleeps at all."""
    out: dict = {}
    props = _run(
        [
            "systemctl",
            "show",
            "systemd-logind",
            "-p",
            "IdleAction",
            "-p",
            "IdleActionUSec",
            "-p",
            "HandleLidSwitch",
            "-p",
            "HandlePowerKey",
            "-p",
            "HandleSuspendKey",
            "-p",
            "HandleLidSwitchDocked",
        ]
    )
    for line in props.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    # The config as written, including drop-ins — `systemctl show` reports the daemon's loaded
    # view, which is what is in force, but the files say where a value came from.
    files = []
    for path in ["/etc/systemd/logind.conf"] + sorted(
        glob.glob("/etc/systemd/logind.conf.d/*.conf")
    ):
        body = _read(path)
        if body:
            files.append(
                {
                    "path": path,
                    "settings": [
                        ln
                        for ln in body.splitlines()
                        if ln.strip() and not ln.strip().startswith("#")
                    ],
                }
            )
    out["files"] = files
    return out


def usb_autosuspend() -> list[dict]:
    """USB devices currently attached, with their wakeup state.

    A dock, a controller or a charger that emits activity while the machine sleeps is the
    classic "it turned itself on" cause, and the device that did it is usually obvious from
    the product string once the list is in front of you.
    """
    out: list[dict] = []
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        product = _read(os.path.join(dev, "product"))
        if not product:
            continue
        out.append(
            {
                "device": os.path.basename(dev),
                "product": product,
                "manufacturer": _read(os.path.join(dev, "manufacturer")),
                "wakeup": _read(os.path.join(dev, "power", "wakeup"), "n/a"),
                "count": _read(os.path.join(dev, "power", "wakeup_count"), ""),
                "active_count": _read(os.path.join(dev, "power", "wakeup_active_count"), ""),
            }
        )
    return out


def gpe_counts() -> list[dict]:
    """ACPI General Purpose Events, with a firing count each.

    The lowest level at which a spurious wake shows up. A GPE whose count climbs while the
    machine is asleep is a hardware-level wake signal, and it names the event even when no
    driver logged anything.
    """
    out: list[dict] = []
    for gpe in sorted(glob.glob("/sys/firmware/acpi/interrupts/*")):
        raw = _read(gpe)
        if not raw:
            continue
        parts = raw.split()
        if not parts or not parts[0].isdigit():
            continue
        count = int(parts[0])
        # Zero-count events are the overwhelming majority and say nothing; keeping them would
        # bury the handful that have actually fired.
        if count == 0:
            continue
        out.append(
            {
                "event": os.path.basename(gpe),
                "count": count,
                "flags": " ".join(parts[1:]),
            }
        )
    return sorted(out, key=lambda e: e["count"], reverse=True)


def wakeup_irq() -> dict:
    """The IRQ that ended the LAST suspend, resolved to the device that owns it.

    THE SINGLE MOST DIRECT ANSWER available: /sys/power/pm_wakeup_irq holds the IRQ number, and
    that line of /proc/interrupts names its owner (e.g. the PCIe PME of the USB controller's root
    port) — naming the wake path in one read, where the sysfs flags alone said
    only that most devices were permitted to wake the machine.

    It survives only until the next suspend, so it answers "what woke it last time", not "what
    has been waking it all week" — which is why the counts and the journal are collected too.
    """
    irq = _read("/sys/power/pm_wakeup_irq")
    out: dict = {"irq": irq, "owner": ""}
    if not irq:
        return out
    for line in _read("/proc/interrupts").splitlines():
        num, _, rest = line.partition(":")
        if num.strip() == irq:
            # Trailing columns are the controller and the handler names; the per-CPU counts in
            # between are not interesting here beyond confirming it has fired at all.
            out["owner"] = " ".join(rest.split())
            break
    return out


def suspend_durations() -> list[dict]:
    """How LONG each suspend lasted, paired from the kernel's own entry/exit records.

    THE QUESTION NOBODY THINKS TO ASK. "It keeps turning on" and "it never stays asleep" are the
    same observation from the user's side, and a list of wake sources answers neither. A run of
    suspends lasting two to four SECONDS says the machine is not being woken by anything
    external — it is failing to stay down, and the fix is whatever is holding it up rather than
    whatever is permitted to wake it.
    """
    raw = _run(
        [
            "journalctl",
            "--no-pager",
            "--since",
            "-7 days",
            "-o",
            "short-iso",
            "_TRANSPORT=kernel",
            "--grep",
            "PM: suspend entry|PM: suspend exit",
        ]
    )
    events: list[tuple[str, str]] = []
    for line in raw.splitlines():
        stamp = line.split(" ", 1)[0]
        if "suspend entry" in line:
            events.append((stamp, "entry"))
        elif "suspend exit" in line:
            events.append((stamp, "exit"))

    def _secs(ts: str) -> float | None:
        # short-iso is like '2026-01-01T12:00:00+00:00'. Only the wall-clock time is needed for a
        # duration, and pairs never straddle more than a few minutes.
        m = re.search(r"T(\d{2}):(\d{2}):(\d{2})", ts)
        if not m:
            return None
        h, mi, se = (int(g) for g in m.groups())
        return h * 3600 + mi * 60 + se

    out: list[dict] = []
    for (t1, k1), (t2, k2) in zip(events, events[1:]):
        if k1 != "entry" or k2 != "exit":
            continue
        a, b = _secs(t1), _secs(t2)
        if a is None or b is None:
            continue
        d = b - a
        if d < 0:  # crossed midnight; a duration is not recoverable from the time alone
            continue
        out.append({"entry": t1, "exit": t2, "seconds": round(d, 1)})
    return out[-40:]


def main() -> None:
    print(
        json.dumps(
            {
                "acpi_wakeup": acpi_wakeup(),
                "sysfs_wakeup": sysfs_wakeup(),
                "rtc": rtc_alarm(),
                "wake_timers": wake_timers(),
                "history": suspend_history(),
                "inhibitors": inhibitors(),
                "logind": logind_policy(),
                "usb": usb_autosuspend(),
                "gpe": gpe_counts(),
                "wakeup_irq": wakeup_irq(),
                "suspend_durations": suspend_durations(),
                "uptime_seconds": float(_read("/proc/uptime", "0 0").split()[0]),
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
