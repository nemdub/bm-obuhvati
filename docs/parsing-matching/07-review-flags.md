# 7. Review flags & reason codes

Code: `pipeline/stage04_match_addresses.py` (finalize loop in `main`); localized in
`worker/src/i18n.ts` (`REVIEW_REASONS`).

stage04 records **why** each segment is flagged in `coverage_segments.review_reason` as
comma‑separated codes. `needs_review` is set when any *flagging* reason is present.

> Test target: the reason‑assembly logic and the `flagging = reasons - {parity_unconfirmed}`
> rule; per‑method confidence values.

## 7.1 Confidence by method

| method | confidence | flagged? |
|--------|-----------:|----------|
| exact (compact) | 0.75 | no |
| exact (structured) | 0.95 | no |
| `manual` / `manual_settlement` | 0.9 | no |
| `manual_none` | 0.9 | no (resolved, no street) |
| `settlement` (village) | 0.8 | yes (`settlement_claim`) |
| `base_parts` | 0.7 | yes |
| `alias` | 0.6 | yes |
| `fuzzy` | 0.5 | yes |
| `muni_fallback` | 0.4 | yes |
| `ambiguous` | 0.2 | yes |
| unresolved (`street_id is None`) | 0.2 | yes (`street_unresolved`) |

## 7.2 Reason codes

Assembled per segment (`reasons` list):

| Code | When |
|------|------|
| `street_unresolved` | resolved to no street (and not ambiguous/manual_none) |
| `ambiguous:S1\|S2\|…` | same name in several other settlements; lists their names |
| `fuzzy` | method fuzzy / token‑subset |
| `alias` | hand‑maintained alias substitution |
| `base_parts` | plain base name expanded to numbered part streets |
| `settlement_claim:НАЗИВ` | whole‑settlement (village) claim |
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
flagging = [x for x in reasons if x != "parity_unconfirmed"]
needs_review = int(bool(flagging))
```

**Rule:** `parity_unconfirmed` is **informational only** — a segment flagged *only* for
parity is treated as resolved (`needs_review = 0`), but the code stays in `review_reason` so
it shows as context when the segment is flagged for another reason. All other codes set
`needs_review = 1`.

## 7.5 Name discrepancy appended by the Worker

For `fuzzy` / `muni_fallback` the segments API appends the actual names
(„документски назив" → „регистарски назив") because the card title shows the **resolved
register** name, so the discrepancy isn't otherwise visible. Example: doc „Виноградска" →
register „Виноградарска".

## 7.6 Always‑flagged sources

- **Amendment** segments (`source == "amendment"`) always carry `amendment` → always reviewed.
- **`no_match`** is added to any non‑`whole` segment that produced no links (and isn't an
  old‑name‑dup). A whole‑street claim that legitimately covers everything is never
  `no_match`.
</content>
