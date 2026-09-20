# Project log

Engineering record of how HWCheck was built, what broke, and what is and is not
verified. Written to be useful to a future maintainer, not as a changelog of
commits.

**Scope:** one build session, 2026-09-19.
**Outcome:** a bootable USB kit that inspects a used laptop's hardware with no
OS on the target machine, in English, French and Arabic.

---

## 1. What the project is

Boot a laptop with no operating system from the USB stick, run one command, and
get a pass/fail verdict on every hardware subsystem plus an HTML/TXT/JSON report
written back to the stick. Built for evaluating a second-hand laptop at the
point of sale, where you have minutes and no ability to install anything.

Nothing on the target machine is modified; it all runs from the live session.

## 2. Final state

| Component | State |
|---|---|
| `hwcheck/hwcheck.py` | ~2400 lines, 14 check sections |
| `hwcheck/i18n.py` | translation layer, language detection, RTL support |
| `hwcheck/lang/{en,fr,ar}.py` | 416 messages each, identical key and placeholder sets |
| `hwcheck/debs/` | 14 packages, 17 MB — the entire offline dependency set |
| `iso/sparkylinux-8.4-x86_64-xfce.iso` | 1.95 GiB, sha256 verified |
| `iso/MemTest86Plus-8.10.iso` | 6.2 MB, sha256 verified |
| Payload written to the stick | 2.0 GB, fits a 4 GB stick |

`iso/`, `ventoy/` and the `.deb` set are gitignored — large binaries, all
regenerable from the commands in the README.

## 3. Architecture decisions

**Ventoy + a distro ISO + a payload folder**, rather than a purpose-built ISO.
Ventoy gives a multiboot menu for free (drop another ISO at the root and it
appears), and the payload stays editable as plain files on the stick. The
checker finds its own payload by partition label and marker files, and mounts
the partition itself if the live session did not automount it.

**An offline `.deb` bundle**, because a live ISO never has `smartctl`,
`memtester` or `v4l-utils`, and the target laptop has no internet. The launcher
installs *only* packages the running live session is missing, so shipping a few
extras can never clobber a working session.

**Key-based i18n with English as the source.** Every display string goes through
`tr("key")`; catalogs are plain dicts. Identical English strings share one key,
and section ids / status codes stay English internally so grouping and counting
work in every language. Only the display labels are localised.

**The distro choice.** The first build used a full-size Ubuntu-based desktop ISO
(3.03 GB). It was replaced with **SparkyLinux 8.4 Xfce** (1.94 GB, Debian 13
"trixie") — half the size *and* a newer kernel (6.12 LTS vs 6.8), which matters
because undetected hardware reads as *missing* hardware in the report, i.e. a
false failure. The binding constraint was that the ISO must have a **GUI
session**: the screen tests need `xrandr` and a fullscreen image viewer.
CLI-only minimal builds (e.g. antiX Core, ~660 MB) were rejected for that
reason, despite being much smaller.

## 4. Defect log

Every one of these was found by running the thing, not by reading it. Several
were only visible because a *known-good* machine was tested first — a false
`FAIL` on healthy hardware is the failure mode that would most damage trust in
the verdict.

### Reporting logic

| Defect | Symptom | Fix |
|---|---|---|
| Thermal throttling compared against the turbo ceiling | healthy laptop reported as failing, running at base clock under all-core load | compare against `/sys/.../cpufreq/base_frequency`; an all-core load is not supposed to reach single-core turbo |
| `sensors` parser read limit columns | reported a 100 °C *idle* machine | skip lines containing `high =`, `crit =`, `min =`, `max =`, `low =` |
| Below-native resolution flagged as a fault | false warning | it is not a fault; the fraud signal is a *low native* resolution or a missing EDID preferred mode |
| `iw scan` failure unexplained | bare "Operation not permitted" | say it needs root |
| Two runs in the same minute shared a report folder | reports overwrote each other | `-2`, `-3` suffix |
| Report write crashed on a read-only stick | lost a completed hardware test | fall back through kit dir → home → `/tmp`, plus `--report-dir` |
| `/dev/fd0` treated as a disk | spurious "SMART not supported" (a VM exposes a floppy) | exclude `fd`, `dm-`, `loop`, `zram`, `md`, `nbd`, `rbd` |
| Launcher printed a report path it was not using | misleading output with `--report-dir` | point at the path printed above |

