#!/bin/bash
# ============================================================================
#  build-deb-bundle.sh - build the offline package set for the live ISO
#
#  The live ISO is missing a handful of diagnostic tools. This downloads those
#  tools AND their dependency tree as .deb files into ./debs, so the kit can
#  install everything with no internet on the target laptop.
#
#  Usage:
#     ./build-deb-bundle.sh                                    # Debian trixie
#     ./build-deb-bundle.sh --iso-list ../iso/sparky.package-list.txt
#     ./build-deb-bundle.sh --tools "smartmontools feh"
#     ./build-deb-bundle.sh --engine podman --suite trixie
#     ./build-deb-bundle.sh --clean
#
#  Two things make this far smaller and more reliable than a naive closure:
#
#   1. --iso-list. Subtracting the packages the ISO ALREADY ships removes
#      almost the entire closure (libc, libX11, GTK, ALSA...). For Sparky 8.4
#      Xfce that took the bundle from 223 packages / 141 MB down to a handful.
#   2. The closure is resolved INSIDE a container of the target distro, so
#      versions always match the ISO. Building against the wrong base (Ubuntu
#      debs for a Debian ISO) produces packages that will not install.
#
#  The launcher still installs only packages MISSING from the running live
#  session, so shipping a few extras can never clobber that session.
# ============================================================================
set -u

KIT="$(cd "$(dirname "$0")" && pwd)"
OUT="$KIT/debs"

SUITE="trixie"
ENGINE=""
ISO_LIST=""
IN_CONTAINER=0
CLEAN=0
TOOLS="smartmontools memtester v4l-utils feh eog"

while [ $# -gt 0 ]; do
    case "$1" in
        --suite)        SUITE="${2:-trixie}"; shift 2 ;;
        --engine)       ENGINE="${2:-}"; shift 2 ;;
        --iso-list)     ISO_LIST="${2:-}"; shift 2 ;;
        --tools)        TOOLS="${2:-}"; shift 2 ;;
        --in-container) IN_CONTAINER=1; shift ;;
        --clean)        CLEAN=1; shift ;;
        -h|--help)      sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              echo "unknown option: $1"; exit 1 ;;
    esac
done

if [ "$CLEAN" = "1" ]; then
    rm -f "$OUT"/*.deb 2>/dev/null
    echo "cleared $OUT"
fi

mkdir -p "$OUT"

# ---------------------------------------------------------------------------
# Inside the target-distro container: resolve the closure and download
# ---------------------------------------------------------------------------
if [ "$IN_CONTAINER" = "1" ]; then
    echo "==================================================================="
    echo "  Building bundle inside the container"
    echo "  suite : $SUITE"
    echo "  arch  : $(dpkg --print-architecture)"
    echo "  tools : $TOOLS"
    echo "==================================================================="
    echo

    export DEBIAN_FRONTEND=noninteractive
    if ! apt-get update -qq; then
        echo "!! apt-get update failed - is there network in the container?"
        exit 1
    fi

    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT

    # ---- 1. recursive dependency closure of the tool list ---------------
    # shellcheck disable=SC2086
    apt-cache depends --recurse --no-recommends --no-suggests \
        --no-conflicts --no-breaks --no-replaces --no-enhances \
        $TOOLS 2>/dev/null > "$WORK/deps.txt"

    {
        for t in $TOOLS; do echo "$t"; done
        grep -E '^[[:space:]]*(Depends|PreDepends):' "$WORK/deps.txt" \
            | sed 's/.*:[[:space:]]*//' | sed 's/[[:space:]]*(.*//'
    } | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
      | grep -vE '^<|^$|^\|' | sort -u > "$WORK/closure.txt"

    echo ">> dependency closure : $(wc -l < "$WORK/closure.txt") packages"

    # ---- 2. subtract what the ISO already ships -------------------------
    if [ -n "$ISO_LIST" ] && [ -f "$ISO_LIST" ]; then
        # dpkg status format: "ii  <name>  <version>  <arch>  <desc>".
        # Multi-Arch:same packages are listed ARCH-QUALIFIED ("libc6:amd64"),
        # while apt-cache depends emits plain names - strip the ":arch" suffix
        # or almost nothing matches and the whole closure gets shipped.
        awk '$1 == "ii" { print $2 }' "$ISO_LIST" \
            | sed 's/:.*$//' | sort -u > "$WORK/iso.txt"
        comm -23 "$WORK/closure.txt" "$WORK/iso.txt" > "$WORK/keep.txt"
        echo ">> ISO already ships  : $(wc -l < "$WORK/iso.txt") packages"
        echo ">> difference to ship : $(wc -l < "$WORK/keep.txt") packages"
    else
        echo ">> no --iso-list: shipping the WHOLE closure (much larger)."
        echo "   Pass the ISO's .package-list.txt to keep the bundle small."
        cp "$WORK/closure.txt" "$WORK/keep.txt"
    fi

    # The tools themselves are always included.
    for t in $TOOLS; do echo "$t" >> "$WORK/keep.txt"; done
    sort -u "$WORK/keep.txt" -o "$WORK/keep.txt"

    # Keep only names that actually exist in the archive (drops virtuals).
    : > "$WORK/real.txt"
    while read -r pkg; do
        [ -z "$pkg" ] && continue
        if apt-cache policy "$pkg" 2>/dev/null | grep -q "Candidate: [0-9]"; then
            echo "$pkg" >> "$WORK/real.txt"
        fi
    done < "$WORK/keep.txt"
    mv "$WORK/real.txt" "$WORK/keep.txt"
    echo ">> to fetch           : $(wc -l < "$WORK/keep.txt") packages"
    echo

    # ---- 3. download (no root; does not touch the dpkg database) --------
    cd "$OUT" || exit 1
    # shellcheck disable=SC2046
    apt-get download $(cat "$WORK/keep.txt") >"$WORK/dl.log" 2>&1
    echo ">> bulk download finished"

    recovered=0
    while read -r pkg; do
        ls "${pkg}"_*.deb >/dev/null 2>&1 && continue
        apt-get download "$pkg" >/dev/null 2>&1 && recovered=$((recovered+1))
    done < "$WORK/keep.txt"
    [ "$recovered" -gt 0 ] && echo ">> recovered $recovered straggler(s)"

    echo
    echo "=== sanity check: every requested tool must be present ==="
    missed=0
    for t in $TOOLS; do
        if ls "${t}"_*.deb >/dev/null 2>&1; then
            printf "  [ ok ] %s\n" "$t"
        else
            printf "  [MISS] %s   <-- investigate\n" "$t"
            missed=$((missed+1))
        fi
    done
    echo "  ($missed missing)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Outside: pick a container engine and re-exec inside it
