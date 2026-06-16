# 6. Claim resolution (house assignment)

Code: `pipeline/stage04_match_addresses.py` — `resolve_street_claims`, `_parity_ok`,
`_bounds_ok`, `_iv_parity`, plus the old‑name‑dup pass in `main`.

Once each segment is resolved to a register street, every station's **claims** on that
street are reconciled against the street's real register houses. **Each register house goes
to exactly one station.**

> Test target: `resolve_street_claims`, `_bounds_ok`, `_parity_ok`, the specificity
> constants, and the `old_name_dup` filter.

## 6.1 Claim kinds and specificity

A claim is one of these kinds; higher specificity wins a contested house:

| Kind | Constant | Value | Claims |
|------|----------|-------|--------|
| exact single (num + suffix) | `SPEC_EXACT_SINGLE` | 3 | the exact `(num, suffix)` house |
| bare single → suffixed | `SPEC_IMPLIED_SINGLE` | 2 | `5` also claims `5а/5б/…` |
| interval | `SPEC_INTERVAL` | 1 | houses in `[lo,hi]` (parity/suffix‑bounded) |
| `бб` (bez_broja) | `SPEC_BEZ_BROJA` | 1 | only `house_num IS NULL` houses |
| interval, implied suffix | `SPEC_INTERVAL_IMPLIED_SUFFIX` | 0.5 | a *suffixed* house reached only via a **bare** range bound (e.g. `2‑60` → `60а`) — yields to an explicit suffix claim (see §6.3) |
| whole street | `SPEC_WHOLE` | 0 | every house incl. NULL‑house |
| settlement (village) | `SPEC_SETT_WHOLE` | −1 | every street of a settlement |

For each register house `(aid, num, suffix)`:
- collect candidate claims that match it, each with its specificity,
- take the **max specificity**,
- if all top claims belong to **one station** → assign the house to it,
- if top claims span **multiple stations** → **conflict** (record opposing station ids on
  each segment; the house is left unassigned).

## 6.2 Bare number implies suffixed variants (`SPEC_IMPLIED_SINGLE`)

**Rule:** a bare single `5` claims `5, 5а, 5б, …` **unless** another station explicitly lists
the exact suffixed address (which wins at `SPEC_EXACT_SINGLE`).

Implementation in the per‑house loop:
- `c["num"] == num and c["suffix"] == suf` → `SPEC_EXACT_SINGLE` (incl. bare matching a bare
  register house).
- `c["num"] == num and c["suffix"] == "" and suf != ""` → `SPEC_IMPLIED_SINGLE` (bare claim,
  suffixed house).

### Example

Street has houses `5`, `5а`. Station A claims `5`, station B claims `5а` exactly:
- `5` → A (exact bare↔bare).
- `5а` → B wins at spec 3 over A's implied spec 2.

## 6.3 Intervals: parity + suffix bounds

A house at `num`/`suffix` is in an interval claim iff **all** hold:

1. `lo <= num <= hi`,
2. `_parity_ok(num, parity)` — `parity=="all"`, or odd/even matches `num`,
3. `_bounds_ok(num, suffix, claim)` — suffix bounds at the edges.

### `_bounds_ok` (suffix‑bounded ranges)

- At the **lo** edge: if the claim has `losfx` and `suffix_rank(suffix) < suffix_rank(losfx)`
  → excluded. `12б-16` starts at `12б` (12 and 12а excluded).
- At the **hi** edge: if the claim has `hisfx` and `suffix_rank(suffix) > suffix_rank(hisfx)`
  → excluded. `1-23ц` ends at `23ц` (`23`, `23д` included since `Д < Ц` in azbuka; `23ш`
  excluded).
- An **empty** bound suffix keeps historical behavior: **all** suffixed variants at that
  number match.

### Bare bound implies suffixes, but yields to an explicit suffix claim (`_interval_spec`)

The empty‑bound rule above means a bare range `2-60` *covers* `60а`. But when **another
station** names that suffix explicitly — an exact single `60а`, or a suffix‑bounded range edge
like `60а-80` (`losfx="А"`) — the bare range should **yield** `60а`, not raise a spurious
`conflict` ("Адресе се преклапају…").

`_interval_spec(num, suf, claim)` demotes the implied match: a **suffixed** house (`suf != ""`)
matched only because a **bare** bound implies all suffixes (no `losfx`/`hisfx` pinning that
edge — interior houses count as implied too) scores `SPEC_INTERVAL_IMPLIED_SUFFIX = 0.5`
instead of `SPEC_INTERVAL = 1`. Membership in the interval is unchanged (`_bounds_ok` still
returns True) — only the *ranking* of who wins the house changes:

