#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HWCheck - Used-Laptop Hardware Verification Suite
=================================================
Boot the USB stick, run this, and it walks you through testing every
piece of hardware on a second-hand laptop: CPU, RAM, disks, battery,
screen, sound, keyboard, touchpad, webcam, wifi, bluetooth, USB ports,
sensors/fans and more.

Writes an HTML + TXT + JSON report back to the USB stick so you can
keep it as evidence of the machine's condition at purchase time.

Runs from a live Linux session (no OS needed on the target machine).
Needs root for full hardware access (dmidecode, smartctl, /dev/input).

Usage:
    sudo python3 hwcheck.py            # full guided run
    sudo python3 hwcheck.py --quick    # skip the slow deep tests
    sudo python3 hwcheck.py --auto     # no prompts, automatic checks only
"""

import os
import sys
import re
import json
import time
import zlib
import glob
import struct
import shutil
import socket
import platform
import subprocess
import threading
import datetime
import argparse

# ----------------------------------------------------------------------------
# Globals / plumbing
# ----------------------------------------------------------------------------

VERSION = "1.0"
QUICK = False
AUTO = False          # non-interactive: skip every step needing a human
RESULTS = []          # list of dicts: section, name, status, detail
ARTIFACTS = []        # extra files to copy into the report folder
KIT_ROOT = None       # USB payload root, set by find_kit_root()
REPORT_DIR = None
SCRATCH = "/tmp/hwcheck-scratch"

C_RST = "\033[0m"
C_B = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[31m"
C_GRN = "\033[32m"
C_YEL = "\033[33m"
C_BLU = "\033[34m"
C_CYA = "\033[36m"

PLAIN = not sys.stdout.isatty()
if PLAIN:
    C_RST = C_B = C_DIM = C_RED = C_GRN = C_YEL = C_BLU = C_CYA = ""


def c(text, color):
    return f"{color}{text}{C_RST}"


def hr(char="=", n=74):
    print(c(char * n, C_DIM))


def title(text):
    print()
    hr()
    print(c(f"  {text}", C_B + C_CYA))
    hr()


def info(msg):
    print(f"  {msg}")


def note(msg):
    print(c(f"  {msg}", C_DIM))


def has(prog):
    """Is an executable on PATH?"""
    return shutil.which(prog) is not None


def run(cmd, timeout=60, stdin_null=True):
    """Run a command, return (returncode, combined_output). Never raises."""
    if isinstance(cmd, str):
        cmd = cmd.split()
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            stdin=subprocess.DEVNULL if stdin_null else None,
            universal_newlines=True,
            errors="replace",
        )
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:                                   # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def read_file(path, default=""):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read().strip()
    except Exception:                                        # noqa: BLE001
        return default


def add(section, name, status, detail="", warn_if=None):
    """Record one check result.

    status: PASS | FAIL | WARN | INFO | SKIP
    """
    RESULTS.append({
        "section": section,
        "name": name,
        "status": status,
        "detail": str(detail).strip(),
    })
    icon = {
        "PASS": c("[ PASS ]", C_GRN),
        "FAIL": c("[ FAIL ]", C_RED),
        "WARN": c("[ WARN ]", C_YEL),
        "INFO": c("[ INFO ]", C_BLU),
        "SKIP": c("[ SKIP ]", C_DIM),
    }.get(status, "[ ???? ]")
    print(f"  {icon} {name}")
    if detail:
        for ln in str(detail).splitlines():
            print(c(f"          {ln}", C_DIM))
    return status


def ask(prompt, default="n"):
    """Y/N prompt. Returns bool. Always yes in AUTO mode."""
    if AUTO:
        return True
    try:
        a = input(c(f"  ? {prompt} ", C_YEL)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not a:
        a = default
    return a in ("y", "yes", "o", "oui")


def pause(msg="Press Enter to continue"):
    if AUTO:
        return
    try:
        input(c(f"  {msg}...", C_DIM))
    except (EOFError, KeyboardInterrupt):
        print()


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ----------------------------------------------------------------------------
# Tiny pure-python PNG writer (no PIL / ImageMagick needed)
# ----------------------------------------------------------------------------

def write_solid_png(path, width, height, rgb, band=None):
    """Write a solid-colour (or 4-band test) PNG using only zlib+struct."""
    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body +
                struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    rows = []
    if band:
        for y in range(height):
            v = band[int(y * len(band) / height)]
            rows.append(b"\x00" + bytes(v) * width)
    else:
        row = b"\x00" + bytes(rgb) * width
        rows = [row] * height
    raw = b"".join(rows)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", header) +
           chunk(b"IDAT", zlib.compress(raw, 1)) +
           chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    return path


# ----------------------------------------------------------------------------
# USB payload discovery
# ----------------------------------------------------------------------------

MARKERS = ("hwcheck", "BUYERKIT.marker", "ventoy")


def find_kit_root():
    """Return the directory on the USB stick that holds this kit.

    The Ventoy data partition is exFAT labelled 'Ventoy'; the live session
    may or may not have auto-mounted it, so we also mount unmounted
    removable partitions ourselves.
    """
    # 1. Where does this script physically live?
    here = os.path.dirname(os.path.abspath(__file__))
    if _looks_like_kit(here):
        return here
    parent = os.path.dirname(here)
    if _looks_like_kit(parent):
        return parent

    # 2. Already-mounted candidates
    for pat in ("/media/*/*", "/run/media/*/*", "/mnt/*", "/media/*"):
        for cand in sorted(glob.glob(pat)):
            if _looks_like_kit(cand):
                return cand
            for sub in sorted(glob.glob(os.path.join(cand, "hwcheck"))):
                return sub

    # 3. Mount unmounted removable partitions (labelled Ventoy or with marker)
    rc, out = run(["lsblk", "-J", "-o", "NAME,PATH,LABEL,RM,SIZE,FSTYPE"])
    if rc == 0:
        try:
            data = json.loads(out)
        except Exception:                                    # noqa: BLE001
            data = {}
        for dev in data.get("blockdevices", []):
            for part in ([dev] + dev.get("children", []) or []):
                label = (part.get("label") or "")
                path = part.get("path")
                if not path:
                    continue
                if "ventoy" not in label.lower() and not part.get("rm"):
                    continue
                mnt = os.path.join("/mnt/hwkit", os.path.basename(path))
                os.makedirs("/mnt/hwkit", exist_ok=True)
                os.makedirs(mnt, exist_ok=True)
                if not os.path.ismount(mnt):
                    run(["mount", "-o", "ro", path, mnt], timeout=20)
                if os.path.ismount(mnt):
                    if _looks_like_kit(mnt):
                        return mnt
                    sub = os.path.join(mnt, "hwcheck")
                    if os.path.isdir(sub):
                        return sub
    return None


def _looks_like_kit(path):
    if not os.path.isdir(path):
        return False
    return any(os.path.exists(os.path.join(path, m)) for m in MARKERS)


def setup_report_dir():
    global REPORT_DIR
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    model = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                   f"{sys_vendor()}-{product_name()}".strip("-_") or "unknown")[:48]
    base = os.path.join(KIT_ROOT, "report") if KIT_ROOT else "/tmp/hwcheck-report"
    REPORT_DIR = os.path.join(base, f"{ts}_{model}")
    os.makedirs(REPORT_DIR, exist_ok=True)
    return REPORT_DIR


# ----------------------------------------------------------------------------
# DMI helpers
# ----------------------------------------------------------------------------

def dmi(what):
    v = read_file(f"/sys/class/dmi/id/{what}")
    return v.replace("\n", " ").strip()


def sys_vendor():
    return dmi("sys_vendor") or dmi("board_vendor") or ""


def product_name():
    return dmi("product_name") or dmi("board_name") or ""


# ============================================================================
# SECTION 1 - Identity
# ============================================================================

def sec_identity():
    title("1. MACHINE IDENTITY  (what you are actually buying)")

    info(f"Vendor / Model : {c(sys_vendor() + ' ' + product_name(), C_B)}")
    info(f"Board          : {dmi('board_name')}")
    info(f"BIOS           : {dmi('bios_version')}  ({dmi('bios_date')})")
    info(f"Serial         : {dmi('product_serial')}")
    info(f"Board serial   : {dmi('board_serial')}")
    info(f"Chassis        : {dmi('chassis_type')}")
    info(f"Kernel         : {platform.release()}   {platform.machine()}")

    problems = []
    if not product_name():
        problems.append("DMI product name unreadable")
    if dmi("bios_date"):
        try:
            y = int(dmi("bios_date").split("/")[-1])
            age = datetime.date.today().year - y
            if age >= 12:
                problems.append(f"BIOS is {age} years old - check for firmware updates")
        except Exception:                                    # noqa: BLE001
            pass

    # Serial-number tampering proxy: mismatched vendor strings
    if sys_vendor() and dmi("board_vendor") and sys_vendor() != dmi("board_vendor"):
        problems.append("Chassis vendor != mainboard vendor (possible franken-laptop / swapped board)")

    if problems:
        add("Identity", "Identity sanity check", "WARN", "\n".join(problems))
    else:
        add("Identity", "Identity sanity check", "PASS",
            "DMI readable, vendor data consistent")

    # Uptime / previous OS traces
    _, blkid = run(["blkid"])
    os_traces = []
    if re.search(r"ntfs|BitLocker", blkid, re.I):
        os_traces.append("Windows-era filesystem found")
    if re.search(r"Type=\"ext4\"|Type=\"btrfs\"", blkid):
        os_traces.append("Linux filesystem found")
    if "BitLocker" in run(["blkid"])[1]:
        os_traces.append("BitLocker encrypted volume - could be a stolen/company machine")
    add("Identity", "Existing data on disks",
        "INFO" if os_traces else "PASS",
        "; ".join(os_traces) if os_traces else "Disks appear blank / freshly wiped")


# ============================================================================
# SECTION 2 - CPU
# ============================================================================

def cpu_threads():
    n = os.cpu_count() or 1
    return max(1, n)


def cpu_model():
    for line in read_file("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def cpu_max_mhz():
    t = read_file("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    try:
        return int(t) // 1000
    except Exception:                                        # noqa: BLE001
        return None


def cpu_cur_mhz():
    vals = []
    for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"):
        try:
            vals.append(int(read_file(p)) // 1000)
        except Exception:                                    # noqa: BLE001
            pass
    if vals:
        return sum(vals) // len(vals)
    m = re.search(r"cpu MHz\s*:\s*([\d.]+)", read_file("/proc/cpuinfo"))
    return int(float(m.group(1))) if m else None


def load_cpu(seconds, threads):
    """Saturate the CPU with pure-python math for `seconds`."""
    stop = time.time() + seconds

    def worker():
        x = 0.0001
        while time.time() < stop:
            for _ in range(2000):
                x = (x * 1.0000001) + 0.0000001
                if x > 1e6:
                    x = 0.0001

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for t in ts:
        t.start()
    return ts, stop


def cpu_base_mhz():
    """The CPU's guaranteed all-core base clock (not the turbo ceiling)."""
    try:
        return int(read_file("/sys/devices/system/cpu/cpu0/cpufreq/base_frequency")) // 1000
    except Exception:                                        # noqa: BLE001
        pass
    m = re.search(r"@\s*([\d.]+)\s*GHz", cpu_model())
    if m:
        try:
            return int(float(m.group(1)) * 1000)
        except Exception:                                    # noqa: BLE001
            return None
    return None


