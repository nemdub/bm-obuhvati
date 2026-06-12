# Pipeline unit tests

Unit tests for the parsing and matching rules documented in
[`docs/parsing-matching/`](../../docs/parsing-matching/README.md). Every test references the
spec section it pins.

## Running

```bash
# from the repo root or pipeline/
.venv/bin/python -m pip install -e "pipeline[dev]"   # or: pip install pytest
.venv/bin/python -m pytest pipeline                  # or: cd pipeline && pytest
```

`conftest.py` puts the pipeline directory on `sys.path` so `import config` /
`from common.x import y` resolve. The tests are pure‑function only — no parquet, no
`textutil`, no network — so they run in well under a second.

## Layout

| File | Module under test | Spec |
|------|-------------------|------|
| `test_transliterate.py` | `common/transliterate.py` | 01 §1.8 |
| `test_normalize.py` | `common/normalize.py` | 01 |
| `test_coverage_parse.py` | `common/coverage_parse.py` | 02 |
| `test_document_extraction.py` | `stage02_extract_docs.py` (pure helpers) | 03 |
| `test_amendments.py` | `stage03b_apply_amendments.py` (pure helpers) | 04 |
| `test_claim_resolution.py` | `stage04` `resolve_street_claims` + helpers | 06 |

## Notes / latent issues surfaced by these tests

- **`lat_to_cyr("dj")` → `"дј"`, not `"ђ"`.** The ASCII fallback pair `("dj","ђ")` is listed
  *after* the single `("d","д")` pair, so greedy matching consumes `d` first and the digraph
  never fires. `test_dj_fallback_is_shadowed_by_single_d` documents the current behavior; if
  the pair is ever reordered to fix it, flip that test deliberately.

## Not covered here (integration, not unit)

- `resolve_street` / `build_indexes` (stage04) — need the register parquet; best covered by a
  small fixture‑backed integration test.
- stage02 file globbing + `textutil` shelling; stage05 Voronoi geometry; the D1/R2 build.
- The Worker TypeScript mirror (`worker/src/db.ts`) — see spec 08; would use a JS test runner
  (vitest) and should be pinned against these Python cases so the two can't drift.
</content>
