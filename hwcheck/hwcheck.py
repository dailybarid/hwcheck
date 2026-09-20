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

# --- i18n --------------------------------------------------------------------
# The kit runs straight off a USB stick as root, so make sure this directory is
# importable no matter how the script was invoked.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import i18n
from i18n import tr, section as sec_label, status as status_label

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
REPORT_OVERRIDE = None    # --report-dir, if given
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
    print(c(text, C_B + C_CYA))
    hr()


def info(msg):
    print(msg)


def note(msg):
    print(c(msg, C_DIM))


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


def add(sec_id, name, code, detail="", warn_if=None):
    """Record one check result.

    `sec_id` and `code` are INTERNAL logic keys (see i18n.SECTIONS/STATUSES),
    stored untranslated so grouping and counting keep working in every
    language. Only the display labels are localised.
    """
    RESULTS.append({
        "section": sec_id,
        "name": name,
        "status": code,
        "detail": str(detail).strip(),
    })
    w = i18n.status_width()
    label = f"[ {status_label(code):<{w}} ]"
    color = {
        "PASS": C_GRN,
        "FAIL": C_RED,
        "WARN": C_YEL,
        "INFO": C_BLU,
        "SKIP": C_DIM,
    }.get(code, C_B)
    print(tr("main.indent_pair", v0=c(label, color), v1=name))
    if detail:
        for ln in str(detail).splitlines():
            print(c(tr("main.indent10", v0=ln), C_DIM))
    return code