def parse_sensor_temps(text):
    """Pull real temperature readings out of `sensors` output.

    Skips the high/crit/min/max limit columns, which otherwise inflate the
    maximum (e.g. 'high = +100.0C').
    """
    temps = []
    for line in text.splitlines():
        if re.search(r"high\s*=|crit\s*=|min\s*=|max\s*=|low\s*=", line):
            continue
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*°C", line)
        if m:
            try:
                v = float(m.group(1))
            except Exception:                                # noqa: BLE001
                continue
            if -30 < v < 130:
                temps.append(v)
    return temps


def temp_c():
    """Best available CPU temperature in Celsius."""
    # 1. hwmon (coretemp / k10temp / acpitz)
    best = None
    for p in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")) + \
             sorted(glob.glob("/sys/class/hwmon/hwmon*/temp1_input")):
        try:
            v = int(read_file(p)) / 1000.0
        except Exception:                                    # noqa: BLE001
            continue
        if -20 < v < 130:
            best = v if best is None else max(best, v)
    return best


def fan_rpms():
    out = {}
    for p in sorted(glob.glob("/sys/class/hwmon/hwmon*/fan*_input")):
        try:
            v = int(read_file(p))
        except Exception:                                    # noqa: BLE001
            continue
        if 0 < v < 30000:
            out[os.path.basename(p)] = v
    return out


def sec_cpu():
    title("2. CPU  (real spec, thermals, throttling)")

    info(f"Model      : {c(cpu_model(), C_B)}")
    info(f"Cores      : {cpu_threads()} logical")
    info(f"Max freq   : {cpu_max_mhz() or '?'} MHz   (current {cpu_cur_mhz() or '?'} MHz)")
    info(f"Base freq  : {cpu_base_mhz() or '?'} MHz  (all-core guaranteed clock)")
    gov = read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    info(f"Governor   : {gov or 'n/a'}")

    flags = ""
    for line in read_file("/proc/cpuinfo").splitlines():
        if line.lower().startswith("flags"):
            flags = line.split(":", 1)[1]
            break
    feats = [f for f in ("vmx", "svm", "avx2", "avx512f", "aes", "sse4_2")
             if f in flags.split()]
    info(f"Features   : {', '.join(feats) or 'none detected'}")
    info(f"Microcode  : {read_file('/proc/cpuinfo').count('microcode') } entries")

    if not any(f in flags.split() for f in ("vmx", "svm")):
        add("CPU", "Virtualisation support", "WARN",
            "No VT-x/AMD-V - fine for daily use, limits VMs")

    n = cpu_threads()
    dur = 8 if QUICK else 45
    t_start = temp_c()
    f_start = fan_rpms()
    mhz_start = cpu_cur_mhz()

    print()
    note(f"Loading all {n} threads for {dur}s - watch the temps...")
    threads, stop = load_cpu(dur, n)
    samples_t, samples_mhz = [], []
    while time.time() < stop:
        t = temp_c()
        if t is not None:
            samples_t.append(t)
        m = cpu_cur_mhz()
        if m:
            samples_mhz.append(m)
        time.sleep(1.5)
    for t in threads:
        t.join(timeout=2)

    t_end = temp_c()
    f_end = fan_rpms()
    mhz_end = cpu_cur_mhz()

    tmax = max(samples_t) if samples_t else None
    mhz_max = max(samples_mhz) if samples_mhz else None
    bef = (f"idle {mhz_start} MHz"
           if mhz_start else "idle freq unknown")
    aft = (f"loaded {mhz_max} MHz" if mhz_max else f"loaded {mhz_end} MHz")

    # Thermal throttle detection. Compare against the BASE clock: an all-core
    # load is not supposed to reach the single-core turbo ceiling, so judging
    # by cpuinfo_max_freq produces false alarms on healthy machines.
    rated = cpu_base_mhz() or cpu_max_mhz()
    turbo = cpu_max_mhz()
    if rated and mhz_max:
        ratio = mhz_max / rated
        extra = f" (turbo ceiling {turbo} MHz)" if turbo and turbo != rated else ""
        if ratio < 0.80:
            add("CPU", "Sustained clock under load", "FAIL",
                f"{aft} vs {rated} MHz base clock ({ratio*100:.0f}%){extra} - "
                f"heavy throttling, likely dried thermal paste or clogged fan")
        elif ratio < 0.95:
            add("CPU", "Sustained clock under load", "WARN",
                f"{aft} vs {rated} MHz base clock ({ratio*100:.0f}%){extra} - "
                f"mild throttling, clean the cooling system")
        else:
            add("CPU", "Sustained clock under load", "PASS",
                f"{aft} vs {rated} MHz base clock ({ratio*100:.0f}%){extra} - "
                f"holds its rated speed under full load")
    else:
        add("CPU", "Sustained clock under load", "INFO", f"{bef} -> {aft}")

    if tmax is not None:
        if tmax >= 95:
            add("CPU", "Peak temperature", "FAIL",
                f"{tmax:.0f} C under load - overheating, will throttle or shut down")
        elif tmax >= 85:
            add("CPU", "Peak temperature", "WARN",
                f"{tmax:.0f} C under load (idle {t_start:.0f} C) - warm, "
                f"clean the fan / repaste")
        else:
            add("CPU", "Peak temperature", "PASS",
                f"peak {tmax:.0f} C under load (idle {t_start:.0f} C)")
    else:
        add("CPU", "Peak temperature", "INFO",
            "No thermal sensors exposed (install lm-sensors for detail)")

    if f_end or f_start:
        add("CPU", "Cooling fan", "PASS",
            f"RPM idle {f_start or '?'} -> load {f_end or '?'}")
    else:
        add("CPU", "Cooling fan", "INFO",
            "Fan RPM not exposed to the OS (normal on many consumer laptops) - "
            "confirm by ear: fan must audibly spin up under load")

    # Locked multiplier / reprovisioned CPU warning
    if "Intel" in cpu_model() and cpu_threads() <= 2:
        add("CPU", "Core count", "WARN",
            f"Only {cpu_threads()} threads - confirm this matches the advertised CPU model")

    rc, out = run(["dmesg", "--level=err,warn"], timeout=20)
    if rc == 0:
        mce = [l for l in out.splitlines()
               if re.search(r"mce|machine check|therm|thermal throttl", l, re.I)]
        if mce:
            add("CPU", "Kernel hardware errors", "WARN",
                "\n".join(mce[:6]))
        else:
            add("CPU", "Kernel hardware errors", "PASS",
                "No MCE / thermal-throttle events in dmesg")


# ============================================================================
# SECTION 3 - Memory
# ============================================================================

def mem_total_gb():
    for line in read_file("/proc/meminfo").splitlines():
        if line.startswith("MemTotal"):
            kb = int(re.search(r"(\d+)", line).group(1))
            return kb / 1024 / 1024
    return 0.0


def sec_memory():
    title("3. RAM  (size, slots, speed, integrity)")

    total = mem_total_gb()
    info(f"Total      : {c(f'{total:.2f} GiB', C_B)}")
    info(f"Swap       : {read_file('/proc/meminfo')}")

    # Physical slots via dmidecode
    slots_used, slots_total, dimms = 0, 0, []
    rc, out = run(["dmidecode", "-t", "17"], timeout=25)
    if rc == 0:
        for blk in out.split("Memory Device")[1:]:
            slots_total += 1
            size = re.search(r"^\s*Size:\s*(.+)$", blk, re.M)
            size = size.group(1).strip() if size else "Unknown"
            if "No Module" in size or "Not Installed" in size or size == "0 MB":
                continue
            slots_used += 1
            spd = re.search(r"^\s*Speed:\s*(.+)$", blk, re.M)
            conf = re.search(r"^\s*Configured Memory Speed:\s*(.+)$", blk, re.M)
            manu = re.search(r"^\s*Manufacturer:\s*(.+)$", blk, re.M)
            pn = re.search(r"^\s*Part Number:\s*(.+)$", blk, re.M)
            typ = re.search(r"^\s*Type:\s*(.+)$", blk, re.M)
            dimms.append(
                f"{size} {typ.group(1).strip() if typ else ''} "
                f"@ {(conf or spd).group(1).strip() if (conf or spd) else '?'}"
                f"{' | ' + manu.group(1).strip() if manu else ''}"
                f"{' ' + pn.group(1).strip() if pn else ''}".strip())
    if dimms:
        for d in dimms:
            info(f"  DIMM     : {d}")
    if slots_total:
        add("RAM", "Memory slots", "PASS" if slots_used else "WARN",
            f"{slots_used} of {slots_total} slots populated"
            + (f" - upgradable: {slots_total - slots_used} free slot(s)" if slots_total > slots_used else ""))

    # Consumer-grade RAM check: is the installed amount what was advertised?
    if total < 3.5:
        add("RAM", "Installed capacity", "WARN",
            f"{total:.1f} GiB - too little for modern use, check if a stick is dead/missing")
    else:
        add("RAM", "Installed capacity", "PASS", f"{total:.2f} GiB usable")

    # Mixed / mismatched sticks (dual-channel loss)
    if len(dimms) >= 2 and len(set(d.split("@")[0].split()[-1] for d in dimms)) > 1:
        add("RAM", "Memory configuration", "WARN",
            "Mismatched module sizes - dual channel may be asymmetrical")

    # Integrity test
    print()
    if AUTO:
        add("RAM", "Quick memory integrity", "SKIP",
            "Skipped in --auto mode (needs a few minutes of RAM testing)")
    elif has("memtester"):
        mb = int(min(2048, max(256, total * 1024 * 0.25)))
        note(f"Free memory:   {read_file('/proc/meminfo').splitlines()[0] if read_file('/proc/meminfo') else ''}")
        if ask(f"Run a memtester pass on {mb} MB now (~1-2 min)? [y/N]"):
            rc, out = run(["memtester", str(mb), "1"], timeout=600)
            tail = "\n".join(out.splitlines()[-6:])
            if rc == 0 and "ok" in out.lower():
                add("RAM", "Memory integrity (memtester)", "PASS",
                    f"{mb} MB pattern test completed without errors")
            else:
                add("RAM", "Memory integrity (memtester)", "FAIL",
                    f"Errors detected - RAM is faulty!\n{tail}")
        else:
            add("RAM", "Memory integrity (memtester)", "SKIP", "Not run")
    else:
        add("RAM", "Memory integrity (memtester)", "SKIP",
            "memtester not installed - use the MEMTEST86+ boot entry for a full test")

    note("For a full pass, reboot into the MemTest86+ entry on this USB and let it run")
    note("at least one complete pass (30-60 min). Red errors = walk away or renegotiate.")


# ============================================================================
# SECTION 4 - Storage
# ============================================================================

def list_disks():
    rc, out = run(["lsblk", "-d", "-n", "-o", "NAME,TYPE,SIZE,TRAN,MODEL"])
    disks = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "disk" and not parts[0].startswith(("loop", "sr", "zram")):
            disks.append({
                "name": parts[0], "size": parts[2],
                "tran": parts[3] if len(parts) > 3 else "",
                "model": " ".join(parts[4:]) if len(parts) > 4 else "",
            })
    return disks


