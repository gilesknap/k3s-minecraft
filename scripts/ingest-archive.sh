#!/usr/bin/env bash
#
# ingest-archive.sh - unpack Google Drive "minecraft-k8s-backup" wrapper zips
# into the flat, read-only archive tier used by the Minecraft dashboard.
#
# Usage:
#   ingest-archive.sh <dest-dir> <drive-zip>...
#
# Each Drive download part (minecraft-k8s-backup-*-NNN.zip) contains inner
# per-world zips under minecraft-k8s-backup/. Every inner zip is extracted
# FLAT into <dest-dir>, keeping its original filename and timestamp, then:
#   - verified with `unzip -t` (CRC check of every entry), and
#   - checked to contain a world/level.dat entry (any prefix).
# Only verified files are moved into place (via a temp dir on the same
# filesystem, so a crash never leaves a half-written file under a real name).
#
# Idempotent: a file already present in <dest-dir> with the same size is
# skipped. A file present with a DIFFERENT size is reported as a conflict and
# left untouched - nothing in <dest-dir> is ever overwritten or deleted.
#
# Requirements (all present in busybox and on QNAP/Synology):
#   bash, unzip (busybox or Info-ZIP; needs -l -t -p), sed, grep, mkdir, mv,
#   rm, touch, chmod, and either `stat -c %s` or `wc -c`.
#   Inner zips over 4 GiB need an unzip with Zip64 support (Info-ZIP 6.0).
#
# Typical NAS workflow:
#   chmod u+w /share/bigdisk/minecraft-archive      # if previously locked
#   ingest-archive.sh /share/bigdisk/minecraft-archive minecraft-k8s-backup-*.zip
#   chmod -R a-w /share/bigdisk/minecraft-archive   # lock it again
#
# Exit status: 0 if every inner zip was extracted or already present,
# 1 if anything failed verification or conflicted, 2 on usage errors.

set -euo pipefail

usage() {
    sed -n '3,/^$/s/^# \{0,1\}//p' "$0" >&2
    exit 2
}

[ "$#" -ge 2 ] || usage
case "$1" in -h|--help) usage ;; esac

dest=$1
shift

if [ ! -d "$dest" ]; then
    mkdir -p -- "$dest"
fi
if [ ! -w "$dest" ]; then
    echo "error: $dest is not writable (locked archive? chmod u+w it first)" >&2
    exit 2
fi
command -v unzip >/dev/null 2>&1 || { echo "error: unzip not found" >&2; exit 2; }

tmpdir=$(mktemp -d "$dest/.ingest-tmp.XXXXXX")
cleanup() { rm -rf -- "$tmpdir"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

n_extracted=0
n_skipped=0
n_failed=0
n_conflict=0
failures=()

file_size() {
    stat -c %s -- "$1" 2>/dev/null || wc -c <"$1" | tr -d ' '
}

# Escape unzip wildcard characters so a member name matches literally.
escape_pattern() {
    printf '%s' "$1" | sed 's/[][*?\\]/\\&/g'
}

# `unzip -l` data lines -> "<size>\t<YYYY-MM-DD HH:MM>\t<name>".
# Handles both busybox/old Info-ZIP (MM-DD-YYYY) and Debian Info-ZIP
# (YYYY-MM-DD) date formats; names may contain spaces.
list_zip() {
    local t=$'\t'
    unzip -l "$1" | sed -nE \
        -e "s/^ *([0-9]+) +([0-9]{2})-([0-9]{2})-([0-9]{4}) +([0-9]{2}:[0-9]{2}) +(.*)\$/\\1$t\\4-\\2-\\3 \\5$t\\6/p" \
        -e "s/^ *([0-9]+) +([0-9]{4}-[0-9]{2}-[0-9]{2}) +([0-9]{2}:[0-9]{2}) +(.*)\$/\\1$t\\2 \\3$t\\4/p"
}

fail() {
    n_failed=$((n_failed + 1))
    failures+=("$1")
    echo "  FAILED   $1" >&2
}

for wrapper in "$@"; do
    echo "== $wrapper"
    if [ ! -f "$wrapper" ]; then
        fail "$wrapper (wrapper not found)"
        continue
    fi
    if ! listing=$(list_zip "$wrapper"); then
        fail "$wrapper (cannot list wrapper zip)"
        continue
    fi

    found=0
    while IFS=$'\t' read -r size mtime member; do
        case "$member" in
            minecraft-k8s-backup/*.zip) ;;
            *) continue ;;
        esac
        name=${member##*/}
        case "$name" in
            ''|.*) continue ;;
        esac
        found=$((found + 1))
        target="$dest/$name"

        if [ -e "$target" ]; then
            have=$(file_size "$target")
            if [ "$have" = "$size" ]; then
                echo "  present  $name"
                n_skipped=$((n_skipped + 1))
            else
                echo "  CONFLICT $name: existing size $have != $size in $wrapper (left untouched)" >&2
                n_conflict=$((n_conflict + 1))
                failures+=("$name (size conflict)")
            fi
            continue
        fi

        part="$tmpdir/$name"
        rm -f -- "$part"
        if ! unzip -p "$wrapper" "$(escape_pattern "$member")" >"$part"; then
            fail "$name (extract error)"
            rm -f -- "$part"
            continue
        fi
        got=$(file_size "$part")
        if [ "$got" != "$size" ]; then
            fail "$name (extracted $got bytes, expected $size)"
            rm -f -- "$part"
            continue
        fi
        if ! unzip -tq "$part" >/dev/null 2>&1; then
            fail "$name (unzip -t failed: corrupt inner zip)"
            rm -f -- "$part"
            continue
        fi
        # Capture first: `grep -q` exiting early would SIGPIPE the pipeline.
        if ! entries=$(list_zip "$part" | cut -f3) ||
            ! grep -qE '(^|/)world/level\.dat$' <<<"$entries"; then
            fail "$name (no world/level.dat entry)"
            rm -f -- "$part"
            continue
        fi
        touch -d "$mtime" -- "$part" 2>/dev/null || true
        chmod a-w -- "$part"
        mv -- "$part" "$target"
        echo "  added    $name ($size bytes)"
        n_extracted=$((n_extracted + 1))
    done <<<"$listing"

    if [ "$found" -eq 0 ]; then
        echo "  (no minecraft-k8s-backup/*.zip members)"
    fi
done

echo
echo "Summary: $n_extracted added, $n_skipped already present," \
    "$n_conflict conflicts, $n_failed failed -> $dest"
if [ "${#failures[@]}" -gt 0 ]; then
    printf '  problem: %s\n' "${failures[@]}" >&2
    exit 1
fi