def ask(prompt, default="n"):
    """Y/N prompt. Returns bool. Always yes in AUTO mode.

    Accepts the Latin forms the prompts advertise (y/n) plus the French (o/n)
    and Arabic (نعم / ي / لا / ن) equivalents, so a user who answers in their
    own script is not silently treated as saying no.
    """
    if AUTO:
        return True
    try:
        a = input(c(f"  ? {prompt} ", C_YEL)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not a:
        a = default.strip().lower()
    return a in ("y", "yes", "o", "oui", "yeah", "ok", "1",
                 "نعم", "ي", "ج", "أجل")


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
    """Create the report directory, degrading gracefully.

    Never let a failed report write take down a hardware test that already
    ran: fall back to the home directory, then /tmp, and say where it went.
    """
    global REPORT_DIR
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    model = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                   f"{sys_vendor()}-{product_name()}".strip("-_") or "unknown")[:48]
    folder = f"{ts}_{model}"

    bases = []
    if REPORT_OVERRIDE:
        bases.append(REPORT_OVERRIDE)
    if KIT_ROOT:
        bases.append(os.path.join(KIT_ROOT, "report"))
    bases.append(os.path.join(os.path.expanduser("~"), "hwcheck-report"))
    bases.append("/tmp/hwcheck-report")
    preferred = bases[0]

    for base in bases:
        path = os.path.join(base, folder)
        try:
            os.makedirs(path, exist_ok=True)
            # Two runs in the same minute on the same model would otherwise
            # share a folder and overwrite each other's report.
            n = 2
            while os.path.exists(os.path.join(path, "report.json")):
                path = os.path.join(base, f"{folder}-{n}")
                os.makedirs(path, exist_ok=True)
                n += 1
            probe = os.path.join(path, ".write-probe")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            REPORT_DIR = path
            if base != preferred:
                print(tr("main.report_dir_fallback", v0=path))
            return path
        except Exception:                                    # noqa: BLE001
            continue

    REPORT_DIR = "/tmp/hwcheck-report"
    os.makedirs(REPORT_DIR, exist_ok=True)
    print(tr("main.report_dir_fallback", v0=REPORT_DIR))
    return REPORT_DIR


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
    title(tr("identity.machine_identity_what_actually"))

    info(tr("identity.vendor_model", v0=c(sys_vendor() + ' ' + product_name(), C_B)))
    info(tr("identity.board", v0=dmi('board_name')))
    info(tr("identity.bios", v0=dmi('bios_version'), v1=dmi('bios_date')))
    info(tr("identity.serial", v0=dmi('product_serial')))
    info(tr("identity.board_serial", v0=dmi('board_serial')))
    info(tr("identity.chassis", v0=dmi('chassis_type')))
    info(tr("identity.kernel", v0=platform.release(), v1=platform.machine()))

    problems = []
    if not product_name():
        problems.append(tr("identity.dmi_product_name_unreadable"))
    if dmi("bios_date"):
        try:
            y = int(dmi("bios_date").split("/")[-1])
            age = datetime.date.today().year - y
            if age >= 12:
                problems.append(tr("identity.bios_years_old_check", v0=age))
        except Exception:                                    # noqa: BLE001
            pass

    # Serial-number tampering proxy: mismatched vendor strings
    if sys_vendor() and dmi("board_vendor") and sys_vendor() != dmi("board_vendor"):
        problems.append(tr("identity.chassis_vendor_mainboard_vendor"))

    if problems:
        add("Identity", tr("identity.identity_sanity_check"), "WARN", "\n".join(problems))
    else:
        add("Identity", tr("identity.identity_sanity_check"), "PASS",
            tr("identity.dmi_readable_vendor_data"))

    # Uptime / previous OS traces
    _, blkid = run(["blkid"])
    os_traces = []
    if re.search(r"ntfs|BitLocker", blkid, re.I):
        os_traces.append(tr("identity.windows_era_filesystem_found"))
    if re.search(r"Type=\"ext4\"|Type=\"btrfs\"", blkid):
        os_traces.append(tr("identity.linux_filesystem_found"))
    if "BitLocker" in run(["blkid"])[1]:
        os_traces.append(tr("identity.bitlocker_encrypted_volume_could"))
    add("Identity", tr("identity.existing_data_disks"),
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
    title(tr("cpu.cpu_real_spec_thermals"))

    info(tr("cpu.model", v0=c(cpu_model(), C_B)))
    info(tr("cpu.cores_logical", v0=cpu_threads()))
    info(tr("cpu.max_freq_mhz_current", v0=cpu_max_mhz() or '?', v1=cpu_cur_mhz() or '?'))
    info(tr("cpu.base_freq_mhz_all", v0=cpu_base_mhz() or '?'))
    gov = read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    info(tr("cpu.governor", v0=gov or 'n/a'))

    flags = ""
    for line in read_file("/proc/cpuinfo").splitlines():
        if line.lower().startswith("flags"):
            flags = line.split(":", 1)[1]
            break
    feats = [f for f in ("vmx", "svm", "avx2", "avx512f", "aes", "sse4_2")
             if f in flags.split()]
    info(tr("cpu.features", v0=', '.join(feats) or 'none detected'))
    info(tr("cpu.microcode_entries", v0=read_file('/proc/cpuinfo').count('microcode')))

    if not any(f in flags.split() for f in ("vmx", "svm")):
        add("CPU", tr("cpu.virtualisation_support"), "WARN",
            tr("cpu.vt_x_amd_v"))

    n = cpu_threads()
    dur = 8 if QUICK else 45
    t_start = temp_c()
    f_start = fan_rpms()
    mhz_start = cpu_cur_mhz()

    print()
    note(tr("cpu.loading_all_threads_s", v0=n, v1=dur))
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
            add("CPU", tr("cpu.sustained_clock_under_load"), "FAIL",
                tr("cpu.vs_mhz_base_clock", v0=aft, v1=rated, v2=f"{ratio*100:.0f}", v3=extra))
        elif ratio < 0.95:
            add("CPU", tr("cpu.sustained_clock_under_load"), "WARN",
                tr("cpu.vs_mhz_base_clock_2", v0=aft, v1=rated, v2=f"{ratio*100:.0f}", v3=extra))
        else:
            add("CPU", tr("cpu.sustained_clock_under_load"), "PASS",
                tr("cpu.vs_mhz_base_clock_3", v0=aft, v1=rated, v2=f"{ratio*100:.0f}", v3=extra))
    else:
        add("CPU", tr("cpu.sustained_clock_under_load"), "INFO", tr("cpu.freq_change", v0=bef, v1=aft))

    if tmax is not None:
        if tmax >= 95:
            add("CPU", tr("cpu.peak_temperature"), "FAIL",
                tr("cpu.c_under_load_overheating", v0=f"{tmax:.0f}"))
        elif tmax >= 85:
            add("CPU", tr("cpu.peak_temperature"), "WARN",
                tr("cpu.c_under_load_idle", v0=f"{tmax:.0f}", v1=f"{t_start:.0f}"))
        else:
            add("CPU", tr("cpu.peak_temperature"), "PASS",
                tr("cpu.peak_c_under_load", v0=f"{tmax:.0f}", v1=f"{t_start:.0f}"))
    else:
        add("CPU", tr("cpu.peak_temperature"), "INFO",
            tr("cpu.thermal_sensors_exposed_install"))

    if f_end or f_start:
        add("CPU", tr("cpu.cooling_fan"), "PASS",
            tr("cpu.rpm_idle_load", v0=f_start or '?', v1=f_end or '?'))
    else:
        add("CPU", tr("cpu.cooling_fan"), "INFO",
            tr("cpu.fan_rpm_exposed_os"))

    # Locked multiplier / reprovisioned CPU warning
    if "Intel" in cpu_model() and cpu_threads() <= 2:
        add("CPU", tr("cpu.core_count"), "WARN",
            tr("cpu.only_threads_confirm_matches", v0=cpu_threads()))

    rc, out = run(["dmesg", "--level=err,warn"], timeout=20)
    if rc == 0:
        mce = [l for l in out.splitlines()
               if re.search(r"mce|machine check|therm|thermal throttl", l, re.I)]
        if mce:
            add("CPU", tr("cpu.kernel_hardware_errors"), "WARN",
                "\n".join(mce[:6]))
        else:
            add("CPU", tr("cpu.kernel_hardware_errors"), "PASS",
                tr("cpu.mce_thermal_throttle_events"))


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
    title(tr("memory.ram_size_slots_speed"))

    total = mem_total_gb()
    info(tr("memory.total", v0=c(f'{total:.2f} GiB', C_B)))
    info(tr("memory.swap", v0=read_file('/proc/meminfo')))

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
            info(tr("memory.dimm", v0=d))
    if slots_total:
        add("RAM", tr("memory.memory_slots"), "PASS" if slots_used else "WARN",
            f"{slots_used} of {slots_total} slots populated"
            + (f" - upgradable: {slots_total - slots_used} free slot(s)" if slots_total > slots_used else ""))

    # Consumer-grade RAM check: is the installed amount what was advertised?
    if total < 3.5:
        add("RAM", tr("memory.installed_capacity"), "WARN",
            tr("memory.gib_too_little_modern", v0=f"{total:.1f}"))
    else:
        add("RAM", tr("memory.installed_capacity"), "PASS", tr("memory.gib_usable", v0=f"{total:.2f}"))

    # Mixed / mismatched sticks (dual-channel loss)
    if len(dimms) >= 2 and len(set(d.split("@")[0].split()[-1] for d in dimms)) > 1:
        add("RAM", tr("memory.memory_configuration"), "WARN",
            tr("memory.mismatched_module_sizes_dual"))

    # Integrity test
    print()
    if AUTO:
        add("RAM", tr("memory.quick_memory_integrity"), "SKIP",
            tr("memory.skipped_auto_mode_needs"))
    elif has("memtester"):
        mb = int(min(2048, max(256, total * 1024 * 0.25)))
        note(tr("memory.free_memory", v0=read_file('/proc/meminfo').splitlines()[0] if read_file('/proc/meminfo') else ''))
        if ask(tr("memory.run_memtester_pass_mb", v0=mb)):
            rc, out = run(["memtester", str(mb), "1"], timeout=600)
            tail = "\n".join(out.splitlines()[-6:])
            if rc == 0 and "ok" in out.lower():
                add("RAM", tr("memory.memory_integrity_memtester"), "PASS",
                    tr("memory.mb_pattern_test_completed", v0=mb))
            else:
                add("RAM", tr("memory.memory_integrity_memtester"), "FAIL",
                    tr("memory.errors_detected_ram_faulty", v0=tail))
        else:
            add("RAM", tr("memory.memory_integrity_memtester"), "SKIP", tr("audio.run"))
    else:
        add("RAM", tr("memory.memory_integrity_memtester"), "SKIP",
            tr("memory.memtester_installed_use_memtest86"))

    note(tr("memory.full_pass_reboot_into"))
    note(tr("memory.least_one_complete_pass"))


# ============================================================================
# SECTION 4 - Storage
# ============================================================================

def list_disks():
    """Physical disks only - skip pseudo and virtual block devices."""
    rc, out = run(["lsblk", "-d", "-n", "-o", "NAME,TYPE,SIZE,TRAN,MODEL"])
    skip = ("loop", "sr", "zram", "fd", "ram", "dm-", "md", "nbd", "rbd")
    disks = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "disk"                 and not parts[0].startswith(skip):
            disks.append({
                "name": parts[0], "size": parts[2],
                "tran": parts[3] if len(parts) > 3 else "",
                "model": " ".join(parts[4:]) if len(parts) > 4 else "",
            })
    return disks


def sec_storage():
    title(tr("storage.storage_reason_used_laptops"))

    disks = list_disks()
    if not disks:
        add("Storage", tr("storage.disks_detected"), "FAIL", tr("storage.internal_disk_found"))
        return

    for d in disks:
        dev = f"/dev/{d['name']}"
        info(tr("storage.disk_line", v0=c(dev, C_B), v1=d['size'], v2=d['tran'], v3=d['model']))

    rc, out = run(["smartctl", "--version"])
    if rc != 0 or "not found" in out:
        add("Storage", tr("storage.smart_tooling"), "SKIP",
            tr("storage.smartmontools_missing_cannot_read"))
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
                    add("Storage", tr("storage.smart", v0=label), "INFO",
                        tr("storage.health_string_unavailable_but", v0=detail))
                else:
                    add("Storage", tr("storage.smart", v0=label), "WARN",
                        tr("storage.smart_supported_blocked_by"))
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
                bad.append(tr("storage.smart_health", v0=verdict))
            if realloc > 0:
                bad.append(tr("storage.reallocated_sectors", v0=realloc))
            if pending > 0:
                bad.append(tr("storage.pending_unstable_sectors", v0=pending))
            if uncorr > 0:
                bad.append(tr("storage.uncorrectable_sectors", v0=uncorr))
            if metrics.get("Critical_Warning"):
                bad.append(tr("storage.nvme_critical_warning_set"))
            if used >= 90:
                bad.append(tr("storage.ssd_wear", v0=used))

            warn = []
            if crc > 0:
                warn.append(tr("storage.udma_crc_errors_cable", v0=crc))
            if used >= 75:
                warn.append(tr("storage.ssd_wear_nearing_end", v0=used))
            if hours > 25000:
                warn.append(tr("storage.h_power_years_check", v0=hours, v1=f"{hours/8760:.1f}"))
            if metrics.get("Spare") and int(metrics["Spare"]) < 20:
                warn.append(tr("storage.spare_blocks_failing_ssd", v0=metrics['Spare']))

            summary = f"{detail}"

            if bad:
                add("Storage", tr("storage.health", v0=label), "FAIL",
                    "; ".join(bad) + f"\n{summary}")
            elif warn:
                add("Storage", tr("storage.health", v0=label), "WARN",
                    "; ".join(warn) + f"\n{summary}")
            else:
                add("Storage", tr("storage.health", v0=label), "PASS",
                    tr("storage.smart_h_bad_sectors", v0=verdict, v1=hours, v2=summary))

        # Filesystem-level read check (catches errors SMART misses)
        print()
        if not AUTO and ask(tr("storage.run_disk_read_speed")):
            targets = []
            for d in disks:
                for part in glob.glob(f"/dev/{d['name']}[0-9]*"):
                    targets.append(part)
            if targets:
                p = targets[0]
                note(tr("storage.timing_mb_sequential_read", v0=p))
                rc, out = run(["dd", f"if={p}", "of=/dev/null", "bs=1M", "count=512",
                               "iflag=direct"], timeout=300)
                m = re.search(r"([\d.]+) (GB|MB)/s", out)
                spd = " ".join(m.groups()) + "/s" if m else "n/a"
                status = "PASS" if m and float(m.group(1)) > (30 if m.group(2) == "MB" else 0.3) else "WARN"
                if m and m.group(2) == "MB" and float(m.group(1)) < 60:
                    status = "FAIL"
                add("Storage", tr("storage.sequential_read_speed", v0=p), status,
                    tr("storage.two_lines", v0=spd, v1=first_line(out.splitlines()[-1:]) if out else ''))
                # Check dmesg for I/O errors during the read
                rc, dout = run(["dmesg", "--level=err,warn"], timeout=20)
                ioerr = [l for l in dout.splitlines()
                         if re.search(r"I/O error|ata\d|nvme.*error|SMART error", l, re.I)]
                if ioerr:
                    add("Storage", tr("storage.i_o_errors_during"), "FAIL",
                        "\n".join(ioerr[:8]))
                else:
                    add("Storage", tr("storage.i_o_errors_during"), "PASS",
                        tr("storage.kernel_i_o_errors"))
            else:
                add("Storage", tr("storage.disk_read_test"), "SKIP", tr("storage.partitions_read"))
        else:
            add("Storage", tr("storage.disk_read_test"), "SKIP", tr("audio.run"))

    note(tr("storage.also_check_bios_see"))
    note(tr("storage.reporting_gb_means_swapped"))


# ============================================================================
# SECTION 5 - Battery & power
# ============================================================================

def sec_battery():
    title(tr("battery.battery_power_usually_most"))

    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if not bats:
        add("Battery", tr("battery.battery_present"), "WARN",
            tr("battery.battery_detected_either_removed"))
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

        info(tr("battery.battery_line", v0=c(name, C_B), v1=manu, v2=model, v3=tech))
        info(f"  design={design:.1f} Wh" if design else "  design=unknown")
        info(f"  full  ={full:.1f} Wh" if full else "  full=unknown")
        info(f"  now   ={now:.1f} Wh ({pct:.0f}%)" if now and pct else f"  now={now}")
        info(tr("battery.cycles_status", v0=cycles or '?', v1=status or '?'))

        if design and full:
            health = full / design * 100
            if health < 60:
                add("Battery", tr("battery.health", v0=name), "FAIL",
                    tr("battery.design_capacity_wh_expect", v0=f"{health:.0f}", v1=f"{full:.1f}", v2=f"{design:.1f}"))
            elif health < 80:
                add("Battery", tr("battery.health", v0=name), "WARN",
                    tr("battery.design_capacity_wh_usable", v0=f"{health:.0f}", v1=f"{full:.1f}", v2=f"{design:.1f}"))
            else:
                add("Battery", tr("battery.health", v0=name), "PASS",
                    tr("battery.design_capacity_wh", v0=f"{health:.0f}", v1=f"{full:.1f}", v2=f"{design:.1f}"))
        else:
            add("Battery", tr("battery.capacity", v0=name), "INFO",
                tr("battery.kernel_expose_design_capacity"))

        if cycles:
            try:
                cy = int(cycles)
                if cy > 800:
                    add("Battery", tr("battery.cycle_count", v0=name), "WARN",
                        tr("battery.cycles_deep_into_its", v0=cy))
                else:
                    add("Battery", tr("battery.cycle_count", v0=name), "PASS", tr("battery.cycles", v0=cy))
            except Exception:                                # noqa: BLE001
                pass

        # Does the laptop run on AC without the battery / is the charger working?
        if pct is not None and pct < 5 and status.lower() == "discharging" and read_file("/sys/class/power_supply/AC/online") != "1":
            add("Battery", tr("battery.charging", v0=name), "FAIL",
                tr("battery.charging_ac_detected_suspect"))

    # AC adapter
    acs = sorted(glob.glob("/sys/class/power_supply/AC*")) + \
        sorted(glob.glob("/sys/class/power_supply/ADP*"))
    for a in acs:
        online = read_file(os.path.join(a, "online"))
        add("Battery", tr("battery.ac_adapter", v0=os.path.basename(a)),
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
    title(tr("display.screen_resolution_pixels_backlight"))

    st = xrandr_state()
    if not st:
        add("Display", tr("display.x_server"), "FAIL",
            tr("display.x_display_available_run"))
        return

    any_connected = False
    for name, o in st.items():
        if not o["connected"]:
            continue
        any_connected = True
        info(tr("display.output_line", v0=c(name, C_B), v1=o['info']))
        maxmode = max(o["modes"], key=lambda m: int(m[0].split("x")[0]) * int(m[0].split("x")[1]),
                      default=None)
        info(tr("display.running", v0=o['current']))
        info(tr("display.preferred", v0=o['native']))
        info(tr("display.max_mode", v0=maxmode[0] if maxmode else '?'))
        info(tr("display.modes", v0=len(o['modes'])))

        if o["current"] and maxmode:
            cur_px = int(o["current"].split("x")[0]) * int(o["current"].split("x")[1])
            native = o["native"] or maxmode[0]
            nat_px = int(native.split("x")[0]) * int(native.split("x")[1])
            if cur_px < nat_px:
                add("Display", tr("display.resolution", v0=name), "INFO",
                    tr("display.running_panel_native_lower", v0=o['current'], v1=native))
            else:
                add("Display", tr("display.resolution", v0=name), "PASS",
                    tr("display.panel_native", v0=o['current'], v1=native))

            # The fraud signal is a LOW native resolution / missing EDID:
            # a swapped panel is usually a cheap 1366x768 unit.
            nat_w = int(native.split("x")[0])
            if not o["native"]:
                add("Display", tr("display.edid", v0=name), "WARN",
                    tr("display.preferred_mode_reported_panel"))
            elif nat_w < 1366:
                add("Display", tr("display.panel_resolution", v0=name), "WARN",
                    tr("display.native_resolution_only_laptop", v0=native))
            else:
                add("Display", tr("display.panel_resolution", v0=name), "PASS",
                    tr("display.native_appropriate_panel_size", v0=native))

        # Physical size helps spot a replaced panel
        m = re.search(r"(\d+)mm x (\d+)mm", o["info"])
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w and h:
                diag = ((w ** 2 + h ** 2) ** 0.5) / 25.4
                add("Display", tr("display.physical_size", v0=name), "PASS",
                    tr("display.x_mm_diagonal", v0=w, v1=h, v2=f"{diag:.1f}"))
                if cur := o["current"]:
                    px = int(cur.split("x")[0])
                    ppi = px / (w / 25.4)
                    info(tr("display.pixel_density_ppi", v0=f"{ppi:.0f}"))

    if not any_connected:
        add("Display", tr("display.connected_outputs"), "FAIL", tr("display.display_output_reports_connected"))

    # Backlight control
    bl = sorted(glob.glob("/sys/class/backlight/*"))
    if bl:
        cur = read_file(os.path.join(bl[0], "brightness"))
        mx = read_file(os.path.join(bl[0], "max_brightness"))
        add("Display", tr("display.backlight_control"), "PASS",
            tr("display.brightness_keys_should_work", v0=os.path.basename(bl[0]), v1=cur, v2=mx))
    else:
        add("Display", tr("display.backlight_control"), "WARN",
            tr("display.backlight_device_exposed_brightness"))

    # Interactive dead-pixel test
    print()
    note(tr("display.dead_stuck_pixels_dust"))
    note(tr("display.flat_colours_next_step"))
    if AUTO:
        add("Display", tr("display.dead_pixel_test"), "SKIP", tr("display.skipped_auto_mode"))
        return
    if not ask(tr("display.run_full_screen_dead"), default="y"):
        add("Display", tr("display.dead_pixel_test"), "SKIP", tr("display.run_by_choice"))
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

    note(tr("display.generated_full_screen_test", v0=len(paths), v1=w, v2=h))

    viewer = None
    for cand in ("feh", "eog", "xdg-open"):
        if has(cand):
            viewer = cand
            break

    shown = False
    if viewer == "feh":
        note(tr("display.showing_them_full_screen"))
        rc, out = run(["feh", "-F", "-Z", "-Y", "--no-menus", "-d", "--auto-zoom"] + paths,
                      timeout=1800, stdin_null=False)
        shown = True
    elif viewer == "eog":
        rc, out = run(["eog", "-f", "-s"] + paths, timeout=1800, stdin_null=False)
        shown = True

    if not shown:
        note(tr("display.image_viewer_bundled_opening"))
        note(tr("display.folder", v0=SCRATCH))
        run(["xdg-open", SCRATCH], timeout=20)

    pause(tr("display.when_have_finished_looking"))

    issues = []
    if ask(tr("display.black_grey_dots_lines")):
        issues.append(tr("display.dead_pixels_white_screen"))
    if ask(tr("display.white_lit_dots_black")):
        issues.append(tr("display.stuck_pixels_black_screen"))
    if ask(tr("display.coloured_red_green_blue")):
        issues.append(tr("display.stuck_sub_pixels"))
    if ask(tr("display.bright_patches_light_leaks")):
        issues.append(tr("display.backlight_bleed"))
    if ask(tr("display.scratches_pressure_marks_bright")):
        issues.append(tr("display.panel_surface_damage"))
    if ask(tr("display.flicker_shimmering_size_colour")):
        issues.append(tr("display.loose_display_cable_hinge"))

    if issues:
        add("Display", tr("display.dead_pixel_panel_defects"), "FAIL", "; ".join(issues))
    else:
        add("Display", tr("display.dead_pixel_panel_defects"), "PASS",
            tr("display.defects_reported_across_black"))


# ============================================================================
# SECTION 7 - Audio
# ============================================================================

def sec_audio():
    title(tr("audio.audio_speakers_microphone_headphone"))

    rc, out = run(["aplay", "-l"], timeout=20)
    cards = re.findall(r"card (\d+): (\S+) \[([^\]]+)\]", out)
    for num, cid, desc in cards:
        info(tr("audio.card", v0=num, v1=cid, v2=desc))
    if not cards:
        add("Audio", tr("audio.sound_card"), "FAIL",
            tr("audio.alsa_playback_device_found"))
    else:
        add("Audio", tr("audio.sound_card"), "PASS", f"{len(cards)} card(s): "
            + ", ".join(d for _, _, d in cards))

    # Codec identification
    rc, codecs = run(["bash", "-c",
                      "grep -H . /proc/asound/card*/codec* 2>/dev/null | "
                      "grep -m4 -E 'Codec|Vendor Id'"], timeout=20)
    if codecs.strip():
        info(tr("audio.codec"))
        for l in codecs.splitlines()[:4]:
            info(tr("main.indent2", v0=l.split(':', 1)[-1].strip() if ':' in l else l))

    if AUTO:
        add("Audio", tr("audio.speaker_output_test"), "SKIP", tr("display.skipped_auto_mode"))
        add("Audio", tr("audio.microphone_test"), "SKIP", tr("display.skipped_auto_mode"))
        return

    print()
    if has("speaker-test") and ask(tr("audio.play_test_tone_through"), "y"):
        note(tr("audio.listen_clean_tone_both"))
        rc, out = run(["speaker-test", "-t", "sine", "-f", "440", "-l", "1", "-c", "2",
                       "-p", "2"], timeout=90, stdin_null=False)
        res = []
        if ask(tr("audio.hear_tone_left_speaker")):
            res.append(tr("audio.l_ok"))
        else:
            res.append(tr("audio.l_fail"))
        if ask(tr("audio.hear_tone_right_speaker")):
            res.append(tr("audio.r_ok"))
        else:
            res.append(tr("audio.r_fail"))
        if ask(tr("audio.crackling_buzzing_distortion")):
            res.append(tr("audio.distortion"))
        bad = [r for r in res if "FAIL" in r or r == "distortion"]
        add("Audio", tr("audio.speaker_output_test"), "FAIL" if bad else "PASS",
            ", ".join(res) + (" - blown speaker or bad amp" if bad else ""))
    else:
        add("Audio", tr("audio.speaker_output_test"), "SKIP", tr("audio.run"))

    # Bass/loudness check (blown drivers often only rattle at volume)
    if not AUTO and ask(tr("audio.play_bass_tone_full")):
        run(["bash", "-c",
             "for f in 60 80 120; do speaker-test -t sine -f $f -l 1 -c 2 >/dev/null 2>&1; done"],
            timeout=90, stdin_null=False)
        if ask(tr("audio.rattling_buzzing_high_volume")):
            add("Audio", tr("audio.speaker_high_volume"), "FAIL",
                tr("audio.rattle_buzz_blown_loose"))
        else:
            add("Audio", tr("audio.speaker_high_volume"), "PASS", tr("audio.clean_high_volume"))
        if ask(tr("audio.left_right_volume_roughly"), "y"):
            add("Audio", tr("audio.channel_balance"), "PASS", tr("audio.balanced"))
        else:
            add("Audio", tr("audio.channel_balance"), "WARN",
                tr("audio.uneven_channels_one_driver"))

    # Microphone
    print()
    if ask(tr("audio.test_microphone_now_5s"), "y"):
        wav = os.path.join(SCRATCH, "mic.wav")
        note(tr("audio.recording_seconds_speak_into"))
        rc, out = run(["arecord", "-d", "5", "-f", "S16_LE", "-r", "44100",
                       "-c", "1", wav], timeout=40)
        if rc != 0 or not os.path.exists(wav) or os.path.getsize(wav) < 1000:
            add("Audio", tr("audio.microphone"), "FAIL",
                tr("audio.recording_failed_check_mic", v0=first_line(out)))
        else:
            note(tr("audio.playing_back_what_was"))
            run(["aplay", wav], timeout=30)
            if ask(tr("audio.hear_own_voice")):
                add("Audio", tr("audio.microphone"), "PASS", tr("audio.recorded_played_back_voice"))
                ARTIFACTS.append(wav)
            else:
                if ask(tr("audio.was_there_noise_recording")):
                    add("Audio", tr("audio.microphone"), "WARN",
                        tr("audio.signal_present_but_unrecognisable"))
                else:
                    add("Audio", tr("audio.microphone"), "FAIL", tr("audio.silent_recording_dead_mic"))
    else:
        add("Audio", tr("audio.microphone"), "SKIP", tr("audio.run"))

    # Headphone jack detection (hardware jack sense)
    print()
    note(tr("audio.plug_pair_headphones_now"))
    pause(tr("audio.plug_headphones_then_press"))
    rc, out = run(["bash", "-c",
                   "cat /proc/asound/card*/codec* 2>/dev/null | grep -iE "
                   "'Pin-ctls|jack' | head -20; amixer -c0 contents 2>/dev/null | "
                   "grep -i -A2 'Headphone' | head -20"], timeout=20)
    if ask(tr("audio.headphones_produce_sound_nothing")):
        add("Audio", tr("audio.headphone_jack"), "PASS", tr("audio.audio_routed_jack"))
    else:
        add("Audio", tr("audio.headphone_jack"), "WARN",
            tr("audio.sound_jack_reposition_plug"))


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
    title(tr("keyboard.keyboard_every_single_key"))

    devs = input_devices()
    kb = [d for d in devs if "keyboard" in d[1].lower() or
          (d[2] and "kbd" in d[1].lower())]
    if not devs:
        add("Input", tr("keyboard.input_devices"), "FAIL", tr("keyboard.dev_input_event_run"))
        return
    for path, name, _ in devs:
        info(tr("keyboard.event_line", v0=f"{os.path.basename(path):<12}", v1=name))
    add("Input", tr("keyboard.input_devices_detected"), "PASS",
        f"{len(devs)} event device(s); keyboards: "
        + (", ".join(n for _, n, _ in kb) or "none named 'keyboard'"))

    if AUTO or not ask(tr("keyboard.run_interactive_keyboard_test"), "y"):
        add("Input", tr("keyboard.keyboard_key_test"), "SKIP", tr("audio.run"))
        return

    print()
    note(tr("keyboard.press_every_key_keyboard"))
    note(tr("keyboard.include_fn_f1_f12"))
    note(tr("keyboard.press_esc_three_times"))
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
                note(tr("keyboard.s_inactivity_finishing_key"))
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
    info(tr("keyboard.keys_registered", v0=c(len(pressed), C_B)))
    info(tr("keyboard.pointer_events_motion_buttons", v0=touches['moves'], v1=touches['clicks']))
    if missing:
        info(tr("keyboard.pressed", v0=c(' '.join(missing), C_YEL)))

    if missing:
        add("Input", tr("keyboard.keyboard_key_test"), "WARN",
            tr("keyboard.keys_registered_registered_wrong", v0=len(pressed), v1=' '.join(missing)))
    else:
        add("Input", tr("keyboard.keyboard_key_test"), "PASS",
            tr("keyboard.all_keys_responded", v0=len(pressed)))

    # Fn / F-row behaviour
    if not AUTO:
        print()
        note(tr("keyboard.now_test_fn_row"))
        note(tr("keyboard.airplane_mode_keyboard_backlight"))
        pause(tr("keyboard.fn_combinations_then_press"))
        if ask(tr("keyboard.brightness_mute_volume_fn")):
            add("Input", tr("keyboard.fn_hotkeys"), "PASS", tr("keyboard.function_keys_respond"))
        else:
            add("Input", tr("keyboard.fn_hotkeys"), "WARN",
                tr("keyboard.some_fn_keys_respond"))

    # Touchpad confirmation from the same capture
    if touches["moves"] > 50:
        add("Input", tr("keyboard.touchpad_motion"), "PASS",
            tr("keyboard.pointer_events_captured", v0=touches['moves']))
    elif touches["moves"] > 0:
        add("Input", tr("keyboard.touchpad_motion"), "WARN",
            tr("keyboard.only_pointer_events_barely", v0=touches['moves']))
    else:
        add("Input", tr("keyboard.touchpad_motion"), "WARN", tr("keyboard.pointer_movement_captured"))

    if touches["clicks"] >= 1:
        add("Input", tr("keyboard.buttons_clicks"), "PASS",
            tr("keyboard.button_presses_captured", v0=touches['clicks']))
    else:
        add("Input", tr("keyboard.buttons_clicks"), "WARN",
            tr("keyboard.button_presses_captured_click"))


# ============================================================================
# SECTION 9 - Touchpad / trackpoint dedicated test
# ============================================================================

def sec_pointer():
    title(tr("pointer.touchpad_trackpoint_precision_edges"))

    rc, out = run(["bash", "-c", "grep -iE 'Name|Handlers' /proc/bus/input/devices | "
                                 "paste - - | grep -iE 'touch|track|synaptics|elan|alps|mouse'"],
                  timeout=20)
    if out.strip():
        for l in out.splitlines()[:6]:
            info(l.strip())
    pointers = [d for d in input_devices()
                if any(w in d[1].lower() for w in ("touchpad", "track", "mouse", "elan", "synaptic"))]
    if pointers:
        add("Pointer", tr("pointer.pointing_devices"), "PASS",
            ", ".join(n for _, n, _ in pointers))
    else:
        add("Pointer", tr("pointer.pointing_devices"), "WARN",
            tr("pointer.touchpad_trackpoint_named_device"))

    if AUTO or not ask(tr("pointer.run_touchpad_edge_gesture"), "y"):
        add("Pointer", tr("pointer.touchpad_full_area_test"), "SKIP", tr("audio.run"))
        return

    print()
    note(tr("pointer.follow_these_steps_touchpad"))
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
            add("Pointer", tr("pointer.touchpad_gesture_area"), "WARN",
                tr("pointer.failed_unsure", v0=s))
    if not any(r["section"] == "Pointer" and r["status"] == "WARN" for r in RESULTS):
        add("Pointer", tr("pointer.touchpad_full_area_test"), "PASS",
            tr("pointer.all_area_button_gesture"))


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
    title(tr("camera.webcam_privacy_shutter"))

    vids = sorted(glob.glob("/dev/video*"))
    rc, lsusb_out = run(["lsusb"])
    cam_usb = [l for l in lsusb_out.splitlines()
               if re.search(r"cam|webcam|imaging|HD Web|Integrated", l, re.I)]

    if not vids:
        add("Camera", tr("camera.webcam_device"), "FAIL",
            tr("camera.dev_video_webcam_missing"))
        return
    add("Camera", tr("camera.webcam_device"), "PASS",
        tr("camera.video_device_s", v0=len(vids), v1=', '.join(os.path.basename(v) for v in vids)))
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
            add("Camera", tr("camera.supported_formats"), "PASS",
                tr("camera.max", v0=', '.join(sorted(set(formats))), v1=max(resos, key=lambda r: int(r.split('x')[0])) if resos else '?'))
        # Try an actual capture
        note(tr("camera.capturing_frame"))
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
            add("Camera", tr("camera.frame_capture"), "PASS",
                tr("camera.captured_kb_x_image", v0=len(data) // 1024, v1=prev_w, v2=prev_h))
            if not AUTO and has("xdg-open"):
                if ask(tr("camera.look_captured_image_screen"), "y"):
                    run(["xdg-open", png], timeout=60)
            if not AUTO:
                if ask(tr("camera.was_image_clear_black")):
                    add("Camera", tr("camera.image_quality"), "PASS", tr("camera.visible_clear_image"))
                else:
                    add("Camera", tr("camera.image_quality"), "FAIL",
                        tr("camera.black_garbled_image_lens"))
        else:
            add("Camera", tr("camera.frame_capture"), "WARN",
                tr("camera.could_stream_frame_bundled"))
    else:
        add("Camera", tr("camera.webcam_tooling"), "SKIP", tr("camera.v4l_utils_installed"))

    note(tr("camera.physically_inspect_lens_scratches"))


# ============================================================================
# SECTION 11 - Networking
# ============================================================================

def sec_network():
    title(tr("network.networking_wifi_ethernet_bluetooth"))

    rc, out = run(["lspci"])
    wlan = [l.split(":")[-1].strip() for l in out.splitlines()
            if re.search(r"network controller|wireless", l, re.I)]
    eth = [l.split(":")[-1].strip() for l in out.splitlines()
           if re.search(r"ethernet controller", l, re.I)]
    if wlan:
        add("Network", tr("network.wifi_adapter_hardware"), "PASS", wlan[0])
    else:
        add("Network", tr("network.wifi_adapter_hardware"), "FAIL",
            tr("network.wireless_controller_pci_bus"))
    if eth:
        add("Network", tr("network.ethernet_adapter_hardware"), "PASS", eth[0])
    else:
        add("Network", tr("network.ethernet_adapter_hardware"), "WARN",
            tr("network.ethernet_controller_fine_ultrabooks"))

    # rfkill state
    rc, out = run(["rfkill", "list"], timeout=15)
    if out.strip():
        blocked = [l for l in out.splitlines() if "blocked: yes" in l]
        if blocked:
            add("Network", tr("network.radio_kill_switches"), "WARN",
                "Hard/soft blocked: " + "; ".join(b.strip() for b in blocked))
        else:
            add("Network", tr("network.radio_kill_switches"), "PASS", tr("network.all_radios_unblocked"))
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
        info(tr("network.interface", v0=iface))
        run(["ip", "link", "set", iface, "up"], timeout=15)
        time.sleep(2)
        found = []
        if has("iw"):
            rc, out = run(["iw", "dev", iface, "scan"], timeout=45)
            found = re.findall(r"SSID: (.+)", out)
            bands = set(re.findall(r"^\s*(\d{4}) MHz", out, re.M))
            if found:
                add("Network", tr("network.wifi_scan"), "PASS",
                    f"Detected {len(found)} networks "
                    f"(bands: {', '.join(sorted(bands)) if bands else '?'} MHz)\n"
                    f"Strongest: " + ", ".join(found[:5]))
            else:
                hint = (" (needs root - the launcher runs it as root)"
                        if os.geteuid() != 0 else "")
                add("Network", tr("network.wifi_scan"), "WARN",
                    tr("network.iw_scan_returned_nothing", v0=first_line(out), v1=hint))
        else:
            add("Network", tr("network.wifi_scan"), "SKIP", tr("network.iw_installed"))
    else:
        add("Network", tr("network.wifi_interface"), "WARN",
            tr("network.wireless_interface_appeared_driver"))

    # Ethernet link
    eths = [i for i in (os.path.basename(p) for p in glob.glob("/sys/class/net/*"))
            if i.startswith(("eth", "enp", "eno", "enx"))]
    for e in eths:
        carrier = read_file(f"/sys/class/net/{e}/carrier")
        speed = read_file(f"/sys/class/net/{e}/speed")
        if carrier == "1":
            add("Network", tr("network.ethernet_link", v0=e), "PASS",
                tr("network.cable_detected_link_speed", v0=speed or '?'))
        else:
            add("Network", tr("network.ethernet_link", v0=e), "INFO",
                tr("network.cable_plugged_plug_one"))

    # Bluetooth
    rc, out = run(["bash", "-c", "lsusb | grep -i bluetooth; ls /sys/class/bluetooth 2>/dev/null"],
                  timeout=20)
    if out.strip() and ("hci" in out or "bluetooth" in out.lower()):
        add("Network", tr("network.bluetooth_adapter"), "PASS",
            first_line(out) if "Bluetooth" in out else f"adapters: {out.strip()}")
    else:
        add("Network", tr("network.bluetooth_adapter"), "WARN",
            tr("network.bluetooth_adapter_detected_verify"))


# ============================================================================
# SECTION 12 - USB ports
# ============================================================================

def sec_usb():
    title(tr("usb.usb_ports_each_port"))

    rc, out = run(["lsusb"])
    lines = [l for l in out.splitlines() if l.strip()]
    add("USB", tr("usb.devices_enumerated"), "PASS",
        tr("usb.usb_device_s_visible", v0=len(lines)))
    for l in lines[:12]:
        info(l.strip())

    rc, out = run(["bash", "-c",
                   "for d in /sys/bus/usb/devices/usb*/; do "
                   "echo \"$(basename $d) $(cat $d/speed 2>/dev/null) "
                   "$(cat $d/product 2>/dev/null)\"; done"], timeout=20)
    roots = [l for l in out.splitlines() if l.strip()]
    info(tr("usb.root_hubs"))
    for r in roots:
        info(tr("main.indent2", v0=r))

    note(tr("usb.test_every_physical_usb"))
    note(tr("usb.plug_usb_stick_mouse"))
    note(tr("usb.device_appear_work"))
    note(tr("usb.device_needs_power_external"))
    if AUTO:
        add("USB", tr("usb.physical_port_test"), "SKIP", tr("usb.needs_human_stick"))
        note(tr("usb.wobbling_loose_port_very"))
        note(tr("usb.gently_wiggling_plugged_device"))
        return

    # Live monitor: watch for connect/disconnect while the user plugs things in
    print()
    if ask(tr("usb.watch_usb_events_while"), "y"):
        note(tr("usb.plug_device_into_each"))
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
                            print(c(tr("usb.connected", v0=line.strip()[:100]), C_GRN))
                        elif "remove" in line:
                            print(c(tr("usb.removed", v0=line.strip()[:100]), C_YEL))
            proc.terminate()
        removals = [e for e in events if " remove " in e]
        adds = [e for e in events if " add " in e]
        add("USB", tr("usb.port_plug_unplug_events"), "PASS",
            tr("usb.connect_disconnect_events_captured", v0=len(adds), v1=len(removals)))
        # Look for ports that drop when untouched (bad connector / power)
        rc, dout = run(["dmesg", "--level=err,warn"], timeout=20)
        usb_err = [l for l in dout.splitlines()
                   if re.search(r"usb.*(error|reset|over-?current|disconnect)", l, re.I)]
        if usb_err:
            add("USB", tr("usb.usb_errors_kernel_log"), "WARN",
                "\n".join(usb_err[:6]) +
                "\nOver-current warnings mean a damaged port - do not ignore")
        else:
            add("USB", tr("usb.usb_errors_kernel_log"), "PASS", tr("usb.usb_errors_logged"))
    else:
        add("USB", tr("usb.physical_port_test"), "SKIP", tr("audio.run"))

    # USB write speed test on the stick itself
    if not AUTO and ask(tr("usb.run_usb_write_read")):
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
            add("USB", tr("usb.stick_write_read_speed"), "PASS", tr("usb.write_read", v0=wspd, v1=rspd))
        else:
            add("USB", tr("usb.stick_write_read_speed"), "SKIP", tr("usb.stick_read_only"))

    # Card reader present?
    rc, out = run(["lspci"])
    if re.search(r"card reader|SD Host|Realtek.*Card", out, re.I):
        add("USB", tr("usb.card_reader"), "INFO",
            tr("usb.card_reader_present_test"))


# ============================================================================
# SECTION 13 - Sensors, thermal, chassis
# ============================================================================

def sec_sensors():
    title(tr("sensors.sensors_fans_chassis"))

    if has("sensors"):
        rc, out = run(["sensors"], timeout=20)
        info(out.strip()[:1800] if out.strip() else "sensors returned nothing")
        temps = parse_sensor_temps(out)
        if temps:
            add("Sensors", tr("sensors.sensor_readout"), "PASS",
                tr("sensors.temperature_readings_hottest_c", v0=len(temps), v1=f"{max(temps):.0f}"))
        else:
            add("Sensors", tr("sensors.sensor_readout"), "WARN",
                tr("sensors.sensors_ran_but_produced"))
    else:
        add("Sensors", tr("sensors.sensor_readout"), "SKIP", tr("sensors.lm_sensors_installed"))

    # ACPI thermal zones
    zones = glob.glob("/sys/class/thermal/thermal_zone*")
    info(tr("sensors.acpi_thermal_zones", v0=len(zones)))
    for z in zones[:8]:
        t = read_file(os.path.join(z, "temp"))
        typ = read_file(os.path.join(z, "type"))
        try:
            info(tr("sensors.c", v0=typ or os.path.basename(z), v1=f"{int(t)/1000:.0f}"))
        except Exception:                                    # noqa: BLE001
            pass

    # Fan presence
    fans = fan_rpms()
    if fans:
        add("Sensors", tr("sensors.fan_tachometer"), "PASS",
            ", ".join(f"{k}={v} rpm" for k, v in fans.items()))
    else:
        add("Sensors", tr("sensors.fan_tachometer"), "INFO",
            tr("sensors.fan_rpm_exposed_verify"))

    # Chassis inspection checklist
    print()
    note(tr("sensors.now_look_machine_itself"))
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
                add("Chassis", tr("sensors.physical_inspection"), "WARN", tr("sensors.issue", v0=q))
    if not any(r["section"] == "Chassis" for r in RESULTS):
        add("Chassis", tr("sensors.physical_inspection"), "PASS",
            "All physical checks looked clean" if not AUTO
            else "Manual inspection recommended")

    # Swollen battery is a hard fail
    if not AUTO and ask(tr("sensors.there_sign_swollen_battery")):
        add("Chassis", tr("sensors.battery_swelling"), "FAIL",
            tr("sensors.swollen_battery_fire_hazard"))


# ============================================================================
# SECTION 14 - Extra hardware
# ============================================================================

def sec_extras():
    title(tr("extras.other_hardware_fingerprint_card"))

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
        add("Extras", tr("extras.tpm_sysfs"), "PASS",
            tr("extras.tpm_device_s", v0=len(glob.glob('/sys/class/tpm/tpm*'))))

    probe("Card reader", r"card reader|SD Host|Ricoh|Realtek.*Card", pci,
          "Card reader present", "No card reader")

    probe("HDMI / display output",
          r"HDMI|DisplayPort|VGA", run(["bash", "-c", "xrandr --query"])[1],
          "External display connector(s) detected",
          "No external display output detected by xrandr")

    # Thunderbolt / USB-C
    if glob.glob("/sys/bus/thunderbolt/devices/*"):
        add("Extras", tr("extras.thunderbolt"), "PASS",
            tr("extras.device_s", v0=len(glob.glob('/sys/bus/thunderbolt/devices/*'))))
    elif re.search(r"USB4|Thunderbolt", pci, re.I):
        add("Extras", tr("extras.thunderbolt"), "PASS", tr("extras.controller_present_pci_bus"))

    # Speakers / audio codec already covered; check for a DVD drive
    if glob.glob("/dev/sr*"):
        add("Extras", tr("extras.optical_drive"), "PASS", tr("extras.dvd_cd_drive_present"))

    # Webcam privacy shutter, mic array holes - visual only
    if not AUTO:
        print()
        note(tr("extras.test_these_by_hand"))
        if ask(tr("extras.there_dead_silent_keys"), "y"):
            add("Extras", tr("extras.keycaps_complete"), "PASS", tr("extras.missing_keycaps"))
        else:
            add("Extras", tr("extras.keycaps_complete"), "WARN", tr("extras.missing_damaged_keycaps"))
        if ask(tr("extras.microphone_grill_look_intact"), "y"):
            add("Extras", tr("extras.mic_grill"), "PASS", tr("extras.mic_grill_intact"))
        else:
            add("Extras", tr("extras.mic_grill"), "WARN", tr("extras.mic_grill_damaged_blocked"))


# ============================================================================
# REPORT
# ============================================================================

def verdict_counts():
    cts = {}
    for r in RESULTS:
        cts[r["status"]] = cts.get(r["status"], 0) + 1
    return cts


def overall_verdict():
    """Return (verdict_id, colour_class, failures, warnings).

    The id is translated at display time, so no logic depends on English text.
    """
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    warns = [r for r in RESULTS if r["status"] == "WARN"]
    if fails:
        return ("fail", "FAIL", fails, warns)
    if len(warns) >= 4:
        return ("caution", "WARN", fails, warns)
    if warns:
        return ("minor", "WARN", fails, warns)
    return ("good", "PASS", fails, warns)


def render_txt():
    lines = []
    app = f"{sys_vendor()} {product_name()}".strip()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    sw = i18n.status_width()

    lines.append("=" * 78)
    lines.append(tr("report.title_line", v0=VERSION))
    lines.append("=" * 78)
    lines.append(tr("report.machine", v0=app))
    lines.append(tr("report.serial", v0=dmi("product_serial") or tr("report_html.na")))
    lines.append(tr("report.bios", v0=dmi("bios_version"), v1=dmi("bios_date")))
    lines.append(tr("report.cpu", v0=cpu_model()))
    lines.append(tr("report.ram_gib", v0=f"{mem_total_gb():.2f} GiB"))
    lines.append(tr("report.tested", v0=ts))
    lines.append(tr("report.kernel", v0=platform.release()))
    lines.append("")

    vid, cls, fails, warns = overall_verdict()
    lines.append(tr("report.verdict", v0=tr(f"verdict.{vid}")))
    cts = verdict_counts()
    lines.append(tr("report.counts", v0=", ".join(
        f"{status_label(k)}={n}" for k, n in sorted(cts.items()))))
    lines.append("")

    sec = None
    for r in RESULTS:
        if r["section"] != sec:
            sec = r["section"]
            lines.append("")
            lines.append("-" * 78)
            lines.append(tr("report.section_header", v0=sec_label(sec).upper()))
            lines.append("-" * 78)
        lines.append(tr("report.status_line",
                       v0=f"{status_label(r['status']):<{sw}}",
                       v1=r["name"]))
        for ln in r["detail"].splitlines():
            lines.append(tr("report.detail_line", v0=ln))

    lines.append("")
    lines.append("=" * 78)
    lines.append(tr("report.decision_guide"))
    lines.append("=" * 78)
    if fails:
        lines.append(tr("report.hard_failures_found_these"))
        lines.append(tr("report.fix_by_reinstalling_software"))
        for f in fails:
            lines.append(tr("report.fail_bullet",
                           v0=sec_label(f["section"]), v1=f["name"]))
            if f["detail"]:
                lines.append(tr("report.fail_detail_more",
                               v0=f["detail"].splitlines()[0]))
    else:
        lines.append(tr("report.hard_failures_detected"))
    if warns:
        lines.append("")
        lines.append(tr("report.warnings_things_renegotiate"))
        for w in warns:
            lines.append(tr("report.fail_bullet_detail",
                           v0=sec_label(w["section"]), v1=w["name"],
                           v2=w["detail"].splitlines()[0] if w["detail"] else ""))
    lines.append("")
    lines.append(tr("report.reminder_suite_cannot_fully"))
    lines.append(tr("report.usb_boot_menu_nor"))
    lines.append("=" * 78)
    return "\n".join(lines)


def render_html():
    vid, cls, fails, warns = overall_verdict()
    color = {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#cf222e",
             "INFO": "#0969da", "SKIP": "#6e7781"}[cls]
    cts = verdict_counts()
    direction = ' dir="rtl"' if i18n.is_rtl() else ""

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    rows = []
    sec = None
    for r in RESULTS:
        if r["section"] != sec:
            sec = r["section"]
            rows.append(f'<tr class="sec"><td colspan="2">{esc(sec_label(sec))}</td></tr>')
        det = esc(r["detail"]).replace("\n", "<br>")
        rows.append(
            f'<tr><td class="st" style="color:{color_of(r["status"])}">'
            f'{esc(status_label(r["status"]))}</td>'
            f'<td><b>{esc(r["name"])}</b>'
            + (f'<div class="det">{det}</div>' if det else "")
            + "</td></tr>")

    fail_list = "".join(
        f"<li><b>{esc(sec_label(f['section']))}</b> - {esc(f['name'])}: "
        f"{esc(f['detail'].splitlines()[0] if f['detail'] else '')}</li>" for f in fails)
    warn_list = "".join(
        f"<li><b>{esc(sec_label(w['section']))}</b> - {esc(w['name'])}: "
        f"{esc(w['detail'].splitlines()[0] if w['detail'] else '')}</li>" for w in warns)

    # Plain string, not an f-string: CSS is full of braces, and we only need
    # two colour substitutions, done with replace().
    css = """
 body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans Arabic", sans-serif;
        margin: 0; padding: 24px; background: #f6f8fa; color: #1f2328; line-height: 1.5; }
 .wrap { max-width: 1000px; margin: 0 auto; }
 h1 { font-size: 22px; margin: 0 0 4px; }
 .sub { color: #59636e; margin-bottom: 18px; font-size: 14px; }
 .verdict { background: #fff; border-inline-start: 6px solid @COLOR@; border-radius: 6px;
            padding: 16px 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
 .verdict h2 { margin: 0 0 6px; color: @COLOR@; font-size: 19px; }
 .meta { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr));
         gap: 10px; background: #fff; padding: 16px 20px; border-radius: 6px;
         box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 20px; font-size: 14px; }
 .meta b { display: block; color: #59636e; font-weight: 600; font-size: 12px;
           text-transform: uppercase; letter-spacing: .04em; }
 table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px;
         overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); font-size: 14px; }
 td { padding: 9px 14px; border-bottom: 1px solid #e6e9ed; vertical-align: top; }
 .st { font-weight: 700; font-size: 11px; letter-spacing: .05em; white-space: nowrap;
       width: 70px; }
 tr.sec td { background: #eef1f5; font-weight: 700; font-size: 12px;
             text-transform: uppercase; letter-spacing: .06em; color: #404854; }
 .det { color: #59636e; font-size: 13px; margin-top: 3px; }
 ul { background: #fff; padding: 16px 20px 16px 40px; border-radius: 6px;
      box-shadow: 0 1px 3px rgba(0,0,0,.06); font-size: 14px; }
 li { margin-bottom: 5px; }
 .foot { margin-top: 22px; color: #59636e; font-size: 12px; }
 .chips span { display:inline-block; padding:2px 9px; border-radius:10px;
               background:#eef1f5; margin-inline-end:6px; font-size:12px; }
""".replace("@COLOR@", color)

    chips = "".join(f"<span>{esc(status_label(k))}: {n}</span>"
                    for k, n in sorted(cts.items()))
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        "<!DOCTYPE html>",
        f'<html lang="{i18n.LANG}"{direction}><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(tr('report_html.title'))} - {esc(sys_vendor())} "
        f"{esc(product_name())}</title>",
        f"<style>{css}</style></head><body><div class=\"wrap\">",
        f" <h1>{esc(tr('report_html.title'))}</h1>",
        f" <div class=\"sub\">{esc(tr('report_html.subtitle', v0=VERSION, v1=ts))}</div>",
        ' <div class="verdict">',
        f"   <h2>{esc(tr(f'verdict.{vid}'))}</h2>",
        f"   <div class=\"chips\">{chips}</div>",
        " </div>",
        ' <div class="meta">',
        f"   <div><b>{esc(tr('report_html.machine'))}</b>"
        f"{esc(sys_vendor())} {esc(product_name())}</div>",
        f"   <div><b>{esc(tr('report_html.serial'))}</b>"
        f"{esc(dmi('product_serial') or tr('report_html.na'))}</div>",
        f"   <div><b>{esc(tr('report_html.bios'))}</b>"
        f"{esc(dmi('bios_version'))} ({esc(dmi('bios_date'))})</div>",
        f"   <div><b>{esc(tr('report_html.cpu'))}</b>{esc(cpu_model())}</div>",
        f"   <div><b>{esc(tr('report_html.ram'))}</b>{mem_total_gb():.2f} GiB</div>",
        f"   <div><b>{esc(tr('report_html.kernel'))}</b>{esc(platform.release())}</div>",
        " </div>",
    ]
    if fails:
        parts.append(f" <h2 style=\"font-size:16px\">"
                     f"{esc(tr('report_html.hard_failures'))}</h2>")
        parts.append(f" <ul>{fail_list}</ul>")
    if warns:
        parts.append(f" <h2 style=\"font-size:16px\">"
                     f"{esc(tr('report_html.warnings'))}</h2>")
        parts.append(f" <ul>{warn_list}</ul>")
    parts.append(f" <h2 style=\"font-size:16px\">"
                 f"{esc(tr('report_html.full_results'))}</h2>")
    parts.append(f" <table>{''.join(rows)}</table>")
    parts.append(' <div class="foot">')
    parts.append(f"   {esc(tr('report_html.footer_note'))}<br>")
    parts.append(f"   {esc(tr('report_html.footer_note2'))}")
    parts.append(" </div>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


def color_of(status):
    return {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#cf222e",
            "INFO": "#0969da", "SKIP": "#6e7781"}.get(status, "#59636e")


def write_report():
    global REPORT_DIR
    if REPORT_DIR is None:
        setup_report_dir()
    txt = render_txt()
    html = render_html()
    vid = overall_verdict()[0]
    files = {
        "report.txt": txt,
        "report.html": html,
        "report.json": json.dumps({
            "version": VERSION,
            "language": i18n.LANG,
            "timestamp": datetime.datetime.now().isoformat(),
            "machine": {"vendor": sys_vendor(), "model": product_name(),
                        "serial": dmi("product_serial"),
                        "bios": dmi("bios_version"), "bios_date": dmi("bios_date"),
                        "cpu": cpu_model(), "ram_gib": round(mem_total_gb(), 2),
                        "kernel": platform.release()},
            "results": RESULTS,
            "verdict_id": vid,
            "verdict": tr(f"verdict.{vid}"),
            "counts": verdict_counts(),
        }, indent=2, ensure_ascii=False),
    }
    for name, content in files.items():
        with open(os.path.join(REPORT_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(content)
    for a in ARTIFACTS:
        try:
            shutil.copy2(a, REPORT_DIR)
        except Exception:                                    # noqa: BLE001
            pass
    return REPORT_DIR


def print_summary():
    title(tr("summary.summary"))
    v, cls, fails, warns = overall_verdict()
    cc = {"PASS": C_GRN, "WARN": C_YEL, "FAIL": C_RED}.get(cls, C_B)
    print()
    print(c(tr(f"verdict.{v}"), cc + C_B))
    print()
    cts = verdict_counts()
    info(", ".join(f"{status_label(k)}: {n}" for k, n in sorted(cts.items())))
    print()
    if fails:
        print(c(tr("summary.hard_failures_physical_faults"), C_RED))
        for f in fails:
            info(tr("summary.fail_bullet", v0=sec_label(f["section"]), v1=f["name"]))
            if f["detail"]:
                note(tr("summary.detail_line", v0=f["detail"].splitlines()[0]))
        print()
    if warns:
        print(c(tr("summary.warnings_negotiate_price_plan"), C_YEL))
        for w in warns:
            info(tr("summary.fail_bullet", v0=sec_label(w["section"]), v1=w["name"]))
            if w["detail"]:
                note(tr("summary.detail_line", v0=w["detail"].splitlines()[0]))
        print()
    note(tr("summary.reminder_run_memtest86_usb"))
    note(tr("summary.verify_storage_surface_if"))


# ============================================================================
# MAIN
# ============================================================================

def preflight():
    global KIT_ROOT, REPORT_DIR
    os.makedirs(SCRATCH, exist_ok=True)

    # Choose the language before printing anything, so even the banner comes
    # out in the right language.
    if not AUTO and sys.stdin.isatty():
        i18n.choose_language_interactive(i18n.LANG)

    KIT_ROOT = find_kit_root()

    root = os.geteuid() == 0
    print()
    hr()
    print(c("   " + tr("main.banner", v0=VERSION), C_B + C_CYA))
    hr()
    info(tr("main.running_as",
           v0=tr("main.as_root") if root else tr("main.as_user"))
         + ("" if root else c(tr("main.sudo_hint"), C_RED)))
    info(tr("main.usb_kit_root", v0=KIT_ROOT or c(tr("main.kit_not_found"), C_YEL)))
    info(tr("main.scratch_dir", v0=SCRATCH))
    info(tr("main.mode",
           v0=tr("main.mode_quick") if QUICK else tr("main.mode_full"),
           v1=tr("main.mode_auto") if AUTO else ""))
    print()
    if not root:
        note(tr("main.without_root_dmidecode_smartctl"))

    if not QUICK and not AUTO:
        print()
        note(tr("main.takes_minutes_thorough_machine"))
        note(tr("main.memtester_disk_read_test"))
        note(tr("main.everything_else_few_minutes"))
        pause(tr("main.press_enter_start"))


def main():
    global QUICK, AUTO, REPORT_OVERRIDE
    ap = argparse.ArgumentParser(description="Used-laptop hardware verification suite")
    ap.add_argument("--quick", action="store_true", help="skip the slow deep tests")
    ap.add_argument("--auto", action="store_true",
                    help="no prompts: automatic checks only (for a fast screening)")
    ap.add_argument("--lang", default=None, choices=list(i18n.LANGUAGES),
                    help="interface language: en, fr, ar "
                         "(default: $HWCHECK_LANG, then the system locale)")
    ap.add_argument("--report-dir", default=None,
                    help="where to write the report (default: <usb>/report, "
                         "falling back to ~/hwcheck-report then /tmp)")
    ap.add_argument("--sections", default="",
                    help="comma list: identity,cpu,memory,storage,battery,display,"
                         "audio,keyboard,pointer,camera,network,usb,sensors,extras")
    args = ap.parse_args()
    QUICK, AUTO = args.quick, args.auto

    REPORT_OVERRIDE = args.report_dir
    i18n.detect_language(args.lang)

    preflight()

    wanted = set(s.strip() for s in args.sections.split(",") if s.strip())

    def want(name):
        return not wanted or name in wanted

    # (selector, internal section id, function)
    plan = [
        ("identity", "Identity", sec_identity),
        ("cpu", "CPU", sec_cpu),
        ("memory", "RAM", sec_memory),
        ("storage", "Storage", sec_storage),
        ("battery", "Battery", sec_battery),
        ("display", "Display", sec_display),
        ("audio", "Audio", sec_audio),
        ("keyboard", "Input", sec_keyboard),
        ("pointer", "Pointer", sec_pointer),
        ("camera", "Camera", sec_camera),
        ("network", "Network", sec_network),
        ("usb", "USB", sec_usb),
        ("sensors", "Sensors", sec_sensors),
        ("extras", "Extras", sec_extras),
    ]

    try:
        for name, sec_id, fn in plan:
            if not want(name):
                continue
            try:
                fn()
            except KeyboardInterrupt:
                print()
                if not ask(tr("main.skip_rest_section"), "y"):
                    raise
            except Exception as e:                           # noqa: BLE001
                add(sec_id, tr("main.section_error", v0=name), "WARN",
                    tr("display.output_line", v0=type(e).__name__, v1=e))
    except KeyboardInterrupt:
        print()
        note(tr("main.interrupted_writing_report_what"))

    print_summary()
    path = write_report()
    title(tr("main.report_saved"))
    info(tr("main.report_html_open_browser", v0=path))
    info(tr("main.report_txt_plain_text", v0=path))
    info(tr("main.report_json_machine_readable", v0=path))
    print()
    note(tr("main.keep_usb_stick_if"))
    note(tr("main.machine_s_condition_day"))
    print()


# NOTE: the module's own `if __name__ == "__main__": main()` block is preserved
# by replace_func, so this body must NOT add a second one - that would run the
# whole suite twice.


if __name__ == "__main__":
    main()