def sec_storage():
    title("4. STORAGE  (the #1 reason used laptops die)")

    disks = list_disks()
    if not disks:
        add("Storage", "Disks detected", "FAIL", "No internal disk found!")
        return

    for d in disks:
        dev = f"/dev/{d['name']}"
        info(f"{c(dev, C_B)}  {d['size']}  {d['tran']}  {d['model']}")

    rc, out = run(["smartctl", "--version"])
    if rc != 0 or "not found" in out:
        add("Storage", "SMART tooling", "SKIP",
            "smartmontools missing - cannot read disk health (install from the bundle)")
    else:
        for d in disks:
            dev = f"/dev/{d['name']}"
            rc, info_out = run(["smartctl", "-i", dev], timeout=30)
            rc2, health = run(["smartctl", "-H", dev], timeout=30)
            rc3, attrs = run(["smartctl", "-A", dev], timeout=30)
            _, dat = run(["smartctl", "-x", dev], timeout=40)

            passfail = re.search(r"SMART overall-health self-assessment test result:\s*(\S+)",
                                 health + info_out)
            verdict = passfail.group(1) if passfail else None

            # Key attributes
            metrics = {}
            for line in attrs.splitlines():
                m = re.match(r"\s*\d+\s+(\S+)\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\S+)", line)
                if m:
                    metrics[m.group(1)] = m.group(2)
            for key, pat in (("Power_On_Hours", r"Power_On_Hours\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("Reallocated_Sector_Ct", r"Reallocated_Sector_Ct\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("Current_Pending_Sector", r"Current_Pending_Sector\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("Offline_Uncorrectable", r"Offline_Uncorrectable\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("Percent_Used", r"Percentage_Used\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("Media_Wearout", r"Media_Wearout_Indicator\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("Wear_Leveling", r"Wear_Leveling_Count\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("UDMA_CRC", r"UDMA_CRC_Error_Count\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)"),
                             ("Temperature", r"Temperature_Celsius\s+0x\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\d+)")):
                m = re.search(pat, attrs)
                if m:
                    metrics[key] = m.group(1)

            # NVMe style fields live elsewhere
            m = re.search(r"Percentage Used:\s+(\d+)%", dat)
            if m and "Percent_Used" not in metrics:
                metrics["Percent_Used"] = m.group(1)
            m = re.search(r"Power On Hours:\s+([\d,]+)", dat)
            if m and "Power_On_Hours" not in metrics:
                metrics["Power_On_Hours"] = m.group(1).replace(",", "")
            m = re.search(r"Data Units Written:\s+([\d,]+)", dat)
            if m:
                metrics["Host_Writes"] = m.group(1).replace(",", "")
            m = re.search(r"Available Spare:\s+(\d+)%", dat)
            if m:
                metrics["Spare"] = m.group(1)
            m = re.search(r"Critical Warning:\s+0x([0-9a-fA-F]+)", dat)
            if m and m.group(1) != "00":
                metrics["Critical_Warning"] = m.group(1)
            m = re.search(r"Rotation Rate:\s+(Solid State Memory|(\d+) rpm)", dat)
            if m:
                metrics["Media"] = "SSD" if m.group(1).startswith("Solid") else f"{m.group(2)} rpm HDD"

            detail = " | ".join(f"{k}={v}" for k, v in metrics.items()) or "no attributes read"
            model = ""
            m = re.search(r"(Device Model|Model Number|Product):\s+(.+)", info_out)
            if m:
                model = m.group(2).strip()

            label = f"{dev} {model}".strip()

            if verdict is None:
                if metrics:
                    add("Storage", f"{label} SMART", "INFO",
                        f"health string unavailable but attributes read: {detail}")
                else:
                    add("Storage", f"{label} SMART", "WARN",
                        "SMART not supported / blocked by BIOS - cannot verify disk health. "
                        "Ask the seller to enable SMART or discount accordingly.")
                continue

            healthy = verdict.upper().startswith("PASSED") or verdict.upper() == "OK"

            realloc = int(metrics.get("Reallocated_Sector_Ct", 0) or 0)
            pending = int(metrics.get("Current_Pending_Sector", 0) or 0)
            uncorr = int(metrics.get("Offline_Uncorrectable", 0) or 0)
            crc = int(metrics.get("UDMA_CRC", 0) or 0)
            hours = int(metrics.get("Power_On_Hours", 0) or 0)
            used = int(metrics.get("Percent_Used", 0) or 0)

            bad = []
            if not healthy:
                bad.append(f"SMART health = {verdict}")
            if realloc > 0:
                bad.append(f"{realloc} reallocated sectors")
            if pending > 0:
                bad.append(f"{pending} pending (unstable) sectors")
            if uncorr > 0:
                bad.append(f"{uncorr} uncorrectable sectors")
            if metrics.get("Critical_Warning"):
                bad.append("NVMe critical warning set")
            if used >= 90:
                bad.append(f"SSD wear {used}%")

            warn = []
            if crc > 0:
                warn.append(f"{crc} UDMA CRC errors (cable/controller issue, often benign)")
            if used >= 75:
                warn.append(f"SSD wear {used}% - nearing end of life")
            if hours > 25000:
                warn.append(f"{hours}h power-on (~{hours/8760:.1f} years) - check price vs wear")
            if metrics.get("Spare") and int(metrics["Spare"]) < 20:
                warn.append(f"spare blocks {metrics['Spare']}% - failing SSD")

            summary = f"{detail}"

            if bad:
                add("Storage", f"{label} - HEALTH", "FAIL",
                    "; ".join(bad) + f"\n{summary}")
            elif warn:
                add("Storage", f"{label} - HEALTH", "WARN",
                    "; ".join(warn) + f"\n{summary}")
            else:
                add("Storage", f"{label} - HEALTH", "PASS",
                    f"SMART {verdict}, {hours}h on, no bad sectors\n{summary}")

        # Filesystem-level read check (catches errors SMART misses)
        print()
        if not AUTO and ask("Run a disk read-speed + integrity scan (~1-3 min)? [y/N]"):
            targets = []
            for d in disks:
                for part in glob.glob(f"/dev/{d['name']}[0-9]*"):
                    targets.append(part)
            if targets:
                p = targets[0]
                note(f"Timing a 512 MB sequential read from {p} ...")
                rc, out = run(["dd", f"if={p}", "of=/dev/null", "bs=1M", "count=512",
                               "iflag=direct"], timeout=300)
                m = re.search(r"([\d.]+) (GB|MB)/s", out)
                spd = " ".join(m.groups()) + "/s" if m else "n/a"
                status = "PASS" if m and float(m.group(1)) > (30 if m.group(2) == "MB" else 0.3) else "WARN"
                if m and m.group(2) == "MB" and float(m.group(1)) < 60:
                    status = "FAIL"
                add("Storage", f"Sequential read speed ({p})", status,
                    f"{spd}\n{first_line(out.splitlines()[-1:]) if out else ''}")
                # Check dmesg for I/O errors during the read
                rc, dout = run(["dmesg", "--level=err,warn"], timeout=20)
                ioerr = [l for l in dout.splitlines()
                         if re.search(r"I/O error|ata\d|nvme.*error|SMART error", l, re.I)]
                if ioerr:
                    add("Storage", "I/O errors during read test", "FAIL",
                        "\n".join(ioerr[:8]))
                else:
                    add("Storage", "I/O errors during read test", "PASS",
                        "No kernel I/O errors logged")
            else:
                add("Storage", "Disk read test", "SKIP", "No partitions to read")
        else:
            add("Storage", "Disk read test", "SKIP", "Not run")

    note("Also check: does the BIOS see the correct disk size? A '1 TB' drive")
    note("reporting 250 GB means a swapped or mislabelled unit.")


# ============================================================================
# SECTION 5 - Battery & power
# ============================================================================

def sec_battery():
    title("5. BATTERY & POWER  (usually the most degraded part)")

    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if not bats:
        add("Battery", "Battery present", "WARN",
            "No battery detected - either removed, dead, or not visible to the OS")
    for b in bats:
        name = os.path.basename(b)
        def rv(f, div=1):
            t = read_file(os.path.join(b, f))
            try:
                return float(t) / div
            except Exception:                                # noqa: BLE001
                return None

        design = rv("energy_full_design", 1e6) or rv("charge_full_design", 1e6)
        full = rv("energy_full", 1e6) or rv("charge_full", 1e6)
        now = rv("energy_now", 1e6) or rv("charge_now", 1e6)
        pct = rv("capacity")
        cycles = read_file(os.path.join(b, "cycle_count"))
        status = read_file(os.path.join(b, "status"))
        vmin = rv("voltage_min_design", 1e6)
        tech = read_file(os.path.join(b, "technology"))
        manu = read_file(os.path.join(b, "manufacturer"))
        model = read_file(os.path.join(b, "model_name"))

        info(f"{c(name, C_B)}  {manu} {model}  {tech}")
        info(f"  design={design:.1f} Wh" if design else "  design=unknown")
        info(f"  full  ={full:.1f} Wh" if full else "  full=unknown")
        info(f"  now   ={now:.1f} Wh ({pct:.0f}%)" if now and pct else f"  now={now}")
        info(f"  cycles={cycles or '?'}   status={status or '?'}")

        if design and full:
            health = full / design * 100
            if health < 60:
                add("Battery", f"{name} health", "FAIL",
                    f"{health:.0f}% of design capacity ({full:.1f}/{design:.1f} Wh) - "
                    f"expect under an hour of runtime; budget a replacement")
            elif health < 80:
                add("Battery", f"{name} health", "WARN",
                    f"{health:.0f}% of design capacity ({full:.1f}/{design:.1f} Wh) - "
                    f"usable but noticeably worn")
            else:
                add("Battery", f"{name} health", "PASS",
                    f"{health:.0f}% of design capacity ({full:.1f}/{design:.1f} Wh)")
        else:
            add("Battery", f"{name} capacity", "INFO",
                "Kernel does not expose design capacity - read the label or the "
                "manufacturer tool (lenovo: upower -i on Lenovo Vantage)")

        if cycles:
            try:
                cy = int(cycles)
                if cy > 800:
                    add("Battery", f"{name} cycle count", "WARN",
                        f"{cy} cycles - deep into its service life")
                else:
                    add("Battery", f"{name} cycle count", "PASS", f"{cy} cycles")
            except Exception:                                # noqa: BLE001
                pass

        # Does the laptop run on AC without the battery / is the charger working?
        if pct is not None and pct < 5 and status.lower() == "discharging" and read_file("/sys/class/power_supply/AC/online") != "1":
            add("Battery", f"{name} charging", "FAIL",
                "Not charging and no AC detected - suspect charger, DC jack or charge circuit")

    # AC adapter
    acs = sorted(glob.glob("/sys/class/power_supply/AC*")) + \
        sorted(glob.glob("/sys/class/power_supply/ADP*"))
    for a in acs:
        online = read_file(os.path.join(a, "online"))
        add("Battery", f"AC adapter ({os.path.basename(a)})",
            "PASS" if online == "1" else "WARN",
            "Plugged in and detected" if online == "1"
            else "AC not detected - test with the charger plugged in! "
                 "A dead DC jack is a common hidden fault")

    # Thermal design power info
    rc, out = run(["upower", "-i"], timeout=20)


# ============================================================================
# SECTION 6 - Display
# ============================================================================

def xrandr_state():
    """Return {output: dict(connected, current, native, modes)}"""
    rc, out = run(["xrandr", "--query"], timeout=20)
    if rc != 0:
        return None
    res = {}
    cur_out = None
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+(connected|disconnected)(.*)$", line)
        if m:
            cur_out = m.group(1)
            res[cur_out] = {
                "connected": m.group(2) == "connected",
                "info": m.group(3).strip(),
                "current": None, "native": None, "modes": [],
            }
            cm = re.search(r"(\d+x\d+)\+(\d+)\+(\d+)", line)
            if cm:
                res[cur_out]["current"] = cm.group(1)
            continue
        if cur_out:
            mm = re.match(r"^\s+(\d+x\d+)\s+([\d.]+)\s*(.*)$", line)
            if mm:
                res[cur_out]["modes"].append((mm.group(1), mm.group(2), mm.group(3)))
                if mm.group(3).strip().startswith("+") or \
                   (res[cur_out]["native"] is None and mm.group(1) == res[cur_out]["current"]):
                    if "+" in mm.group(3) and res[cur_out]["native"] is None:
                        res[cur_out]["native"] = mm.group(1)
    return res


def sec_display():
    title("6. SCREEN  (resolution, pixels, backlight, backlight bleed)")

    st = xrandr_state()
    if not st:
        add("Display", "X server", "FAIL",
            "No X display available - run this from the graphical live session")
        return

    any_connected = False
    for name, o in st.items():
        if not o["connected"]:
            continue
        any_connected = True
        info(f"{c(name, C_B)}: {o['info']}")
        maxmode = max(o["modes"], key=lambda m: int(m[0].split("x")[0]) * int(m[0].split("x")[1]),
                      default=None)
        info(f"  running    : {o['current']}")
        info(f"  preferred  : {o['native']}")
        info(f"  max mode   : {maxmode[0] if maxmode else '?'}")
        info(f"  modes      : {len(o['modes'])}")

        if o["current"] and maxmode:
            cur_px = int(o["current"].split("x")[0]) * int(o["current"].split("x")[1])
            native = o["native"] or maxmode[0]
            nat_px = int(native.split("x")[0]) * int(native.split("x")[1])
            if cur_px < nat_px:
                add("Display", f"{name} resolution", "INFO",
                    f"Running {o['current']}, panel native is {native} - "
                    f"lower than native is fine (scaling / your own setting)")
            else:
                add("Display", f"{name} resolution", "PASS",
                    f"{o['current']} (panel native {native})")

            # The fraud signal is a LOW native resolution / missing EDID:
            # a swapped panel is usually a cheap 1366x768 unit.
            nat_w = int(native.split("x")[0])
            if not o["native"]:
                add("Display", f"{name} EDID", "WARN",
                    "No preferred mode reported - the panel may be a cheap "
                    "replacement or the EDID is missing. Confirm the resolution "
                    "matches the model's spec sheet.")
            elif nat_w < 1366:
                add("Display", f"{name} panel resolution", "WARN",
                    f"Native resolution only {native} - a laptop this size should "
                    f"normally be 1920x1080. Possible downgraded/replaced panel.")
            else:
                add("Display", f"{name} panel resolution", "PASS",
                    f"native {native} is appropriate for this panel size")

        # Physical size helps spot a replaced panel
        m = re.search(r"(\d+)mm x (\d+)mm", o["info"])
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w and h:
                diag = ((w ** 2 + h ** 2) ** 0.5) / 25.4
                add("Display", f"{name} physical size", "PASS",
                    f"{w}x{h} mm = {diag:.1f}\" diagonal")
                if cur := o["current"]:
                    px = int(cur.split("x")[0])
                    ppi = px / (w / 25.4)
                    info(f"  pixel density ~{ppi:.0f} PPI")

    if not any_connected:
        add("Display", "Connected outputs", "FAIL", "No display output reports connected")

    # Backlight control
    bl = sorted(glob.glob("/sys/class/backlight/*"))
    if bl:
        cur = read_file(os.path.join(bl[0], "brightness"))
        mx = read_file(os.path.join(bl[0], "max_brightness"))
        add("Display", "Backlight control", "PASS",
            f"{os.path.basename(bl[0])}: {cur}/{mx} - brightness keys should work")
    else:
        add("Display", "Backlight control", "WARN",
            "No backlight device exposed - brightness keys may not work under Linux")

    # Interactive dead-pixel test
    print()
    note("Dead/stuck pixels, dust under the glass and backlight bleed only show up")
    note("on flat colours. The next step shows full-screen colours.")
    if AUTO:
        add("Display", "Dead pixel test", "SKIP", "Skipped in --auto mode")
        return
    if not ask("Run the full-screen dead-pixel test now? [Y/n]", default="y"):
        add("Display", "Dead pixel test", "SKIP", "Not run by choice")
        return

    w, h = 1920, 1080
    if st:
        for o in st.values():
            if o["current"]:
                try:
                    w, h = [int(x) for x in o["current"].split("x")]
                except Exception:                            # noqa: BLE001
                    pass
                break
    cols = [("black", (0, 0, 0)), ("white", (255, 255, 255)),
            ("red", (255, 0, 0)), ("green", (0, 255, 0)),
            ("blue", (0, 0, 255)), ("grey50", (128, 128, 128))]
    paths = []
    for nm, rgb in cols:
        p = os.path.join(SCRATCH, f"px_{nm}.png")
        write_solid_png(p, w, h, rgb)
        paths.append(p)
    # backlight bleed probe: black with a white border
    bp = os.path.join(SCRATCH, "px_bleed.png")
    write_solid_png(bp, w, h, (0, 0, 0),
                    band=[(255, 255, 255)] * 6 + [(0, 0, 0)] * 40 +
                         [(255, 255, 255)] * 6 + [(0, 0, 0)] * 40)
    paths.append(bp)

    note(f"Generated {len(paths)} full-screen test images at {w}x{h}")

    viewer = None
    for cand in ("feh", "eog", "xdg-open"):
        if has(cand):
            viewer = cand
            break

    shown = False
    if viewer == "feh":
        note("Showing them full-screen. Arrow keys / space = next, q = quit, f = fullscreen.")
        rc, out = run(["feh", "-F", "-Z", "-Y", "--no-menus", "-d", "--auto-zoom"] + paths,
                      timeout=1800, stdin_null=False)
        shown = True
    elif viewer == "eog":
        rc, out = run(["eog", "-f", "-s"] + paths, timeout=1800, stdin_null=False)
        shown = True

    if not shown:
        note("No image viewer bundled. Opening the images folder - inspect each file.")
        note(f"Folder: {SCRATCH}")
        run(["xdg-open", SCRATCH], timeout=20)

    pause("When you have finished looking at the colours, press Enter")

    issues = []
    if ask("Any BLACK/grey dots or lines on the white screen? [y/N]"):
        issues.append("dead pixels (white screen)")
    if ask("Any WHITE or lit dots on the black screen? [y/N]"):
        issues.append("stuck pixels (black screen)")
    if ask("Any coloured (red/green/blue) dots on any colour? [y/N]"):
        issues.append("stuck sub-pixels")
    if ask("Any bright patches / light leaks around the bezel on the black screen? [y/N]"):
        issues.append("backlight bleed")
    if ask("Any scratches, pressure marks, or a bright/dark corner? [y/N]"):
        issues.append("panel surface damage")
    if ask("Any flicker, shimmering or size/colour change when the lid is moved? [y/N]"):
        issues.append("loose display cable (hinge)")

    if issues:
        add("Display", "Dead pixel / panel defects", "FAIL", "; ".join(issues))
    else:
        add("Display", "Dead pixel / panel defects", "PASS",
            "No defects reported across black/white/RGB/grey + bleed pattern")


# ============================================================================
# SECTION 7 - Audio
# ============================================================================

def sec_audio():
    title("7. AUDIO  (speakers, microphone, headphone jack)")

    rc, out = run(["aplay", "-l"], timeout=20)
    cards = re.findall(r"card (\d+): (\S+) \[([^\]]+)\]", out)
    for num, cid, desc in cards:
        info(f"card {num}: {cid} - {desc}")
    if not cards:
        add("Audio", "Sound card", "FAIL",
            "No ALSA playback device found - check BIOS audio setting")
    else:
        add("Audio", "Sound card", "PASS", f"{len(cards)} card(s): "
            + ", ".join(d for _, _, d in cards))

    # Codec identification
    rc, codecs = run(["bash", "-c",
                      "grep -H . /proc/asound/card*/codec* 2>/dev/null | "
                      "grep -m4 -E 'Codec|Vendor Id'"], timeout=20)
    if codecs.strip():
        info("Codec:")
        for l in codecs.splitlines()[:4]:
            info(f"  {l.split(':', 1)[-1].strip() if ':' in l else l}")

    if AUTO:
        add("Audio", "Speaker output test", "SKIP", "Skipped in --auto mode")
        add("Audio", "Microphone test", "SKIP", "Skipped in --auto mode")
        return

    print()
    if has("speaker-test") and ask("Play a test tone through the SPEAKERS now? [Y/n]", "y"):
        note("Listen for: clean tone from both sides, no crackle, no buzzing, both L+R")
        rc, out = run(["speaker-test", "-t", "sine", "-f", "440", "-l", "1", "-c", "2",
                       "-p", "2"], timeout=90, stdin_null=False)
        res = []
        if ask("Did you hear the tone from the LEFT speaker? [y/N]"):
            res.append("L ok")
        else:
            res.append("L FAIL")
        if ask("Did you hear the tone from the RIGHT speaker? [y/N]"):
            res.append("R ok")
        else:
            res.append("R FAIL")
        if ask("Any crackling / buzzing / distortion? [y/N]"):
            res.append("distortion")
        bad = [r for r in res if "FAIL" in r or r == "distortion"]
        add("Audio", "Speaker output test", "FAIL" if bad else "PASS",
            ", ".join(res) + (" - blown speaker or bad amp" if bad else ""))
    else:
        add("Audio", "Speaker output test", "SKIP", "Not run")

    # Bass/loudness check (blown drivers often only rattle at volume)
    if not AUTO and ask("Play a BASS tone at full volume to check for rattle? [y/N]"):
        run(["bash", "-c",
             "for f in 60 80 120; do speaker-test -t sine -f $f -l 1 -c 2 >/dev/null 2>&1; done"],
            timeout=90, stdin_null=False)
        if ask("Rattling / buzzing at high volume? [y/N]"):
            add("Audio", "Speaker at high volume", "FAIL",
                "Rattle/buzz - blown or loose driver cone")
        else:
            add("Audio", "Speaker at high volume", "PASS", "Clean at high volume")
        if ask("Is left and right volume roughly equal? (y = equal) [y/N]", "y"):
            add("Audio", "Channel balance", "PASS", "Balanced")
        else:
            add("Audio", "Channel balance", "WARN",
                "Uneven channels - one driver may be damaged")

    # Microphone
    print()
    if ask("Test the MICROPHONE now (5s record + playback)? [Y/n]", "y"):
        wav = os.path.join(SCRATCH, "mic.wav")
        note("Recording 5 seconds... SPEAK into the laptop now")
        rc, out = run(["arecord", "-d", "5", "-f", "S16_LE", "-r", "44100",
                       "-c", "1", wav], timeout=40)
        if rc != 0 or not os.path.exists(wav) or os.path.getsize(wav) < 1000:
            add("Audio", "Microphone", "FAIL",
                f"Recording failed: {first_line(out)}\n"
                "Check the mic is not muted: alsamixer -> F4 capture")
        else:
            note("Playing back what was recorded...")
            run(["aplay", wav], timeout=30)
            if ask("Did you hear your own voice? [y/N]"):
                add("Audio", "Microphone", "PASS", "Recorded and played back your voice")
                ARTIFACTS.append(wav)
            else:
                if ask("Was there ANY noise in the recording? [y/N]"):
                    add("Audio", "Microphone", "WARN",
                        "Signal present but unrecognisable - gain/mute issue, "
                        "try alsamixer capture level")
                else:
                    add("Audio", "Microphone", "FAIL", "Silent recording - dead mic")
    else:
        add("Audio", "Microphone", "SKIP", "Not run")

    # Headphone jack detection (hardware jack sense)
    print()
    note("Plug in a pair of headphones now.")
    pause("Plug the headphones in, then press Enter")
    rc, out = run(["bash", "-c",
                   "cat /proc/asound/card*/codec* 2>/dev/null | grep -iE "
                   "'Pin-ctls|jack' | head -20; amixer -c0 contents 2>/dev/null | "
                   "grep -i -A2 'Headphone' | head -20"], timeout=20)
    if ask("Did the headphones produce sound (nothing needed from software)? [y/N]"):
        add("Audio", "Headphone jack", "PASS", "Audio routed to the jack")
    else:
        add("Audio", "Headphone jack", "WARN",
            "No sound on the jack - reposition the plug and retry; a broken jack "
            "is common on used laptops. Software may need the output profile switched.")


# ============================================================================
# SECTION 8 - Keyboard
# ============================================================================

KEYMAP = {
    1: "ESC", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8",
    10: "9", 11: "0", 12: "-", 13: "=", 14: "BKSP", 15: "TAB", 16: "Q", 17: "W",
    18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I", 24: "O", 25: "P",
    26: "[", 27: "]", 28: "ENTER", 29: "LCTRL", 30: "A", 31: "S", 32: "D",
    33: "F", 34: "G", 35: "H", 36: "J", 37: "K", 38: "L", 39: ";", 40: "'",
    41: "`", 42: "LSHIFT", 43: "\\", 44: "Z", 45: "X", 46: "C", 47: "V",
    48: "B", 49: "N", 50: "M", 51: ",", 52: ".", 53: "/", 54: "RSHIFT",
    55: "KP*", 56: "LALT", 57: "SPACE", 58: "CAPS", 59: "F1", 60: "F2",
    61: "F3", 62: "F4", 63: "F5", 64: "F6", 65: "F7", 66: "F8", 67: "F9",
    68: "F10", 69: "NUMLK", 70: "SCRLK", 71: "KP7", 72: "KP8", 73: "KP9",
    74: "KP-", 75: "KP4", 76: "KP5", 77: "KP6", 78: "KP+", 79: "KP1",
    80: "KP2", 81: "KP3", 82: "KP0", 83: "KP.", 87: "F11", 88: "F12",
    96: "KPENTER", 97: "RCTRL", 98: "KP/", 99: "SYSRQ", 100: "RALT",
    102: "HOME", 103: "UP", 104: "PGUP", 105: "LEFT", 106: "RIGHT",
    107: "END", 108: "DOWN", 109: "PGDN", 110: "INSERT", 111: "DELETE",
    113: "MUTE", 114: "VOLDN", 115: "VOLUP", 116: "POWER", 119: "PAUSE",
    121: "KPCMMA", 125: "LMETA", 126: "RMETA", 127: "MENU", 138: "HELP",
    152: "COFFEE", 158: "BACK", 159: "FORWARD", 163: "NEXT", 164: "PLAY",
    165: "PREV", 166: "STOPCD", 172: "HOMEPG", 173: "REFRESH", 174: "EXIT",
    183: "F13", 184: "F14", 185: "F15", 186: "F16", 187: "F17", 188: "F18",
    189: "F19", 190: "F20", 191: "F21", 192: "F22", 193: "F23", 194: "F24",
    217: "SEARCH", 240: "UNKNOWN",
}
MOUSEBTN = {
    272: "MOUSE-LEFT", 273: "MOUSE-RIGHT", 274: "MOUSE-MID",
    275: "MOUSE-SIDE1", 276: "MOUSE-SIDE2",
    277: "TABLET-TIP", 320: "TOUCHPAD-TAP",
}
EV_KEY, EV_REL, EV_ABS, EV_SYN, EV_MSC = 1, 2, 3, 0, 4


def input_devices():
    """Return [(path, name, capabilities)] for readable input devices."""
    devs = []
    for path in sorted(glob.glob("/dev/input/event*")):
        name = read_file(f"/sys/class/input/{os.path.basename(path)}/device/name")
        caps = read_file(f"/sys/class/input/{os.path.basename(path)}/device/capabilities/key")
        devs.append((path, name or "?", caps))
    return devs


def sec_keyboard():
    title("8. KEYBOARD  (every single key, dead keys are invisible otherwise)")

    devs = input_devices()
    kb = [d for d in devs if "keyboard" in d[1].lower() or
          (d[2] and "kbd" in d[1].lower())]
    if not devs:
        add("Input", "Input devices", "FAIL", "No /dev/input/event* - run as root!")
        return
    for path, name, _ in devs:
        info(f"{os.path.basename(path):<12} {name}")
    add("Input", "Input devices detected", "PASS",
        f"{len(devs)} event device(s); keyboards: "
        + (", ".join(n for _, n, _ in kb) or "none named 'keyboard'"))

    if AUTO or not ask("Run the interactive KEYBOARD test? [Y/n]", "y"):
        add("Input", "Keyboard key test", "SKIP", "Not run")
        return

    print()
    note("Press EVERY key on the keyboard now, one at a time.")
    note("Include Fn/F1-F12, arrows, Enter, Backspace, Del, volume keys, the lot.")
    note("Press ESC three times (or take 90 s of silence) to finish.")
    print()

    fds = []
    import fcntl
    for path, name, _ in devs:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            os.set_blocking(fd, False)
            fds.append((fd, os.path.basename(path), name))
        except Exception:                                    # noqa: BLE001
            continue

    EV_FMT = "llHHi"
    EV_SIZE = struct.calcsize(EV_FMT)
    pressed = {}
    touches = {"moves": 0, "clicks": 0}
    last_activity = time.time()
    start = time.time()
    limit = 600
    esc_count = 0

    try:
        while time.time() - start < limit:
            got = False
            for fd, base, name in fds:
                try:
                    data = os.read(fd, EV_SIZE * 64)
                except BlockingIOError:
                    continue
                except OSError:
                    continue
                if not data:
                    continue
                for off in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
                    _, _, etype, code, value = struct.unpack(
                        EV_FMT, data[off:off + EV_SIZE])
                    got = True
                    if etype == EV_KEY and value == 1:
                        last_activity = time.time()
                        if code in KEYMAP:
                            k = KEYMAP[code]
                            if k not in pressed:
                                pressed[k] = name
                                print(c(f"  + {k:<10}", C_GRN) +
                                      c(f" ({name})", C_DIM))
                                if k == "ESC":
                                    esc_count += 1
                        elif code in MOUSEBTN:
                            k = MOUSEBTN[code]
                            if k not in pressed:
                                pressed[k] = name
                                touches["clicks"] += 1
                                print(c(f"  + {k:<10}", C_CYA) +
                                      c(f" ({name})", C_DIM))
                    elif etype in (EV_REL, EV_ABS):
                        touches["moves"] += 1
            if esc_count >= 3:
                break
            if time.time() - last_activity > 90:
                note("90 s of inactivity - finishing the key test.")
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        for fd, _, _ in fds:
            try:
                os.close(fd)
            except Exception:                                # noqa: BLE001
                pass

    # The "expected" set for a standard laptop keyboard
    expected = set()
    for k in ("ESC BKSP TAB ENTER LCTRL A S D F G H J K L ; ' ` LSHIFT \\ Z X C "
              "V B N M , . / RSHIFT LALT SPACE CAPS 1 2 3 4 5 6 7 8 9 0 - = [ ] "
              "F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F12 UP DOWN LEFT RIGHT HOME END "
              "PGUP PGDN INSERT DELETE").split():
        expected.add(k)

    missing = sorted(expected - set(pressed))
    print()
    info(f"Keys registered : {c(len(pressed), C_B)}")
    info(f"Pointer events  : {touches['moves']} motion, {touches['clicks']} buttons")
    if missing:
        info(f"Not pressed    : {c(' '.join(missing), C_YEL)}")

    if missing:
        add("Input", "Keyboard key test", "WARN",
            f"{len(pressed)} keys registered; not registered/wrong: {' '.join(missing)}\n"
            "(keys you simply did not press also show here - re-run if unsure)")
    else:
        add("Input", "Keyboard key test", "PASS",
            f"All {len(pressed)} keys responded")

    # Fn / F-row behaviour
    if not AUTO:
        print()
        note("Now test the Fn row: hold Fn and press F1..F12 (brightness, volume,")
        note("airplane mode, keyboard backlight, screen off).")
        pause("Do the Fn combinations, then press Enter")
        if ask("Do brightness/mute/volume Fn keys work (with on-screen feedback)? [y/N]"):
            add("Input", "Fn / hotkeys", "PASS", "Function keys respond")
        else:
            add("Input", "Fn / hotkeys", "WARN",
                "Some Fn keys did not respond - often just missing Linux drivers, "
                "but test them in the BIOS or Windows before deciding")

    # Touchpad confirmation from the same capture
    if touches["moves"] > 50:
        add("Input", "Touchpad motion", "PASS",
            f"{touches['moves']} pointer events captured")
    elif touches["moves"] > 0:
        add("Input", "Touchpad motion", "WARN",
            f"Only {touches['moves']} pointer events - barely moved")
    else:
        add("Input", "Touchpad motion", "WARN", "No pointer movement captured")

    if touches["clicks"] >= 1:
        add("Input", "Buttons / clicks", "PASS",
            f"{touches['clicks']} button presses captured")
    else:
        add("Input", "Buttons / clicks", "WARN",
            "No button presses captured - click the touchpad buttons")


# ============================================================================
# SECTION 9 - Touchpad / trackpoint dedicated test
# ============================================================================

def sec_pointer():
    title("9. TOUCHPAD / TRACKPOINT  (precision, edges, gestures)")

    rc, out = run(["bash", "-c", "grep -iE 'Name|Handlers' /proc/bus/input/devices | "
                                 "paste - - | grep -iE 'touch|track|synaptics|elan|alps|mouse'"],
                  timeout=20)
    if out.strip():
        for l in out.splitlines()[:6]:
            info(l.strip())
    pointers = [d for d in input_devices()
                if any(w in d[1].lower() for w in ("touchpad", "track", "mouse", "elan", "synaptic"))]
    if pointers:
        add("Pointer", "Pointing devices", "PASS",
            ", ".join(n for _, n, _ in pointers))
    else:
        add("Pointer", "Pointing devices", "WARN",
            "No touchpad/trackpoint named device found - check BIOS or driver")

    if AUTO or not ask("Run the touchpad edge + gesture test? [Y/n]", "y"):
        add("Pointer", "Touchpad full-area test", "SKIP", "Not run")
        return

    print()
    note("Follow these steps with the touchpad (not a USB mouse):")
    steps = [
        "Move the cursor to all FOUR corners of the screen - does it reach without lifting twice?",
        "Drag your finger slowly along the very top edge - smooth, or does it jump?",
        "Draw slow spirals in the centre - is tracking smooth and free of dead zones?",
        "Press the touchpad down at the bottom-left (left click) - does it click?",
        "Press at the bottom-right (right click) - does it click?",
        "Do a two-finger scroll up and down in a text window - does it scroll?",
        "Do a two-finger tap - does it right-click?",
        "Pinch with two fingers - does it zoom (browser)?",
        "Three-finger swipe up - does it show window overview?",
        "If there is a TrackPoint: push it in all 8 directions and check the three buttons.",
    ]
    for s in steps:
        if ask(s + " OK? [Y/n]", "y"):
            pass
        else:
            add("Pointer", "Touchpad gesture/area", "WARN",
                f"Failed/unsure: {s}")
    if not any(r["section"] == "Pointer" and r["status"] == "WARN" for r in RESULTS):
        add("Pointer", "Touchpad full-area test", "PASS",
            "All area, button and gesture checks passed")


# ============================================================================
# SECTION 10 - Webcam
# ============================================================================

def yuyv_to_png(raw, width, height, path):
    """Convert a YUYV422 frame to RGB PNG in pure python."""
    rgb = bytearray()
    rows = []
    for y in range(height):
        row = raw[y * width * 2:(y + 1) * width * 2]
        if len(row) < width * 2:
            break
        out = bytearray()
        for x in range(0, width * 2 - 3, 4):
            y0, u, y1, v = row[x], row[x + 1], row[x + 2], row[x + 3]
            for yv in (y0, y1):
                c = yv - 16
                d = u - 128
                e = v - 128
                r = (298 * c + 409 * e + 128) >> 8
                g = (298 * c - 100 * d - 208 * e + 128) >> 8
                b = (298 * c + 516 * d + 128) >> 8
                out += bytes((max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))))
        rows.append(b"\x00" + bytes(out))
    if len(rows) != height:
        height = len(rows)
    raw_rows = b"".join(rows)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body +
                struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw_rows, 1)) +
           chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    return path


