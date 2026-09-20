#!/bin/bash
# ============================================================================
#  install-deps.sh - fallback dependency installer
#
#  Tries, in order:
#    1. the offline .deb bundle on the USB stick
#    2. the distro package manager (needs internet - phone tethering works)
#
#  Safe to run repeatedly.
# ============================================================================
set -u
KIT="$(cd "$(dirname "$0")" && pwd)"
DEBDIR="$KIT/debs"

PKGS="smartmontools lm-sensors memtester v4l-utils hdparm evtest feh stress-ng acpi pciutils usbutils dmidecode ethtool iw rfkill xinput x11-xserver-utils alsa-utils nvme-cli"

echo "== HWCheck dependency installer =="
echo

missing=""
for t in smartctl sensors memtester v4l2-ctl hdparm evtest feh stress-ng acpi; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done

if [ -z "$missing" ]; then
    echo "All diagnostic tools are already present. Nothing to do."
    exit 0
fi
echo "Missing:$missing"
echo

# --- 1. offline debs --------------------------------------------------------
if [ -d "$DEBDIR" ] && [ "$(find "$DEBDIR" -name '*.deb' | wc -l)" -gt 0 ]; then
    echo ">>> Installing from the offline bundle in $DEBDIR ..."
    dpkg --force-depends --force-confold -i "$DEBDIR"/*.deb
    dpkg --configure -a
    command -v apt-get >/dev/null 2>&1 && apt-get -f install -y --no-download || true
    echo
fi

# --- re-check ---------------------------------------------------------------
missing=""
for t in smartctl sensors memtester v4l2-ctl hdparm evtest feh stress-ng acpi; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
[ -z "$missing" ] && { echo "All tools now available."; exit 0; }

# --- 2. online --------------------------------------------------------------
echo "Still missing:$missing"
echo
if command -v apt-get >/dev/null 2>&1; then
    echo ">>> Trying apt (needs internet)..."
    apt-get update -qq
    # shellcheck disable=SC2086
    apt-get install -y $PKGS
elif command -v dnf >/dev/null 2>&1; then
    echo ">>> Trying dnf..."
    dnf install -y smartmontools lm_sensors memtester v4l-utils hdparm evtest feh stress-ng acpi
elif command -v pacman >/dev/null 2>&1; then
    echo ">>> Trying pacman..."
    pacman -Sy --noconfirm smartmontools lm_sensors memtester v4l-utils hdparm evtest feh stress-ng acpi
elif command -v zypper >/dev/null 2>&1; then
    echo ">>> Trying zypper..."
    zypper --non-interactive install smartmontools sensors memtester v4l-utils hdparm evtest feh stress-ng
else
    echo "!! No supported package manager found."
fi

echo
echo "== Final status =="
for t in smartctl sensors memtester v4l2-ctl hdparm evtest feh stress-ng acpi; do
    printf "  %-12s %s\n" "$t" "$(command -v "$t" || echo MISSING)"
done
