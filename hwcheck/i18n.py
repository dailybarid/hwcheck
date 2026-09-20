#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i18n for HWCheck
================

Three languages ship with the kit: English (source), French, Arabic.

The message catalogs live in lang/<code>.py and are plain dicts:

    MESSAGES  key -> template containing {v0}... placeholders
    SECTIONS  internal section id -> display label   (e.g. "Storage" -> "Stockage")
    STATUSES  internal verdict id -> display label   (e.g. "FAIL"    -> "ÉCHEC")

Why {vN} markers and str.replace() instead of str.format():
the HTML report template is full of CSS braces; str.format() would try to
interpret them and explode. Looking up exact "{v0}" tokens and replacing them
is safe against any other brace in the text, and lets a translator move a
placeholder anywhere in the sentence.

Fallback chain: requested language -> English -> the key itself. A missing
translation never crashes the run; it just shows English.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

LANGUAGES = ("en", "fr", "ar")

LANGUAGE_NAMES = {
    "en": "English",
    "fr": "Français",
    "ar": "العربية",
}

LANG = "en"
_CAT = {}

# Internal ids -> display. Section names and status codes are used as logic
# keys throughout the program, so they must never be translated in place,
# only re-rendered at display time.
_SECTIONS = {}
_STATUSES = {}


def _load(code):
    """Import lang.<code> and return (messages, sections, statuses)."""
    try:
        mod = __import__(f"lang.{code}", fromlist=["MESSAGES"])
    except Exception as exc:                                  # noqa: BLE001
        if code != "en":
            sys.stderr.write(f"i18n: cannot load language '{code}': {exc}\n")
        return None
    return (getattr(mod, "MESSAGES", {}),
            getattr(mod, "SECTIONS", {}),
            getattr(mod, "STATUSES", {}))


def set_language(code):
    """Activate a language. Falls back to English if the catalog is broken."""
    global LANG, _CAT, _SECTIONS, _STATUSES
    code = (code or "en").split(".")[0].split("_")[0].lower()
    if code not in LANGUAGES:
        code = "en"

    en = _load("en")
    if en is None:
        raise RuntimeError("lang/en.py is missing - the kit is incomplete")
    en_msgs, en_secs, en_stats = en

    if code == "en":
        _CAT = dict(en_msgs)
        _SECTIONS = dict(en_secs)
        _STATUSES = dict(en_stats)
    else:
        other = _load(code)
        if other is None:
            code = "en"
            _CAT, _SECTIONS, _STATUSES = dict(en_msgs), dict(en_secs), dict(en_stats)
        else:
            o_msgs, o_secs, o_stats = other
            merged = dict(en_msgs)
            merged.update({k: v for k, v in o_msgs.items() if v})
            _CAT = merged
            _SECTIONS = dict(en_secs)
            _SECTIONS.update({k: v for k, v in o_secs.items() if v})
            _STATUSES = dict(en_stats)
            _STATUSES.update({k: v for k, v in o_stats.items() if v})

    LANG = code
    return LANG


def detect_language(explicit=None):
    """--lang > HWCHECK_LANG > system locale > English."""
    if explicit:
        return set_language(explicit)
    env = os.environ.get("HWCHECK_LANG")
    if env:
        return set_language(env)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            code = val.split(".")[0].split("_")[0].split("@")[0].lower()
            if code in LANGUAGES:
                return set_language(code)
    return set_language("en")


def is_rtl():
    return LANG == "ar"


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def tr(key, **values):
    """Translate `key`, filling {vN} placeholders from `values`.

    NOTE: deliberately NOT called t(). The checker functions assign to local
    variables named `t` (temp_c() results, thread handles in load_cpu()), which
    would shadow the translator and raise UnboundLocalError at the call site.
    

    Unknown keys return the key itself so a missing entry is visible but
    never fatal.
    """
    tmpl = _CAT.get(key)
    if tmpl is None:
        return key
    if not values:
        return tmpl
    for name, val in values.items():
        token = "{" + name + "}"
        if token in tmpl:
            tmpl = tmpl.replace(token, "" if val is None else str(val))
    return tmpl


def raw(key):
    """The untranslated template - used by the translator tooling."""
    return _CAT.get(key, key)


def section(name):
    """Display label for an internal section id."""
    return _SECTIONS.get(name, name)


def status(code):
    """Display label for an internal status code (PASS/FAIL/WARN/INFO/SKIP)."""
    return _STATUSES.get(code, code)


def status_width():
    """Widest translated status label, for column alignment."""
    return max([len(v) for v in _STATUSES.values()] + [4])


def language_name(code=None):
    return LANGUAGE_NAMES.get(code or LANG, code or LANG)


# ---------------------------------------------------------------------------
# Interactive picker
# ---------------------------------------------------------------------------

_SUGGESTED = {
    "fr": "1",
    "ar": "2",
    "en": "3",
}


def choose_language_interactive(current):
    """Ask the user which language to use. Returns the chosen code."""
    print()
    print("  " + "-" * 66)
    print("   Language / Langue / اللغة")
    print("  " + "-" * 66)
    order = [("en", "1"), ("fr", "2"), ("ar", "3")]
    for code, num in order:
        mark = " *" if code == current else ""
        print(f"     {num}) {LANGUAGE_NAMES[code]}{mark}")
    print()
    try:
        ans = input(f"   [{LANGUAGE_NAMES[current]}] > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return current
    for code, num in order:
        if ans == num or ans.lower() == code or ans == LANGUAGE_NAMES[code]:
            return set_language(code)
    return current