def sec_camera():
    title("10. WEBCAM  (and the privacy shutter)")

    vids = sorted(glob.glob("/dev/video*"))
    rc, lsusb_out = run(["lsusb"])
    cam_usb = [l for l in lsusb_out.splitlines()
               if re.search(r"cam|webcam|imaging|HD Web|Integrated", l, re.I)]

    if not vids:
        add("Camera", "Webcam device", "FAIL",
            "No /dev/video* - the webcam is missing, disabled in BIOS, or unplugged "
            "(check the internal ribbon cable!)")
        return
    add("Camera", "Webcam device", "PASS",
        f"{len(vids)} video device(s): {', '.join(os.path.basename(v) for v in vids)}")
    for l in cam_usb[:4]:
        info(l.strip())

    if has("v4l2-ctl"):
        rc, out = run(["v4l2-ctl", "--list-devices"], timeout=20)
        for l in out.splitlines()[:10]:
            if l.strip():
                info(l.strip())
        rc, out = run(["v4l2-ctl", "-d", vids[0], "--list-formats-ext"], timeout=25)
        formats = re.findall(r"\[\d+\]: '(\w+)'", out)
        resos = re.findall(r"Size: Discrete (\d+x\d+)", out)
        if formats:
            add("Camera", "Supported formats", "PASS",
                f"{', '.join(sorted(set(formats)))}; max {max(resos, key=lambda r: int(r.split('x')[0])) if resos else '?'}")
        # Try an actual capture
        note("Capturing a frame...")
        prev_w, prev_h = 640, 480
        m = re.search(r"Size: Discrete (\d+)x(\d+)", out)
        if m:
            prev_w, prev_h = int(m.group(1)), int(m.group(2))
        raw = os.path.join(SCRATCH, "cam.raw")
        cmd = ["v4l2-ctl", "-d", vids[0],
               f"--set-fmt-video=width={prev_w},height={prev_h},pixelformat=YUYV",
               "--stream-mmap", "--stream-count=1"]
        try:
            with open(raw, "wb") as fh:
                p = subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL,
                                   timeout=25)
            data = open(raw, "rb").read()
        except Exception as e:                               # noqa: BLE001
            data = b""
        if len(data) >= prev_w * prev_h * 2 // 2:
            png = yuyv_to_png(data, prev_w, prev_h, os.path.join(SCRATCH, "camera.png"))
            ARTIFACTS.append(png)
            add("Camera", "Frame capture", "PASS",
                f"Captured {len(data) // 1024} KB at {prev_w}x{prev_h} "
                f"(image saved to the report)")
            if not AUTO and has("xdg-open"):
                if ask("Look at the captured image on screen now? [Y/n]", "y"):
                    run(["xdg-open", png], timeout=60)
            if not AUTO:
                if ask("Was the image clear (not black, not green, not frozen)? [y/N]"):
                    add("Camera", "Image quality", "PASS", "Visible, clear image")
                else:
                    add("Camera", "Image quality", "FAIL",
                        "Black/garbled image - lens covered, cable loose or sensor dead")
        else:
            add("Camera", "Frame capture", "WARN",
                "Could not stream a frame with the bundled tools - test with "
                "the Cheese / guvcview app in the live session instead")
    else:
        add("Camera", "Webcam tooling", "SKIP", "v4l-utils not installed")

    note("Physically inspect the lens: scratches, dust, or a broken shutter.")


