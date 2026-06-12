#!/bin/sh
# Upload the per-municipality polygon blobs (stage06's artifacts/r2/polygons/**) to the R2
# bucket the Worker reads via the POLY binding. Each object is independently valid, so a
# partial run just leaves some munis stale and a re-run heals it (like D1's delete+reload).
# Transient R2/network errors are retried with linear backoff.
#
# Only blobs whose CONTENT changed since the last successful upload are re-put: stage06
# rebuilds every muni blob each recompute, so we sha256 each one and skip those that match
# the manifest written by the previous run (artifacts/r2/.uploaded.sha256). A full recompute
# that only touched a handful of stations then uploads only that handful of muni blobs.
# If the manifest is missing (first run) everything is uploaded; the manifest is rewritten
# only after a clean pass, so a mid-run failure just re-uploads the changed blobs next time.
# summary.json (homepage counts) is tiny and changes almost every run, so it's always put.
#
# Usage: sync_r2.sh [bucket]
#   env: R2_RETRIES (default 4), R2_FORCE=1 (ignore manifest, upload everything)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BUCKET="${1:-bm-obuhvati-polygons}"
RETRIES="${R2_RETRIES:-4}"
SRC="$DIR/artifacts/r2"
MANIFEST="$SRC/.uploaded.sha256"
NEW_MANIFEST="$SRC/.uploaded.sha256.new"
[ -d "$SRC/polygons" ] || { echo "sync_r2: no $SRC/polygons (run stage06 first)" >&2; exit 2; }
cd "$DIR/../worker"

sha() { shasum -a 256 "$1" | awk '{print $1}'; }

old_sha() {  # key -> prints the previously-uploaded sha for this key, if any
  [ "${R2_FORCE:-0}" = 1 ] && return 0
  [ -f "$MANIFEST" ] || return 0
  awk -v k="$1" '$2 == k { print $1; exit }' "$MANIFEST"
}

put() {  # key file
  attempt=1
  while :; do
    if npx wrangler r2 object put "$BUCKET/$1" --file="$2" \
         --content-type=application/json --remote >/dev/null 2>"$DIR/artifacts/.r2put.log"; then
      return 0
    fi
    if [ "$attempt" -ge "$RETRIES" ]; then
      echo "r2 put FAILED ($1) after $attempt attempt(s):" >&2
      cat "$DIR/artifacts/.r2put.log" >&2
      return 1
    fi
    sleep $((5 * attempt))
    attempt=$((attempt + 1))
  done
}

: > "$NEW_MANIFEST"

# summary.json: always upload (counts change most runs); kept out of the manifest.
put "polygons/summary.json" "$SRC/polygons/summary.json"

n=0        # uploaded (changed) this run
skipped=0  # unchanged, skipped
for f in "$SRC"/polygons/m/*.json; do
  key="polygons/m/$(basename "$f")"
  h="$(sha "$f")"
  if [ "$h" = "$(old_sha "$key")" ]; then
    skipped=$((skipped + 1))
  else
    put "$key" "$f"
    n=$((n + 1))
    printf '\r   uploaded %d changed muni blob(s)' "$n"
  fi
  printf '%s  %s\n' "$h" "$key" >> "$NEW_MANIFEST"
done
[ "$n" -gt 0 ] && echo

# Clean pass -> commit the manifest so the next run can diff against it.
mv "$NEW_MANIFEST" "$MANIFEST"
echo "   r2 sync complete -> $BUCKET ($n uploaded, $skipped unchanged + summary)"
