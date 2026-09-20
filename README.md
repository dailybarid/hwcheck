# HWCheck — a USB stick that inspects a used laptop

<p align="center">
  <img src="docs/assets/header.png"
       alt="HWCheck — hardware verification for a used laptop, no OS required"
       width="100%">
</p>

Boot a laptop that has **no operating system** from this USB stick and it walks
you through testing every piece of hardware, then writes a verdict and a report
back onto the stick.

Built for the situation where you are buying a second-hand laptop from a
stranger and have ten minutes to decide whether it is sound. Nothing is
installed and nothing on the target machine is modified — it runs entirely from
the live session.

```
  [ PASS ] BAT0 health
           82% of design capacity (19.7/24.0 Wh)
  [ FAIL ] /dev/sda - HEALTH
           3 reallocated sectors; 1 pending (unstable) sectors
  [ WARN ] eDP-1 panel resolution
           Native resolution only 1366x768 - a laptop this size should
           normally be 1920x1080. Possible downgraded/replaced panel.

   DO NOT BUY / RENEGOTIATE
```

## Languages

English, **French** and **Arabic** — the interface, the prompts and the HTML
report. Choose with `--lang fr`, `$HWCHECK_LANG`, or from the first screen of
the run. Arabic switches the report to right-to-left.

## What it checks

| Area | What it looks for |
|---|---|
| Identity | vendor/model/BIOS/serial sanity, chassis-vs-board vendor mismatch, traces of a previous OS, BitLocker |
| CPU | real model, cores, base vs turbo clocks, **thermal throttling under sustained all-core load**, peak temperature, fan RPM, kernel MCEs |
| RAM | total, slots used, module speeds, mismatched pairs, optional `memtester` pass (plus MemTest86+ on the stick) |
| Storage | **SMART health**, power-on hours, reallocated/pending/uncorrectable sectors, SSD wear, NVMe spare + critical warning, sequential read speed, kernel I/O errors |
| Battery | design vs actual capacity, cycle count, charging circuit, AC detection |
| Display | running vs native resolution, physical size, PPI, backlight control, full-screen dead/stuck-pixel test, backlight bleed, hinge-cable flicker |
| Audio | speakers L/R, distortion at volume, channel balance, microphone record + playback, headphone jack |
| Keyboard | **every individual key** (raw `/dev/input` capture), Fn/hotkeys |
| Touchpad | movement, buttons, edges, two-finger scroll, gestures, TrackPoint |
| Camera | device, formats, a real captured frame saved into the report |
| Network | wifi hardware + live scan, ethernet link, bluetooth, rfkill switches |
| USB | device enumeration, per-port plug/unplug events, over-current errors, stick speed |
| Sensors | temperatures, fan tachometer, ACPI zones |
| Chassis | cracks, hinges, missing screws, liquid damage, **swollen battery (hard fail)** |
| Extras | fingerprint reader, TPM, card reader, HDMI, Thunderbolt, optical drive |

Plus a per-machine verdict of `GOOD`, `ACCEPTABLE`, `BUY WITH CAUTION` or
`DO NOT BUY / RENEGOTIATE`, with hard failures listed separately from the
things worth renegotiating.

## How it works

Ventoy multiboot USB + a Debian-based live ISO + this toolkit as a payload
folder. The default ISO is **SparkyLinux 8.4 Xfce** (1.94 GB, Debian 13
"trixie"), chosen over a bigger desktop ISO because it is half the size while
carrying a *newer* kernel (6.12 LTS), and it still ships a full Xfce session —
which the screen tests need.

```
<Ventoy partition>/
├── sparkylinux-8.4-x86_64-xfce.iso   boot entry 1: live desktop
├── MemTest86Plus-8.10.iso          boot entry 2: full RAM test
├── HOWTO-USE.txt                   instructions for whoever holds the stick
└── hwcheck/
    ├── hwcheck.py                  the checker
    ├── i18n.py  lang/              message catalogs (en, fr, ar)
    ├── RUN-HWCHECK.sh              launcher: installs deps, runs as root
    ├── install-deps.sh             fallback dependency installer
    └── debs/                       offline .deb bundle
```