# ---------------------------------------------------------------------------
if [ -z "$ENGINE" ]; then
    for cand in podman docker; do
        if command -v "$cand" >/dev/null 2>&1 && "$cand" info >/dev/null 2>&1; then
            ENGINE="$cand"; break
        fi
    done
fi

REPO="$(dirname "$KIT")"

if [ -z "$ENGINE" ]; then
    echo "!! No working container engine (podman/docker) found."
    echo "   Falling back to this host's package base. That is only correct if"
    echo "   this host IS the target distro ($SUITE). Continuing in 5s..."
    sleep 5
    if [ -n "$ISO_LIST" ]; then
        exec "$0" --in-container --suite "$SUITE" --tools "$TOOLS" \
                 --iso-list "$ISO_LIST"
    fi
    exec "$0" --in-container --suite "$SUITE" --tools "$TOOLS"
fi

echo "==================================================================="
echo "  Building the offline bundle"
echo "  engine : $ENGINE"
echo "  image  : docker.io/library/debian:$SUITE"
echo "  output : $OUT"
echo "==================================================================="
echo

ISO_ARG=""
if [ -n "$ISO_LIST" ]; then
    if [ ! -f "$ISO_LIST" ]; then
        echo "!! --iso-list file not found: $ISO_LIST"
        exit 1
    fi
    REL="$(realpath --relative-to="$REPO" "$ISO_LIST")"
    ISO_ARG="--iso-list /kit/$REL"
fi

# shellcheck disable=SC2086
"$ENGINE" run --rm -i \
    -v "$REPO":/kit:z \
    -w /kit/hwcheck \
    "docker.io/library/debian:$SUITE" \
    bash /kit/hwcheck/build-deb-bundle.sh --in-container \
         --suite "$SUITE" --tools "$TOOLS" $ISO_ARG
rc=$?

n=$(find "$OUT" -name '*.deb' 2>/dev/null | wc -l)
sz=$(du -sh "$OUT" 2>/dev/null | cut -f1)
echo
echo "==================================================================="
echo "  Bundle: $n .deb files, $sz"
echo "  Location: $OUT"
echo "==================================================================="
echo
echo "The launcher installs only what the live session is missing, so a few"
echo "extra debs cost space but can never break the session."
exit "$rc"