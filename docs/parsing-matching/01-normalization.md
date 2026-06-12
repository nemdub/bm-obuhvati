# 1. Normalization

Code: `pipeline/common/normalize.py`, `pipeline/common/transliterate.py`

All matching happens on **normalized Cyrillic strings**. Both the document side (street
names parsed out of the RIK docs) and the register side (`streets.name_norm`, built in
stage01) pass through the *same* `normalize_street()` so the two forms converge. House
numbers are split into a numeric part and a normalized suffix so that `190`, `190Б`,
`190-Б` and the register's Latin `190B` all line up.

> Test target: `normalize_street`, `normalize_house`, `normalize_suffix`, `suffix_rank`,
> `genitive_variants`, `cyr_to_lat`, `lat_to_cyr`, `nfc`.

---

## 1.1 `normalize_street(name) -> str`

Builds the Cyrillic match key for a street name. Pipeline order (this order matters —
several steps depend on earlier ones):

1. **NFC** normalize (`nfc`) so precomposed/decomposed Unicode compares equal.
2. **Strip a leading `Улица:` label** (`_STREET_LABEL_RE`, case‑insensitive).
3. **Uppercase.**
4. **Expand abbreviations** (`_STREET_ABBREV`, literal substring replace):
   - `Ј.Н.А.` → `ЈНА`, `ЈНА.` → `ЈНА`
   - `БР.` → `` (dropped), `УЛ.` → `` (dropped)
   - `ДР.` → `ДР`
5. **Drop punctuation** — keep only Cyrillic/Latin letters, digits, spaces; everything else
   becomes a space.
6. **Fold Latin↔Cyrillic homoglyphs** (`_fold_homoglyphs`, see 1.2).
7. **Expand `ДР` → `ДОКТОРА`** as a *whole word* (`_DR_RE = \bДР\b`).
8. **Roman numerals → Arabic** (`_ROMAN_RE`, values 1–39; see 1.3).
9. **Spelled‑out Serbian ordinals → Arabic** (`_ORDINAL_RE`; see 1.4).
10. **Collapse whitespace**, strip.

### Examples

| Input | Normalized output |
|-------|-------------------|
| `Улица: 8. Март` | `8 МАРТ` |
| `Др Ђорђа Лазића` | `ДОКТОРА ЂОРЂА ЛАЗИЋА` |
| `Ј.Н.А.` | `ЈНА` |
| `XII војвођанске` | `12 ВОЈВОЂАНСКЕ` |
| `Краља Петра Првог` | `КРАЉА ПЕТРА 1` |
| `Краља Петра I` | `КРАЉА ПЕТРА 1` |
| `AПАТИН` (Latin A) | `АПАТИН` (all Cyrillic) |

### Rationale

Documents and the register disagree on abbreviation, script, and number spelling for the
*same* street. Normalizing both sides to one canonical form is what makes exact matching do
most of the work and keeps fuzzy matching (which invents matches) rare.

---

## 1.2 Homoglyph folding (`_fold_homoglyphs`)

**Rule:** within a single whitespace‑delimited word, if the word contains **both** a Latin
A–Z letter **and** a Cyrillic letter, map the Latin homoglyphs to Cyrillic. Words that are
purely Latin or purely Cyrillic are left untouched.

Mapped letters (`_HOMOGLYPH_TRANS`): `A B C E H J K M O P T X Y → А В С Е Н Ј К М О Р Т Х У`.

### Examples

| Input word | Output | Why |
|------------|--------|-----|
| `AПАТИН` (Latin `A` + Cyrillic) | `АПАТИН` | mixed‑script → fold |
| `VIII` (pure Latin) | `VIII` | pure Latin → untouched (Roman numeral) |
| `ПАТИН` (pure Cyrillic) | `ПАТИН` | pure Cyrillic → untouched |
| `190B` (digit + Latin) | `190B` | no Cyrillic in the word → untouched (register house‑letter) |

### Rationale

