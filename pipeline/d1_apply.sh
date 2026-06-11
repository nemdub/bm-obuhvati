#!/bin/sh
# Apply a (possibly large) .sql file to remote D1 in CPU-limit-safe chunks.
#
# `wrangler d1 execute --remote --file` runs the whole file as one operation, which D1
# kills if it exceeds its per-execution CPU time limit. This splits the file into ordered
# chunks of <= STMTS statements and applies them sequentially, aborting on the first error.
# Chunks are contiguous slices of the original ordering (deletes -> parents -> children),
# so foreign keys hold across chunk boundaries. Each chunk is its own D1 transaction, so a
# mid-run failure leaves earlier chunks applied — re-running recompute regenerates the same
# DELETE+INSERT dump and heals it (the deletes wipe any half-applied rows first).
#
# Usage: d1_apply.sh <sqlfile> [stmts_per_chunk]
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../.venv/bin/python"
SQL="$1"
STMTS="${2:-20}"   # ~20 statements ≈ 10k rows/chunk — comfortably under the CPU limit
[ -f "$SQL" ] || { echo "d1_apply: no such file: $SQL" >&2; exit 2; }

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
TOTAL="$("$PY" "$DIR/split_sql.py" "$SQL" "$TMPD" "$STMTS")"
echo "   applying $(basename "$SQL") in $TOTAL chunk(s) of <= $STMTS statements"

cd "$DIR/../worker"
i=0
for cf in "$TMPD"/chunk.*.sql; do
  [ -s "$cf" ] || continue
  i=$((i + 1))
  if npx wrangler d1 execute bm-obuhvati --remote --file="$cf" >"$TMPD/out.log" 2>&1; then
    printf '   chunk %d/%s ok%s\n' "$i" "$TOTAL" \
      "$(grep -oE 'rows_written[^,}]*' "$TMPD/out.log" | tail -1 | sed 's/^/ — /')"
  else
    echo "   chunk $i/$TOTAL FAILED:" >&2
    cat "$TMPD/out.log" >&2
    exit 1
  fi
done
echo "   import complete ($TOTAL chunk(s))"
