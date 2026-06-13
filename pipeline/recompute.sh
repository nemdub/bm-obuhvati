#!/bin/sh
# Review -> recompute loop, in one command:
#   1. fetch reviewer overrides + the dirty-station snapshot from remote D1
#   2. stage04 (match, honoring overrides) -> stage05 (Voronoi)
#   3. stage06 build the (small) derived dump + per-municipality R2 polygon blobs
#   4. upload polygon blobs to R2, then write the derived rows to remote D1
#   5. clear the `dirty` flag for the stations that were just synced
#
# Touches only derived tables + station_status.dirty; never addresses or segment_overrides.
# Safe to re-run any time (idempotent). Stages 01-03 are NOT run (use them only when source
# data or the parser changed).
#
# The byte-heavy polygons now live in R2 (per-muni GeoJSON blobs, uploaded only when their
# content changed), and the write-only station_address_links table is no longer shipped. The
# D1 derived dump is small text (segments + stations + amendments) and is shipped as a per-row
# delta (UPSERT changed rows, DELETE vanished ones) vs. the last successful import — so a
# recompute that re-matched a few stations writes only those rows, not all ~76k every run.
#
# Usage:
#   ./recompute.sh             # recompute everything; full reload of the derived dump
#   ./recompute.sh --no-fetch  # reuse existing artifacts/overrides.json + snapshot
#   ./recompute.sh --no-import # rebuild locally, skip the remote R2/D1 import (and clear)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../.venv/bin/python"
FETCH=1
IMPORT=1
for arg in "$@"; do
  case "$arg" in
    --no-fetch)   FETCH=0 ;;
    --no-import)  IMPORT=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "== recompute started $(date +%H:%M:%S) =="

if [ "$FETCH" = 1 ]; then
  echo "-- fetching reviewer overrides + dirty snapshot from remote D1"
  "$DIR/fetch_overrides.sh"
else
  echo "-- skipping fetch (using existing artifacts/overrides.json + dirty_snapshot.json)"
fi

echo "-- stage04: matching (with overrides)"
"$PY" "$DIR/stage04_match_addresses.py"
echo "-- stage05: Voronoi polygons"
"$PY" "$DIR/stage05_voronoi.py"
echo "-- stage06: build derived dump + R2 polygon blobs"
"$PY" "$DIR/stage06_build_sqlite.py" >/dev/null

if [ "$IMPORT" = 1 ]; then
  echo "-- uploading changed polygon blobs to R2"
  "$DIR/sync_r2.sh"

  IMPORT_OK=1
  DERIVED_SQL="$DIR/artifacts/import_derived.sql"
  if [ -s "$DERIVED_SQL" ]; then
    echo "-- importing derived rows into remote D1 (chunked + retry)"
    "$DIR/d1_apply.sh" "$DERIVED_SQL" || IMPORT_OK=0
  else
    echo "-- nothing to import; D1 already matches the recompute"
  fi

  if [ "$IMPORT_OK" = 1 ]; then
    # Import succeeded (or nothing to write) -> advance the derived-state manifest so the next
    # run diffs against what D1 now holds. Done only here, after a clean import, so a failed
    # import leaves the old manifest and the next run re-emits the same delta (idempotent).
    for nf in "$DIR"/artifacts/derived_state/*.tsv.new; do
      [ -e "$nf" ] || continue
      mv "$nf" "${nf%.new}"
    done

    # Clear dirty flags race-safely (a station re-edited during the run keeps its flag, so
    # its edit isn't lost — next run catches it).
    N="$("$PY" "$DIR/dirty_scope.py" clear-sql)"
    if [ "$N" -gt 0 ] 2>/dev/null; then
      echo "-- clearing dirty flag for $N station(s)"
      cd "$DIR/../worker"
      npx wrangler d1 execute bm-obuhvati --remote --file="$DIR/artifacts/clear_dirty.sql" \
        | grep -E "rows_written|error" || true
    else
      echo "-- no dirty flags to clear"
    fi
  else
    echo "!! import failed -- NOT clearing dirty flags and NOT advancing the derived-state" >&2
    echo "   manifest, so the next run re-emits the same delta (UPSERT/DELETE is idempotent)." >&2
    echo "   Just re-run ./recompute.sh." >&2
    exit 1
  fi
fi

echo "== recompute finished $(date +%H:%M:%S) =="