# ============================================================================
# SECTION 11 - Networking
# ============================================================================

def sec_network():
    title("11. NETWORKING  (wifi, ethernet, bluetooth)")

    rc, out = run(["lspci"])
    wlan = [l.split(":")[-1].strip() for l in out.splitlines()
            if re.search(r"network controller|wireless", l, re.I)]
    eth = [l.split(":")[-1].strip() for l in out.splitlines()
           if re.search(r"ethernet controller", l, re.I)]
    if wlan:
        add("Network", "WiFi adapter (hardware)", "PASS", wlan[0])
    else:
        add("Network", "WiFi adapter (hardware)", "FAIL",
            "No wireless controller on the PCI bus - check the BIOS/M.2 slot")
    if eth:
        add("Network", "Ethernet adapter (hardware)", "PASS", eth[0])
    else:
        add("Network", "Ethernet adapter (hardware)", "WARN",
            "No ethernet controller (fine on ultrabooks, wrong on business laptops)")

    # rfkill state
    rc, out = run(["rfkill", "list"], timeout=15)
    if out.strip():
        blocked = [l for l in out.splitlines() if "blocked: yes" in l]
        if blocked:
            add("Network", "Radio kill switches", "WARN",
                "Hard/soft blocked: " + "; ".join(b.strip() for b in blocked))
        else:
            add("Network", "Radio kill switches", "PASS", "All radios unblocked")
    else:
        run(["bash", "-c", "for f in /sys/class/rfkill/*/soft; do echo -n \"$f=\"; cat $f; done"],
            timeout=10)

    # Live wifi scan - the real proof the radio + antenna work
    ifaces = [os.path.basename(p) for p in glob.glob("/sys/class/net/*")
              if os.path.islink(p) and "wl" in os.path.basename(p)]
    if not ifaces:
        ifaces = [os.path.basename(p) for p in glob.glob("/sys/class/net/*")
                  if os.path.basename(p).startswith(("wlan", "wlp"))]
    if ifaces:
        iface = ifaces[0]
        info(f"Interface: {iface}")
        run(["ip", "link", "set", iface, "up"], timeout=15)
        time.sleep(2)
        found = []
        if has("iw"):
            rc, out = run(["iw", "dev", iface, "scan"], timeout=45)
            found = re.findall(r"SSID: (.+)", out)
            bands = set(re.findall(r"^\s*(\d{4}) MHz", out, re.M))
            if found:
                add("Network", "WiFi scan", "PASS",
                    f"Detected {len(found)} networks "
                    f"(bands: {', '.join(sorted(bands)) if bands else '?'} MHz)\n"
                    f"Strongest: " + ", ".join(found[:5]))
            else:
                hint = (" (needs root - the launcher runs it as root)"
                        if os.geteuid() != 0 else "")
                add("Network", "WiFi scan", "WARN",
                    f"iw scan returned nothing: {first_line(out)}{hint}\n"
                    "Try: unblock rfkill, move next to a router, or use the "
                    "Network panel in the live session")
        else:
            add("Network", "WiFi scan", "SKIP", "iw not installed")
    else:
        add("Network", "WiFi interface", "WARN",
            "No wireless interface appeared - driver may need firmware. "
            "Test the wifi in the live session's network menu.")

    # Ethernet link
    eths = [i for i in (os.path.basename(p) for p in glob.glob("/sys/class/net/*"))
            if i.startswith(("eth", "enp", "eno", "enx"))]
    for e in eths:
        carrier = read_file(f"/sys/class/net/{e}/carrier")
        speed = read_file(f"/sys/class/net/{e}/speed")
        if carrier == "1":
            add("Network", f"Ethernet link ({e})", "PASS",
                f"cable detected, link speed {speed or '?'} Mb/s")
        else:
            add("Network", f"Ethernet link ({e})", "INFO",
                "No cable plugged in - plug one in to verify the port works")

    # Bluetooth
    rc, out = run(["bash", "-c", "lsusb | grep -i bluetooth; ls /sys/class/bluetooth 2>/dev/null"],
                  timeout=20)
    if out.strip() and ("hci" in out or "bluetooth" in out.lower()):
        add("Network", "Bluetooth adapter", "PASS",
            first_line(out) if "Bluetooth" in out else f"adapters: {out.strip()}")
    else:
        add("Network", "Bluetooth adapter", "WARN",
            "No Bluetooth adapter detected - verify in the BIOS/UEFI")


