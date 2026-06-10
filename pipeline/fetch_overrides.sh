#!/bin/sh
# Export reviewer overrides (segment_overrides) from remote D1 into
# pipeline/artifacts/overrides.json, for stage04 to consume on the next pipeline run.
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