Of ~20 external tools the checker invokes, Sparky's ISO already ships 15
(`lm-sensors`, `alsa-utils`, `pciutils`, `usbutils`, `dmidecode`, `upower`,
`iw`, `rfkill`, `x11-xserver-utils`, `xdg-utils`, …) and lacks 5:
`smartmontools`, `memtester`, `v4l-utils`, `feh` and `eog`. So the kit ships a
**14-package, 17 MB** offline `.deb` bundle and the launcher installs **only the
packages the live system is missing** — it can never clobber a working live
session. Once built, the stick needs no internet to be useful.

## Repository layout

```
.
├── build-usb.sh            writes the kit onto a USB stick (DESTRUCTIVE)
├── HOWTO-USE.txt           instructions copied to the stick for the buyer
├── docs/PROJECT-LOG.md     engineering log: decisions, defects, what is verified
├── docs/assets/            logo + README header (hand-written SVG, PNG exports)
├── hwcheck/                the payload that ends up on the stick
│   ├── hwcheck.py          the checker
│   ├── i18n.py  lang/      translation layer + catalogs
│   ├── RUN-HWCHECK.sh      launcher (trilingual)
│   ├── install-deps.sh     fallback dependency installer
│   ├── build-deb-bundle.sh rebuilds the offline package set
│   └── build/              pristine English source, used by the i18n pipeline
└── tools/                  build-machine tooling, NOT shipped on the stick
    ├── i18n_pipeline.py    extract every display string into a catalog
    ├── integrate.py        wire the catalog into the program
    └── check_clean.py      refuse to publish machine-specific data
```

`iso/`, `ventoy/` and the `.deb` set are **not** in the repository — they are
large binaries. See the next two sections to fetch them.

## Getting the ISOs

```bash
mkdir -p iso && cd iso

# SparkyLinux 8.4 Xfce 64bit (~1.94 GB) - Debian 13 "trixie" based, live desktop
# It ships fonts-dejavu-core/mono, which cover Arabic script, so the Arabic
# interface renders correctly without adding a font package.
curl -LO https://sourceforge.net/projects/sparkylinux/files/xfce/sparkylinux-8.4-x86_64-xfce.iso/download
mv download sparkylinux-8.4-x86_64-xfce.iso
curl -LO https://sourceforge.net/projects/sparkylinux/files/xfce/sparkylinux-8.4-x86_64-xfce.iso.allsums.txt/download
mv download sparkylinux-8.4-x86_64-xfce.iso.allsums.txt
# The allsums file has md5/sha1/sha256/sha512 sections, so pull the sha256 line
# and check it. (A naive `grep -A2 sha256 | tail -1` grabs a blank line and
# sha256sum errors with "no properly formatted checksum lines".)
grep -A1 '^# sha256sum:' sparkylinux-8.4-x86_64-xfce.iso.allsums.txt | tail -1 | sha256sum -c -
# must print: sparkylinux-8.4-x86_64-xfce.iso: OK
# expected:   7103af4d18741842202fca3cbe838855c51724d66563931c24522c0437796537

# The ISO's own installed-package list - the bundle builder subtracts it, which
# is what keeps the offline bundle at ~14 packages instead of ~200.
curl -LO https://sourceforge.net/projects/sparkylinux/files/xfce/sparkylinux-8.4-x86_64-xfce.iso.package-list.txt/download
mv download sparkylinux-8.4-x86_64-xfce.iso.package-list.txt

# MemTest86+ (~6 MB) - an independent boot entry with a newer build than the
# memtest this ISO already carries on its own boot menu.
curl -LO https://www.memtest.org/download/v8.10/mt86plus_8.10_x86_64.iso.zip
unzip mt86plus_8.10_x86_64.iso.zip
mv memtest.iso MemTest86Plus-8.10.iso
```

Always verify the checksums against the vendor's published values — a corrupted
ISO produces a stick that fails to boot in ways that look like hardware faults.

## Building the stick

```bash
# 1. Ventoy bootloader
mkdir -p ventoy && curl -L https://github.com/ventoy/Ventoy/releases/download/v1.1.17/ventoy-1.1.17-linux.tar.gz \
  | tar xz -C ventoy

# 2. the offline .deb bundle (needs internet and a container engine, once)
./hwcheck/build-deb-bundle.sh --iso-list ../iso/sparkylinux-8.4-x86_64-xfce.iso.package-list.txt

# 3. write the stick  (4 GB+ USB stick; the payload is ~2.0 GB)
./build-usb.sh --list             # show candidate devices
sudo ./build-usb.sh /dev/sdX      # write it - erases the stick
```