# ============================================================================
# SECTION 12 - USB ports
# ============================================================================

def sec_usb():
    title("12. USB PORTS  (each port, data + power)")

    rc, out = run(["lsusb"])
    lines = [l for l in out.splitlines() if l.strip()]
    add("USB", "Devices enumerated", "PASS",
        f"{len(lines)} USB device(s) visible on the bus")
    for l in lines[:12]:
        info(l.strip())

    rc, out = run(["bash", "-c",
                   "for d in /sys/bus/usb/devices/usb*/; do "
                   "echo \"$(basename $d) $(cat $d/speed 2>/dev/null) "
                   "$(cat $d/product 2>/dev/null)\"; done"], timeout=20)
    roots = [l for l in out.splitlines() if l.strip()]
    info("Root hubs:")
    for r in roots:
        info(f"  {r}")

    note("Test EVERY physical USB port on the laptop, one at a time:")
    note("  * plug the USB stick (or a mouse) into the port")
    note("  * does the device appear and work?")
    note("  * does a device that needs power (external HDD / phone) charge?")
    if AUTO:
        add("USB", "Physical port test", "SKIP", "Needs a human with the stick")
        note("A wobbling / loose port is a very common used-laptop fault - test by")
        note("gently wiggling a plugged device and watching for disconnects.")
        return

    # Live monitor: watch for connect/disconnect while the user plugs things in
    print()
    if ask("Watch USB events while you plug into every port (~60 s)? [Y/n]", "y"):
        note("Plug a device into each port now. Disconnects while idle = bad port.")
        proc = None
        try:
            proc = subprocess.Popen(["udevadm", "monitor", "--udev"],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    universal_newlines=True)
        except Exception:                                    # noqa: BLE001
            proc = None
        events = []
        if proc:
            import select
            t0 = time.time()
            while time.time() - t0 < 60:
                r, _, _ = select.select([proc.stdout], [], [], 0.5)
                if r:
                    line = proc.stdout.readline()
                    if line:
                        events.append(line.strip())
                        if "add" in line:
                            print(c(f"  + connected: {line.strip()[:100]}", C_GRN))
                        elif "remove" in line:
                            print(c(f"  - removed:   {line.strip()[:100]}", C_YEL))
            proc.terminate()
        removals = [e for e in events if " remove " in e]
        adds = [e for e in events if " add " in e]
        add("USB", "Port plug/unplug events", "PASS",
            f"{len(adds)} connect, {len(removals)} disconnect events captured")
        # Look for ports that drop when untouched (bad connector / power)
        rc, dout = run(["dmesg", "--level=err,warn"], timeout=20)
        usb_err = [l for l in dout.splitlines()
                   if re.search(r"usb.*(error|reset|over-?current|disconnect)", l, re.I)]
        if usb_err:
            add("USB", "USB errors in kernel log", "WARN",
                "\n".join(usb_err[:6]) +
                "\nOver-current warnings mean a damaged port - do not ignore")
        else:
            add("USB", "USB errors in kernel log", "PASS", "No USB errors logged")
    else:
        add("USB", "Physical port test", "SKIP", "Not run")

    # USB write speed test on the stick itself
    if not AUTO and ask("Run a USB write/read speed test on this stick? [y/N]"):
        if KIT_ROOT and os.access(KIT_ROOT, os.W_OK):
            tf = os.path.join(KIT_ROOT, ".speedtest.bin")
            rc, out = run(["dd", "if=/dev/zero", f"of={tf}", "bs=1M", "count=256",
                           "conv=fsync"], timeout=300)
            m = re.search(r"([\d.]+) (GB|MB)/s", out)
            wspd = " ".join(m.groups()) + "/s" if m else "n/a"
            run(["bash", "-c", f"sync; echo 3 > /proc/sys/vm/drop_caches"])
            rc, out = run(["dd", f"if={tf}", "of=/dev/null", "bs=1M"], timeout=300)
            m2 = re.search(r"([\d.]+) (GB|MB)/s", out)
            rspd = " ".join(m2.groups()) + "/s" if m2 else "n/a"
            try:
                os.remove(tf)
            except Exception:                                # noqa: BLE001
                pass
            add("USB", "Stick write / read speed", "PASS", f"write {wspd}, read {rspd}")
        else:
            add("USB", "Stick write / read speed", "SKIP", "Stick is read-only")

    # Card reader present?
    rc, out = run(["lspci"])
    if re.search(r"card reader|SD Host|Realtek.*Card", out, re.I):
        add("USB", "Card reader", "INFO",
            "Card reader present - test with an SD card if you plan to use one")


