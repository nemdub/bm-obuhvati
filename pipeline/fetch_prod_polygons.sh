#!/bin/sh
# Fetch the CURRENT production automated polygon blobs from R2 into a local staging dir, so the
# volunteer combine (combine_volunteer.py) merges against production truth rather than a possibly
# stale local stage06 output. Read-only counterpart to sync_r2.sh (get instead of put).
#
# Pulls EVERY municipality (the combined dataset covers all munis, not just the volunteer ones). The
# id list is the local stage06 blob set (artifacts/r2/polygons/m/*.json — the canonical ~165 munis);
# if that is absent it falls back to the volunteer-mapped ids in artifacts/volunteer-compare/mapping.csv.
# Idempotent: each object -> artifacts/prod-r2/polygons/m/<id>.json; already-present files are skipped
# (FORCE=1 re-fetches). Individual fetch failures are reported but do not abort the run.
#
# Usage: fetch_prod_polygons.sh [bucket]
#   env: R2_RETRIES (default 4), FORCE=1 (re-fetch even if the local file already exists)
DIR="$(cd "$(dirname "$0")" && pwd)"
BUCKET="${1:-bm-obuhvati-polygons}"
RETRIES="${R2_RETRIES:-4}"
MAPPING="$DIR/artifacts/volunteer-compare/mapping.csv"
LOCAL_BLOBS="$DIR/artifacts/r2/polygons/m"
DEST="$DIR/artifacts/prod-r2/polygons/m"
mkdir -p "$DEST"

# muni id list: prefer the full local stage06 blob set; else the volunteer-mapped ids.
if ls "$LOCAL_BLOBS"/*.json >/dev/null 2>&1; then
  ids="$(ls "$LOCAL_BLOBS"/*.json | xargs -n1 basename | sed 's/\.json$//' | sort -u)"
elif [ -f "$MAPPING" ]; then
  ids="$(awk -F, 'NR>1 && $6 != "" { print $6 }' "$MAPPING" | sort -u)"
else
  echo "fetch_prod_polygons: no local blob set and no $MAPPING to derive muni ids from" >&2
  exit 2
fi
cd "$DIR/../worker"

get() {  # key file
  attempt=1
  while :; do
    if npx wrangler r2 object get "$BUCKET/$1" --file="$2" --remote >/dev/null 2>"$DIR/artifacts/.r2get.log"; then
      return 0
    fi
    if [ "$attempt" -ge "$RETRIES" ]; then
      echo "" >&2; echo "r2 get FAILED ($1) after $attempt attempt(s):" >&2
      cat "$DIR/artifacts/.r2get.log" >&2
      return 1
    fi
    sleep $((5 * attempt))
    attempt=$((attempt + 1))
  done
}

n=0; skipped=0; failed=0
for id in $ids; do
  out="$DEST/$id.json"
  if [ "${FORCE:-0}" != 1 ] && [ -s "$out" ]; then
    skipped=$((skipped + 1))
    continue
  fi
  if get "polygons/m/$id.json" "$out"; then
    n=$((n + 1))
    printf '\r   fetched %d muni blob(s)' "$n"
  else
    failed=$((failed + 1))
  fi
done
[ "$n" -gt 0 ] && echo
echo "   prod polygon fetch complete <- $BUCKET ($n fetched, $skipped already present, $failed failed)"
[ "$failed" -gt 0 ] && echo "   NOTE: $failed muni blob(s) missing in production — those munis will be skipped by the combine." >&2
exit 0
