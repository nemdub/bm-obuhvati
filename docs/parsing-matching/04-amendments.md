# 4. Amendments

Code: `pipeline/stage03b_apply_amendments.py`

Amendment documents (izmena / dopuna / ispravka) are **surgical prose operations** keyed by
station number + street. stage03b parses each bullet into a typed op, applies it to the
matching base segment, records every op in an audit table, and tags touched/created segments
`source='amendment'`. stage04 then **force‑flags every amended segment for review**.

> Test target: `parse_bullet`, `street_matches`, `_nums_from`, the three op regexes, and the
> apply logic in `main`.

## 4.1 Bullet anchoring

Bullets are anchored on `BULLET = "Гласачко место број (\d+)"`. The text between one anchor
and the next is one bullet for that station number. The instruction is the part after the
first `:` (if any), stripped of `•`.

Station lookup: `(municipality_id, printed_number) -> station_id` via `station_by_num`
(first wins on duplicate numbers; such cases are flagged via `needs_review` anyway). If the
station can't be found (we never extracted it), the op is skipped.

## 4.2 Op classification (`parse_bullet`)

Three typed ops, tried in order. `Q = [„“"']` matches Serbian and ASCII quotes.

| Op | Regex (`RE_*`) | Fields |
|----|----------------|--------|
| `fix_street_name` | `назив улице „X" се исправља … гласи: „Y"` | `street/old = X`, `new = Y` |
| `replace_range` | `у улици X распон кућних бројева (од) OLD мења се … гласи: „NEW"` | `street = X`, `old = OLD`, `new = NEW` |
| `add_house` | `у улици X (после кућног броја P) додаје се (кућни) број N` | `street = X`, `old = P`, `new = N` |

A bullet matching none → audit op `other` (unparsed; counted in the run summary).

## 4.3 Street matching within a station (`street_matches`)

**Rule:** an amendment street matches a base segment's street if, after `normalize_street`,
either name **equals** the other or is a **substring** of the other:

```python
na == nb or na in nb or nb in na   # (both non-empty)
```

This substring tolerance handles minor name differences between the amendment prose and the
base table.

## 4.4 Applying ops

- **`fix_street_name`**: find the segment whose street matches `old` (fallback to `street`),
  set `street_raw = new`.
- **`replace_range`**: if no matching segment exists, **create** one (street = op street,
  numbers from `new`); else remove the `old` intervals/singles, then extend with `new`'s, and
  set `whole = False`. Number parsing reuses `_nums_from` → `parse_number_token` (same range/
  single/parity logic as the main parser).
- **`add_house`**: like replace but additive — create a segment if absent, else extend with
  `new` numbers (the `old` "after house P" anchor is captured but only the added number is
  applied).

`_nums_from(value)` splits on `,` and `… и …` and feeds each token to `parse_number_token`.

## 4.5 Auditing and flagging

- Every op (applied or not) is recorded in `amendments.parquet` with `applied`,
  `target_segment_id`, `op`, `old_value`, `new_value`, `raw_instruction`, `source_file`.
- Touched/created segments get `source = "amendment"` and `amendment_note = instruction`.
- Touched **stations** get `is_amendment = 1` in `stations.parquet`.
- stage04 adds review reason `amendment` to every such segment → always `needs_review`.

## 4.6 Known limitation

Only Subotica‑style phrasing parses today. ~45 amendment docs use different wording and
currently yield `other` ops (the base coverage is still loaded; only the surgical edit is
skipped). New phrasings are a natural place to add regexes + tests.