`build-usb.sh` installs Ventoy, copies the ISOs to the root (Ventoy lists them
as boot entries), copies `hwcheck/` to `<USB>/hwcheck`, drops the `.deb` bundle
and `HOWTO-USE.txt`, then verifies the result and unmounts cleanly.

It refuses to touch a device that does not look like USB/removable media unless
`FORCE=1` is set, and makes you type the device name before erasing anything.

To stage the payload as a plain folder first — useful for inspecting it or
zipping it up — no root and no device needed:

```bash
./build-usb.sh --target-dir /tmp/payload
```

`--refresh` re-copies the payload onto an already-Ventoy-formatted stick
without repartitioning it.

## The offline `.deb` bundle

```bash
./hwcheck/build-deb-bundle.sh --iso-list ../iso/sparkylinux-8.4-x86_64-xfce.iso.package-list.txt
```

Two design decisions keep this small and correct:

1. **`--iso-list` subtracts what the ISO already ships.** Without it you get the
   whole dependency closure (~250 packages). With it you get the genuine
   difference: 14 packages, 17 MB. Always pass the ISO's own
   `.package-list.txt` — Sparky publishes one next to the ISO, and it is also
   inside the image at `/live/`.

2. **The closure is resolved inside a container of the target distro**
   (`debian:trixie` via podman or docker), so package versions always match the
   ISO. Building against the wrong base — Ubuntu debs for a Debian ISO —
   produces packages that will not install. If no container engine is
   available the script falls back to the host and warns that this is only
   correct when the host *is* the target distro.

Verify the result before shipping: every dependency of every bundled package
must be satisfied by the bundle itself, by a virtual `Provides:`, or by a
package in the ISO's list. That is what makes the offline install work on a
machine with no internet.

> **Pitfall that produced a 193-package bundle:** the ISO package list prints
> **architecture-qualified** names for `Multi-Arch: same` packages
> (`libc6:amd64`), while `apt-cache depends` emits plain names. Comparing them
> directly matched almost nothing, so nearly the entire closure was shipped.
> Strip the `:arch` suffix when building the "already installed" set.

1. Plug it in, power on, tap the boot-menu key (Lenovo/Dell/Acer `F12`,
   HP `F9`, Asus `Esc`).
2. Pick the Sparky ISO in the Ventoy menu → live desktop (1–2 minutes).
3. Open the stick in the file manager → `hwcheck` → right-click
   `RUN-HWCHECK.sh` → *Run in Terminal*.
4. Answer the prompts. First run installs the bundled tools offline (~30 s).
5. Read the verdict. The report is saved under `<USB>/report/<date>_<model>/`.

`HOWTO-USE.txt` on the stick is the same instructions written for a
non-technical buyer, including per-brand boot keys and a troubleshooting list.

Each run writes one folder, `<date>_<model>`, with a `-2`, `-3`… suffix if you
test twice in the same minute. It contains `report.html` (a printable page),
`report.txt`, `report.json` and the captured webcam frame — so the machine's
state on the day of sale is on the record.

The launcher is translated as well (banner, install progress, errors) and
passes the language through:

```bash
sudo ./RUN-HWCHECK.sh --lang ar
```

For a full RAM test, reboot and pick **MemTest86Plus** from the Ventoy menu.
Let it complete at least one pass (30–60 min). Any red error means walk away.
The in-session `memtester` check is only a quick sample.

## Running the checker without a stick

```bash
cd hwcheck
python3 hwcheck.py --quick --auto          # no prompts, automatic checks only
python3 hwcheck.py --quick --lang fr       # guided, skipping the slow tests
python3 hwcheck.py                         # full run
python3 hwcheck.py --sections cpu,storage,battery
python3 hwcheck.py --report-dir /tmp/out   # where the report goes
```

Run it as root for the full set: `dmidecode`, `smartctl` and the raw
`/dev/input` keyboard capture need it. Without root the program degrades
gracefully and says so in the report.

