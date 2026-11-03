#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the Steam Deck's live hardware state — power, thermals, fan, storage, network.

WHY A SCRIPT. The interesting values are spread across half a dozen hwmon devices, the power
supply class, DRM sysfs and /proc, each with its own units and naming. Doing it here keeps the
play readable and makes the unit conversions testable.

HWMON INDICES ARE NOT STABLE. /sys/class/hwmon/hwmonN numbering is assignment order and can
change between boots, so every device here is located by its `name` file — never by index. A
hardcoded hwmon3 works until the day it doesn't, and then silently reports another chip's
readings as if they were the battery's.

UNITS. hwmon reports millidegrees, microvolts, microamps and milliwatts; this converts to
human units at the edge so nothing downstream has to remember which is which.
"""

from __future__ import annotations

import glob
import json
import os
import sys


def _read(path: str, default: str = "") -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _num(path: str, scale: float = 1.0, ndigits: int = 1):
    raw = _read(path)
    if not raw:
        return None
    try:
        return round(int(raw) / scale, ndigits)
    except ValueError:
        return None


def hwmon_by_name(name: str) -> str | None:
    """Locate a hwmon device by its driver name rather than its index."""
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if _read(os.path.join(path, "name")) == name:
            return path
    return None


def battery() -> dict:
    out: dict = {}
    for supply in sorted(glob.glob("/sys/class/power_supply/*")):
        kind = _read(os.path.join(supply, "type"))
        if kind == "Battery":
            out["capacity_percent"] = _num(os.path.join(supply, "capacity"))
            out["status"] = _read(os.path.join(supply, "status"))
            # A battery reports EITHER charge_* (microamp-hours) or energy_* (microwatt-hours)
            # depending on the driver — never both. This Deck reports charge_*, so reading only
            # energy_* returned nothing and health showed as '?'. Try charge first, fall back.
            full = _num(os.path.join(supply, "charge_full"), 1e6, 2)
            design = _num(os.path.join(supply, "charge_full_design"), 1e6, 2)
            now = _num(os.path.join(supply, "charge_now"), 1e6, 2)
            unit = "Ah"
            if full is None:
                full = _num(os.path.join(supply, "energy_full"), 1e6, 2)
                design = _num(os.path.join(supply, "energy_full_design"), 1e6, 2)
                now = _num(os.path.join(supply, "energy_now"), 1e6, 2)
                unit = "Wh"
            out["charge_unit"] = unit
            out["charge_now"] = now
            out["charge_full"] = full
            out["charge_design"] = design
            if full and design:
                out["health_percent"] = round(full / design * 100, 1)
            out["cycle_count"] = _num(os.path.join(supply, "cycle_count"), 1, 0)
            # Negative current = discharging on this platform.
            out["current_a"] = _num(os.path.join(supply, "current_now"), 1e6, 2)
            out["voltage_v"] = _num(os.path.join(supply, "voltage_now"), 1e6, 2)
        elif kind == "Mains":
            out["mains_online"] = _read(os.path.join(supply, "online")) == "1"

    deck = hwmon_by_name("steamdeck_hwmon")
    if deck:
        limit = _num(os.path.join(deck, "max_battery_charge_level"), 1, 0)
        # 0 means "no cap", which reads as a nonsense limit if reported literally.
        out["charge_limit_percent"] = None if limit in (0, None) else limit
    return out


def thermals() -> dict:
    """Temperatures from every source that reports one, labelled by chip."""
    out: dict = {}
    # steamdeck_hwmon's temp1 is labelled "Battery Temp" — attributing it to the APU would be
    # actively misleading, since the two run tens of degrees apart under load.
    for name, key in (
        ("steamdeck_hwmon", "deck"),
        ("amdgpu", "gpu"),
        ("nvme", "nvme"),
        ("acpitz", "acpi"),
        ("ath11k_hwmon", "wifi"),
    ):
        path = hwmon_by_name(name)
        if not path:
            continue
        for temp in sorted(glob.glob(os.path.join(path, "temp*_input"))):
            label = _read(temp.replace("_input", "_label")) or os.path.basename(temp)
            value = _num(temp, 1000.0)
            if value is not None:
                out[f"{key}_{label.lower().replace(' ', '_')}"] = value
    return out


def fan() -> dict:
    path = hwmon_by_name("steamdeck_hwmon")
    if not path:
        return {}
    return {
        "rpm": _num(os.path.join(path, "fan1_input"), 1, 0),
        "target_rpm": _num(os.path.join(path, "fan1_target"), 1, 0),
        # A fault here means the fan is not reporting — worth surfacing rather than showing 0.
        "fault": _read(os.path.join(path, "fan1_fault")) == "1",
    }


def power() -> dict:
    """APU draw, and the GPU's own reading where available."""
    out: dict = {}
    deck = hwmon_by_name("steamdeck_hwmon")
    if deck:
        out["apu_current_a"] = _num(os.path.join(deck, "curr1_input"), 1000.0, 2)
        out["apu_voltage_v"] = _num(os.path.join(deck, "in0_input"), 1000.0, 2)
    gpu = hwmon_by_name("amdgpu")
    if gpu:
        out["gpu_watts"] = _num(os.path.join(gpu, "power1_average"), 1e6, 1)
        out["gpu_cap_watts"] = _num(os.path.join(gpu, "power1_cap"), 1e6, 1)
    return out


