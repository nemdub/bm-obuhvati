#!/bin/sh
# Review -> recompute loop, in one command:
#   1. fetch reviewer overrides + the dirty-station snapshot from remote D1
#   2. stage04 (match, honoring overrides) -> stage05 (Voronoi) -> stage06 (build SQL)
#   3. import the derived tables into remote D1
#   4. clear the `dirty` flag for the stations whose polygons were just rebuilt
#
# Touches only derived tables (delete+reload) + station_status.dirty; never addresses or
# segment_overrides. Safe to re-run any time. Stages 01-03 are NOT run (use them only when
# source data or the parser changed).
#
# Usage:
#   ./recompute.sh               # full rebuild of all polygons
#   ./recompute.sh --only-dirty  # rebuild ONLY the municipalities with dirty stations
#                                #   (byte-identical to a full run for those stations,
#                                #    just far faster — Voronoi is the bottleneck)
#   ./recompute.sh --no-fetch    # reuse existing artifacts/overrides.json + snapshot
#   ./recompute.sh --no-import   # rebuild locally, skip the remote D1 import (and clear)
#
# After a successful import the dirty flags are cleared race-safely: a station re-edited
# during the run keeps its flag (the snapshot's updated_at no longer matches), so its
# edit isn't silently lost — the next recompute picks it up.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../.venv/bin/python"
FETCH=1
IMPORT=1
ONLY_DIRTY=0
for arg in "$@"; do
  case "$arg" in
    --no-fetch)   FETCH=0 ;;
    --no-import)  IMPORT=0 ;;
    --only-dirty) ONLY_DIRTY=1 ;;
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

# In --only-dirty mode, derive the group_rep municipalities to recompute from the snapshot.
SCOPE=""
if [ "$ONLY_DIRTY" = 1 ]; then
  MUNIS="$("$PY" "$DIR/dirty_scope.py" munis)"
  if [ -z "$MUNIS" ]; then
    echo "-- no dirty stations; nothing to recompute"
    echo "== recompute finished $(date +%H:%M:%S) =="
    exit 0
  fi
  echo "-- only-dirty: recomputing municipalities $MUNIS"
  SCOPE="--municipalities $MUNIS"
fi

echo "-- stage04: matching (with overrides) $SCOPE"
"$PY" "$DIR/stage04_match_addresses.py" $SCOPE
echo "-- stage05: Voronoi polygons $SCOPE"
"$PY" "$DIR/stage05_voronoi.py" $SCOPE

# Full mode rebuilds the whole derived dump (quietly); only-dirty emits a small scoped
# import_derived_partial.sql (station-keyed DELETE+INSERT) instead of the 200MB+ reload.
DERIVED_SQL="$DIR/artifacts/import_derived.sql"
echo "-- stage06: build import SQL $SCOPE"
if [ "$ONLY_DIRTY" = 1 ]; then
  "$PY" "$DIR/stage06_build_sqlite.py" $SCOPE
  DERIVED_SQL="$DIR/artifacts/import_derived_partial.sql"
else
  "$PY" "$DIR/stage06_build_sqlite.py" >/dev/null
fi

if [ "$IMPORT" = 1 ]; then
  echo "-- importing derived tables into remote D1"
  cd "$DIR/../worker"
  if npx wrangler d1 execute bm-obuhvati --remote --file="$DERIVED_SQL" \
       >"$DIR/artifacts/d1_import.log" 2>&1; then
    grep -E "rows_written|error" "$DIR/artifacts/d1_import.log" || true
    # Import succeeded -> clear the dirty flag for the snapshotted stations (race-safe).
    N="$("$PY" "$DIR/dirty_scope.py" clear-sql)"
    if [ "$N" -gt 0 ] 2>/dev/null; then
      echo "-- clearing dirty flag for $N station(s)"
      npx wrangler d1 execute bm-obuhvati --remote --file="$DIR/artifacts/clear_dirty.sql" \
        | grep -E "rows_written|error" || true
    else
      echo "-- no dirty flags to clear"
    fi
  else
    echo "!! import failed -- NOT clearing dirty flags; see artifacts/d1_import.log" >&2
    cat "$DIR/artifacts/d1_import.log" >&2
    exit 1
  fi
fi

echo "== recompute finished $(date +%H:%M:%S) =="
