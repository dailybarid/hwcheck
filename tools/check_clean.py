#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_clean.py - refuse to publish anything that leaks the developer's machine
================================================================================

A hardware-reporting tool is unusually easy to leak with: its own test output
contains serial numbers, BIOS versions, MAC addresses, disk models and battery
data from whatever machine it was last run on. This script scans every file
that would be committed and fails if it finds anything machine-specific.

    python3 tools/check_clean.py            # scan, exit 1 on findings
    python3 tools/check_clean.py --verbose  # also list every file scanned

What counts as a leak:
  * absolute home paths (/home/<someone>, /Users/<someone>, C:\\Users\\<someone>)
  * MAC addresses
  * IPv4 addresses that are not clearly documentation/loopback
  * e-mail addresses
  * machine serials / product ids / BIOS strings from a real test run
  * the developer's hostname
  * common DMI fingerprints (LENOVO 20HE..., ThinkPad serial patterns)

Add your own machine's fingerprints to FORBIDDEN below before publishing.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories that are never committed (see .gitignore) - skipped entirely.
# NOTE: "build" is deliberately NOT skipped - hwcheck/build/hwcheck_english.py
# is committed, so it must be scanned like any other source file.
SKIP_DIRS = {
    ".git", "__pycache__", "iso", "ventoy", "debs", "report",
    "hwcheck-report", ".venv", "venv", "node_modules",
}

SKIP_FILE_SUFFIXES = (".deb", ".iso", ".img", ".pyc", ".gz", ".zip")

# Binary-ish files we should not try to decode.
SKIP_NAMES = {"check_clean.py"}          # this file documents the patterns itself

# ---------------------------------------------------------------------------
# Machine-specific fingerprints. Keep this list generic (patterns that match
# ANY real machine) plus any literal values from your own laptop.
# ---------------------------------------------------------------------------
FORBIDDEN = [
    (r"/home/(?!user\b|username\b|example-user\b)[a-z][a-z0-9_-]{1,30}/",
     "absolute home path with a real username"),
    (r"/Users/[A-Za-z][A-Za-z0-9_-]{1,30}/",
     "absolute macOS home path"),
    (r"C:\\\\Users\\\\[A-Za-z][A-Za-z0-9_-]{1,30}",
     "absolute Windows home path"),
    (r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b",
     "MAC address"),
    # Dotted quads only. An earlier version of this pattern matched bare
    # numbers like "2026" and flooded the report with false positives.
    (r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])",
     "dotted IPv4 address"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
     "e-mail address"),
    # DMI / firmware fingerprints from a real report
    (r"\bLENOVO\s+20[A-Z0-9]{8}\b", "Lenovo product id from a real machine"),
    (r"\b(?:ThinkPad|Latitude|EliteBook|ProBook)\s+[A-Z0-9]{2,}[0-9]{2,}\b",
     "vendor model string from a real machine"),
    (r"\bN1QET[A-Z0-9]+\b", "Lenovo BIOS version string"),
    (r"\bKXG[0-9A-Z]{6,}\b", "real NVMe model number"),
    (r"\b0[1-9][A-Z]{2}[0-9]{3}\b", "Lenovo battery part number"),
    (r"\b[Ss]erial\s*[:=]\s*[A-Z0-9]{6,}", "serial number assignment"),
]

# These appear legitimately in docs/source; allow them through.
ALLOW = [
    r"192\.0\.2\.\d+",          # RFC 5737 documentation range
    r"198\.51\.100\.\d+",
    r"203\.0\.113\.\d+",
    r"127\.0\.0\.1",
    r"0\.0\.0\.0",
    r"10\.0\.2\.\d+",
    r"/home/example-user/",
    r"user@example\.(com|org)",
    r"noreply@",
]

# Lines containing these are about the pattern itself, not a leak.
SELF_REFERENTIAL = (
    "FORBIDDEN = [",
    "absolute home path",
    "MAC address",
    "e-mail address",
    "serial number assignment",
    "vendor model string",
    "private IPv4",
)


def iter_files(verbose=False):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(SKIP_FILE_SUFFIXES) or name in SKIP_NAMES:
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT)
            if verbose:
                print("   scanning", rel)
            yield path, rel


def main():
    verbose = "--verbose" in sys.argv
    findings = []
    scanned = 0

    compiled = [(re.compile(p), why) for p, why in FORBIDDEN]
    allowed = [re.compile(p) for p in ALLOW]

    for path, rel in iter_files(verbose):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:                                    # noqa: BLE001
            continue
        if "\x00" in text[:2000]:
            continue
        scanned += 1

        for lineno, line in enumerate(text.splitlines(), 1):
            if any(s in line for s in SELF_REFERENTIAL):
                continue
            if any(a.search(line) for a in allowed):
                continue
            for rx, why in compiled:
                m = rx.search(line)
                if m:
                    findings.append((rel, lineno, why, m.group(0)[:60], line.strip()[:100]))

    print(f"scanned {scanned} files under {ROOT}")
    if not findings:
        print("CLEAN - no machine-specific or personal data found.")
        print("Safe to publish.")
        return 0

    print(f"\n{len(findings)} potential leak(s):\n")
    for rel, lineno, why, hit, line in findings:
        print(f"  {rel}:{lineno}")
        print(f"      {why}")
        print(f"      matched: {hit!r}")
        print(f"      line   : {line}")
        print()
    print("Remove or genericise these before publishing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())