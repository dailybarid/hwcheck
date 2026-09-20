#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_translations.py - validate every message catalog
======================================================

Run before committing a translation:

    python3 tools/check_translations.py
    python3 tools/check_translations.py --lang fr     # one language only

Hard failures (exit 1):
  * a key in en.py missing from the translation, or an extra invented key
  * a placeholder set that differs per key ({v0} dropped, renamed or invented)
  * section/status ids missing or extra
  * an empty translation

Warnings (exit 0):
  * a string identical to the English one - sometimes legitimate (keys that are
    just "CPU", "RAM", "USB"), so these are listed for a human to skim
  * a translation much longer than the English one, which will wrap badly in a
    74-column terminal
"""

import os
import re
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
LANGDIR = os.path.join(os.path.dirname(HERE), "hwcheck", "lang")
PLACEHOLDER = re.compile(r"\{v\d+\}")

# Strings that are legitimately identical to English in any language.
SKIP_IDENTICAL = {
    "main.as_root", "main.mode_quick", "main.mode_full",
    "report_html.bios", "report_html.cpu", "report_html.ram",
    "report_html.kernel", "report_html.serial", "report_html.machine",
    "verdict.fail", "verdict.caution", "verdict.minor", "verdict.good",
    "audio.distortion", "identity.dmi_readable_vendor_data",
}


def load(name):
    path = os.path.join(LANGDIR, name)
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(f"_lang_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def placeholders(text):
    return sorted(PLACEHOLDER.findall(text or ""))


def check(code, en):
    other = load(f"{code}.py")
    if other is None:
        print(f"\n{code}: MISSING FILE {LANGDIR}/{code}.py")
        return 1

    errors, warns = [], []

    for attr in ("MESSAGES", "SECTIONS", "STATUSES"):
        base = getattr(en, attr, {})
        theirs = getattr(other, attr, None)
        if theirs is None:
            errors.append(f"{attr} dict is missing entirely")
            continue
        missing = sorted(set(base) - set(theirs))
        extra = sorted(set(theirs) - set(base))
        if missing:
            errors.append(f"{attr}: {len(missing)} missing key(s): {missing[:8]}")
        if extra:
            errors.append(f"{attr}: {len(extra)} unknown key(s): {extra[:8]}")

        for key in sorted(set(base) & set(theirs)):
            val = theirs[key]
            if not isinstance(val, str) or not val.strip():
                errors.append(f"{attr}[{key}] is empty")
                continue
            if attr != "MESSAGES":
                continue
            want, got = placeholders(base[key]), placeholders(val)
            if want != got:
                errors.append(
                    f"{key}: placeholders differ\n"
                    f"      expected {want}\n"
                    f"      got      {got}")

    # soft checks
    for key, val in sorted(getattr(other, "MESSAGES", {}).items()):
        eng = getattr(en, "MESSAGES", {}).get(key)
        if eng and val == eng and key not in SKIP_IDENTICAL:
            warns.append(f"identical to English: {key}")
        if eng and len(val) > max(96, len(eng) * 1.8):
            warns.append(f"much longer than English ({len(val)} vs {len(eng)}): {key}")

    n = len(getattr(other, "MESSAGES", {}))
    if errors:
        print(f"\n{code}: {len(errors)} problem(s)  [{n} messages]")
        for e in errors:
            print(f"   ERROR {e}")
    else:
        print(f"\n{code}: OK  [{n} messages, "
              f"{len(getattr(other, 'SECTIONS', {}))} sections, "
              f"{len(getattr(other, 'STATUSES', {}))} statuses]")

    untranslated = [w for w in warns if w.startswith("identical")]
    if untranslated:
        print(f"   {len(untranslated)} string(s) identical to English "
              f"(fine if they are acronyms like CPU/RAM/USB):")
        for w in untranslated[:15]:
            print(f"     {w.split(': ', 1)[1]}")
        if len(untranslated) > 15:
            print(f"     ... and {len(untranslated) - 15} more")
    longish = [w for w in warns if w.startswith("much longer")]
    if longish:
        print(f"   {len(longish)} string(s) much longer than the English - "
              f"check terminal wrapping:")
        for w in longish[:6]:
            print(f"     {w.split(': ', 1)[1]}")

    return 1 if errors else 0


def main():
    want = None
    if "--lang" in sys.argv:
        i = sys.argv.index("--lang")
        want = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    en = load("en.py")
    if en is None:
        print(f"FATAL: {LANGDIR}/en.py not found")
        return 1

    codes = [want] if want else ["fr"]
    for extra in sorted(os.listdir(LANGDIR)):
        m = re.fullmatch(r"([a-z]{2})\.py", extra)
        if m and m.group(1) != "en" and m.group(1) not in codes:
            codes.append(m.group(1))

    print(f"reference: en.py [{len(en.MESSAGES)} messages]")
    rc = 0
    for code in codes:
        rc |= check(code, en)

    print()
    if rc == 0:
        print("All catalogs valid.")
    else:
        print("Problems found - fix the ERROR lines above before committing.")
    return rc


if __name__ == "__main__":
    sys.exit(main())