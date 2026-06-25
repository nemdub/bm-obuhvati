# 7. Review flags & reason codes

Code: `pipeline/stage04_match_addresses.py` (finalize loop in `main`); localized in
`worker/src/i18n.ts` (`REVIEW_REASONS`).

stage04 records **why** each segment is flagged in `coverage_segments.review_reason` as
comma‑separated codes. `needs_review` is set when any *flagging* reason is present.

> Test target: the reason‑assembly logic and the `flagging = reasons - {parity_unconfirmed,
> settlement_claim}` rule; per‑method confidence values.

## 7.1 Confidence by method

| method | confidence | flagged? |
|--------|-----------:|----------|
| exact (compact) | 0.75 | no |
| exact (structured) | 0.95 | no |
| `manual` / `manual_settlement` | 0.9 | no |
| `manual_none` | 0.9 | no (resolved, no street) |
| `settlement` (village) | 0.8 | no¹ (`settlement_claim` is informational) |
| `base_parts` | 0.7 | yes |
| `locality` | 0.7 | yes |
| `alias` | 0.6 | yes |
| `abbrev` | 0.6 | yes |
| `fuzzy` | 0.5 | yes |
| `proximity` | 0.5 | yes |
| `muni_fallback` | 0.4 | yes |
| `ambiguous` | 0.2 | yes |
| unresolved (`street_id is None`) | 0.2 | yes (`street_unresolved`) |

¹ A whole‑settlement match is trusted on its own. It is only reviewed when **another station
claims the same settlement** — a real conflict that adds a `conflict:` reason (see §7.4).

## 7.2 Reason codes

Assembled per segment (`reasons` list):

| Code | When |
|------|------|
| `street_unresolved` | resolved to no street (and not ambiguous/manual_none) |
| `ambiguous:S1\|S2\|…` | same name in several other settlements; lists their names |
| `fuzzy` | method fuzzy / token‑subset |
| `proximity` | resolved by the geographic proximity pass (nearest unclaimed same‑named / fuzzy‑close street near the station's coverage — see [05](05-street-resolution.md) §5.14) |
| `alias` | hand‑maintained alias substitution |
| `abbrev` | initial/title‑abbreviated name expanded to a settlement street (`М.Пупина` → `МИХАЈЛА ПУПИНА` — see [05](05-street-resolution.md) §5.10a) |
| `base_parts` | plain base name expanded to numbered part streets |
| `locality` | single-word coverage expanded to a register sub-locality/hamlet cluster (заселак prefix — see [05](05-street-resolution.md) §5.15) |
| `settlement_claim:НАЗИВ` | whole‑settlement (village) claim — **informational only** (see §7.4) |
| `muni_fallback` | exact match found only municipality‑wide |
| `unknown_tokens` | parse left unclassified number‑side tokens (blocks etc.) |
| `named_block` | segment kind is `named_block` |
| `unknown_kind` | segment kind is `unknown` |
| `amendment` | segment came from / was touched by an amendment |
| `no_match` | non‑whole segment produced **no** links |
| `conflict:N1\|N2\|…` | opposing station **numbers** contesting a house |
| `parity_unconfirmed` | inferred odd/even side not corroborated by a sibling |

## 7.3 Parameterized codes

Some codes carry a `:`‑separated parameter that the Worker splits off for the
`REVIEW_REASONS` lookup before rendering:

- `conflict:7|12` — opposing **printed station numbers** (stage04 maps opposing station ids
  via `conflict_map` → `station_number`). Worker renders "(бр. 7, 12)".
- `ambiguous:SETT1|SETT2|…` — settlements that also have the same street name.
- `settlement_claim:НАЗИВ` — the claimed settlement's name.

## 7.4 `needs_review` computation

```python
INFORMATIONAL = {"parity_unconfirmed", "settlement_claim"}
flagging = [x for x in reasons if x.split(":", 1)[0] not in INFORMATIONAL]
needs_review = int(bool(flagging))
```

**Rule:** `parity_unconfirmed` and `settlement_claim` are **informational only** — a segment
flagged *only* for one of these is treated as resolved (`needs_review = 0`), but the code
stays in `review_reason` so it shows as context (and lets the Worker title a settlement card
by its area name). All other codes set `needs_review = 1`.

- `parity_unconfirmed` — the inferred odd/even side has proven correct in the vast majority
  of cases.
- `settlement_claim:НАЗИВ` — an exact whole‑settlement match is trusted. It is surfaced for
  review **only when another station claims the same settlement**: both stations emit
  `sett_whole` claims, tie on every shared house, and so land in `conflict_map` → the segment
  also carries a `conflict:` reason, which *does* flag it. A settlement claimed by exactly one
  station is therefore not reviewed.

## 7.5 Name discrepancy appended by the Worker

For `fuzzy` / `muni_fallback` / `alias` / `proximity` the segments API appends the actual
names („документски назив" → „регистарски назив") because the card title shows the
**resolved register** name, so the discrepancy isn't otherwise visible. Example: doc
„Виноградска" → register „Виноградарска".

## 7.6 Always‑flagged sources

- **Amendment** segments (`source == "amendment"`) always carry `amendment` → always reviewed.
- **`no_match`** is added to any non‑`whole` segment that produced no links (and isn't an
  old‑name‑dup). A whole‑street claim that legitimately covers everything is never
  `no_match`.
