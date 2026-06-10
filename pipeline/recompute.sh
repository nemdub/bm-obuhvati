#!/bin/sh
# Review -> recompute loop, in one command:
#   1. fetch reviewer overrides from remote D1
#   2. stage04 (match, honoring overrides) -> stage05 (Voronoi) -> stage06 (build SQL)
#   3. import the derived tables into remote D1
#
# Touches only derived tables (delete+reload); never addresses or segment_overrides.
# Safe to re-run any time. Stages 01-03 are NOT run (use them only when source data
# or the parser changed).
#
# Usage:
#   ./recompute.sh               # full loop
#   ./recompute.sh --no-fetch    # reuse existing artifacts/overrides.json
#   ./recompute.sh --no-import   # rebuild locally, skip the remote D1 import
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../.venv/bin/python"
FETCH=1
IMPORT=1
for arg in "$@"; do
  case "$arg" in
    --no-fetch)  FETCH=0 ;;
    --no-import) IMPORT=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "== recompute started $(date +%H:%M:%S) =="

if [ "$FETCH" = 1 ]; then
  echo "-- fetching reviewer overrides from remote D1"
  "$DIR/fetch_overrides.sh"
else
  echo "-- skipping fetch (using existing artifacts/overrides.json)"
fi

echo "-- stage04: matching (with overrides)"
"$PY" "$DIR/stage04_match_addresses.py"
echo "-- stage05: Voronoi polygons"
"$PY" "$DIR/stage05_voronoi.py"
echo "-- stage06: build import SQL"
"$PY" "$DIR/stage06_build_sqlite.py" >/dev/null

if [ "$IMPORT" = 1 ]; then
  echo "-- importing derived tables into remote D1"
  cd "$DIR/../worker"
  npx wrangler d1 execute bm-obuhvati --remote --file="$DIR/artifacts/import_derived.sql" \
    | grep -E "rows_written|error" || true
fi

echo "== recompute finished $(date +%H:%M:%S) =="
