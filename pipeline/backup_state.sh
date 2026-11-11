#!/bin/sh
# Snapshot the CURRENT state of the project (production D1 + production R2 polygon blobs + local
# pipeline artifacts + the station source docs and exports) into backups/<date>-<label>/, so a
# source switch or a risky re-run can be rolled back. Read-only against production.
#
# Usage: backup_state.sh [label]          (default label: "snapshot")
#   env: SKIP_REMOTE=1  skip the D1 export + R2 fetch (local artifacts only)
#        REMOTE_ONLY=1  add the remote parts to an EXISTING backup dir of the same date+label
#                       (use after a SKIP_REMOTE=1 run, e.g. once `wrangler login` is done);
#                       BACKUP_DIR=<path> targets a specific existing backup dir instead
#
# Layout:
#   d1-full.sql            full remote D1 dump (schema + every table, incl. the Worker-owned
#                          review tables: segment_overrides, station_added_segments,
#                          station_text_overrides, added_stations, removed_stations, station_status)
#   review-json/           the same review tables as JSON (fetch_overrides.sh output)
#   artifacts/             pipeline/artifacts minus the regenerable bulk (bm.sqlite, import_addresses.sql);
#                          artifacts/prod-r2/ = production polygon blobs (polygons/m/<muni>.json + summary.json)
#   data/                  polling_stations_2022/, exports/, volunteer-combined/
#   MANIFEST.md, SHA256SUMS, RESTORE.md
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
LABEL="${1:-snapshot}"
DEST="${BACKUP_DIR:-$ROOT/backups/$(date +%Y-%m-%d)-$LABEL}"
PY="$ROOT/.venv/bin/python"
BUCKET="bm-obuhvati-polygons"

if [ "${REMOTE_ONLY:-0}" = 1 ]; then
  [ -d "$DEST" ] || { echo "backup_state: REMOTE_ONLY needs an existing $DEST" >&2; exit 2; }
elif [ -e "$DEST" ]; then
  echo "backup_state: $DEST already exists — refusing to overwrite" >&2
  exit 2
fi
mkdir -p "$DEST"
echo "==> backup -> $DEST"

if [ "${SKIP_REMOTE:-0}" != 1 ]; then
  echo "==> D1 full export (remote)"
  ( cd "$ROOT/worker" && npx wrangler d1 export bm-obuhvati --remote --output="$DEST/d1-full.sql" )

  echo "==> review tables as JSON"
  "$DIR/fetch_overrides.sh"
  mkdir -p "$DEST/review-json"
  for f in overrides additions text_overrides added_stations removed_stations dirty_snapshot; do
    cp "$DIR/artifacts/$f.json" "$DEST/review-json/"
  done

  echo "==> production R2 polygon blobs"
  FORCE=1 "$DIR/fetch_prod_polygons.sh" "$BUCKET"
  ( cd "$ROOT/worker" && npx wrangler r2 object get "$BUCKET/polygons/summary.json" \
      --file="$DIR/artifacts/prod-r2/polygons/summary.json" --remote >/dev/null )
  mkdir -p "$DEST/artifacts/prod-r2"
  rsync -a "$DIR/artifacts/prod-r2/" "$DEST/artifacts/prod-r2/"
fi

if [ "${REMOTE_ONLY:-0}" != 1 ]; then
  echo "==> local pipeline artifacts"
  mkdir -p "$DEST/artifacts"
  rsync -a \
    --exclude 'bm.sqlite' --exclude 'import_addresses.sql' \
    --exclude '*.log' --exclude '.r2get.log' \
    "$DIR/artifacts/" "$DEST/artifacts/"

  echo "==> source docs + exports"
  mkdir -p "$DEST/data"
  for d in polling_stations_2022 exports volunteer-combined; do
    if [ -d "$ROOT/data/$d" ]; then cp -R "$ROOT/data/$d" "$DEST/data/"; fi
  done
fi

echo "==> manifest"
( cd "$DEST" && find . -type f ! -name SHA256SUMS ! -name MANIFEST.md ! -name RESTORE.md -print0 \
    | sort -z | xargs -0 shasum -a 256 > SHA256SUMS )
