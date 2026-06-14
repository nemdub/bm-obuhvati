#!/bin/sh
# Export reviewer overrides (segment_overrides) from remote D1 into
# pipeline/artifacts/overrides.json, for stage04 to consume on the next pipeline run.
# Also snapshots the per-station `dirty` flags so recompute.sh can scope an incremental
# rebuild to the touched municipalities and clear the flags race-safely afterwards.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DIR/artifacts"
cd "$DIR/../worker"
npx wrangler d1 execute bm-obuhvati --remote --json \
  --command "SELECT segment_id, manual_json, manual_street_id, reviewed FROM segment_overrides" \
  | "$DIR/../.venv/bin/python" -c "
import sys, json
data = json.load(sys.stdin)
rows = data[0]['results'] if data and 'results' in data[0] else []
json.dump(rows, open('$DIR/artifacts/overrides.json', 'w'), ensure_ascii=False)
print(f'wrote {len(rows)} overrides -> artifacts/overrides.json')
"
npx wrangler d1 execute bm-obuhvati --remote --json \
  --command "SELECT id, station_id, street_id, manual_json FROM station_added_segments" \
  | "$DIR/../.venv/bin/python" -c "
import sys, json
data = json.load(sys.stdin)
rows = data[0]['results'] if data and 'results' in data[0] else []
json.dump(rows, open('$DIR/artifacts/additions.json', 'w'), ensure_ascii=False)
print(f'wrote {len(rows)} added street claims -> artifacts/additions.json')
"
npx wrangler d1 execute bm-obuhvati --remote --json \
  --command "SELECT station_id, updated_at FROM station_status WHERE dirty = 1" \
  | "$DIR/../.venv/bin/python" -c "
import sys, json
data = json.load(sys.stdin)
rows = data[0]['results'] if data and 'results' in data[0] else []
json.dump(rows, open('$DIR/artifacts/dirty_snapshot.json', 'w'), ensure_ascii=False)
print(f'wrote {len(rows)} dirty stations -> artifacts/dirty_snapshot.json')
"

# Station-level edits (stage03c_reconcile_edits consumes these): corrected source text,
# brand-new stations, and tombstones. See docs/parsing-matching/10-station-edits.md.
npx wrangler d1 execute bm-obuhvati --remote --json \
  --command "SELECT station_id, raw_coverage_text FROM station_text_overrides" \
  | "$DIR/../.venv/bin/python" -c "
import sys, json
data = json.load(sys.stdin)
rows = data[0]['results'] if data and 'results' in data[0] else []
json.dump(rows, open('$DIR/artifacts/text_overrides.json', 'w'), ensure_ascii=False)
print(f'wrote {len(rows)} text overrides -> artifacts/text_overrides.json')
"
npx wrangler d1 execute bm-obuhvati --remote --json \
  --command "SELECT id, municipality_id, number, name_cyr, address_cyr, raw_coverage_text FROM added_stations" \
  | "$DIR/../.venv/bin/python" -c "
import sys, json
data = json.load(sys.stdin)
rows = data[0]['results'] if data and 'results' in data[0] else []
json.dump(rows, open('$DIR/artifacts/added_stations.json', 'w'), ensure_ascii=False)
print(f'wrote {len(rows)} added stations -> artifacts/added_stations.json')
"
npx wrangler d1 execute bm-obuhvati --remote --json \
  --command "SELECT station_id FROM removed_stations" \
  | "$DIR/../.venv/bin/python" -c "
import sys, json
data = json.load(sys.stdin)
rows = data[0]['results'] if data and 'results' in data[0] else []
json.dump(rows, open('$DIR/artifacts/removed_stations.json', 'w'), ensure_ascii=False)
print(f'wrote {len(rows)} removed stations -> artifacts/removed_stations.json')
"
