# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

See [docs/PROJECT-LOG.md](docs/PROJECT-LOG.md) for the engineering record: the
design decisions behind these changes, the defects found while building, and an
explicit list of what is and is not verified.

## [Unreleased]

## [1.0.0] - 2026-09-20

First release.

### Added

- **Fourteen hardware checks**, run from a live session on a machine with no OS
  installed: machine identity, CPU thermals and throttling, RAM, SMART storage
  health, battery wear, display and dead pixels, audio, keyboard, touchpad,
  webcam, network, USB ports, sensors and fans, and chassis.
- **Verdict reporting**: a per-machine result of `GOOD`, `ACCEPTABLE`,
  `BUY WITH CAUTION` or `DO NOT BUY / RENEGOTIATE`, with hard failures listed
  separately from the items worth renegotiating. Written as printable HTML,
  plain text and JSON, onto the USB stick.
- **Three languages**: English, French and Arabic — 416 messages each, covering
  the interface, the prompts, the launcher and the report. The Arabic report is
  rendered right-to-left.
- **An offline `.deb` bundle** (14 packages, 17 MB) so the live session needs no
  internet. The launcher installs only packages the running session is missing,
  so it cannot clobber a working live session.
- **Ventoy multiboot payload**: SparkyLinux 8.4 Xfce (Debian 13) as the live
  desktop plus MemTest86+ as an independent boot entry, with the kit as a plain
  payload folder that stays editable on the stick.
- **Interactive checks** that need a human: full-screen dead-pixel and
  backlight-bleed testing, speaker and microphone, every key on the keyboard,
  touchpad gestures, and a webcam frame captured into the report.
- **Build tooling**: `build-usb.sh` to write the stick, `build-deb-bundle.sh` to
  rebuild the offline package set, an i18n migration pipeline, and the three
  verification tools (`check_clean.py`, `check_translations.py`,
  `selftest.py`) that gate releases.
- **Branding assets**: a size-verified square mark, a README header and a
  1280x640 social preview card, all hand-written SVG with rendered PNGs.

### Notes

- Only the reporting logic is hardware-generic; the graphical checks (display
  and dead pixels) require a desktop session, so a text-mode boot reports them
  as unavailable rather than failing them.
- `--auto` runs the automatic checks only, for non-interactive screening.
- Requires root for the full set (`dmidecode`, `smartctl` and the raw
  `/dev/input` keyboard capture). Without root the program degrades gracefully
  and says so in the report.

[Unreleased]: https://github.com/dailybarid/hwcheck/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dailybarid/hwcheck/releases/tag/v1.0.0
