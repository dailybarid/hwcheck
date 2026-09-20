#!/bin/bash
# ============================================================================
#  HWCheck launcher - run this from the live Linux session on the target laptop
#
#  Usage:  sudo ./RUN-HWCHECK.sh              (full guided run)
#          sudo ./RUN-HWCHECK.sh --quick      (skip the slow deep tests)
#          sudo ./RUN-HWCHECK.sh --auto       (no prompts, quick screening)
#          sudo ./RUN-HWCHECK.sh --lang fr    (English / French / Arabic)
#
#  Installs the diagnostic tools from the bundled .deb directory (no internet
#  needed) and then starts hwcheck.py as root.
# ============================================================================
set -u

KIT="$(cd "$(dirname "$0")" && pwd)"
LOG="$KIT/hwcheck-launch.log"

# ---------------------------------------------------------------------------
# Language. Precedence: --lang > $HWCHECK_LANG > system locale > English.
# Only these launcher messages are localised here; hwcheck.py itself is fully
# translated and picks the language up again from --lang / the environment.
# ---------------------------------------------------------------------------
LANG_CODE=""
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --lang)   LANG_CODE="${2:-}"; ARGS+=("--lang" "${2:-}"); shift 2 ;;
        --lang=*) LANG_CODE="${1#--lang=}"; ARGS+=("$1"); shift ;;
        *)        ARGS+=("$1"); shift ;;
    esac
done
if [ -z "$LANG_CODE" ]; then
    LANG_CODE="${HWCHECK_LANG:-}"
fi
if [ -z "$LANG_CODE" ]; then
    for v in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
        case "$v" in
            fr*) LANG_CODE=fr; break ;;
            ar*) LANG_CODE=ar; break ;;
            en*) LANG_CODE=en; break ;;
        esac
    done
fi
case "$LANG_CODE" in
    fr|ar|en) ;;
    *) LANG_CODE=en ;;
esac
export HWCHECK_LANG="$LANG_CODE"

msg() {
    local key="$1"; shift
    case "$LANG_CODE:$key" in
        fr:title)
            echo "  HWCheck - verification materielle d'un portable d'occasion" ;;
        en:title)
            echo "  HWCheck - used laptop hardware verification" ;;
        ar:title)
            echo "  HWCheck - فحص عتاد الحاسوب المحمول المستعمل" ;;

        fr:kitdir)     echo "  Dossier du kit : $1" ;;
        en:kitdir)     echo "  Kit directory  : $1" ;;
        ar:kitdir)     echo "  مجلد الأدوات   : $1" ;;

        fr:sudo)
            echo "!! Pas root. Relance avec sudo : necessaire pour dmidecode,"
            echo "   les donnees SMART des disques et le clavier brut." ;;
        en:sudo)
            echo "!! Not root. Re-running with sudo: needed for dmidecode, disk"
            echo "   SMART data and the raw keyboard device." ;;
        ar:sudo)
            echo "!! لست مستخدماً جذراً. إعادة التشغيل عبر sudo: مطلوب لقراءة dmidecode"
            echo "   وبيانات SMART للأقراص ولوحة المفاتيح الخام." ;;

        fr:using)      echo ">> Script : $1" ;;
        en:using)      echo ">> Using  : $1" ;;
        ar:using)      echo ">> السكربت: $1" ;;

        fr:installing)
            echo ">> Installation des paquets MANQUANTS uniquement, depuis la cle USB..." ;;
        en:installing)
            echo ">> Installing only the packages this live system is MISSING (offline)..." ;;
        ar:installing)
            echo ">> تثبيت الحزم الناقصة فقط من ذاكرة USB (دون إنترنت)..." ;;

        fr:instcount)  echo "   $1 sur $2 paquets a installer." ;;
        en:instcount)  echo "   $1 of $2 packages need installing." ;;
        ar:instcount)  echo "   $1 من أصل $2 حزمة تحتاج إلى تثبيت." ;;

        fr:instdone)   echo ">> Termine (journal : $1)" ;;
        en:instdone)   echo ">> Done (log: $1)" ;;
        ar:instdone)   echo ">> تمّ (السجل: $1)" ;;

        fr:nodebs)
            echo ">> Aucun .deb dans $1 - on utilisera ce que fournit la session live." ;;
        en:nodebs)
            echo ">> No .deb files in $1 - will use whatever the live session provides." ;;
        ar:nodebs)
            echo ">> لا توجد ملفات deb في $1 - سيُستخدم ما توفره الجلسة الحية." ;;

        fr:nodeps)
            echo ">> Des outils manquent et aucune archive hors ligne n'a ete trouvee."
            echo "   Le diagnostic continue avec une couverture reduite." ;;
        en:nodeps)
            echo ">> Some tools are missing and no offline bundle was found."
            echo "   The run continues with reduced coverage." ;;
        ar:nodeps)
            echo ">> بعض الأدوات ناقصة ولم يُعثر على حزمة دون إنترنت."
            echo "   سيتابع الفحص بتغطية أقل." ;;

        fr:nopython)
            echo "!! python3 est absent de cette session live - impossible de continuer." ;;
        en:nopython)
            echo "!! python3 is missing on this live system - cannot continue." ;;
        ar:nopython)
            echo "!! python3 غير موجود في هذه الجلسة الحية - لا يمكن المتابعة." ;;

        fr:nokit)
            echo "!! hwcheck.py introuvable. La cle USB est-elle toujours branchee ?" ;;
        en:nokit)
            echo "!! Cannot find hwcheck.py. Is the USB stick still plugged in?" ;;
        ar:nokit)
            echo "!! تعذّر العثور على hwcheck.py. هل ذاكرة USB ما زالت موصولة؟" ;;

        fr:finished)   echo "  Termine (code $1). Le chemin du rapport est affiche ci-dessus." ;;
        en:finished)   echo "  Finished (exit $1). The report path is printed above." ;;
        ar:finished)   echo "  انتهى (الرمز $1). مسار التقرير مطبوع أعلاه." ;;

        fr:enter)      echo "Appuyez sur Entree pour fermer" ;;
        en:enter)      echo "Press Enter to close" ;;
        ar:enter)      echo "اضغط Enter للإغلاق" ;;

        *)             echo "" ;;
    esac
}

