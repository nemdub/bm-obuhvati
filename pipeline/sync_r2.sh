#!/bin/sh
# Upload the per-municipality polygon blobs (stage06's artifacts/r2/polygons/**) to the R2
# bucket the Worker reads via the POLY binding. Each object is independently valid, so a
# partial run just leaves some munis stale and a re-run heals it (like D1's delete+reload).
# Transient R2/network errors are retried with linear backoff.
#
# Usage: sync_r2.sh [bucket]
#   env: R2_RETRIES (default 4)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BUCKET="${1:-bm-obuhvati-polygons}"
RETRIES="${R2_RETRIES:-4}"
SRC="$DIR/artifacts/r2"
[ -d "$SRC/polygons" ] || { echo "sync_r2: no $SRC/polygons (run stage06 first)" >&2; exit 2; }
cd "$DIR/../worker"

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

put "polygons/summary.json" "$SRC/polygons/summary.json"
n=0
for f in "$SRC"/polygons/m/*.json; do
  put "polygons/m/$(basename "$f")" "$f"
  n=$((n + 1))
  printf '\r   uploaded %d muni blob(s)' "$n"
done
echo
echo "   r2 sync complete -> $BUCKET ($n munis + summary)"
