#!/bin/sh
# Apply a (possibly large) .sql file to remote D1 in safe chunks, with retry/backoff.
#
# `wrangler d1 execute --remote --file` runs the whole file as one operation, which D1
# kills if it exceeds its per-execution CPU time limit. This splits the file into ordered
# chunks and applies them sequentially. Chunks are contiguous slices of the original
# ordering (deletes -> parents -> children), so foreign keys hold across chunk boundaries.
# Each chunk is its own atomic D1 transaction (D1 rolls a failed chunk back), so a chunk can
# be safely retried, and a re-run of recompute regenerates the same DELETE+INSERT dump and
# heals any partial progress (the deletes wipe half-applied rows first).
#
# D1 also throws *transient* errors under sustained write churn ("storage operation exceeded
# timeout / object was reset", 429/503, "Network connection lost"). Those are retryable, so
# each chunk is retried with linear backoff, and chunks are paced apart to let D1's storage
# object settle. A non-transient error (e.g. a SQL/FK error) fails fast without retrying.
#
# Usage: d1_apply.sh <sqlfile> [stmts_per_chunk]
#   env: D1_RETRIES (default 6), D1_PACE_SECS (default 2)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../.venv/bin/python"
SQL="$1"
STMTS="${2:-20}"
RETRIES="${D1_RETRIES:-6}"
PACE="${D1_PACE_SECS:-2}"
[ -f "$SQL" ] || { echo "d1_apply: no such file: $SQL" >&2; exit 2; }

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
TOTAL="$("$PY" "$DIR/split_sql.py" "$SQL" "$TMPD" "$STMTS")"
echo "   applying $(basename "$SQL") in $TOTAL chunk(s); retries=$RETRIES pace=${PACE}s"

cd "$DIR/../worker"
i=0
for cf in "$TMPD"/chunk.*.sql; do
  [ -s "$cf" ] || continue
  i=$((i + 1))
  attempt=1
  while :; do
    if npx wrangler d1 execute bm-obuhvati --remote --file="$cf" >"$TMPD/out.log" 2>&1; then
      printf '   chunk %d/%s ok%s\n' "$i" "$TOTAL" \
        "$(grep -oE 'rows_written[^,}]*' "$TMPD/out.log" | tail -1 | sed 's/^/ — /')"
      break
    fi
    # Retry only transient D1/network errors; fail fast on real SQL/FK errors.
    if ! grep -qiE 'reset|timeout|exceeded|storage|overloaded|internal error|connection|network|fetch failed|50[0-9]|429' "$TMPD/out.log" \
       || [ "$attempt" -ge "$RETRIES" ]; then
      echo "   chunk $i/$TOTAL FAILED after $attempt attempt(s):" >&2
      cat "$TMPD/out.log" >&2
      exit 1
    fi
    wait=$((10 * attempt))
    echo "   chunk $i/$TOTAL attempt $attempt hit a transient D1 error; retrying in ${wait}s..." >&2
    sleep "$wait"
    attempt=$((attempt + 1))
  done
  sleep "$PACE"
done
echo "   import complete ($TOTAL chunk(s))"