# ============================================================================
# SECTION 13 - Sensors, thermal, chassis
# ============================================================================

def sec_sensors():
    title("13. SENSORS, FANS & CHASSIS")

    if has("sensors"):
        rc, out = run(["sensors"], timeout=20)
        info(out.strip()[:1800] if out.strip() else "sensors returned nothing")
        temps = parse_sensor_temps(out)
        if temps:
            add("Sensors", "Sensor readout", "PASS",
                f"{len(temps)} temperature readings, hottest {max(temps):.0f} C "
                f"(idle) - lm-sensors works")
        else:
            add("Sensors", "Sensor readout", "WARN",
                "sensors ran but produced no temperatures - try 'sensors-detect'")
    else:
        add("Sensors", "Sensor readout", "SKIP", "lm-sensors not installed")

    # ACPI thermal zones
    zones = glob.glob("/sys/class/thermal/thermal_zone*")
    info(f"ACPI thermal zones: {len(zones)}")
    for z in zones[:8]:
        t = read_file(os.path.join(z, "temp"))
        typ = read_file(os.path.join(z, "type"))
        try:
            info(f"  {typ or os.path.basename(z)}: {int(t)/1000:.0f} C")
        except Exception:                                    # noqa: BLE001
            pass

    # Fan presence
    fans = fan_rpms()
    if fans:
        add("Sensors", "Fan tachometer", "PASS",
            ", ".join(f"{k}={v} rpm" for k, v in fans.items()))
    else:
        add("Sensors", "Fan tachometer", "INFO",
            "No fan RPM exposed - verify by ear and by the thermal test above")

    # Chassis inspection checklist
    print()
    note("Now look at the machine itself (2 minutes, worth it):")
    checks = [
        "Is the chassis straight? Lay it on a table - does it rock or does the lid warp?",
        "Any cracks near the hinges? Open and close the lid fully 5 times - any creak/snap?",
        "Any screws missing on the bottom cover (sign of a previous repair)?",
        "Any sticky residue, cigarette smell, or liquid marks on the keyboard?",
        "Are the rubber feet all present and not hardened?",
        "Does the lid stay in position when half-open (hinge stiffness)?",
        "Any deep scratches on the screen bezel or body?",
        "Does the bottom cover sit flush (battery bulge = swollen battery)?",
        "Is the battery swollen? (check for a bulge - swollen Li-ion is a fire risk)",
    ]
    if not AUTO:
        for q in checks:
            if not ask(q + " OK? [Y/n]", "y"):
                add("Chassis", "Physical inspection", "WARN", f"Issue: {q}")
    if not any(r["section"] == "Chassis" for r in RESULTS):
        add("Chassis", "Physical inspection", "PASS",
            "All physical checks looked clean" if not AUTO
            else "Manual inspection recommended")

    # Swollen battery is a hard fail
    if not AUTO and ask("Is there ANY sign of a swollen battery? [y/N]"):
        add("Chassis", "Battery swelling", "FAIL",
            "SWOLLEN BATTERY - fire hazard, do not buy or replace it immediately")


# ============================================================================
# SECTION 14 - Extra hardware
# ============================================================================

def sec_extras():
    title("14. OTHER HARDWARE  (fingerprint, card reader, TPM, ports)")

    rc, pci = run(["lspci"])
    rc, usb = run(["lsusb"])

    def probe(label, pattern, source, good, bad, warn=False):
        if re.search(pattern, source, re.I):
            add("Extras", label, "PASS" if good else "WARN", good or bad)
        else:
            add("Extras", label, "INFO" if warn else "INFO", bad)

    probe("Fingerprint reader",
          r"fingerprint|validity|Synaptics.*FP|Goodix",
          pci + usb,
          "Fingerprint sensor detected",
          "No fingerprint reader (check the spec sheet if advertised)")

    probe("TPM / security chip",
          r"TPM|Trusted Platform", pci + usb,
          "TPM present (needed for Windows 11 / BitLocker)",
          "No discrete TPM listed (may still be firmware TPM - check the BIOS)")

    # TPM via sysfs
    if glob.glob("/sys/class/tpm/tpm*"):
        add("Extras", "TPM (sysfs)", "PASS",
            f"{len(glob.glob('/sys/class/tpm/tpm*'))} TPM device(s)")

    probe("Card reader", r"card reader|SD Host|Ricoh|Realtek.*Card", pci,
          "Card reader present", "No card reader")

    probe("HDMI / display output",
          r"HDMI|DisplayPort|VGA", run(["bash", "-c", "xrandr --query"])[1],
          "External display connector(s) detected",
          "No external display output detected by xrandr")

    # Thunderbolt / USB-C
    if glob.glob("/sys/bus/thunderbolt/devices/*"):
        add("Extras", "Thunderbolt", "PASS",
            f"{len(glob.glob('/sys/bus/thunderbolt/devices/*'))} device(s)")
    elif re.search(r"USB4|Thunderbolt", pci, re.I):
        add("Extras", "Thunderbolt", "PASS", "Controller present on the PCI bus")

    # Speakers / audio codec already covered; check for a DVD drive
    if glob.glob("/dev/sr*"):
        add("Extras", "Optical drive", "PASS", "DVD/CD drive present")

    # Webcam privacy shutter, mic array holes - visual only
    if not AUTO:
        print()
        note("Test these by hand/eye if they matter to you:")
        if ask("Are there any dead/silent keys or missing keycaps? (y = all fine) [y/N]", "y"):
            add("Extras", "Keycaps complete", "PASS", "No missing keycaps")
        else:
            add("Extras", "Keycaps complete", "WARN", "Missing/damaged keycaps")
        if ask("Does the microphone grill look intact and unblocked? (y = yes) [y/N]", "y"):
            add("Extras", "Mic grill", "PASS", "Mic grill intact")
        else:
            add("Extras", "Mic grill", "WARN", "Mic grill damaged/blocked")


# ============================================================================
# REPORT
# ============================================================================

def verdict_counts():
    cts = {}
    for r in RESULTS:
        cts[r["status"]] = cts.get(r["status"], 0) + 1
    return cts


def overall_verdict():
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    warns = [r for r in RESULTS if r["status"] == "WARN"]
    if fails:
        return ("DO NOT BUY / RENEGOTIATE", "FAIL", fails, warns)
    if len(warns) >= 4:
        return ("BUY WITH CAUTION", "WARN", fails, warns)
    if warns:
        return ("ACCEPTABLE - minor issues", "WARN", fails, warns)
    return ("GOOD - no hardware faults found", "PASS", fails, warns)


