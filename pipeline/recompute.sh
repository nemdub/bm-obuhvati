#!/bin/sh
# Review -> recompute loop, in one command:
#   1. fetch reviewer overrides + the dirty-station snapshot from remote D1
#   2. stage04 (match, honoring overrides) -> stage05 (Voronoi)
#   3. write the changed derived rows to remote D1 (full reload, or reconciled delta)
#   4. clear the `dirty` flag for the stations that were just synced
#
# Touches only derived tables + station_status.dirty; never addresses or segment_overrides.
# Safe to re-run any time (idempotent). Stages 01-03 are NOT run (use them only when source
# data or the parser changed).
#
# Usage:
#   ./recompute.sh               # recompute all municipalities; full truncate+reload import
#   ./recompute.sh --only-dirty  # recompute ONLY the municipalities with dirty stations
#                                #   (Voronoi is the bottleneck: ~160s -> ~10s) and write
#                                #   back the RECONCILED delta -- only the stations that
#                                #   actually differ from what's in D1 (minimal writes).
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

# Full mode builds the whole derived dump and reloads it (whole-table truncate + bulk load,
# which D1 handles efficiently). only-dirty reconciles instead: it reads the affected
# stations' current rows from D1, diffs against the recompute, and writes ONLY the stations
# that actually differ -- minimal D1 writes, and the few small targeted deletes stay well
# under D1's limits. (A "rewrite all affected links" partial does NOT work on D1: repeated
# targeted deletes over the full 1.9M-row links table trip its CPU/storage watchdog.)
if [ "$ONLY_DIRTY" != 1 ]; then
  echo "-- stage06: build full derived dump"
  "$PY" "$DIR/stage06_build_sqlite.py" >/dev/null
fi

if [ "$IMPORT" = 1 ]; then
  cd "$DIR/../worker"
  if [ "$ONLY_DIRTY" = 1 ]; then
    echo "-- reconcile: read affected rows from D1, diff, emit minimal import"
    "$PY" "$DIR/d1_reconcile.py" --municipalities "$MUNIS"
    DERIVED_SQL="$DIR/artifacts/import_reconcile.sql"
  else
    DERIVED_SQL="$DIR/artifacts/import_derived.sql"
  fi

  IMPORT_OK=1
  if [ -s "$DERIVED_SQL" ]; then
    echo "-- importing into remote D1 (chunked + retry)"
    "$DIR/d1_apply.sh" "$DERIVED_SQL" || IMPORT_OK=0
  else
    echo "-- nothing to import; D1 already matches the recompute"
  fi

  if [ "$IMPORT_OK" = 1 ]; then
    # Import succeeded (or nothing to write) -> clear dirty flags race-safely.
    N="$("$PY" "$DIR/dirty_scope.py" clear-sql)"
    if [ "$N" -gt 0 ] 2>/dev/null; then
      echo "-- clearing dirty flag for $N station(s)"
      npx wrangler d1 execute bm-obuhvati --remote --file="$DIR/artifacts/clear_dirty.sql" \
        | grep -E "rows_written|error" || true
    else
      echo "-- no dirty flags to clear"
    fi
  else
    echo "!! import failed -- NOT clearing dirty flags. The dump is delete+insert and" >&2
    echo "   reconcile re-derives from D1's current state, so just re-run:" >&2
    echo "   ./recompute.sh --only-dirty" >&2
    exit 1
  fi
fi

echo "== recompute finished $(date +%H:%M:%S) =="