### The i18n migration

The migration rewrote ~466 call sites automatically from the original
plain-English source, using exact AST source spans (not `ast.unparse`, which
would reflow the file and destroy comments). Three of the four problems below
produced *silently wrong code* that still parsed.

| Defect | Symptom | Fix |
|---|---|---|
| `ast.FormattedValue.conversion` is `-1` when absent, not `None` | generated a literal `!-1` into the source | test it is an `int >= 0` |
| The generated f-string wrapper dropped its `f` prefix | format specs became dead string literals; values printed unformatted | keep the `f` |
| An f-string's `format_spec` node inherits the **enclosing f-string's position** (CPython 3.11) | `get_source_segment` returned the whole literal, generating `'{expr:f"whole literal"}'` | rebuild the spec from the node's own constant children |
| The translator was named `t()` | `UnboundLocalError` — six checker functions assign to a local `t` (temperatures, thread handles), shadowing it | renamed to `tr()` |

Two more that were structural rather than silent: a duplicated
`if __name__ == "__main__"` block made the whole suite run twice, and the
catalog was pruned after integration so translators are never handed dead keys.

### The offline bundle

| Defect | Symptom | Fix |
|---|---|---|
| ISO package lists print **architecture-qualified** names for `Multi-Arch: same` packages (`libc6:amd64`) while `apt-cache depends` emits plain names | the "already installed" subtraction matched almost nothing and shipped the entire closure: **193 packages instead of 14** | strip the `:arch` suffix |
| Building the closure on the wrong distro base | Ubuntu debs will not install on a Debian ISO (glibc, t64 packaging) | resolve the closure inside a container of the target distro (`podman run debian:trixie`) |
| A broken checksum command in the README | `grep -A2 sha256 \| tail -1` grabs a blank line; `sha256sum -c` errors | use `grep -A1 '^# sha256sum:'` and pipe to `sha256sum -c -` |

## 5. Verification performed

**Confirmed by execution:**

- sha256 of both ISOs against the vendors' published sums.
- The checker runs end-to-end on a known-good development laptop and returns
  `GOOD` — the regression that catches the reporting false positives above.
- All three languages run; the Arabic report sets `dir="rtl"`; the interactive
  language picker switches correctly.
- The offline bundle's dependency closure is fully covered: every dependency of
  every bundled package is satisfied by the bundle, a virtual `Provides:`, or a
  package in the ISO's own list.
- The ISO **boots** (QEMU, serial console, kernel `6.12.101+deb13`), the live
  session logs in, finds the kit, installs all 14 debs **with no internet**, and
  runs all 14 sections to completion with a report written — confirmed from the
  live session's own launcher log.
- The publishing scanner reports clean; all catalogs validate.

**Not verified — read this before trusting the tool on real hardware:**

- **The graphical checks were never exercised on the current distro.** The boot
  test ran in text mode (no X), so `xrandr` resolution/EDID detection and the
  full-screen dead-pixel / backlight-bleed test did not run there. That code
  path is proven on the previous distro, not on Sparky. Verify once on real
  hardware.
- **The USB write has never been performed** — no stick was available, so the
  Ventoy install path is unexercised. `build-usb.sh`'s payload copying and
  self-verification are tested; its partitioning step is not.
- **MemTest86+ has never been booted** from the stick.
- **The French and Arabic catalogs are machine-generated.** Key sets and
  placeholder sets are provably correct, but the wording has not been reviewed
  by a native speaker.

## 6. Font coverage note

A locale is not a font. Arabic renders only if the shipped fonts have the
glyphs, for both the UI font *and* the terminal's monospace font. DejaVu Sans
and DejaVu Sans Mono both cover Arabic (165/256 of U+0600–U+06FF) and are
present in the ISO, so no font package is required. `FreeSans` and DejaVu Serif
do **not** — and a loose grep over the charset range gives false reassurance.
Parse the ranges and test the actual codepoints.

## 8. Branding assets

`docs/assets/` holds the mark and the README header. Both are hand-written SVG
with the PNGs rendered from them by Inkscape.

