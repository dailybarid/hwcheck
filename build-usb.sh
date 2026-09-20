#!/bin/bash
# ============================================================================
#  build-usb.sh - write the buyer kit onto a USB stick
#
#  Creates a Ventoy multiboot USB, then drops the payload on it:
#     <USB>/sparkylinux-8.4-x86_64-xfce.iso   bootable live desktop (the "OS")
#     <USB>/MemTest86Plus-*.iso             full RAM test (its own boot entry)
#     <USB>/hwcheck/                        this toolkit
#     <USB>/hwcheck/debs/                   offline packages
#     <USB>/HOWTO-USE.txt                   instructions for the buyer
#
#  Usage:
#     ./build-usb.sh --list                    show candidate devices
#     sudo ./build-usb.sh /dev/sdX             write the stick (DESTRUCTIVE)
#     sudo ./build-usb.sh /dev/sdX --refresh   only update the payload
#     ./build-usb.sh --target-dir /some/dir    build the payload into a folder
#                                              (no root, no device - for testing
#                                              or for zipping up the kit)
#
#  !! EVERYTHING ON THE TARGET DEVICE IS DESTROYED. Get the device name right.
# ============================================================================
set -u

KIT="$(cd "$(dirname "$0")" && pwd)"
REFRESH=0
DEV=""
TARGET_DIR=""
FORCE="${FORCE:-0}"

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --list)
            echo "Removable / USB block devices on this machine:"
            echo
            lsblk -o NAME,PATH,SIZE,TYPE,TRAN,LABEL,MODEL | grep -E "NAME|usb|disk" || true
            echo
            echo "Pick the DEVICE (e.g. /dev/sdb), not a partition (/dev/sdb1)."
            echo "If your stick is not listed, it is probably not plugged in."
            echo
            echo "  sudo $0 /dev/sdX"
            exit 0
            ;;
        --target-dir)
            TARGET_DIR="${2:-}"
            [ -z "$TARGET_DIR" ] && { echo "--target-dir needs a path"; exit 1; }
            shift 2
            ;;
        --refresh) REFRESH=1; shift ;;
        -h|--help) usage ;;
        -*) echo "unknown option: $1"; usage ;;
        *) DEV="$1"; shift ;;
    esac
done

if [ -z "$DEV" ] && [ -z "$TARGET_DIR" ]; then
    usage
fi