echo "=============================================================="
msg title
msg kitdir "$KIT"
echo "=============================================================="
echo

if [ "$(id -u)" -ne 0 ]; then
    msg sudo
    echo
    exec sudo --preserve-env=DISPLAY,XAUTHORITY,XDG_RUNTIME_DIR,HWCHECK_LANG \
              "$0" "${ARGS[@]+"${ARGS[@]}"}"
fi

# ---------------------------------------------------------------------------
# Locate the kit (this script may be running from a copy)
# ---------------------------------------------------------------------------
find_kit() {
    for p in "$KIT" "$(dirname "$(dirname "$0")")" \
             /media/*/* /run/media/*/* /mnt/*; do
        [ -f "$p/hwcheck/hwcheck.py" ] && { echo "$p/hwcheck"; return; }
        [ -f "$p/hwcheck.py" ] && { echo "$p"; return; }
    done
    # Fall back: scan every removable partition by label
    while read -r dev label; do
        case "$label" in
            *entoy*|*HWKIT*|*BUYERKIT*)
                mnt="/mnt/hwkit-$(basename "$dev")"
                mkdir -p "$mnt" 2>/dev/null
                mount -o ro "$dev" "$mnt" 2>/dev/null || mount "$dev" "$mnt" 2>/dev/null
                [ -f "$mnt/hwcheck/hwcheck.py" ] && { echo "$mnt/hwcheck"; return; }
                ;;
        esac
    done < <(lsblk -pnro PATH,LABEL 2>/dev/null)
    echo "$KIT"
}

KIT_ROOT="$(find_kit)"
SCRIPT="$KIT_ROOT/hwcheck.py"
[ -f "$SCRIPT" ] || SCRIPT="$(find / -maxdepth 6 -name hwcheck.py 2>/dev/null | head -1)"

if [ ! -f "$SCRIPT" ]; then
    msg nokit
    read -rp "$(msg enter)" _
    exit 1
fi
msg using "$SCRIPT"
echo

# ---------------------------------------------------------------------------
# Offline dependency installation from the bundled .deb directory
# ---------------------------------------------------------------------------
DEBDIR=""
for cand in "$KIT_ROOT/debs" "$KIT_ROOT/../debs" "$(dirname "$KIT_ROOT")/debs"; do
    [ -d "$cand" ] && { DEBDIR="$cand"; break; }
done

need_install=0
for t in smartctl sensors memtester v4l2-ctl hdparm evtest feh stress-ng acpi; do
    command -v "$t" >/dev/null 2>&1 || need_install=1
done

if [ "$need_install" = "1" ] && [ -n "$DEBDIR" ]; then
    n=$(find "$DEBDIR" -name '*.deb' 2>/dev/null | wc -l)
    if [ "$n" -gt 0 ]; then
        msg installing
        # Install ONLY packages that are not already present, so a working live
        # session can never be clobbered by the bundled copies.
        SEL=$(mktemp)
        : > "$SEL"
        for deb in "$DEBDIR"/*.deb; do
            [ -e "$deb" ] || continue
            pkg=$(dpkg-deb -f "$deb" Package 2>/dev/null) || continue
            [ -z "$pkg" ] && continue
            dpkg -s "$pkg" >/dev/null 2>&1 && continue
            echo "$deb" >> "$SEL"
        done
        seln=$(wc -l < "$SEL")
        msg instcount "$seln" "$n"
        if [ "$seln" -gt 0 ]; then
            # shellcheck disable=SC2046
            dpkg --force-depends --force-confold -i $(cat "$SEL") >"$LOG" 2>&1
            dpkg --configure -a >>"$LOG" 2>&1
            # a second pass fixes dependency ordering inside the argument list
            dpkg --force-depends --force-confold -i $(cat "$SEL") >>"$LOG" 2>&1
            dpkg --configure -a >>"$LOG" 2>&1
        fi
        rm -f "$SEL"
        msg instdone "$LOG"
    else
        msg nodebs "$DEBDIR"
    fi
elif [ "$need_install" = "1" ]; then
    msg nodeps
    echo "   apt-get install -y smartmontools lm-sensors memtester v4l-utils \\"
    echo "     hdparm evtest feh stress-ng acpi"
fi
echo

# ---------------------------------------------------------------------------
# Python check
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    msg nopython
    read -rp "$(msg enter)" _
    exit 1
fi

# ---------------------------------------------------------------------------
# Run it, keeping a copy of the console output next to the report
# ---------------------------------------------------------------------------
export DISPLAY="${DISPLAY:-:0}"
OUTROOT="$KIT_ROOT"
[ -w "$OUTROOT" ] || OUTROOT="$(dirname "$SCRIPT")"

python3 "$SCRIPT" "${ARGS[@]+"${ARGS[@]}"}" 2>&1 | tee "$OUTROOT/hwcheck-session.log"
rc=${PIPESTATUS[0]}

echo
echo "=============================================================="
msg finished "$rc"
echo "=============================================================="
if [ -t 0 ]; then
    read -rp "$(msg enter)" _
fi
exit "$rc"