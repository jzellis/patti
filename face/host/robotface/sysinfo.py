"""Gather host stats for `facectl info`: hostname, IPs, uptime, load, CPU
temperature, memory, disk.

Linux only, standard library only (reads /proc and /sys, runs `ip`), so it
behaves the same on the Pi and on a laptop. Anything unavailable comes back
as None and is simply left off the screen.
"""

import os
import shutil
import socket
import subprocess

# Interfaces that are noise on a robot's info screen.
_SKIP_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vnet")


def ip_addresses():
    """[(iface, ipv4)] for real interfaces, Tailscale (100.x) included."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    addrs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface, ip = parts[1], parts[3].split("/")[0]
        if not iface.startswith(_SKIP_IFACE_PREFIXES):
            addrs.append((iface, ip))
    if not addrs:
        # Fallback: the address the default route would use (no packet sent).
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))
                addrs.append(("ip", s.getsockname()[0]))
        except OSError:
            pass
    return addrs


def uptime_s():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return None


def cpu_temp_c():
    """Hottest thermal zone that looks like a CPU, else the hottest zone."""
    base = "/sys/class/thermal"
    best = None
    try:
        zones = [z for z in os.listdir(base) if z.startswith("thermal_zone")]
    except OSError:
        return None
    for z in zones:
        try:
            with open(os.path.join(base, z, "temp")) as f:
                t = int(f.read()) / 1000
            with open(os.path.join(base, z, "type")) as f:
                kind = f.read().strip().lower()
        except (OSError, ValueError):
            continue
        cpuish = any(k in kind for k in ("cpu", "x86_pkg", "soc", "package"))
        key = (cpuish, t)
        if best is None or key > best[0]:
            best = (key, t)
    return best[1] if best else None


def memory_gb():
    """(used, total) in GiB, 'used' meaning not MemAvailable."""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])  # kB
    except (OSError, ValueError):
        return None
    if "MemTotal" not in info or "MemAvailable" not in info:
        return None
    total = info["MemTotal"] / 1048576
    return total - info["MemAvailable"] / 1048576, total


def disk_gb(path="/"):
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return u.used / 1024 ** 3, u.total / 1024 ** 3


def fmt_uptime(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return "%dd %dh %dm" % (d, h, m) if d else "%dh %dm" % (h, m)


def gather():
    """Title plus (label, value) rows, in display order."""
    rows = []
    for iface, ip in ip_addresses()[:3]:
        label = "TS" if ip.startswith("100.") and iface.startswith("tailscale") else iface[:5]
        rows.append((label, ip))
    if not rows:
        rows.append(("IP", "no network"))
    up = uptime_s()
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    if up is not None:
        rows.append(("UP", fmt_uptime(up) + ("  LOAD %.2f" % load if load is not None else "")))
    t = cpu_temp_c()
    mem = memory_gb()
    if t is not None:
        rows.append(("CPU", "%.0fC" % t + ("  MEM %.1f/%.1fG" % mem if mem else "")))
    elif mem:
        rows.append(("MEM", "%.1f/%.1fG" % mem))
    disk = disk_gb()
    if disk:
        rows.append(("DISK", "%.0f/%.0fG" % disk))
    return socket.gethostname(), rows
