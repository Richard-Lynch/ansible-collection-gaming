#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Survey a Steam Deck: hardware, OS, streaming client, and what is worth managing.

Emits JSON so the result can be diffed between runs and asserted on, rather than eyeballed.

WHY A SCRIPT rather than a pile of shell in the play: the interesting facts live in half a dozen
unrelated places (DMI, sysfs, flatpak, a Qt .conf, Decky's plugin directory), and several need
parsing rather than grepping. Doing it here keeps the play readable and makes the parsing
testable.

IT READS SECRETS AND MUST NOT PRINT THEM. Moonlight's config contains the client's TLS PRIVATE
KEY and the paired host's certificate. Those keys are explicitly excluded from the output — a
probe that helpfully dumped the whole config would leak a private key into the play log, and
from there into any transcript of the run.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess

# Keys in Moonlight.conf whose VALUES are secret. Matched case-insensitively against the key
# name; the value is replaced with a marker rather than omitted, so its presence is still visible.
SECRET_KEYS = {"key", "certificate", "srvcert"}

MOONLIGHT_CONF = os.path.expanduser(
    "~/.var/app/com.moonlight_stream.Moonlight/config/"
    "Moonlight Game Streaming Project/Moonlight.conf"
)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def hardware() -> dict:
    """Model, panel and battery.

    The MODEL matters for a real decision: 'Galileo' is the OLED Deck, whose panel runs at 90Hz,
    while 'Jupiter' is the LCD at 60Hz. Setting a 90Hz stream on an LCD Deck would be wrong, so
    the recommendation below is gated on this rather than assumed.
    """
    model = _read("/sys/devices/virtual/dmi/id/product_name")
    modes = []
    for path in glob.glob("/sys/class/drm/card*-eDP*/modes"):
        modes = [m for m in _read(path).splitlines() if m]
        break
    caps = []
    for path in glob.glob("/sys/class/power_supply/BAT*/capacity"):
        caps.append(_read(path))
    return {
        "model": model,
        "model_name": {"Galileo": "Steam Deck OLED", "Jupiter": "Steam Deck LCD"}.get(
            model, model or "unknown"
        ),
        "panel_hz": 90 if model == "Galileo" else (60 if model == "Jupiter" else None),
        "panel_modes": modes[:3],
        "battery_percent": int(caps[0]) if caps and caps[0].isdigit() else None,
    }


def os_info() -> dict:
    rel = _read("/etc/os-release")
    pretty = ""
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', rel, re.M)
    if m:
        pretty = m.group(1)
    rootfs_ro = None
    mounts = _read("/proc/mounts")
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[1] == "/":
            rootfs_ro = parts[3].split(",")[0] == "ro"
            break
    return {
        "pretty_name": pretty,
        "kernel": os.uname().release,
        "hostname": _read("/etc/hostname"),
        "rootfs_readonly": rootfs_ro,
        # Whether unattended privileged work is possible at all. `sudo -n` succeeds only if no
        # password is needed.
        "sudo_passwordless": subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode
        == 0,
    }


def network() -> dict:
    """Interfaces and their PERMANENT MAC addresses.

    THE PERMANENT ADDRESS IS THE POINT. SteamOS randomises WiFi MACs per-SSID by default, so the
    address an adapter is currently associated as rotates — a DHCP reservation keyed to it
    silently stops matching weeks later, and the symptom is a device that mysteriously stops
    getting its reserved address. `ethtool -P` reports the address burned into the adapter,
    which is what a reservation must use.

    Also reports Wake-on-LAN capability, since a handheld that sleeps for days is exactly the
    case where waking it on demand matters.
    """
    out = {}
    for iface in sorted(os.listdir("/sys/class/net")):
        if iface == "lo":
            continue
        current = _read(f"/sys/class/net/{iface}/address")
        perm = ""
        m = re.search(r"Permanent address:\s*([0-9a-f:]{17})", _run(["ethtool", "-P", iface]), re.I)
        if m:
            perm = m.group(1)
        wol = ""
        w = re.search(r"^\s*Wake-on:\s*(\S+)", _run(["ethtool", iface]), re.M)
        if w:
            wol = w.group(1)
        wol_supported = ""
        ws = re.search(r"Supports Wake-on:\s*(\S+)", _run(["ethtool", iface]))
        if ws:
            wol_supported = ws.group(1)
        out[iface] = {
            "current_mac": current,
            "permanent_mac": perm,
            # A randomised address differs from the permanent one, and has the
            # locally-administered bit (bit 1 of the first octet) set.
            "randomised": bool(perm and current and perm.lower() != current.lower()),
            "wol_supported": wol_supported,
            "wol_enabled": wol,
            "carrier": _read(f"/sys/class/net/{iface}/carrier") == "1",
        }
    return out


def flatpaks() -> dict:
    out = _run(["flatpak", "list", "--app", "--columns=application,version"])
    apps = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            apps[parts[0]] = parts[1] if len(parts) > 1 else ""
    return apps


def decky() -> dict:
    """Decky Loader plugins.

    Reported so automation can AVOID them: these are hand-installed customisations, and a role
    that reconciled this directory would silently undo the user's own choices.
    """
    plugin_dir = os.path.expanduser("~/homebrew/plugins")
    if not os.path.isdir(plugin_dir):
        return {"installed": False, "plugins": []}
    return {
        "installed": True,
        "plugins": sorted(
            d for d in os.listdir(plugin_dir) if os.path.isdir(os.path.join(plugin_dir, d))
        ),
    }


def moonlight() -> dict:
    """Moonlight's streaming settings and paired hosts, with secrets redacted."""
    raw = _read(MOONLIGHT_CONF)
    if not raw:
        return {"installed": False}

    settings: dict[str, str] = {}
    hosts: dict[str, dict] = {}
    for line in raw.splitlines():
        if "=" not in line or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        base = key.split("\\")[-1].lower()
        if base in SECRET_KEYS:
            value = "<redacted: secret>"
        # Host entries are numbered: "1\hostname=streaming-host".
        m = re.match(r"^(\d+)\\(.+)$", key)
        if m:
            hosts.setdefault(m.group(1), {})[m.group(2)] = value.strip()
        else:
            settings[key] = value.strip()

    # Per-app HDR, lifted out of the host entries into one flat map so a mismatch against the
    # global `hdr=` is checkable. Inside `hosts` these are buried as `apps\\1\\hdr` under each
    # host index, which is correct but awkward to compare against a top-level setting.
    app_hdr: dict[str, str] = {}
    for host_idx, entries in hosts.items():
        for key, value in entries.items():
            m = re.match(r"^apps\\(\d+)\\hdr$", key)
            if m:
                app_hdr[f"{host_idx}/app{m.group(1)}"] = value

    return {
        "installed": True,
        "config_path": MOONLIGHT_CONF,
        "settings": {k: v for k, v in settings.items() if k.lower() not in SECRET_KEYS},
        "hosts": hosts,
        "app_hdr": app_hdr,
    }


def storage() -> dict:
    """Filesystems, with the detail that decides where content can actually live.

    DELIBERATELY DUPLICATED from steamdeck_status.py rather than imported.
    `ansible.builtin.script` copies exactly ONE file to the target and runs it there, so an
    import of a sibling module fails with a bare non-zero exit and no useful message. The two
    must be changed together; they are small and the shapes are asserted identical by the role's
    molecule scenario.
    """
    mounts: dict = {}
    for line in _read("/proc/self/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mounts[parts[1].encode().decode("unicode_escape")] = (parts[0], parts[2])

    def stat(path: str) -> dict | None:
        try:
            st = os.statvfs(path)
        except OSError:
            return None
        if st.f_blocks == 0:
            return None
        device, fstype = mounts.get(path, (None, None))
        return {
            "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1),
            "free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1),
            # f_bfree, not f_bavail: the latter excludes root-reserved blocks and overstates use
            # by the reserve percentage (~5% on a default ext4).
            "used_percent": round((st.f_blocks - st.f_bfree) / st.f_blocks * 100, 1),
            "device": device,
            "fstype": fstype,
            "supports_prefixes": fstype in ("ext4", "btrfs", "xfs", "f2fs") if fstype else None,
        }

    out: dict = {}
    for label, path in (("root", "/"), ("home", "/home")):
        info = stat(path)
        if info:
            out[label] = info
    # From the mount table, not a glob of /run/media — /run/media/extras can be a SYMLINK to
    # /run/media/deck/extras, and globbing descends into it and reported
    # libraryfolder.vdf/lost+found/steamapps as filesystems. See steamdeck_status.py.
    for point in sorted(mounts):
        if not point.startswith("/run/media/"):
            continue
        info = stat(point)
        if info:
            out[os.path.basename(point)] = info
    return out


def recommendations(
    hw: dict, ml: dict, recommendations_net: dict | None = None, st: dict | None = None
) -> list[dict]:
    """Concrete, checkable findings — not general advice.

    Each carries the observed value and the suggested one, so the play can report a diff rather
    than a lecture, and so a finding disappears once it is fixed.
    """
    out: list[dict] = []

    # STORAGE FINDINGS FIRST, and OUTSIDE the Moonlight guard below. An earlier version returned
    # early when Moonlight was absent, which would have made every storage finding silently
    # unreachable on a device without it.
    for label, fs in (st or {}).items():
        used = fs.get("used_percent")
        if used is None:
            continue
        # The root filesystem is where SteamOS stages an A/B update, so it needs headroom long
        # before it is actually full — hence a lower threshold here than for content volumes.
        limit = 80 if label == "root" else 90
        if used > limit:
            out.append(
                {
                    "id": f"storage-{label}-full",
                    "severity": "high" if used > 95 else "medium",
                    "observed": f"{label} {used}% used ({fs.get('free_gb')}GB free)",
                    "suggested": f"keep {label} below {limit}%",
                    "why": (
                        "SteamOS stages its A/B update into the root partition; too little free "
                        "space there makes an OS update fail rather than run slowly."
                        if label == "root"
                        else f"{label} is nearly full."
                    ),
                }
            )

    if not ml.get("installed"):
        return out
    s = ml.get("settings", {})

    fps = s.get("fps")
    panel = hw.get("panel_hz")
    if fps and panel and int(fps) < panel:
        out.append(
            {
                "id": "fps-below-panel",
                "severity": "medium",
                "observed": f"fps={fps}",
                "suggested": f"fps={panel}",
                "why": f"{hw['model_name']} has a {panel}Hz panel; streaming at {fps} discards "
                f"{panel - int(fps)}Hz of refresh the hardware can display.",
            }
        )

    # NO BITRATE FINDING. Flagging 30000 as "aggressive for WiFi" blames the client for stutter
    # that is as often the HOST's capture resolution or encoder. Bitrate is a deliberate trade a
    # user makes against their own link, and a check that flags a considered choice is noise that trains
    # people to ignore the report.

    # HDR IS STORED TWICE and the two can disagree. The global `hdr=` is only the default for
    # NEW app entries; every already-paired app carries its own `<host>\apps\<n>\hdr=`, and
    # that one is what the stream actually uses.
    #
    # The earlier version of this finding fired on `hdr=true` alone and called it "client-only",
    # which was a guess about the HOST that this script cannot see — and it would have kept
    # firing after the host was correctly configured, training the reader to ignore it. What IS
    # checkable from here is the internal contradiction.
    app_hdr = ml.get("app_hdr") or {}
    if app_hdr:
        disagreeing = sorted(k for k, v in app_hdr.items() if v != s.get("hdr"))
        if disagreeing:
            out.append(
                {
                    "id": "hdr-global-app-mismatch",
                    "severity": "medium",
                    "observed": f"hdr={s.get('hdr')} globally, but "
                    + ", ".join(f"{k}={app_hdr[k]}" for k in disagreeing),
                    "suggested": "make the per-app values match the global",
                    "why": "The per-app value is what a stream uses; the global is only the default "
                    "for newly-paired apps. A true global with false apps reads as HDR "
                    "enabled while every launchable app streams SDR.",
                }
            )

    for iface, net in (recommendations_net or {}).items():
        if net.get("randomised"):
            out.append(
                {
                    "id": "randomised-mac",
                    "severity": "medium",
                    "observed": f"{iface} using {net['current_mac']}, permanent is {net['permanent_mac']}",
                    "suggested": "reserve against the PERMANENT address",
                    "why": "SteamOS randomises WiFi MACs per-SSID. A DHCP reservation keyed to the "
                    "address currently in use silently stops matching when it rotates, and "
                    "the device quietly loses its reserved IP weeks later.",
                }
            )

    for idx, host in ml.get("hosts", {}).items():
        local = host.get("localaddress", "")
        manual = host.get("manualaddress", "").strip()
        # A discovered IP is only a PROBLEM when there is no name to fall back on. localaddress
        # is populated by mDNS discovery and will always be an IP — flagging it unconditionally
        # would keep reporting a fault that has been fixed, which trains people to ignore the
        # report. manualaddress is the user-supplied field and is tried alongside discovery.
        has_name = bool(manual) and not re.match(r"^\d+\.\d+\.\d+\.\d+$", manual)
        # A DHCP RESERVATION is the preferred fix and cannot be seen from here — it lives in the
        # DHCP server, not on this device. So this finding cannot distinguish "pinned by a
        # reservation" from "free to wander", and reporting it as HIGH would cry wolf on a
        # correctly-reserved network. Reported as INFO with the reservation named as the fix.
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", local) and not has_name:
            out.append(
                {
                    "id": "host-address-is-an-ip",
                    "severity": "info",
                    "observed": f"host {host.get('hostname', idx)} localaddress={local}",
                    "suggested": "confirm a DHCP reservation pins this address",
                    "why": "Moonlight records the address mDNS discovery gave it, which is always an "
                    "IP — it has no hostname to fall back on. That is fine PROVIDED the "
                    "address is pinned by a DHCP reservation; without one, a renewal moves "
                    "the host and Moonlight reports it offline with nothing in any log. "
                    "The reservation is the right fix and is invisible from this device, so "
                    "this is a prompt to check, not a fault.",
                }
            )
    return out


def main() -> int:
    hw = hardware()
    ml = moonlight()
    net = network()
    st = storage()
    print(
        json.dumps(
            {
                "hardware": hw,
                "os": os_info(),
                "network": net,
                "flatpaks": flatpaks(),
                "decky": decky(),
                "moonlight": ml,
                "storage": st,
                "recommendations": recommendations(hw, ml, net, st),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