Source docs occasionally type a visually identical Latin letter inside an otherwise‑Cyrillic
word. If that word is the **settlement head** of a station's address, the home settlement
fails to resolve, matching drops to muni‑wide, and a street present in ≥2 settlements gets
flagged `ambiguous` instead of auto‑matching. Real case: Apatin #1 (`8004700001`)
`Језерска`, address head `AПАТИН` with a Latin `A`. ~19 station addresses nationwide have a
mixed‑script settlement head. The mixed‑script‑only guard keeps pure‑Latin Roman numerals
(VIII, XII) and the register's Latin house‑letters from being corrupted.

---

## 1.3 Roman numerals → Arabic (`_ROMAN_RE`, `_roman_to_arabic`)

**Rule:** convert standalone Roman numerals (Latin letters `I V X`, values **1–39**) to
Arabic, on both doc and register sides. Pattern is strict (`X{0,3}(IX|IV|V?I{0,3})`) so
Latin‑lettered tokens that aren't numerals never convert.

### Examples

| Input | Output |
|-------|--------|
| `XII ВОЈВОЂАНСКЕ` | `12 ВОЈВОЂАНСКЕ` |
| `VIII ВОЈВОЂАНСКА` | `8 ВОЈВОЂАНСКА` |
| `IV` | `4` |

### Rationale

Docs and register disagree both ways: doc `XII војвођанске` ↔ register `12.ВОЈВОЂАНСКЕ`,
and the inverse doc `8. војвођанске` ↔ register `VIII ВОЈВОЂАНСКА`. Both sides fold to
Arabic so they converge. Composes with the spelled‑out‑ordinal path (1.4).

---

## 1.4 Spelled‑out ordinals → Arabic (`_ORDINAL_RE`, `_ORDINAL_STEMS`)

**Rule:** convert spelled‑out Serbian ordinals **1–20** to Arabic, but **only** as
whole, adjective‑declined words (a trailing inflection is **required**).

- Stems (`_ORDINAL_STEMS`): `ПРВ`=1, `ДРУГ`=2, `ТРЕЋ`=3, `ЧЕТВРТ`=4, `ПЕТ`=5, `ШЕСТ`=6,
  `СЕДМ`=7, `ОСМ`=8, `ДЕВЕТ`=9, `ДЕСЕТ`=10, … `ДВАДЕСЕТ`=20.
- Required ending: one of `ОГА ОМЕ ОМУ ЕГА ЕМУ ИМА ОГ ОМ ЕГ ЕМ ИМ ИХ ОЈ ЕЈ И А О Е У`.
- Stems are tried **longest‑first** so `ПЕТНАЕСТ` (15) beats `ПЕТ` (5).

### Examples

| Input | Output | Note |
|-------|--------|------|
| `ПРВОГ` | `1` | declined ordinal |
| `ДРУГОГ` / `ДРУГИ` | `2` / `2` | |
| `ПЕТНАЕСТОГ` | `15` | longest stem wins, not `ПЕТ` |
| `ПЕТ` (cardinal, no inflection) | `ПЕТ` | bare cardinal → untouched |
| `СЕДАМ` (cardinal) | `СЕДАМ` | untouched |
| `ДРУГОВИ` | `ДРУГОВИ` | not an ordinal ending → untouched |
| `ОСМАНЛИЈА`, `ПРВЕНСТВА` | unchanged | not ordinal forms |

### Rationale

The register itself uses `Краља Петра Првог`, `Краља Петра I`, and `Краља Петра 1` for the
*same* street (also `Другог`/`II`/`2`). The whole‑word + required‑inflection rule prevents
cardinals (`ПЕТ`, `СЕДАМ`) and unrelated words (`ДРУГОВИ`, `ОСМАНЛИЈА`, `ПРВЕНСТВА`) from
being mangled. Compound ordinals 21–29 (`двадесет првог`) only partly fold, but symmetrically
on both sides, so no false matches arise.

---

## 1.5 Declension variants (`genitive_variants`, `_word_case_options`)

**Rule:** generate ALL per‑word declension combinations of a normalized name, capped at
`_MAX_GEN_VARIANTS = 16`, excluding the name itself. Per word (only if length ≥ 4 and no
digit; otherwise the word is left as‑is):