`--auto` exists so the whole suite can be smoke-tested non-interactively on a
known-good machine — which is how the false-positive bugs listed below were
found. **A healthy laptop must come out `GOOD`, not `FAIL`.**

## Translating

`lang/en.py` is the source language and the single source of truth. Each other
catalog defines **exactly the same key set**:

```python
MESSAGES = {"cpu.peak_temperature": "Peak temperature", ...}
SECTIONS = {"Storage": "Stockage", ...}     # internal id -> display label
STATUSES = {"FAIL": "ÉCHEC", ...}           # internal code -> short label
```

Rules that will break the program if ignored:

* never rename or delete a key, never invent one;
* never delete or rename a `{v0}`…`{vN}` placeholder — moving it elsewhere in
  the sentence is fine and often necessary, since word order differs;
* values are substituted verbatim, so keep the unit that appears in the English
  string (`GB`, `Wh`, `MHz`, `%`);
* section ids (`Storage`, `CPU`, …) and status codes (`PASS`, `FAIL`, …) are
  **logic keys** — translate only the values in `SECTIONS`/`STATUSES`.

Check a catalog before committing:

```bash
python3 tools/check_translations.py
```

It verifies the key sets match, the placeholder sets match, and that no
translation is an empty string or a verbatim copy of the English.

## The i18n build pipeline

`hwcheck.py` calls `tr("key")` for every user-visible string. Those call sites
were generated once from the plain-English original by `tools/i18n_pipeline.py`
+ `tools/integrate.py`, using AST spans rather than `ast.unparse` so that
comments and formatting survive. The pristine English input is kept in
`hwcheck/build/` so the migration can be re-run.

You do **not** need the pipeline to add a language — add a catalog. You only
need it if you want to re-extract strings from a changed English source.

Named `tr()` and not `t()` on purpose: several checker functions assign to a
local `t` (temperatures, thread handles), which would shadow the translator and
raise `UnboundLocalError` at the call site.

## Publishing checklist

```bash
python3 tools/check_clean.py     # must print CLEAN
```

This tool records machine identities by design, so its own test output is the
easiest thing to leak: serials, BIOS versions, MAC addresses, disk models,
battery data. `check_clean.py` scans every committed file for absolute home
paths, MAC/IP addresses, e-mail addresses, DMI fingerprints and serial
assignments. `report/`, `iso/`, `ventoy/` and `*.deb` are gitignored for the
same reason.

## Gotchas that cost real time to find

* **Thermal throttling** must be judged against the CPU's *base* clock
  (`/sys/.../cpufreq/base_frequency`), not `cpuinfo_max_freq`. An all-core load
  never reaches the single-core turbo ceiling, so comparing against turbo
  reports healthy machines as `FAIL`.
* **`sensors` output** contains `high = +100.0°C` limit columns; a naive
  temperature regex reads those as readings and reports a 100 °C idle machine.
  Skip lines containing `high =`, `crit =`, `min =`, `max =`, `low =`.
* A panel running **below** its native resolution is not a fault. The fraud
  signal is a *low native resolution* or a missing EDID preferred mode.
* `iw scan` needs root; a non-root run only gets "Operation not permitted".
* In Python 3.11 an f-string's `format_spec` node **inherits the position of
  the enclosing f-string**, so `ast.get_source_segment` on it returns the whole
  literal instead of `".0f"`. Rebuild the spec from the node's constants.
* Installing a whole `.deb` bundle over a live session can overwrite core
  libraries. Install only packages `dpkg -s` says are missing.
* Ventoy makes an exFAT partition labelled `Ventoy` plus a small EFI one. Find
  the payload by label *and* by marker files, and mount it yourself if the live
  session did not automount it.
* Dead-pixel testing needs a fullscreen viewer: `feh` is bundled, with `eog`
  and `xdg-open` as fallbacks. Test images are written by a pure-python
  zlib+struct PNG encoder, so no ImageMagick or PIL is needed.

## What this cannot check

Software cannot see a warped chassis, a worn hinge, missing screws or a
swollen battery. The Chassis section asks you to check those by hand — a
swollen battery is a hard fail and a fire risk. `HOWTO-USE.txt` lists the full
manual checklist, including comparing the BIOS-reported CPU/RAM/disk against
the advertisement.

## License

MIT — see [LICENSE](LICENSE).