- exact single `60а` (spec 3) or suffix‑bounded edge `60а-80` (spec 1) **beats** the bare
  range's implied `60а` (spec 0.5).
- a **lone** bare range still picks up `60а` (uncontested, 0.5 is the max).
- **two** bare ranges both implying `60а` tie at 0.5 → genuine `conflict`, still flagged.

Bare houses (`suf == ""`) always score `SPEC_INTERVAL`, so e.g. plain `60` stays with the
`2-60` station while `60а` goes to the `60а-80` station.

### Parity element source (`_iv_parity`)

The interval's parity is element `iv[2]` if present, else recomputed from the bounds via
`interval_parity(lo, hi)`. Suffix bounds are `iv[3]` (lo) and `iv[4]` (hi).

## 6.4 Parity validation (`parity_unconfirmed`)

Parity is **inferred**, so stage04 validates each odd/even assumption against sibling
coverage:

**Rule:** for an interval claim with `parity != "all"`, find the houses on the
**complementary** side within `[lo, hi]`. If such houses exist but **none** is covered by
another station, the assumption is unconfirmed → add the segment id to `parity_unconfirmed`.
If no complementary houses exist, the split is moot (skipped).

### Informational only (since 2026‑06‑11)

`parity_unconfirmed` **no longer triggers review on its own** — the inferred side has proven
correct in the vast majority of cases. It is still recorded in `review_reason` (shown as
context when the segment is flagged for some *other* reason), but the final `needs_review` is
computed from `reasons - {parity_unconfirmed}`. See [07](07-review-flags.md).

## 6.5 `бб` / bez_broja claims

**Rule:** a `bez_broja` claim matches **only** `house_num IS NULL` houses (`num is None`), at
`SPEC_BEZ_BROJA = 1`. `whole` and `sett_whole` **also** cover NULL houses (user decision: a
plain whole‑street claim covers no‑number houses too). Because `бб` (spec 1) outranks `whole`
(spec 0) on a NULL house, an explicit `бб` wins over a generic whole there.

`бб` is **additive** in claim building: a segment with `bez_broja` emits a `bez_broja` claim
**in addition** to any interval/single claims. Interval/single claims are guarded to
`num is not None` (they need a real number).

## 6.6 Whole / settlement claims cover NULL houses

In the per‑house loop, `whole` and `sett_whole` are added as candidates **unconditionally**
(including when `num is None`). All other kinds `continue` on `num is None`. So:

- `whole` claim → every house of the street, numbered or not.
- `sett_whole` (village) → every street of the settlement, at spec −1, yielding to any
  street‑level claim (including another station's whole‑street claim).

### Rationale for `SPEC_SETT_WHOLE = -1`

At spec 0 a village claim tied with sibling stations' whole‑street claims and knocked out
their links (conflicts 1.5k → 5.3k). Dropping it to −1 makes it yield to **any** street‑level
claim. 2,666 such segments, +352k links.

## 6.7 Old‑name restatement dedup (`old_name_dup`)

**Rule:** documents list a renamed street **twice** per station — once current
(`Београдски пут 127-166`) and once under the old name with the OLD street's numbering
(`Београдски пут (Југословенска) 1-31, 2-30`) — same houses, two numbering systems. If the
**same station** also claims the **same resolved street** via a **plain** (non‑parenthetical)
segment, the **parenthetical** segment is a restatement:

- its claims are **dropped** (the plain segment covers the houses),
- it is **excluded from `coverage_segments` output entirely** (no duplicate card in the UI),
- the raw text still appears in the pinned source panel.

Detection: `has_paren and street_id and (station_id, street_id) in plain_pairs`, where
`plain_pairs` are `(station, street)` from non‑parenthetical resolved segments.

### Rationale

Mapping the old numbers onto the current street creates **phantom claims** that conflict with
other stations' real ones (this blocked Subotica #30's `Beogradski put` evens). 117 such
segments nationwide; conflicts 1,456 → 1,446. Note: a reviewer override saved on a
now‑excluded segment id becomes a harmless orphan.

## 6.8 Link emission

For each assigned house, a link row is emitted: `(station_id, address_id, segment_id,
match_method, confidence)`. `match_method` is the claim kind (`whole` → `whole_street`,
else the kind name). Confidence is the segment's score / 100 (`seg_conf`).

## 6.9 Incremental re‑match (`--municipalities`)

With `--municipalities <group_reps>`, only segments whose station belongs to those group‑rep
munis are re‑matched, then merged into the complete parquets (drop affected stations' old
rows, append fresh). Conflict resolution stays identical because **every claimant of a given
street shares a municipality**. Segment ids are preserved so reviewer overrides stay
attached.