"$PY" - "$DEST" "$ROOT" <<'EOF'
import json, pathlib, re, subprocess, sys
dest, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
dirty = subprocess.run(["git", "-C", str(root), "status", "--short"], capture_output=True, text=True).stdout.strip()
out = [f"# Backup manifest — {dest.name}", "", f"- git HEAD: `{sha}`",
       f"- uncommitted changes: {'none' if not dirty else '`' + dirty.replace(chr(10), '`, `') + '`'}", ""]
dump = dest / "d1-full.sql"
if dump.exists():
    counts = {}
    with dump.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r'INSERT INTO "?([A-Za-z_0-9]+)"?', line)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    out += ["## D1 rows in d1-full.sql (INSERT statements per table)", "", "| table | rows |", "|---|---:|"]
    out += [f"| {t} | {n} |" for t, n in sorted(counts.items())] + [""]
rj = dest / "review-json"
if rj.exists():
    out += ["## Review tables (JSON)", "", "| file | rows |", "|---|---:|"]
    out += [f"| {p.name} | {len(json.loads(p.read_text()))} |" for p in sorted(rj.glob("*.json"))] + [""]
try:
    import polars as pl
    out += ["## Local artifact parquets", "", "| file | rows |", "|---|---:|"]
    for p in sorted((dest / "artifacts").glob("*.parquet")):
        out.append(f"| {p.name} | {pl.scan_parquet(p).select(pl.len()).collect().item()} |")
    out.append("")
except Exception as e:  # manifest is best-effort
    out += [f"(parquet row counts unavailable: {e})", ""]
blobs = list((dest / "artifacts" / "prod-r2").rglob("*.json"))
out += [f"- production R2 blobs: {len(blobs)}",
        f"- files total: {sum(1 for _ in dest.rglob('*') if _.is_file())}", ""]
(dest / "MANIFEST.md").write_text("\n".join(out), encoding="utf-8")
EOF

cat > "$DEST/RESTORE.md" <<EOF
# Restore from this backup

Snapshot of production + local state taken $(date '+%Y-%m-%d %H:%M') before a source switch.
Run from the repo root. Verify integrity first: \`(cd $DEST && shasum -a 256 -c SHA256SUMS | grep -v ': OK')\`.

## 1. Code
    git checkout pre-bm-source        # or: STATION_SOURCE=rik with current code

## 2. Local artifacts + source docs
    rsync -a "$DEST/artifacts/" pipeline/artifacts/
    cp -R "$DEST/data/polling_stations_2022" data/
    cp "$DEST"/review-json/*.json pipeline/artifacts/   # only present after the remote part ran
bm.sqlite and import_addresses.sql are not in the backup; regenerate with stage06 if needed.

## 3. Production R2 (polygon blobs)
    cd worker
    for f in "$DEST"/artifacts/prod-r2/polygons/m/*.json; do
      npx wrangler r2 object put "$BUCKET/polygons/m/\$(basename "\$f")" --file="\$f" --remote
    done
    npx wrangler r2 object put "$BUCKET/polygons/summary.json" --file="$DEST/artifacts/prod-r2/polygons/summary.json" --remote

## 4. Production D1
The dump holds schema + all rows. Either restore the whole database into a fresh D1 and repoint
\`worker/wrangler.jsonc\`:
    npx wrangler d1 create bm-obuhvati-restore
    npx wrangler d1 execute bm-obuhvati-restore --remote --file="$DEST/d1-full.sql"
or restore only the reviewer tables into the live database (delete their rows first), extracting
the relevant INSERTs:
    grep -E '^INSERT INTO "?(segment_overrides|station_added_segments|station_text_overrides|added_stations|removed_stations|station_status)"? ' \\
      "$DEST/d1-full.sql" > /tmp/review-restore.sql
    ../pipeline/d1_apply.sh /tmp/review-restore.sql
D1 Time Travel (\`wrangler d1 time-travel restore\`) is an additional option within 30 days.
EOF

du -sh "$DEST" | awk '{print "==> done: " $2 " (" $1 ")"}'