def _mounts() -> dict:
    """Mount point -> (device, fstype), from /proc/self/mounts."""
    out: dict = {}
    for line in _read("/proc/self/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            # Mount points are octal-escaped in /proc (a space is \040), which matters for SD
            # cards whose label contains one.
            point = parts[1].encode().decode("unicode_escape")
            out[point] = (parts[0], parts[2])
    return out


def _fs_stat(path: str, mounts: dict) -> dict | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    if total == 0:
        return None
    free = st.f_bavail * st.f_frsize
    device, fstype = mounts.get(path, (None, None))
    return {
        "total_gb": round(total / 1e9, 1),
        "free_gb": round(free / 1e9, 1),
        # USED IS NOT total-free. f_bavail excludes the root-reserved blocks, so on ext4 with the
        # usual 5% reserve those two differ by about 5% of the volume — enough to report a full
        # disk as comfortable. Used is taken from the blocks actually allocated.
        "used_percent": round((st.f_blocks - st.f_bfree) / st.f_blocks * 100, 1),
        "device": device,
        "fstype": fstype,
        # Whether a Proton prefix can live here: exFAT/vfat have no POSIX permissions or
        # symlinks, so Steam cannot install a Windows game to one however much space it has.
        "supports_prefixes": fstype in ("ext4", "btrfs", "xfs", "f2fs") if fstype else None,
    }


def storage() -> dict:
    out: dict = {}
    mounts = _mounts()
    for label, path in (("root", "/"), ("home", "/home")):
        info = _fs_stat(path, mounts)
        if info:
            out[label] = info
    # The SD card is the usual place emulation content lives, and the usual thing to fill up.
    #
    # TAKEN FROM THE MOUNT TABLE, NOT FROM A GLOB OF /run/media. An earlier version globbed that
    # directory and, for any entry that was not itself a mount point, descended one level to look
    # for the card. That can list `libraryfolder.vdf`, `lost+found` and `steamapps` as if they
    # were filesystems: when /run/media/extras is a SYMLINK to /run/media/deck/extras, the glob
    # resolves it to a real directory, finds it absent from the mount table, and enumerates its
    # CONTENTS. statvfs then happily reports the containing filesystem's numbers for each one —
    # identical entries dressed up as separate volumes.
    #
    # The mount table already knows exactly what is mounted and where, under any of the layouts
    # SteamOS uses (/run/media/<label> or /run/media/<user>/<label>), so ask it instead of
    # guessing from directory names.
    for point in sorted(mounts):
        if not point.startswith("/run/media/"):
            continue
        info = _fs_stat(point, mounts)
        if info:
            out[os.path.basename(point)] = info
    return out


def network() -> dict:
    """Which link is actually carrying traffic, and how good is it?

    ANSWERS "is it docked". The dock's USB ethernet appearing with carrier=1 is what
    distinguishes docked from undocked, and that decides whether Wake-on-LAN can work at all.
    """
    out: dict = {"interfaces": {}, "docked": False}
    for path in sorted(glob.glob("/sys/class/net/*")):
        iface = os.path.basename(path)
        if iface == "lo":
            continue
        driver = (
            os.path.basename(os.path.realpath(os.path.join(path, "device", "driver")))
            if os.path.exists(os.path.join(path, "device", "driver"))
            else None
        )
        entry = {
            "carrier": _read(os.path.join(path, "carrier")) == "1",
            "operstate": _read(os.path.join(path, "operstate")),
            "mac": _read(os.path.join(path, "address")),
            "driver": driver,
        }
        speed = _read(os.path.join(path, "speed"))
        if speed and speed.lstrip("-").isdigit() and int(speed) > 0:
            entry["speed_mbps"] = int(speed)
        # r8152 is the dock's USB ethernet chip. Matched on the DRIVER rather than the interface
        # name, which is generated from the USB topology and changes with the port used.
        if driver == "r8152" and entry["carrier"]:
            out["docked"] = True
        out["interfaces"][iface] = entry

    # WiFi signal, for the common "streaming is stuttering" question. iw is not always present,
    # so this reads /proc/net/wireless, which is always there when a wifi driver is loaded.
    for line in _read("/proc/net/wireless").splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            iface = parts[0].rstrip(":")
            if iface in out["interfaces"]:
                # Columns are link quality, signal level (dBm) and noise; the trailing '.' is
                # part of the format, not a decimal point.
                out["interfaces"][iface]["signal_dbm"] = float(parts[3].rstrip("."))
    return out


def display() -> dict:
    """Panel and refresh rate.

    The OLED (Galileo) runs to 90Hz where the LCD (Jupiter) stops at 60 — so a refresh-rate
    reading is only meaningful next to the model, and a finding like "not running at 90Hz" is
    wrong on the wrong hardware.
    """
    out: dict = {}
    for card in sorted(glob.glob("/sys/class/drm/card*-*")):
        if _read(os.path.join(card, "status")) != "connected":
            continue
        name = os.path.basename(card)
        entry: dict = {"connector": name}
        modes = _read(os.path.join(card, "modes")).splitlines()
        if modes:
            entry["current_mode"] = modes[0]
            entry["available_modes"] = len(modes)
        out[name] = entry
    return out


def os_info() -> dict:
    """SteamOS version and whether the rootfs is sealed.

    An unsealed rootfs matters because SteamOS's A/B atomic update DISCARDS anything written to
    it — so "why did my change disappear after an update" has its answer here.
    """
    out: dict = {}
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("VERSION_ID="):
            out["version"] = line.split("=", 1)[1].strip('"')
        elif line.startswith("BUILD_ID="):
            out["build"] = line.split("=", 1)[1].strip('"')
    # The rootfs SEAL is deliberately NOT reported here, because this script cannot answer it
    # honestly. `steamos-readonly status` is the authoritative source, and the live mount options
    # are not: `steamos-readonly enable` sets a flag that applies at BOOT and does not remount,
    # so `/proc/self/mounts` still says rw for the rest of the session. Reporting the mount
    # option would produce a confident wrong answer. Use steamdeck_action=rootfs_report.
    out["rootfs_fstype"] = _mounts().get("/", (None, None))[1]
    return out


def system() -> dict:
    load = _read("/proc/loadavg").split()[:3]
    mem_total = mem_avail = None
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1]) / 1e6
        elif line.startswith("MemAvailable:"):
            mem_avail = int(line.split()[1]) / 1e6
    return {
        "uptime_seconds": int(float(_read("/proc/uptime", "0").split()[0])),
        "load": [float(x) for x in load] if load else [],
        "mem_total_gb": round(mem_total, 1) if mem_total else None,
        "mem_available_gb": round(mem_avail, 1) if mem_avail else None,
        "kernel": os.uname().release,
    }


def main() -> int:
    print(
        json.dumps(
            {
                "battery": battery(),
                "thermals": thermals(),
                "fan": fan(),
                "power": power(),
                "storage": storage(),
                "network": network(),
                "display": display(),
                "os": os_info(),
                "system": system(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
