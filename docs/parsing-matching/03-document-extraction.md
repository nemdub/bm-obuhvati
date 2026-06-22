# 3. Document extraction

Code: `pipeline/stage02_extract_docs.py`

Reads the RIK Word documents and emits one row per polling station
(`stations.parquet`: id, municipality, number, name, address, `raw_coverage_text`). Uses
macOS `textutil` to convert `.doc` → txt (linearized) and `.docx`/table `.doc` → html
(cells preserved).

> Test target: `rows_from_docx`, `rows_from_doc`, `rows_from_doc_triplets`,
> `_dedupe_dual_script`, `_is_dual_script_doc`, `_is_quoted_fragment`,
> `clean_filename_to_candidate`, `deaccent`, `build_muni_matcher`, `_header_start`, the file
> classification regexes, and the station‑id formula.

## 3.1 File classification

Each input file is classified by **filename** (plus one content check):

- **Special / non-municipal** (`MILITARY_RE` = `vojsk`, `SPECIAL_RE` = `inostran|zavod`):
  national resolutions for voting by the military, abroad, or in institutions/prisons. They
  carry no municipality table, so they are **skipped** (recorded `kind="special"`). Without
  this they fuzzy-match a random muni by filename and inject phantom stations (10 country rows
  under Senta, 29 prison rows under Jagodina).
- **Amendment**: filename matches `AMENDMENT_RE` (`izmena|izmene|dopuna|dopune|ispravka|
  ispravke`) **OR** the body carries `уместо/одређује се` override markers (`OVERRIDE_BODY_RE`
  = `уместо:` / `мења се гласачко место` / `Стари назив гласачког места` / `Треба да стоји`).
  The content check catches override docs whose filename lacks a keyword (e.g. `Palilula.docx`,
  which otherwise parses as a *second base table* and duplicates stations). → raw text stored
  for stage03b, not parsed as base stations.
- **Base**: everything else.
- **Lock files**: names starting with `~$` are skipped from the glob.

## 3.2 Station id (deterministic)

```
id = int(station_municipality) * 100000 + per_municipality_running_index
```

