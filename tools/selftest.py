#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py - load the real i18n layer and exercise it
=====================================================

`check_translations.py` compares the catalogs as DATA. This imports the actual
i18n module the program uses, activates every language, and checks running
behaviour: the module imports, catalogs populate, labels resolve, placeholder
substitution leaves nothing behind, unknown keys degrade safely, and the RTL
flag follows the language.

The overlap with check_translations is deliberate - the two fail for different
reasons. check_translations catches a catalog that is wrong as data. This
catches a catalog that looks fine as data but does not survive being loaded and
used: a syntax error, a broken import, a regression in i18n.py itself, or a
catalog that silently fails to merge and leaves the program in English.

(Note: an invented placeholder in a translation - e.g. French referencing {v9}
where English supplies none - is caught by BOTH tools, because placeholder sets
per key no longer match. That is not what makes this one worth having.)

    python3 tools/selftest.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "hwcheck")

sys.path.insert(0, KIT)
import i18n                                                    # noqa: E402

PLACEHOLDER = re.compile(r"\{v(\d+)\}")
LEFTOVER = re.compile(r"\{v\d+\}")

failures = []


def check(code):
    i18n.set_language(code)
    problems = []

    # 1. the module actually produced a usable catalog
    if not i18n._CAT:
        problems.append("catalog is empty after set_language()")

    # 2. every key, filled with exactly what the ENGLISH template declares
    for key, en_tmpl in sorted(i18n._load("en")[0].items()):
        supplied = {f"v{n}": f"<{n}>" for n in PLACEHOLDER.findall(en_tmpl)}
        out = i18n.tr(key, **supplied)
        left = LEFTOVER.findall(out)
        if left:
            problems.append(
                f"{key}: {left} left unsubstituted - this catalog references "
                f"a placeholder English does not supply (English has "
                f"{sorted(supplied) or 'none'})")

    # 3. labels must resolve, not fall through to the raw id.
    #    Checked as a group: keeping an occasional label identical to its id is
    #    legitimate (acronyms like INFO), but all five being identical means the
    #    map never loaded.
    codes = ("PASS", "FAIL", "WARN", "INFO", "SKIP")
    if code != "en" and all(i18n.status(c) == c for c in codes):
        problems.append("status map did not load - every label equals its id")
    for sec in ("Identity", "Storage", "Display", "Network"):
        if not i18n.section(sec):
            problems.append(f"section {sec} did not resolve")

    # 4. an unknown key must be visibly wrong, not crash
    if i18n.tr("no.such.key") != "no.such.key":
        problems.append("unknown key handling changed")

    # 5. RTL flag
    if i18n.is_rtl() != (code == "ar"):
        problems.append(f"is_rtl() wrong for {code}")

    return problems


def main():
    print("languages:", ", ".join(i18n.LANGUAGES))
    for code in i18n.LANGUAGES:
        problems = check(code)
        n = len(i18n._CAT)
        if problems:
            print(f"\n{code}: {len(problems)} problem(s)  [{n} messages]")
            for p in problems:
                print(f"   ERROR {p}")
            failures.extend(problems)
        else:
            print(f"\n{code}: OK  [{n} messages, placeholders all fillable, "
                  f"labels resolve, rtl={i18n.is_rtl()}]")

    print()
    if failures:
        print(f"{len(failures)} problem(s) found - fix before releasing.")
        return 1
    print("Self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