# ===========================================================================
#  Payload assembly (shared by device mode and --target-dir mode)
# ===========================================================================
copy_payload() {
    local ROOT="$1"
    echo ">> Copying payload to $ROOT ..."

    # ISOs at the root - Ventoy auto-detects them in the boot menu
    for iso in "$KIT"/iso/*.iso "$KIT"/iso/*.img; do
        [ -e "$iso" ] || continue
        echo "   + $(basename "$iso")"
        cp -f --sparse=always "$iso" "$ROOT/" || echo "     (copy failed)"
    done

    # The toolkit, with its own debs folder. Build-time artifacts and runtime
    # logs are excluded so the stick only carries what the checker needs.
    echo "   + hwcheck/ (toolkit)"
    mkdir -p "$ROOT/hwcheck"
    cp -r "$KIT/hwcheck/." "$ROOT/hwcheck/"
    rm -rf "$ROOT/hwcheck/__pycache__" "$ROOT/hwcheck/lang/__pycache__" \
           "$ROOT/hwcheck/build" "$ROOT/hwcheck/lang/_inventory.json"
    find "$ROOT/hwcheck" -maxdepth 2 -name '*.log' -delete 2>/dev/null || true
    chmod -R a+rX "$ROOT/hwcheck"
    chmod a+x "$ROOT/hwcheck"/*.sh "$ROOT/hwcheck/hwcheck.py" 2>/dev/null || true

    # The deb bundle lives in tools/hwcheck/debs (where the launcher looks)
    local DEBSRC="" cand
    for cand in "$KIT/debs" "$KIT/hwcheck/debs"; do
        [ -d "$cand" ] && [ "$(find "$cand" -name '*.deb' 2>/dev/null | wc -l)" -gt 0 ] \
            && { DEBSRC="$cand"; break; }
    done
    if [ -n "$DEBSRC" ]; then
        echo "   + hwcheck/debs/ ($(find "$DEBSRC" -name '*.deb' | wc -l) packages)"
        mkdir -p "$ROOT/hwcheck/debs"
        cp -f "$DEBSRC"/*.deb "$ROOT/hwcheck/debs/"
    else
        echo "   ! no .deb bundle found (offline install will not work)"
    fi

    # Instructions + marker
    [ -f "$KIT/HOWTO-USE.txt" ] && cp -f "$KIT/HOWTO-USE.txt" "$ROOT/HOWTO-USE.txt"
    : > "$ROOT/BUYERKIT.marker"

    {
        echo "kit built      : $(date)"
        echo "builder host   : $(uname -sr)"
        echo "isos           : $(ls "$ROOT"/*.iso 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
        echo "deb packages   : $(find "$ROOT/hwcheck/debs" -name '*.deb' 2>/dev/null | wc -l)"
        echo "hwcheck        : $(grep -m1 'VERSION =' "$ROOT/hwcheck/hwcheck.py" 2>/dev/null)"
    } > "$ROOT/hwcheck/BUILD-INFO.txt"

    sync 2>/dev/null || true
    echo ">> Payload copied."
}

verify_payload() {
    local ROOT="$1"
    local fail=0
    echo
    echo "=============================================================="
    echo "  VERIFICATION"
    echo "=============================================================="

    for f in "$ROOT/HOWTO-USE.txt" "$ROOT/BUYERKIT.marker" \
             "$ROOT/hwcheck/hwcheck.py" "$ROOT/hwcheck/RUN-HWCHECK.sh" \
             "$ROOT/hwcheck/install-deps.sh" "$ROOT/hwcheck/debs"; do
        if [ -e "$f" ]; then
            printf "  [ ok ] %s\n" "${f#$ROOT/}"
        else
            printf "  [MISS] %s\n" "${f#$ROOT/}"
            fail=1
        fi
    done

    local n_iso
    n_iso=$(ls "$ROOT"/*.iso 2>/dev/null | wc -l)
    printf "  [ ok ] %s ISO image(s) at the root\n" "$n_iso"
    if [ "$n_iso" -eq 0 ]; then
        echo "  [MISS] no ISO - the stick will not boot an OS!"; fail=1
    else
        ls "$ROOT"/*.iso | xargs -n1 basename | sed 's/^/          /'
    fi

    local n_deb
    n_deb=$(find "$ROOT/hwcheck/debs" -name '*.deb' 2>/dev/null | wc -l)
    printf "  [ ok ] %s offline .deb packages\n" "$n_deb"
    [ "$n_deb" -eq 0 ] && echo "  [warn] no debs - needs internet on the target laptop"

    if python3 -c "import ast,sys; ast.parse(open('$ROOT/hwcheck/hwcheck.py').read())" 2>/dev/null; then
        echo "  [ ok ] hwcheck.py parses cleanly"
    else
        echo "  [FAIL] hwcheck.py has a syntax error"; fail=1
    fi

    # Every bundled deb must be readable and have a valid control file
    local bad
    bad=$(find "$ROOT/hwcheck/debs" -name '*.deb' -exec dpkg-deb -f {} Package \; 2>/dev/null | grep -c '^$' || true)
    if [ "${bad:-0}" -gt 0 ]; then
        echo "  [warn] $bad .deb file(s) have unreadable control data"
    else
        echo "  [ ok ] all .deb control files readable"
    fi

    echo
    echo "  Free space left: $(df -h "$ROOT" | tail -1 | awk '{print $4}')"
    echo "  Files at the root of the kit:"
    ls -1 "$ROOT" | sed 's/^/    /'
    echo
    if [ "$fail" = "0" ]; then
        echo "  RESULT: payload OK"
    else
        echo "  RESULT: payload has problems - review [MISS]/[FAIL] above"
    fi
    echo "=============================================================="
    return "$fail"
}

# ===========================================================================
#  Mode 1: build into a plain directory (no root, no device)
# ===========================================================================
if [ -n "$TARGET_DIR" ]; then
    mkdir -p "$TARGET_DIR" || exit 1
    TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"
    echo
    echo "=============================================================="
    echo "  BUILDING PAYLOAD INTO A DIRECTORY (no device will be touched)"
    echo "    target: $TARGET_DIR"
    echo "=============================================================="
    echo
    copy_payload "$TARGET_DIR"
    verify_payload "$TARGET_DIR"
    echo
    echo "To turn this into a bootable stick: copy the contents to the root"
    echo "of a Ventoy-formatted USB drive (or run: sudo $0 /dev/sdX)."
    echo
    exit $?
fi

# ===========================================================================
#  Mode 2: real device
# ===========================================================================
if [ "$(id -u)" -ne 0 ]; then
    echo "This must run as root (it will repartition a device)."
    echo "Re-run:  sudo $0 $DEV"
    echo "(or use --target-dir /some/path to build the payload without root)"
    exit 1
fi

if [ ! -b "$DEV" ]; then
    echo "!! $DEV is not a block device."
    exit 1
fi

# --- Safety: refuse to touch internal disks --------------------------------
TRAN=$(lsblk -ndo TRAN "$DEV" | tr -d ' ')
RM=$(lsblk -ndo RM "$DEV" | tr -d ' ')
SIZE=$(lsblk -ndo SIZE "$DEV" | tr -d ' ')
MODEL=$(lsblk -ndo MODEL "$DEV" | tr -d ' ')
echo
echo "=============================================================="
echo "  TARGET DEVICE"
echo "    device : $DEV"
echo "    size   : $SIZE"
echo "    model  : $MODEL"
echo "    bus    : ${TRAN:-unknown}    removable: ${RM:-0}"
echo "=============================================================="

if [ "${TRAN}" != "usb" ] && [ "${RM}" != "1" ]; then
    echo
    echo "!! $DEV does not look like a USB / removable device."
    echo "   Refusing to continue. If you are certain, set FORCE=1."
    [ "$FORCE" = "1" ] || exit 1
fi

echo
echo "Current contents of $DEV:"
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEV" | sed 's/^/    /'
echo
echo "Anything mounted from $DEV will be unmounted."
echo
echo "!!!  ALL DATA ON $DEV WILL BE ERASED  !!!"
echo
if [ "$REFRESH" = "0" ]; then
    printf "Type the device name (%s) to confirm, or anything else to abort: " "$DEV"
    read -r confirm
    if [ "$confirm" != "$DEV" ]; then
        echo "Aborted."
        exit 1
    fi
fi
echo

# --- Unmount anything using the device ------------------------------------
for m in $(lsblk -nro MOUNTPOINT "$DEV" | grep -v '^$'); do
    echo ">> unmounting $m"
    umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
done
swapoff "${DEV}"* 2>/dev/null || true

# --- 1. Install Ventoy ----------------------------------------------------
VENTOY_SH="$(find "$KIT/ventoy" -maxdepth 2 -name 'Ventoy2Disk.sh' 2>/dev/null | head -1)"
if [ "$REFRESH" = "0" ]; then
    if [ -z "$VENTOY_SH" ]; then
        echo "!! Ventoy not found under $KIT/ventoy"
        echo "   Download: https://github.com/ventoy/Ventoy/releases -> *-linux.tar.gz"
        echo "   Then:     tar xzf ventoy-*-linux.tar.gz -C $KIT/ventoy"
        exit 1
    fi
    echo ">> Installing Ventoy onto $DEV"
    ( cd "$(dirname "$VENTOY_SH")" && bash ./Ventoy2Disk.sh -i "$DEV" -s ) || {
        echo "!! Ventoy install failed."
        exit 1
    }
    sync
    sleep 2
    partprobe "$DEV" 2>/dev/null || true
    sleep 3
fi

# --- 2. Mount the data partition -----------------------------------------
USB_ROOT="/mnt/hwkit-build"
mkdir -p "$USB_ROOT"
umount "$USB_ROOT" 2>/dev/null || true

DATAPART=""
# Prefer a partition labelled Ventoy, else any usable filesystem on the device
for p in "${DEV}"?*; do
    [ -b "$p" ] || continue
    lbl=$(lsblk -ndo LABEL "$p" | tr -d ' ')
    fs=$(lsblk -ndo FSTYPE "$p" | tr -d ' ')
    case "$lbl" in *entoy*) DATAPART="$p"; break ;; esac
done
if [ -z "$DATAPART" ]; then
    for p in "${DEV}"?*; do
        [ -b "$p" ] || continue
        fs=$(lsblk -ndo FSTYPE "$p" | tr -d ' ')
        case "$fs" in exfat|vfat|ext4) DATAPART="$p"; break ;; esac
    done
fi

if [ -z "$DATAPART" ]; then
    echo "!! Could not find a usable data partition on $DEV."
    lsblk "$DEV"
    exit 1
fi

echo ">> Mounting $DATAPART at $USB_ROOT"
mount "$DATAPART" "$USB_ROOT" || { echo "!! mount failed"; exit 1; }

# --- 3 + 4. Copy and verify ----------------------------------------------
copy_payload "$USB_ROOT"
verify_payload "$USB_ROOT"
vrc=$?

echo
echo "  Write the stick flush before unplugging:"
sync
if umount "$USB_ROOT"; then
    echo "  unmounted cleanly."
else
    echo "  !! could not unmount - run 'sync' and unplug after it returns"
fi
echo
if [ "$vrc" = "0" ]; then
    echo "  READY. On the target laptop, press the boot-menu key"
    echo "  (Lenovo F12 / Dell F12 / HP F9 / Asus Esc / Acer F12) and pick"
    echo "  the USB stick, then the SparkyLinux entry in the Ventoy menu."
else
    echo "  Finished WITH PROBLEMS - review the output above."
fi
echo "=============================================================="
exit "$vrc"