def render_txt():
    lines = []
    app = f"{sys_vendor()} {product_name()}".strip()
    lines.append("=" * 78)
    lines.append("  USED LAPTOP HARDWARE REPORT - HWCheck v" + VERSION)
    lines.append("=" * 78)
    lines.append(f"  Machine     : {app}")
    lines.append(f"  Serial      : {dmi('product_serial')}")
    lines.append(f"  BIOS        : {dmi('bios_version')}  ({dmi('bios_date')})")
    lines.append(f"  CPU         : {cpu_model()}")
    lines.append(f"  RAM         : {mem_total_gb():.2f} GiB")
    lines.append(f"  Tested at   : {datetime.datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"  Kernel      : {platform.release()}")
    lines.append("")
    v, cls, fails, warns = overall_verdict()
    lines.append(f"  VERDICT: {v}")
    cts = verdict_counts()
    lines.append("  Counts : " + ", ".join(f"{k}={v_}" for k, v_ in sorted(cts.items())))
    lines.append("")

    section = None
    for r in RESULTS:
        if r["section"] != section:
            section = r["section"]
            lines.append("")
            lines.append("-" * 78)
            lines.append(f"  {section.upper()}")
            lines.append("-" * 78)
        lines.append(f"  [{r['status']:<4}] {r['name']}")
        for ln in r["detail"].splitlines():
            lines.append(f"         {ln}")

    lines.append("")
    lines.append("=" * 78)
    lines.append("  DECISION GUIDE")
    lines.append("=" * 78)
    if fails:
        lines.append("  Hard failures found - these are physical faults you cannot")
        lines.append("  fix by reinstalling software:")
        for f in fails:
            lines.append(f"    * {f['section']}: {f['name']}")
            if f["detail"]:
                lines.append(f"      {f['detail'].splitlines()[0]}")
    else:
        lines.append("  No hard failures detected.")
    if warns:
        lines.append("")
        lines.append("  Warnings / things to renegotiate on:")
        for w in warns:
            lines.append(f"    * {w['section']}: {w['name']} - {w['detail'].splitlines()[0] if w['detail'] else ''}")
    lines.append("")
    lines.append("  Reminder: this suite cannot fully verify RAM (run MemTest86+ from")
    lines.append("  the USB boot menu) nor the storage surface (run the read test).")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_html():
    v, cls, fails, warns = overall_verdict()
    color = {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#cf222e",
             "INFO": "#0969da", "SKIP": "#6e7781"}[cls]
    cts = verdict_counts()

    rows = []
    section = None
    for r in RESULTS:
        if r["section"] != section:
            section = r["section"]
            rows.append(f'<tr class="sec"><td colspan="2">{section}</td></tr>')
        det = (r["detail"].replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace("\n", "<br>"))
        rows.append(
            f'<tr><td class="st" style="color:{color_of(r["status"])}">'
            f'{r["status"]}</td>'
            f'<td><b>{r["name"]}</b>'
            + (f'<div class="det">{det}</div>' if det else '')
            + '</td></tr>')

    fail_list = "".join(
        f"<li><b>{f['section']}</b> - {f['name']}: "
        f"{(f['detail'].splitlines()[0] if f['detail'] else '')}</li>" for f in fails)
    warn_list = "".join(
        f"<li><b>{w['section']}</b> - {w['name']}: "
        f"{(w['detail'].splitlines()[0] if w['detail'] else '')}</li>" for w in warns)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>HWCheck report - {sys_vendor()} {product_name()}</title>
<style>
 body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
        margin: 0; padding: 24px; background: #f6f8fa; color: #1f2328; }}
 .wrap {{ max-width: 1000px; margin: 0 auto; }}
 h1 {{ font-size: 22px; margin: 0 0 4px; }}
 .sub {{ color: #59636e; margin-bottom: 18px; font-size: 14px; }}
 .verdict {{ background: #fff; border-left: 6px solid {color}; border-radius: 6px;
             padding: 16px 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
 .verdict h2 {{ margin: 0 0 6px; color: {color}; font-size: 19px; }}
 .meta {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr));
          gap: 10px; background: #fff; padding: 16px 20px; border-radius: 6px;
          box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 20px; font-size: 14px; }}
 .meta b {{ display: block; color: #59636e; font-weight: 600; font-size: 12px;
            text-transform: uppercase; letter-spacing: .04em; }}
 table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px;
          overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); font-size: 14px; }}
 td {{ padding: 9px 14px; border-bottom: 1px solid #e6e9ed; vertical-align: top; }}
 .st {{ font-weight: 700; font-size: 11px; letter-spacing: .05em; white-space: nowrap;
        width: 70px; }}
 tr.sec td {{ background: #eef1f5; font-weight: 700; font-size: 12px;
              text-transform: uppercase; letter-spacing: .06em; color: #404854; }}
 .det {{ color: #59636e; font-size: 13px; margin-top: 3px; }}
 ul {{ background: #fff; padding: 16px 20px 16px 40px; border-radius: 6px;
       box-shadow: 0 1px 3px rgba(0,0,0,.06); font-size: 14px; }}
 li {{ margin-bottom: 5px; }}
 .foot {{ margin-top: 22px; color: #59636e; font-size: 12px; }}
 .chips span {{ display:inline-block; padding:2px 9px; border-radius:10px;
                background:#eef1f5; margin-right:6px; font-size:12px; }}
</style></head><body><div class="wrap">
 <h1>Used-Laptop Hardware Report</h1>
 <div class="sub">HWCheck v{VERSION} &middot; {datetime.datetime.now():%Y-%m-%d %H:%M}</div>

 <div class="verdict">
   <h2>{v}</h2>
   <div class="chips">{''.join(f'<span>{k}: {n}</span>' for k, n in sorted(cts.items()))}</div>
 </div>

 <div class="meta">
   <div><b>Machine</b>{sys_vendor()} {product_name()}</div>
   <div><b>Serial</b>{dmi('product_serial') or 'n/a'}</div>
   <div><b>BIOS</b>{dmi('bios_version')} ({dmi('bios_date')})</div>
   <div><b>CPU</b>{cpu_model()}</div>
   <div><b>RAM</b>{mem_total_gb():.2f} GiB</div>
   <div><b>Kernel</b>{platform.release()}</div>
 </div>

 {'<h2 style="font-size:16px">Hard failures</h2><ul>' + fail_list + '</ul>' if fails else ''}
 {'<h2 style="font-size:16px">Warnings</h2><ul>' + warn_list + '</ul>' if warns else ''}

 <h2 style="font-size:16px">Full results</h2>
 <table>{''.join(rows)}</table>

 <div class="foot">
   RAM integrity and storage surface are only partially covered here &mdash; boot the
   MemTest86+ entry and run the disk read test in the kit for full coverage.<br>
   This report describes the machine's state at the moment of testing.
 </div>
</div></body></html>"""
    return html


def color_of(status):
    return {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#cf222e",
            "INFO": "#0969da", "SKIP": "#6e7781"}.get(status, "#59636e")


def write_report():
    global REPORT_DIR
    if REPORT_DIR is None:
        setup_report_dir()
    txt = render_txt()
    html = render_html()
    files = {
        "report.txt": txt,
        "report.html": html,
        "report.json": json.dumps({
            "version": VERSION,
            "timestamp": datetime.datetime.now().isoformat(),
            "machine": {"vendor": sys_vendor(), "model": product_name(),
                        "serial": dmi("product_serial"),
                        "bios": dmi("bios_version"), "bios_date": dmi("bios_date"),
                        "cpu": cpu_model(), "ram_gib": round(mem_total_gb(), 2),
                        "kernel": platform.release()},
            "results": RESULTS,
            "verdict": overall_verdict()[0],
            "counts": verdict_counts(),
        }, indent=2),
    }
    for name, content in files.items():
        with open(os.path.join(REPORT_DIR, name), "w") as fh:
            fh.write(content)
    for a in ARTIFACTS:
        try:
            shutil.copy2(a, REPORT_DIR)
        except Exception:                                    # noqa: BLE001
            pass
    return REPORT_DIR


def print_summary():
    title("SUMMARY")
    v, cls, fails, warns = overall_verdict()
    cc = {"PASS": C_GRN, "WARN": C_YEL, "FAIL": C_RED}.get(cls, C_B)
    print()
    print(c(f"   {v}", cc + C_B))
    print()
    cts = verdict_counts()
    info(", ".join(f"{k}: {n}" for k, n in sorted(cts.items())))
    print()
    if fails:
        print(c("   Hard failures (physical faults - not fixable by reinstalling):", C_RED))
        for f in fails:
            info(f"* {f['section']}: {f['name']}")
            if f["detail"]:
                note(f"    {f['detail'].splitlines()[0]}")
        print()
    if warns:
        print(c("   Warnings (negotiate the price / plan a repair):", C_YEL))
        for w in warns:
            info(f"* {w['section']}: {w['name']}")
            if w["detail"]:
                note(f"    {w['detail'].splitlines()[0]}")
        print()
    note("  Reminder: run MemTest86+ from the USB boot menu for a full RAM test,")
    note("  and verify the storage surface if you plan to trust the data on it.")


# ============================================================================
# MAIN
# ============================================================================

def preflight():
    global KIT_ROOT, REPORT_DIR
    os.makedirs(SCRATCH, exist_ok=True)
    KIT_ROOT = find_kit_root()

    root = os.geteuid() == 0
    print()
    hr()
    print(c("   HWCheck v" + VERSION + " - used-laptop hardware verification", C_B + C_CYA))
    hr()
    info(f"Running as    : {'root' if root else 'user'}"
         + ("" if root else c("  <-- re-run with sudo for full access!", C_RED)))
    info(f"USB kit root  : {KIT_ROOT or c('not found (running from disk)', C_YEL)}")
    info(f"Scratch dir   : {SCRATCH}")
    info(f"Mode          : {'QUICK' if QUICK else 'FULL'}"
         f"{' / AUTO (no prompts)' if AUTO else ''}")
    print()
    if not root:
        note("Without root: no dmidecode, no smartctl, no /dev/input key test.")

    if not QUICK and not AUTO:
        print()
        note("This takes 15-40 minutes for a thorough machine. Options:")
        note("  * memtester + disk read test are the slow parts (you'll be asked)")
        note("  * everything else is a few minutes plus your own hands-on time")
        pause("Press Enter to start")


def main():
    global QUICK, AUTO
    ap = argparse.ArgumentParser(description="Used-laptop hardware verification suite")
    ap.add_argument("--quick", action="store_true", help="skip the slow deep tests")
    ap.add_argument("--auto", action="store_true",
                    help="no prompts: automatic checks only (for a fast screening)")
    ap.add_argument("--sections", default="",
                    help="comma list: identity,cpu,memory,storage,battery,display,"
                         "audio,keyboard,pointer,camera,network,usb,sensors,extras")
    args = ap.parse_args()
    QUICK, AUTO = args.quick, args.auto

    preflight()

    wanted = set(s.strip() for s in args.sections.split(",") if s.strip())
    def want(name):
        return not wanted or name in wanted

    plan = [
        ("identity", sec_identity),
        ("cpu", sec_cpu),
        ("memory", sec_memory),
        ("storage", sec_storage),
        ("battery", sec_battery),
        ("display", sec_display),
        ("audio", sec_audio),
        ("keyboard", sec_keyboard),
        ("pointer", sec_pointer),
        ("camera", sec_camera),
        ("network", sec_network),
        ("usb", sec_usb),
        ("sensors", sec_sensors),
        ("extras", sec_extras),
    ]

    try:
        for name, fn in plan:
            if not want(name):
                continue
            try:
                fn()
            except KeyboardInterrupt:
                print()
                if not ask("Skip the rest of this section? [Y/n]", "y"):
                    raise
            except Exception as e:                           # noqa: BLE001
                add(name.capitalize(), f"Section '{name}' error", "WARN",
                    f"{type(e).__name__}: {e}")
    except KeyboardInterrupt:
        print()
        note("Interrupted - writing the report with what was collected so far.")

    print_summary()
    path = write_report()
    title("REPORT SAVED")
    info(f"{path}/report.html   <- open this in a browser")
    info(f"{path}/report.txt    <- plain text version")
    info(f"{path}/report.json   <- machine-readable")
    print()
    note("Keep this on the USB stick. If you buy the laptop, it is your evidence")
    note("of the machine's condition on the day of sale.")
    print()


if __name__ == "__main__":
    main()
