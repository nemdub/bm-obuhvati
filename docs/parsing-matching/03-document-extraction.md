# 3. Document extraction

Code: `pipeline/stage02_extract_docs.py`

Reads the RIK Word documents and emits one row per polling station
(`stations.parquet`: id, municipality, number, name, address, `raw_coverage_text`). Uses
macOS `textutil` to convert `.doc` → txt (linearized) and `.docx`/table `.doc` → html
(cells preserved).

> Test target: `rows_from_docx`, `rows_from_doc`, `rows_from_doc_triplets`,
> `_dedupe_dual_script`, `clean_filename_to_candidate`, `deaccent`, `build_muni_matcher`,
> `_header_start`, the file classification regexes, and the station‑id formula.

## 3.1 File classification

Each input file is classified by **filename**:

- **Amendment** (`AMENDMENT_RE`): contains `izmena|izmene|dopuna|dopune|ispravka|ispravke`.
  → raw text stored for stage03b, not parsed as base stations.
- **Military** (`MILITARY_RE`): contains `vojsk`. → skipped (no municipality).
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

## 3.6 Fallbacks for number‑less `.doc` (the parse‑path ladder)

When `rows_from_doc` returns empty (no integer column) and the doc is **not** sectioned:

1. **HTML table** (`rows_from_docx(textutil(path, "html"))`) — many number‑less `.doc` files
   still carry a *real* Word table; `textutil` renders proper `<td>` cells. This keeps the
   printed numbers and is immune to the drift that breaks the triplet fallback.
2. **Triplet fallback** (`rows_from_doc_triplets`) — only when there is no usable table at
   all. Groups the post‑header lines into rigid `(name, address, coverage)` 3‑tuples,
   numbered sequentially. **Fragile**: any leftover header line or wrapped coverage cell
   shifts the grouping and mis‑counts.

Sectioned docs **stay on the txt path** (the HTML table has no section structure).

### Why

The triplet fallback mis‑counted (Novi Sad parsed 215 vs declared 207, name/address shifted).
Routing table‑bearing `.doc`s through the HTML parser corrected: Novi Sad 207, Negotin 72,
Ruma 43, Ćićevac 18, Doljevac 37, Crna Trava 12. Known remaining unrelated count mismatches
still WARN: Pančevo 74→73, Užice 83→88.

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