| Last letter | Options produced (word itself always included first) |
|-------------|------------------------------------------------------|
| `А` | `[w, w[:-1]+"Е"]` — `НИКОЛА → НИКОЛЕ` |
| `О` | `[w, w+"А", w[:-1]+"А"]` — `ДАНКО → ДАНКОА` (Hungarian) or `БРАНКО → БРАНКА` (Serbian) |
| `Е` | `[w, w[:-1]+"А"]` — `ЂОРЂЕ → ЂОРЂА` |
| consonant | `[w, w+"А"]` — `ВУК → ВУКА` |
| other vowel | `[w]` |

The cartesian product over all words is taken (capped at 16 outputs).

### Examples

- `НИКОЛЕ ТЕСЛЕ` ↔ `НИКОЛА ТЕСЛА` (each word А↔Е).
- `ДАНКО ПИШТА` reaches `ПИШТЕ ДАНКОА` (О→ОА on `ДАНКО`, A→E on `ПИШТА`) — needs declension
  **and** word‑order (sortkey) together.

### Rationale

Docs and register disagree on grammatical case — doc genitive `Николе Тесле` vs register
nominative `НИКОЛА ТЕСЛА` scores WRatio 83 (< the 90 fuzzy threshold), so it would never
fuzzy‑match. Declension variants are used **only as alternate settlement‑scoped keys** (and
tried on the doc primary). They never replace literal names and never apply muni‑wide.
`genitive_variant()` returns just the first variant for callers that need only the primary
А↔Е / consonant+А form.

---

## 1.6 House numbers and suffixes (`normalize_house`, `normalize_suffix`)

A house token is `(num: int|None, suffix: str)`.

- **`normalize_house("190Б") -> House(num=190, suffix="Б")`**. Leading digits are the number;
  the rest is the suffix. A token with no leading digit yields `num=None`.
- **`normalize_suffix`**: strip leading `- / space`, fold Latin → Cyrillic (`lat_to_cyr`),
  uppercase.

### Examples

| Token | num | suffix |
|-------|-----|--------|
| `190` | 190 | `` |
| `190Б` / `190-Б` / `190B` | 190 | `Б` |
| `0-ББ` | 0 | `ББ` |
| `бб` (no leading digit) | None | `ББ` |

### Register‑side extraction (stage01)

`addresses.house_num` = leading digits of `kucni_broj` (`str.extract(r"^(\d+)")`, Int64).
`addresses.house_suffix` = the rest, with `-`, `/`, space removed, uppercased. **A house
with no leading digits → `house_num` is NULL** — this is exactly the population that `бб`
(bez broja) claims target (see [02](02-coverage-parsing.md) §`бб` and
[06](06-claim-resolution.md)).

---

## 1.7 Suffix ordering — azbuka (`SUFFIX_AZBUKA`, `suffix_rank`)

**Rule:** house suffixes are ordered by the Serbian Cyrillic alphabet, not codepoint:

```
SUFFIX_AZBUKA = "АБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШ"
```

`suffix_rank(s)` returns a tuple of per‑character ranks; `""` (no suffix) sorts before any
letter. Unknown characters get rank `100 + ord(ch)`.

**Key consequence:** `Д < Ц` in azbuka (Д is 5th, Ц is 24th), unlike naive ordering. This
drives suffix‑bounded ranges (`1-23ц` includes `23д`; see [06](06-claim-resolution.md)).

The Worker mirrors this exactly (`SUFFIX_AZBUKA` + `suffixRank`/`rankCmp` in `db.ts`).

---

## 1.8 Transliteration (`cyr_to_lat`, `lat_to_cyr`)

Serbian Cyrillic ↔ Gaj's Latin, used for: producing `_lat` display columns; folding
register Latin house‑suffixes to Cyrillic.

- **`cyr_to_lat`**: per‑character map; digraphs `Љ→Lj`, `Њ→Nj`, `Џ→Dž`.
- **`lat_to_cyr`**: greedy longest‑match over digraph pairs first (`lj nj dž`), then single
  letters; case‑insensitive input, **returns lowercase**. Includes ASCII fallback `dj → ђ`
  for diacritic‑stripped sources. Intended for short tokens (suffixes).

### Examples

| Cyrillic | Latin |
|----------|-------|
| `Љубоморна` | `Ljubomorna` |
| `Џ` | `Dž` |
| `190B` (suffix `B`) | → `lat_to_cyr` → `в`, then uppercased to `В` by `normalize_suffix` |
</content>
