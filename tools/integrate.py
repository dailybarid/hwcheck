#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 3 of the i18n pipeline: integration  (build-time tool, NOT shipped)

Takes build/hwcheck_extracted.py (all display strings already routed through
t("key")) and produces the final hwcheck.py:

  * imports the i18n layer
  * localises section ids and status codes at DISPLAY time only (they stay
    English internally, so grouping/counting keep working in every language)
  * rewrites the plain-text and HTML report renderers
  * adds language selection (+ RTL direction for Arabic)

Functions are replaced by DEF-BOUNDARY, not by matching long literal strings:
a single whitespace difference in a 40-line literal fails silently otherwise.

Run:  python3 tools/i18n_pipeline.py && python3 tools/integrate.py
"""

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "hwcheck")
BUILD = os.path.join(KIT, "build")

src = open(os.path.join(BUILD, "hwcheck_extracted.py"), encoding="utf-8").read()


def replace_func(src, name, body):
    """Swap out a whole top-level function by locating its boundaries."""
    m = re.search(rf"^def {re.escape(name)}\(", src, re.M)
    if not m:
        sys.exit(f"!! function {name}() not found")
    nxt = re.search(r"^(def |# ===|if __name__)", src[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(src) - m.end())
    return src[:m.start()] + body.strip() + "\n\n\n" + src[end:]


# ---------------------------------------------------------------------------
# 1. import the i18n layer
# ---------------------------------------------------------------------------
old_imports = "import datetime\nimport argparse\n"
assert old_imports in src, "import block not found"
src = src.replace(old_imports, old_imports + '''
# --- i18n --------------------------------------------------------------------
# The kit runs straight off a USB stick as root, so make sure this directory is
# importable no matter how the script was invoked.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import i18n
from i18n import tr, section as sec_label, status as status_label
''', 1)

# ---------------------------------------------------------------------------
# 2. display helpers (undo the no-op passthrough the extractor created)
# ---------------------------------------------------------------------------
src = replace_func(src, "title", '''
def title(text):
    print()
    hr()
    print(c(text, C_B + C_CYA))
    hr()''')

src = replace_func(src, "info", '''
def info(msg):
    print(msg)''')

src = replace_func(src, "note", '''
def note(msg):
    print(c(msg, C_DIM))''')

# ---------------------------------------------------------------------------
# 3. add(): localise section + status labels, keep ids internal
# ---------------------------------------------------------------------------
src = replace_func(src, "add", '''
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
    print(t("main.indent_pair", v0=c(label, color), v1=name))
    if detail:
        for ln in str(detail).splitlines():
            print(c(t("main.indent10", v0=ln), C_DIM))
    return code''')

# ---------------------------------------------------------------------------
# 3b. ask(): accept yes/oui/o/nعم in every locale the kit ships
# ---------------------------------------------------------------------------
src = replace_func(src, "ask", '''
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
                 "\u0646\u0639\u0645", "\u064a", "\u062c", "\u0623\u062c\u0644")
''')

# ---------------------------------------------------------------------------
# 3c. list_disks(): ignore virtual/pseudo block devices
#     A live VM (and some real machines) expose /dev/fd0 - a 4K floppy with no
#     transport or model. It was being probed for SMART and producing a
#     spurious "SMART not supported" warning. dm-* mappings are excluded too so
#     a LUKS/RAID stack does not report the same physical disk twice.
# ---------------------------------------------------------------------------
src = replace_func(src, "list_disks", '''
def list_disks():
    """Physical disks only - skip pseudo and virtual block devices."""
    rc, out = run(["lsblk", "-d", "-n", "-o", "NAME,TYPE,SIZE,TRAN,MODEL"])
    skip = ("loop", "sr", "zram", "fd", "ram", "dm-", "md", "nbd", "rbd")
    disks = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "disk" \
                and not parts[0].startswith(skip):
            disks.append({
                "name": parts[0], "size": parts[2],
                "tran": parts[3] if len(parts) > 3 else "",
                "model": " ".join(parts[4:]) if len(parts) > 4 else "",
            })
    return disks
''')

# ---------------------------------------------------------------------------
# 4. verdict as an internal id
# ---------------------------------------------------------------------------
src = replace_func(src, "overall_verdict", '''
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
    return ("good", "PASS", fails, warns)''')

# ---------------------------------------------------------------------------
# 5. plain-text report
# ---------------------------------------------------------------------------
src = replace_func(src, "render_txt", '''
def render_txt():
    lines = []
    app = f"{sys_vendor()} {product_name()}".strip()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    sw = i18n.status_width()

    lines.append("=" * 78)
    lines.append(t("report.title_line", v0=VERSION))
    lines.append("=" * 78)
    lines.append(t("report.machine", v0=app))
    lines.append(t("report.serial", v0=dmi("product_serial") or t("report_html.na")))
    lines.append(t("report.bios", v0=dmi("bios_version"), v1=dmi("bios_date")))
    lines.append(t("report.cpu", v0=cpu_model()))
    lines.append(t("report.ram_gib", v0=f"{mem_total_gb():.2f} GiB"))
    lines.append(t("report.tested", v0=ts))
    lines.append(t("report.kernel", v0=platform.release()))
    lines.append("")

    vid, cls, fails, warns = overall_verdict()
    lines.append(t("report.verdict", v0=t(f"verdict.{vid}")))
    cts = verdict_counts()
    lines.append(t("report.counts", v0=", ".join(
        f"{status_label(k)}={n}" for k, n in sorted(cts.items()))))
    lines.append("")

    sec = None
    for r in RESULTS:
        if r["section"] != sec:
            sec = r["section"]
            lines.append("")
            lines.append("-" * 78)
            lines.append(t("report.section_header", v0=sec_label(sec).upper()))
            lines.append("-" * 78)
        lines.append(t("report.status_line",
                       v0=f"{status_label(r['status']):<{sw}}",
                       v1=r["name"]))
        for ln in r["detail"].splitlines():
            lines.append(t("report.detail_line", v0=ln))

    lines.append("")
    lines.append("=" * 78)
    lines.append(t("report.decision_guide"))
    lines.append("=" * 78)
    if fails:
        lines.append(t("report.hard_failures_found_these"))
        lines.append(t("report.fix_by_reinstalling_software"))
        for f in fails:
            lines.append(t("report.fail_bullet",
                           v0=sec_label(f["section"]), v1=f["name"]))
            if f["detail"]:
                lines.append(t("report.fail_detail_more",
                               v0=f["detail"].splitlines()[0]))
    else:
        lines.append(t("report.hard_failures_detected"))
    if warns:
        lines.append("")
        lines.append(t("report.warnings_things_renegotiate"))
        for w in warns:
            lines.append(t("report.fail_bullet_detail",
                           v0=sec_label(w["section"]), v1=w["name"],
                           v2=w["detail"].splitlines()[0] if w["detail"] else ""))
    lines.append("")
    lines.append(t("report.reminder_suite_cannot_fully"))
    lines.append(t("report.usb_boot_menu_nor"))
    lines.append("=" * 78)
    return "\\n".join(lines)''')

# ---------------------------------------------------------------------------
# 6. HTML report (CSS kept as a plain string; only two colour substitutions)
# ---------------------------------------------------------------------------
src = replace_func(src, "render_html", '''
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
        det = esc(r["detail"]).replace("\\n", "<br>")
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
\"\"\".replace("@COLOR@", color)

    chips = "".join(f"<span>{esc(status_label(k))}: {n}</span>"
                    for k, n in sorted(cts.items()))
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        "<!DOCTYPE html>",
        f'<html lang="{i18n.LANG}"{direction}><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(t('report_html.title'))} - {esc(sys_vendor())} "
        f"{esc(product_name())}</title>",
        f"<style>{css}</style></head><body><div class=\\"wrap\\">",
        f" <h1>{esc(t('report_html.title'))}</h1>",
        f" <div class=\\"sub\\">{esc(t('report_html.subtitle', v0=VERSION, v1=ts))}</div>",
        ' <div class="verdict">',
        f"   <h2>{esc(t(f'verdict.{vid}'))}</h2>",
        f"   <div class=\\"chips\\">{chips}</div>",
        " </div>",
        ' <div class="meta">',
        f"   <div><b>{esc(t('report_html.machine'))}</b>"
        f"{esc(sys_vendor())} {esc(product_name())}</div>",
        f"   <div><b>{esc(t('report_html.serial'))}</b>"
        f"{esc(dmi('product_serial') or t('report_html.na'))}</div>",
        f"   <div><b>{esc(t('report_html.bios'))}</b>"
        f"{esc(dmi('bios_version'))} ({esc(dmi('bios_date'))})</div>",
        f"   <div><b>{esc(t('report_html.cpu'))}</b>{esc(cpu_model())}</div>",
        f"   <div><b>{esc(t('report_html.ram'))}</b>{mem_total_gb():.2f} GiB</div>",
        f"   <div><b>{esc(t('report_html.kernel'))}</b>{esc(platform.release())}</div>",
        " </div>",
    ]
    if fails:
        parts.append(f" <h2 style=\\"font-size:16px\\">"
                     f"{esc(t('report_html.hard_failures'))}</h2>")
        parts.append(f" <ul>{fail_list}</ul>")
    if warns:
        parts.append(f" <h2 style=\\"font-size:16px\\">"
                     f"{esc(t('report_html.warnings'))}</h2>")
        parts.append(f" <ul>{warn_list}</ul>")
    parts.append(f" <h2 style=\\"font-size:16px\\">"
                 f"{esc(t('report_html.full_results'))}</h2>")
    parts.append(f" <table>{''.join(rows)}</table>")
    parts.append(' <div class="foot">')
    parts.append(f"   {esc(t('report_html.footer_note'))}<br>")
    parts.append(f"   {esc(t('report_html.footer_note2'))}")
    parts.append(" </div>")
    parts.append("</div></body></html>")
    return "\\n".join(parts)''')

# ---------------------------------------------------------------------------
# 7. JSON report + console summary
# ---------------------------------------------------------------------------
src = replace_func(src, "write_report", '''
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
            "verdict": t(f"verdict.{vid}"),
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
    return REPORT_DIR''')

src = replace_func(src, "print_summary", '''
def print_summary():
    title(t("summary.summary"))
    v, cls, fails, warns = overall_verdict()
    cc = {"PASS": C_GRN, "WARN": C_YEL, "FAIL": C_RED}.get(cls, C_B)
    print()
    print(c(t(f"verdict.{v}"), cc + C_B))
    print()
    cts = verdict_counts()
    info(", ".join(f"{status_label(k)}: {n}" for k, n in sorted(cts.items())))
    print()
    if fails:
        print(c(t("summary.hard_failures_physical_faults"), C_RED))
        for f in fails:
            info(t("summary.fail_bullet", v0=sec_label(f["section"]), v1=f["name"]))
            if f["detail"]:
                note(t("summary.detail_line", v0=f["detail"].splitlines()[0]))
        print()
    if warns:
        print(c(t("summary.warnings_negotiate_price_plan"), C_YEL))
        for w in warns:
            info(t("summary.fail_bullet", v0=sec_label(w["section"]), v1=w["name"]))
            if w["detail"]:
                note(t("summary.detail_line", v0=w["detail"].splitlines()[0]))
        print()
    note(t("summary.reminder_run_memtest86_usb"))
    note(t("summary.verify_storage_surface_if"))''')

# ---------------------------------------------------------------------------
# 8. preflight: language picker first, then the banner
# ---------------------------------------------------------------------------
src = replace_func(src, "preflight", '''
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
    print(c("   " + t("main.banner", v0=VERSION), C_B + C_CYA))
    hr()
    info(t("main.running_as",
           v0=t("main.as_root") if root else t("main.as_user"))
         + ("" if root else c(t("main.sudo_hint"), C_RED)))
    info(t("main.usb_kit_root", v0=KIT_ROOT or c(t("main.kit_not_found"), C_YEL)))
    info(t("main.scratch_dir", v0=SCRATCH))
    info(t("main.mode",
           v0=t("main.mode_quick") if QUICK else t("main.mode_full"),
           v1=t("main.mode_auto") if AUTO else ""))
    print()
    if not root:
        note(t("main.without_root_dmidecode_smartctl"))

    if not QUICK and not AUTO:
        print()
        note(t("main.takes_minutes_thorough_machine"))
        note(t("main.memtester_disk_read_test"))
        note(t("main.everything_else_few_minutes"))
        pause(t("main.press_enter_start"))''')

# ---------------------------------------------------------------------------
# 9. main(): --lang and real section ids for the error handler
# ---------------------------------------------------------------------------
src = replace_func(src, "main", '''
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
                if not ask(t("main.skip_rest_section"), "y"):
                    raise
            except Exception as e:                           # noqa: BLE001
                add(sec_id, t("main.section_error", v0=name), "WARN",
                    t("display.output_line", v0=type(e).__name__, v1=e))
    except KeyboardInterrupt:
        print()
        note(t("main.interrupted_writing_report_what"))

    print_summary()
    path = write_report()
    title(t("main.report_saved"))
    info(t("main.report_html_open_browser", v0=path))
    info(t("main.report_txt_plain_text", v0=path))
    info(t("main.report_json_machine_readable", v0=path))
    print()
    note(t("main.keep_usb_stick_if"))
    note(t("main.machine_s_condition_day"))
    print()


# NOTE: the module's own `if __name__ == "__main__": main()` block is preserved
# by replace_func, so this body must NOT add a second one - that would run the
# whole suite twice.''')

# ---------------------------------------------------------------------------
# 10. call sites that used an auto-generated "m_" key name
# ---------------------------------------------------------------------------
for old, new in [
    ('t("battery.m_"', "t(\"battery.battery_line\""),
    ('t("cpu.m_"', "t(\"cpu.freq_change\""),
    ('t("display.m_"', "t(\"display.output_line\""),
    ('t("keyboard.m_"', "t(\"keyboard.event_line\""),
    ('t("storage.m__2"', "t(\"storage.two_lines\""),
    ('t("storage.m_"', "t(\"storage.disk_line\""),
    ('t("main.m_"', "t(\"main.indent2\""),
]:
    src = src.replace(old, new)

# ---------------------------------------------------------------------------
# 10b. the kit lives on removable media that can be mounted read-only: a failed
#      report write must never kill a test that already ran
# ---------------------------------------------------------------------------
src = src.replace(
    "REPORT_DIR = None\n",
    "REPORT_DIR = None\nREPORT_OVERRIDE = None    # --report-dir, if given\n", 1)

src = replace_func(src, "setup_report_dir", '''
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
''')

# ---------------------------------------------------------------------------
# 11. the translator is called tr(), not t(): several checker functions assign
#     to a local `t`, which would shadow it. Rewrite every generated call site.
# ---------------------------------------------------------------------------
src = re.sub(r"\bt\(", "tr(", src)

# ---------------------------------------------------------------------------
# write the finished program
# ---------------------------------------------------------------------------
ast.parse(src)
with open(os.path.join(KIT, "hwcheck.py"), "w", encoding="utf-8") as fh:
    fh.write(src)

# ---------------------------------------------------------------------------
# prune the catalog to the keys the finished program actually references, so
# translators are never handed dead entries
# ---------------------------------------------------------------------------
sys.path.insert(0, HERE)
from i18n_pipeline import write_catalog          # noqa: E402

tree = ast.parse(src)
used, families = set(), set()
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
            and n.func.id == "tr" and n.args:
        a = n.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            used.add(a.value)
        elif isinstance(a, ast.JoinedStr):
            pref = "".join(v.value for v in a.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if pref:
                families.add(pref)

import importlib.util                              # noqa: E402
cat_path = os.path.join(KIT, "lang", "en.py")
spec = importlib.util.spec_from_file_location("_en", cat_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

keep = {}
for k, v in mod.MESSAGES.items():
    if k in used or any(k.startswith(f) for f in families):
        keep[k] = v

dropped = sorted(set(mod.MESSAGES) - set(keep))
write_catalog(keep, cat_path)

print(f"wrote {os.path.join(KIT, 'hwcheck.py')} ({len(src)} bytes)")
print(f"catalog: {len(mod.MESSAGES)} entries -> {len(keep)} kept, "
      f"{len(dropped)} pruned ({', '.join(dropped) if dropped else 'none'})")
print("integration complete")