- `logo.svg` / `logo.png` (512px) / `logo-128.png` — the square mark.
- `header.svg` / `header.png` (3200x800, displayed at 1600x400) — the README
  header.
- `social-preview.svg` / `social-preview.png` (1280x640) — upload this under
  Settings → General → Social preview.

Design constraints, and the reasoning behind the shapes:

- The mark must survive at **40x40 px**, which is how GitHub renders a repo
  avatar in list views. So: one badge, one silhouette, one accent colour, and
  no stroke thinner than about 8/512 units.
- **The first version was wrong and worth recording.** It drew a USB-A plug
  head-on — a rounded rectangle with two contact holes — and review read it as
  *"a laptop screen with two buttons"*, which is exactly what that shape looks
  like. It was redrawn in **profile**: a body plus a narrower metal tip is the
  shape everyone recognises as a flash drive. The ECG trace underneath says what
  the tool does. Verify a logo at 40 px before believing it works; render a size
  ladder and look at it.
- No text in the mark. A wordmark cannot survive 40 px, and keeping it
  letter-free keeps the file neutral across the EN/FR/AR build.
- The header is deliberately self-contained on its own dark ground so it looks
  intentional on both GitHub light and dark themes instead of blending into one.
- The Arabic chip is rendered through Inkscape/Pango, which shapes Arabic
  correctly. Rasterising SVG text with a tool that does not shape will produce
  disconnected letters.
- The three dots in the header use the same green/amber/red the report prints
  for PASS / WARN / FAIL.

Regenerate after editing any SVG:

```bash
cd docs/assets
inkscape --export-type=png --export-filename=logo.png           --export-width=512  logo.svg
inkscape --export-type=png --export-filename=logo-128.png       --export-width=128  logo.svg
inkscape --export-type=png --export-filename=header.png         --export-width=3200 header.svg
inkscape --export-type=png --export-filename=social-preview.png --export-width=1280 social-preview.svg
```

**What GitHub actually lets you set, and what it does not:**

- **Social preview** — Settings → General → Social preview. Upload
  `social-preview.png` (must be 1280x640, under 1 MB — this one is ~86 KB). This
  is the card shown when the repo link is unfurled in chat or on social.
- **There is no per-repository avatar on GitHub.** A repo link with no social
  preview shows the *owner's* avatar alongside auto-generated metadata: repo
  name, description and language/stars/forks. So `logo.png` is not "the repo
  picture" — it exists for any context that needs a square mark (elsewhere on
  the web, a website, a print, an app tile). Do not go looking for an avatar
  upload that does not exist.
- The README header needs no configuration — it renders from the committed file.

Type in these images is set at deliberately large sizes: a social card is seen
as a thumbnail, and small text turns to mush well before a large wordmark does.
Keep the two language runs in the social card as separate anchored `<text>`
elements — centring both on the same x made them overlap and read as
"FRالعربية".

## 9. Maintenance

- **To add a language:** add a catalog. Do not re-run the migration.
  `lang/en.py` is the source of truth; a catalog must define exactly the same
  keys and the same `{v0}`…`{vN}` placeholders per key (they may be moved within
  a sentence, never deleted or renamed). Run `tools/check_translations.py`.
- **To change the ISO:** drop the new ISO in `iso/`, rebuild the bundle against
  that distro's base (`--iso-list` pointing at its package list), and re-run the
  boot test. The bundle's package base must match the ISO.
- **Before publishing:** `tools/check_clean.py` must print CLEAN, and it now
  runs automatically on every push and pull request
  (`.github/workflows/checks.yml`), so it cannot be forgotten. This tool records
  machine identities by design — serials, BIOS versions, disk models, battery
  data — so its own test output is the easiest thing to leak. Reports are
  therefore gitignored.
- **Release history** lives in `CHANGELOG.md` (Keep a Changelog format). Add an
  entry under `[Unreleased]` as you work; this log is the narrative, the
  changelog is the list.
- **The migration pipeline** (`tools/i18n_pipeline.py` + `tools/integrate.py`)
  exists only to re-extract strings if the English source changes. It needs
  `hwcheck/build/hwcheck_english.py`, the pristine English input.