The **printed `number` is not unique** — cities restart numbering per section/area — so the
id uses a per‑municipality running counter (`muni_counter`). For sectioned docs the
*section's* opstina is used, not the document's. Ids are stable across re‑runs so manual
edits keyed to them survive. (Note: the docstring's older `*10000` is stale; the code uses
`*100000`, matching stage03's `*1000` segment multiplier headroom.)

## 3.3 `.docx` / table `.doc` parsing (`rows_from_docx`)

Parses HTML rendered by `textutil`. **Rule for a data row:**

- `<tr>` with **≥ 4 `<td>` cells** and a **non‑empty 2nd cell** (the station name).
- **Skip header rows**: joined text contains `НАЗИВ ГЛАСАЧКОГ` or `ПОДРУЧЈЕ КОЈЕ`.
- **number** = leading integer of cell 0 if present (`_LEAD_INT`), else a running `seq`
  (some docs auto‑number as a list with no text in the number cell).
- **coverage** = cells `[3:]` joined (multi‑cell coverage is concatenated).

Columns: `(None, number, name, address, coverage)` — `None` section because docx isn't
sectioned.

## 3.4 `.doc` linearized parsing (`rows_from_doc`)

For legacy `.doc` with no real table, `textutil -convert txt` linearizes the table to lines.

**Rule:** after the header (`_header_start`), **any lone integer line** (optionally with a
trailing period — `INT_LINE_RE = ^(\d+)\.?$`) starts a new station. The following lines are,
in order: name, address, coverage (coverage = all remaining lines of that station joined).

### Table‑end trimming (`TABLE_END_RE`)

The parser stops at the resolution's closing section so the **last** station doesn't absorb
trailing boilerplate:

```
TABLE_END_RE = ^(II|III|IV|V|VI)\.?$  |  ^Ово решењ  |  доставити Републичк
             |  ИЗБОРНА КОМИСИЈА  |  ПРЕДСЕДНИК
```

**Gated on `cur is not None`** — it only fires once inside the table (a station has started),
because the same phrases also appear in the pre‑table preamble. This trimmed ~21 bogus
boilerplate "stations".

### Dual‑script rows (`_dedupe_dual_script`)

The Sandžak‑region docs (`Tutin`, `Prijepolje`, `Sjenica`) print **every table cell twice** —
once in Cyrillic, then the same content in Latin. Linearized, a station's lines interleave:

```
ЛОКАЛ ХАМЗАГИЋ РЕШАДА            name (cyr)
LOKAL HAMZAGIĆ REŠADA           name (lat)
ТУТИН, БОГОЉУБА ЧУКИЋА ББ        address (cyr)
TUTIN, BOGOLjUBA ČUKIĆA BB      address (lat)
9. црногорске бригаде бб, …      coverage (cyr)
9. crnogorske brigade bb, …      coverage (lat)
```

Without handling, `flush()` reads the Latin **name** as the address and sweeps both
addresses + both coverages into `raw_coverage_text` (so polling station #1 "covered" its own
header `ТУТИН, БОГОЉУБА ЧУКИЋА ББ …` plus the entire Latin list).

**Rule:** before splitting `cur` into name/address/coverage, drop every line that is the
**Latin transliteration of the line before it** (`_is_lat_restatement`: `fuzz.ratio` of
`cyr_to_lat(prev)` vs the line `≥ 85`, allowing minor ијекавица/typo drift), keeping only the
Cyrillic side. Done **pairwise**, not by even/odd index, so a row that doubles only *some*
cells still aligns — e.g. a Sjenica station whose name isn't restated (5 lines):

```
Приватна кућа Неџада Муратовића   name (no Latin twin)
Врсјенице / Vrsjenice             address cyr / lat   → keeps Врсјенице
Баре, Врсјенице / Bare, Vrsjenice coverage cyr / lat  → keeps Баре, Врсјенице
```

**Single‑script docs are untouched**: there the line after the name is the Cyrillic
*address*, which is in a different script from `cyr_to_lat(name)` and never reaches the 85
threshold — so nothing collapses. Verified: dual‑script handling brought Tutin/Prijepolje/
Sjenica to their declared counts (61/66/68) with correct address & coverage columns, and no
single‑script doc changed.

### Multi‑line venue names (`_is_quoted_fragment`)

A venue's name can wrap onto a **second line** — typically a quoted proper name on its own
line:

```
ДЕЧИЈИ ВРТИЋ                              name, line 1
 ``ДУШКО РАДОВИЋ``                        name, line 2  (quoted)
ПОЖАРЕВАЦ, ПОЖАРЕВАЧКИ ПАРТИЗАНСКИ ОДРЕД  ББ   address
Алексе Галибарде, Боре Станковића, …     coverage
```

Naively, `flush()` would read the quoted line as the **address** and shove the real address
(`ПОЖАРЕВАЦ, … ББ`) onto the front of `raw_coverage_text` — where it then parses as a
**whole‑settlement claim** of the town (every Пожаревац street), which both mis‑covers the
station and triggered a Worker 500 (see [08](08-worker-live-preview.md) §8.4).

**Rule:** while splitting `cur`, merge a leading **fully‑quoted** line into the name
(`_is_quoted_fragment`: the line both starts and ends with a quote glyph — `` ` ``, `"`, `„`,
`“`, `”`, `«`, `»`), as long as the address + coverage lines still remain after it. A line that
merely *starts* with a quote but carries address text after the closing quote
(`"КРАЉЕВИЦА" ББ, ЗАЈЕЧАР`) is **not** a fragment and stays the address.

Verified: 3 stations corrected nationwide (Пожаревац #50/#51 `ДЕЧИЈИ ВРТИЋ ``ДУШКО РАДОВИЋ```,
Пирот `БИВША ПРОДАВНИЦА „4 АСА“`); all other 8,189 stations unchanged, including the
quoted‑building‑address control (`ВРТИЋ "ЂУРЂЕВАК"`) and multi‑line *coverage* rows (which the
existing `coverage = remaining lines joined` rule already handles).

## 3.5 Sectioned city docs (`rows_from_doc` with `sections`)

For one document covering several city‑municipalities (`config.SECTIONED_DOCS`, currently
`Nis-glasacka-mesta.doc`). Each `ГРАДСКА ОПШТИНА <name>` section header (`SECTION_RE`) maps to
an opstina id; numbering restarts per section.

**Rules:**
- Parsing starts at the **first section header** (it precedes the table header, which
  `_header_start` would otherwise skip past, dropping the first section).
- A section header that maps to a real sub‑municipality flushes the current station and sets
  `cur_section`. A header that *doesn't* map (a venue literally named "ГРАДСКА ОПШТИНА …") is
  ignored.
- Each station is tagged with its section's opstina, so `id = section_opstina*100000 + idx`
  and per‑section numbers don't collide.

Niš sections: `МЕДИЈАНА`=71331, `ПАЛИЛУЛА`=71323, `ПАНТЕЛЕЈ`=71307, `ЦРВЕНИ КРСТ`=71315,
`НИШКА БАЊА`=71285.

### Member-town sub-table labels (`section_labels_for_rows`)

A few **scope-merge city docs** (`config.CITY_GROUPS` reps: Požarevac, Užice — *not* Vranje)
bundle the member town's table as a **second numbering block**: the printed number restarts at
1, so e.g. Požarevac's #1–11 and Kostolac's #1–11 share a municipality. Unlike sectioned docs
these stations stay under the **city municipality** (matching scope unchanged); they are only
*labelled* so the UI can separate them.

**Rule:** for a doc whose muni is a group rep (`config.is_group_rep`), split the parsed rows at
each **number reset** (`number <= previous`). Segment 0 → the rep city's `name_cyr`; segment *k*
→ the *k*-th `config.group_members` name (Kostolac / Sevojno). The label is written to the
`section_cyr` column. The number reset is the only signal robust across both parse paths — the
`.doc` has a standalone `КОСТОЛАЦ` line, but the Užice `.docx` HTML folds `ГРАДСКА ОПШТИНА
СЕВОЈНО` into its first station row. With no reset (one table) every label is `None`. The
Worker renders a divider per section and includes it in the export `Uparivanje` key (the printed
number alone is not unique within the muni).

## 3.6 `.doc` parse‑path: HTML columns over linearized text

A `.doc`'s real Word table renders to HTML with proper `<td>` cells. Those cells keep a
station's columns intact **even when a cell wraps across a page break** — which is exactly
what corrupts the *linearized* txt parse: textutil emits the wrapped cell as extra lines, so
the rigid name=lines[0] / address=lines[1] / coverage=lines[2:] split shifts a column.
Symptoms seen in the wild: the real address shoved into the coverage (Barajevo #19:
`ПРОКИЋ КРАЈ` as address, `БАРАЈЕВО, ПРОКИЋ КРАЈ 49` lost into coverage), or the address
truncated to its first wrapped line (Čoka: every `Врбица,`; Aleksandrovac villages: `НОВАЦИ`).

So for a `.doc`, after `rows_from_doc` parses the txt, the HTML table is parsed too
(`rows_from_docx`) and **replaces** the txt rows when it **agrees with them on the row count**
(same station delimitation) — same rows, but columns that can't drift. Excluded:

- **Sectioned docs** (`config.SECTIONED_DOCS`, e.g. Niš) — the HTML table carries no
  `ГРАДСКА ОПШТИНА` section structure, so they stay on the txt path.
- **Dual‑script docs** (`_is_dual_script_doc` — Tutin / Prijepolje / Sjenica) — the HTML cells
  keep *both* scripts (`ЛОКАЛ ХАМЗАГИЋ РЕШАДА LOKAL HAMZAGIĆ REŠADA`); only the txt parser
  de‑dups them (§3.4). Detected by the Latin twin: the html name cell contains the
  transliteration of the de‑duped txt name.

When the txt parse finds **no rows at all** (no integer column), the ladder is HTML table →
**triplet fallback** (`rows_from_doc_triplets`, post‑header lines grouped into rigid 3‑tuples,
numbered sequentially — fragile: any leftover header line or wrapped cell mis‑counts).

### Why

The HTML table is the authoritative column model; the linearized text is a lossy flattening.
Preferring HTML when the counts agree fixed wrapped‑cell column shifts across many munis
(Barajevo, Čoka, Aleksandrovac, Beočin, …) — the single fix subsumes the earlier
quoted‑name‑continuation heuristic (§3.4) and is immune to page breaks. The count‑agreement
gate keeps the txt parse wherever the HTML `<tr>` split disagrees (e.g. Backi Petrovac 11 vs
HTML 12, Priboj 48 vs 47, Šabac 100 vs 99, Voždovac 90 vs 91 — txt is right there). It also
corrected the number‑less table‑bearing `.doc`s: Novi Sad 207, Negotin 72, Ruma 43, Ćićevac 18,
Doljevac 37, Crna Trava 12. Known remaining unrelated count mismatches still WARN: Pančevo
74→73, Užice 83→88.

## 3.7 Declared‑count check (`COUNT_RE`)

The resolution preamble states a count: `одре[ђd]\w* се (\d+) гласачк`. If it disagrees with
the parsed row count, stage02 prints `WARN <file>: declared N stations, parsed M`. This is
the canary for parse drift — every WARN is a candidate test case.

## 3.8 Municipality mapping (`build_muni_matcher`, `clean_filename_to_candidate`)

Maps a doc filename to a register municipality:

1. `clean_filename_to_candidate`: strip `.doc/.docx`, `glasacka mesta`, amendment/connector
   words, and a leading `\d+_` dedup prefix from the scraper.
2. `deaccent`: drop diacritics (`đ→d`, NFD strip combining) so ASCII filenames (`Backa`)
   match register names (`Bačka`).
3. Exact uppercase match against register `opstina_ime_lat`, else `rapidfuzz.WRatio`
   best‑match.
4. **Overrides** (`config.DOC_MUNI_OVERRIDES`) win first: `Palilula-glasacka-mesta.doc` and
   `Palilula.docx` are pinned to `PALILULA (BEOGRAD)` (Palilula is ambiguous — Belgrade and
   Niš both have one; fuzzy tie‑break was unstable, and Niš's Palilula comes from the Niš
   sectioned doc).

Base matches scoring `< 92` are reported as low‑confidence → review the CSV map.
