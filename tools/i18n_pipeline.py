#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i18n build pipeline for HWCheck  (build-time tool, NOT shipped to users)
=======================================================================

Run from the hwcheck directory:

    python3 ../../tools/i18n_pipeline.py

It turns the plain-English source into an i18n-ready program + catalog:

    hwcheck.py  (English call sites)
      -> build/hwcheck_i18n.py   every display string routed through t("key")
      -> lang/en.py              the message catalog (source language)
      -> hwcheck.py              the final program

Step 1 AUTOMATIC - AST + exact source-span surgery (never ast.unparse, which
would reflow the whole file and destroy comments). Every display string becomes
t("key", vN=...) and identical English strings share one key.

  CRITICAL PITFALL: in Python 3.11 an f-string's `format_spec` JoinedStr node
  inherits the POSITION OF THE ENCLOSING F-STRING. ast.get_source_segment() on
  it therefore returns the entire literal, not ".0f", and the generated code
  ends up as the garbage `'{expr:f"whole literal"}'`. Rebuild the spec from the
  node's own Constant children instead.

Step 2 MANUAL - renames for the few keys the auto-namer could only call "m_",
plus the handful of strings it cannot see (strings inside f-string
expressions, tuple returns, CSS-bearing HTML templates).

Step 3 INTEGRATION - the display helpers, section/status localisation, the
report renderers, language selection.
"""

import ast
import os
import re
import sys
import json
import keyword

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "hwcheck")
BUILD = os.path.join(KIT, "build")

TRANSLATABLE_CALLS = {"info": [0], "note": [0], "ask": [0], "pause": [0], "title": [0]}
MESSAGE_LISTS = {"problems", "os_traces", "bad", "warn", "issues", "res", "lines"}

PREFIX = {
    "sec_identity": "identity", "sec_cpu": "cpu", "sec_memory": "memory",
    "sec_storage": "storage", "sec_battery": "battery", "sec_display": "display",
    "sec_audio": "audio", "sec_keyboard": "keyboard", "sec_pointer": "pointer",
    "sec_camera": "camera", "sec_network": "network", "sec_usb": "usb",
    "sec_sensors": "sensors", "sec_extras": "extras",
    "render_txt": "report", "render_html": "report", "print_summary": "summary",
    "write_report": "report", "preflight": "main", "main": "main",
    "overall_verdict": "verdict", "find_kit_root": "main",
    "setup_report_dir": "main", "add": "main", "ask": "main", "pause": "main",
    "title": "main", "info": "main", "note": "main",
}

WORD_STOP = {"the", "a", "an", "is", "are", "it", "to", "of", "for", "on", "in",
             "and", "or", "not", "no", "this", "that", "with", "from", "at", "as",
             "you", "your", "any", "did", "do", "does"}


def extract(source):
    """Step 1: rewrite every display string into t("key", vN=...) ."""
    tree = ast.parse(source)
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def enclosing_func(node):
        cur = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parent.get(cur)
        return "module"

    def is_text_node(n):
        if isinstance(n, ast.JoinedStr):
            return True
        return isinstance(n, ast.Constant) and isinstance(n.value, str)

    def receiver_name(node):
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return None

    targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = receiver_name(node)
        if name in TRANSLATABLE_CALLS:
            for idx in TRANSLATABLE_CALLS[name]:
                if idx < len(node.args) and is_text_node(node.args[idx]):
                    targets.append(node.args[idx])
        elif name == "add":
            for idx in (1, 3):
                if idx < len(node.args) and is_text_node(node.args[idx]):
                    targets.append(node.args[idx])
            for kw in node.keywords:
                if kw.arg == "detail" and is_text_node(kw.value):
                    targets.append(kw.value)
        elif name in ("append", "extend"):
            if isinstance(node.func, ast.Attribute) and \
                    isinstance(node.func.value, ast.Name) and \
                    node.func.value.id in MESSAGE_LISTS and \
                    node.args and is_text_node(node.args[0]):
                targets.append(node.args[0])
        elif name == "print":
            if node.args:
                a0 = node.args[0]
                if is_text_node(a0):
                    targets.append(a0)
                elif isinstance(a0, ast.Call) and receiver_name(a0) == "c" \
                        and a0.args and is_text_node(a0.args[0]):
                    targets.append(a0.args[0])

    seen, uniq = set(), []
    for t_ in targets:
        if id(t_) not in seen:
            seen.add(id(t_))
            uniq.append(t_)
    targets = uniq

    def slugify(text, max_words=4):
        text = re.sub(r"\{[^}]*\}", " ", text)
        text = re.sub(r"\[[^\]]*\]", " ", text)
        text = re.sub(r"[^A-Za-z0-9]+", " ", text)
        words = [w.lower() for w in text.split() if w]
        words = [w for w in words if w not in WORD_STOP and not w.isdigit()]
        out = "_".join(words[:max_words])[:44].strip("_")
        if not out or out[0].isdigit() or keyword.iskeyword(out):
            out = "m_" + out
        return out or "text"

    warnings = []

    def spec_text(fs_node):
        """The format spec, rebuilt from the node's own children.

        Do NOT use get_source_segment here: in Python 3.11 the format_spec
        JoinedStr inherits the enclosing f-string's position, so the segment
        would be the whole literal.
        """
        if fs_node is None:
            return ""
        out = []
        for v in fs_node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
            else:
                out.append("")          # nested {} in a spec: not supported
        return "".join(out)

    def pick_quote(*chunks):
        blob = "".join(chunks)
        for q in ('"', "'"):
            if q not in blob:
                return q
        return None

    def template_and_args(node):
        parts, args = [], []
        n = [0]

        def add_value(vnode, conversion, spec):
            name = f"v{n[0]}"
            n[0] += 1
            expr_src = ast.get_source_segment(source, vnode)
            if expr_src is None:
                warnings.append("no source segment for a placeholder")
                expr_src = "''"
            expr_src = expr_src.strip()
            has_conv = isinstance(conversion, int) and conversion >= 0
            if has_conv or spec:
                if "{" in expr_src or "}" in expr_src:
                    warnings.append(f"spec dropped (braces in expr): {expr_src[:50]}")
                else:
                    conv = f"!{chr(conversion)}" if has_conv else ""
                    fmt = f":{spec}" if spec else ""
                    q = pick_quote(expr_src, conv, fmt)
                    if q is None:
                        warnings.append(f"spec dropped (quote clash): {expr_src[:50]}")
                    else:
                        # NOTE the leading 'f': without it the spec becomes a
                        # dead string literal like "{health:.0f}" and the value
                        # is printed unformatted (or not at all).
                        expr_src = f"f{q}{{{expr_src}{conv}{fmt}}}{q}"
            args.append((name, expr_src))
            parts.append("{" + name + "}")

        if isinstance(node, ast.Constant):
            parts.append(node.value)
        else:
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    add_value(v.value, v.conversion, spec_text(v.format_spec))
        return "".join(parts), args

    catalog, tmpl_to_key, occurrences = {}, {}, []
    for node in targets:
        fn = enclosing_func(node)
        prefix = PREFIX.get(fn, re.sub(r"^sec_", "", fn) or "misc")
        tmpl, args = template_and_args(node)
        if not tmpl.strip() and not args:
            continue
        if tmpl in tmpl_to_key:
            key = tmpl_to_key[tmpl]
        else:
            key = f"{prefix}.{slugify(tmpl)}"
            base, i = key, 2
            while key in catalog:
                key = f"{base}_{i}"
                i += 1
            catalog[key] = tmpl
            tmpl_to_key[tmpl] = key
        occurrences.append((node, key, args))

    repl = []
    for node, key, args in occurrences:
        call = (f't("{key}")' if not args else
                f't("{key}", ' + ", ".join(f"{n}={e}" for n, e in args) + ")")
        repl.append(((node.lineno, node.col_offset),
                     (node.end_lineno, node.end_col_offset), call))

    line_starts = [0]
    for ln in source.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(ln))

    def offset(pos):
        return line_starts[pos[0] - 1] + pos[1]

    spans = sorted(((offset(s), offset(e), txt) for s, e, txt in repl), reverse=True)
    out, prev = source, None
    for s, e, txt in spans:
        if prev is not None and e > prev:
            continue
        out = out[:s] + txt + out[e:]
        prev = s

    return out, catalog, len(occurrences), warnings


# ---------------------------------------------------------------------------
# Step 2: catalogue curation
# ---------------------------------------------------------------------------
RENAMES = {
    "battery.m_": "battery.battery_line",
    "cpu.m_": "cpu.freq_change",
    "display.m_": "display.output_line",
    "keyboard.m_": "keyboard.event_line",
    "main.m_": "main.indent2",
    "main.m__2": "main.indent_pair",
    "main.m__3": "main.indent10",
    "report.m_": "report.status_line",
    "report.m__2": "report.detail_line",
    "report.m__3": "report.fail_bullet",
    "report.m__4": "report.fail_bullet_detail",
    "report.m__5": "report.fail_detail_more",
    "storage.m_": "storage.disk_line",
    "storage.m__2": "storage.two_lines",
    "summary.m_": "summary.indent3",
    "summary.m__2": "summary.fail_bullet",
    "summary.m__3": "summary.detail_line",
}

EXTRA = {
    "main.banner": "HWCheck v{v0} - used-laptop hardware verification",
    "main.running_as": "Running as    : {v0}",
    "main.as_root": "root",
    "main.as_user": "user",
    "main.sudo_hint": "  <-- re-run with sudo for full access!",
    "main.kit_not_found": "not found (running from disk)",
    "main.mode_quick": "QUICK",
    "main.mode_full": "FULL",
    "main.mode_auto": " / AUTO (no prompts)",
    "report.title_line": "  USED LAPTOP HARDWARE REPORT - HWCheck v{v0}",
    "report.section_header": "  {v0}",
    "report.counts": "  Counts : {v0}",
    "verdict.fail": "DO NOT BUY / RENEGOTIATE",
    "verdict.caution": "BUY WITH CAUTION",
    "verdict.minor": "ACCEPTABLE - minor issues",
    "verdict.good": "GOOD - no hardware faults found",
    "main.report_dir_fallback": ("Could not write the report to the kit folder - "
                                 "saving it to {v0} instead."),
    "report_html.title": "Used-Laptop Hardware Report",
    "report_html.subtitle": "HWCheck v{v0} &middot; {v1}",
    "report_html.hard_failures": "Hard failures",
    "report_html.warnings": "Warnings",
    "report_html.full_results": "Full results",
    "report_html.machine": "Machine",
    "report_html.serial": "Serial",
    "report_html.bios": "BIOS",
    "report_html.cpu": "CPU",
    "report_html.ram": "RAM",
    "report_html.kernel": "Kernel",
    "report_html.na": "n/a",
    "report_html.footer_note": ("RAM integrity and storage surface are only partially covered "
                                "here &mdash; boot the MemTest86+ entry and run the disk read "
                                "test in the kit for full coverage."),
    "report_html.footer_note2": ("This report describes the machine's state at the moment "
                                 "of testing."),
    "report_html.report_of": "{v0} - {v1}",
}

SECTIONS = {
    "Identity": "Identity", "CPU": "CPU", "RAM": "RAM", "Storage": "Storage",
    "Battery": "Battery", "Display": "Display", "Audio": "Audio",
    "Input": "Input devices", "Pointer": "Touchpad", "Camera": "Camera",
    "Network": "Network", "USB": "USB", "Sensors": "Sensors",
    "Chassis": "Chassis", "Extras": "Extras",
}

STATUSES = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "INFO": "INFO", "SKIP": "SKIP"}


def write_catalog(catalog, path):
    def lit(s):
        return json.dumps(s, ensure_ascii=False)

    groups = {}
    for k, v in catalog.items():
        groups.setdefault(k.split(".")[0], {})[k] = v

    out = ['#!/usr/bin/env python3',
           '# -*- coding: utf-8 -*-',
           '"""English message catalog - the SOURCE language.',
           '',
           'Keys map to templates; {v0}, {v1}... are placeholders filled in by i18n.t().',
           'A placeholder may be moved anywhere in the sentence but must never be',
           'deleted or renamed, and {v0} must keep receiving the same kind of value.',
           'Translated catalogs (fr.py, ar.py) must define exactly the same key set.',
           '"""',
           '',
           'MESSAGES = {']
    for g in sorted(groups):
        out.append(f'    # ---- {g} ' + "-" * max(0, 62 - len(g)))
        for k in sorted(groups[g]):
            out.append(f'    {lit(k)}: {lit(groups[g][k])},')
        out.append("")
    out.append("}")
    out.append("")
    out.append("# Internal section ids -> display label (ids are logic keys, never translate)")
    out.append("SECTIONS = {")
    for k in sorted(SECTIONS):
        out.append(f'    {lit(k)}: {lit(SECTIONS[k])},')
    out.append("}")
    out.append("")
    out.append("# Internal verdict ids -> display label (ids are logic keys, never translate)")
    out.append("STATUSES = {")
    for k in ["PASS", "FAIL", "WARN", "INFO", "SKIP"]:
        out.append(f'    {lit(k)}: {lit(STATUSES[k])},')
    out.append("}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def main():
    src_path = os.path.join(KIT, "hwcheck.py")
    source = open(src_path, encoding="utf-8").read()

    if "import i18n" in source and 't("' in source:
        # already converted: fall back to the pristine copy if one exists
        pristine = os.path.join(BUILD, "hwcheck_english.py")
        if os.path.exists(pristine):
            source = open(pristine, encoding="utf-8").read()
            print(">> using build/hwcheck_english.py as the English source")
        else:
            sys.exit("hwcheck.py is already converted and no pristine copy was "
                     "found in build/hwcheck_english.py - nothing to do.")

    os.makedirs(BUILD, exist_ok=True)
    with open(os.path.join(BUILD, "hwcheck_english.py"), "w", encoding="utf-8") as fh:
        fh.write(source)

    print("== step 1: automatic extraction ==")
    out, catalog, sites, warns = extract(source)
    print(f"   catalogue entries : {len(catalog)}")
    print(f"   call sites        : {sites}")
    if warns:
        print(f"   warnings          : {len(warns)}")
        for w in warns[:10]:
            print("     ", w)

    print("== step 2: curating the catalogue ==")
    for old, new in RENAMES.items():
        if old in catalog:
            catalog[new] = catalog.pop(old)
    dupes = set(EXTRA) & set(catalog)
    if dupes:
        print("   (EXTRA overrides:", ", ".join(sorted(dupes)), ")")
    catalog.update(EXTRA)
    print(f"   final catalogue   : {len(catalog)} messages")
    write_catalog(catalog, os.path.join(KIT, "lang", "en.py"))
    print("   wrote lang/en.py")

    with open(os.path.join(BUILD, "hwcheck_extracted.py"), "w", encoding="utf-8") as fh:
        fh.write(out)
    print("== step 3: integration is applied by build/integrate.py ==")
    print("   applied:", os.path.join(BUILD, "hwcheck_extracted.py"))


if __name__ == "__main__":
    main()