#!/usr/bin/env python3
"""Stage 01 — load and normalize the address + street register.

Reads ``data/kucni_broj.csv`` (~2.48M rows, house numbers) and
``data/ulica.csv`` (~96k rows, the authoritative street register with line
geometry), keeps active addresses, parses the WKT point, reprojects UTM Zone
34N -> WGS84, and writes:

  artifacts/addresses.parquet        (id, street/settlement/municipality ids,
                                       house_num/suffix/raw, lat/lon, x/y)
  artifacts/municipalities.parquet   (id, name_cyr, name_lat)
  artifacts/settlements.parquet      (id, municipality_id, name_cyr, name_lat)
  artifacts/streets.parquet          (id, settlement_id, name_cyr, name_lat, name_norm)
  artifacts/street_geometry.parquet  (street_id, geojson)  — no-house streets only

The ``streets`` table is sourced from ``ulica.csv`` (not derived from the house
register) so that streets with no house numbers — which never appear in
kucni_broj — are still matchable and searchable. The (slow, Python) street-name
normalization runs on the ~96k unique streets. Line geometry is stored only for
the streets that have no addresses (the ones a point-based polygon can't cover).

Usage:
  python3 stage01_load_register.py
  python3 stage01_load_register.py --municipalities "ADA,BOR,SUBOTICA"   # dev subset
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import polars as pl
from pyproj import Transformer
from shapely import make_valid, wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.ops import transform as shapely_transform

import config
from common.io import utm_to_wgs84
from common.normalize import normalize_street

# Source columns we keep (everything read as strings to preserve id formatting).
USE_COLS = [
    "kucni_broj_id", "kucni_broj", "retired",
    "ulica_maticni_broj", "ulica_ime", "ulica_ime_lat",
    "naselje_maticni_broj", "naselje_ime", "naselje_ime_lat",
    "opstina_maticni_broj", "opstina_ime", "opstina_ime_lat",
    "wkt",
]

# ulica.csv columns we keep.
ULICA_COLS = [
    "ulica_maticni_broj", "ulica_ime", "ulica_ime_lat",
    "naselje_maticni_broj", "naselje_ime", "naselje_ime_lat",
    "opstina_maticni_broj", "opstina_ime_lat",
    "wkt",
]

# Light Douglas-Peucker tolerance (meters, UTM) for stored street centerlines —
# keeps the no-house-street geometry payload small without visibly distorting it.
STREET_GEOM_SIMPLIFY_M = 5.0

# UTM 34N -> WGS84 for street line geometry (shapely-friendly, via pyproj).
_TO_WGS84 = Transformer.from_crs(config.UTM_34N, config.WGS84, always_xy=True)


def build_addresses(lf: pl.LazyFrame) -> pl.DataFrame:
    """Filter to active rows with a point, parse house number + WKT (UTM x/y).

    The returned frame includes name columns for reference-table construction;
    callers drop them before writing addresses.parquet.
    """
    df = (
        lf.select(USE_COLS)
        # Active = retired is null/empty.
        .filter(pl.col("retired").is_null() | (pl.col("retired").str.strip_chars() == ""))
        .filter(pl.col("wkt").is_not_null() & pl.col("wkt").str.contains("POINT"))
        .with_columns(
            # House number: leading digits + Cyrillic suffix (register column is Cyrillic).
            pl.col("kucni_broj").str.extract(r"^(\d+)", 1).cast(pl.Int64).alias("house_num"),
            pl.col("kucni_broj")
            .str.replace(r"^\d+", "")
            .str.replace_all(r"[-/ ]", "")
            .str.to_uppercase()
            .alias("house_suffix"),
            # WKT "POINT(x y)" -> x, y (UTM 34N meters).
            pl.col("wkt").str.extract(r"POINT\s*\(\s*([0-9.]+)", 1).cast(pl.Float64).alias("x"),
            pl.col("wkt").str.extract(r"POINT\s*\(\s*[0-9.]+\s+([0-9.]+)", 1).cast(pl.Float64).alias("y"),
        )
        .filter(pl.col("x").is_not_null() & pl.col("y").is_not_null())
        .collect(engine="streaming")
    )

    lon, lat = utm_to_wgs84(df["x"].to_numpy(), df["y"].to_numpy())
    df = df.with_columns(
        pl.Series("lon", lon),
        pl.Series("lat", lat),
        pl.col("house_suffix").fill_null(""),
    )

    # Carry the name columns through (needed for reference tables); they are
    # dropped before writing addresses.parquet.
    return df.select(
        pl.col("kucni_broj_id").alias("id"),
        pl.col("ulica_maticni_broj").alias("street_id"),
        pl.col("naselje_maticni_broj").alias("settlement_id"),
        pl.col("opstina_maticni_broj").alias("municipality_id"),
        "house_num",
        "house_suffix",
        pl.col("kucni_broj").alias("house_raw"),
        "lat", "lon", "x", "y",
        "opstina_ime", "opstina_ime_lat",
        "naselje_ime", "naselje_ime_lat",
    )


def build_muni_sett(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Municipalities + settlements derived from the (filtered) address frame."""
    munis = (
        df.lazy()
        .group_by("municipality_id")
        .agg(pl.first("opstina_ime").alias("name_cyr"), pl.first("opstina_ime_lat").alias("name_lat"))
        .rename({"municipality_id": "id"})
        .collect()
    )
    settlements = (
        df.lazy()
        .group_by("settlement_id")
        .agg(
            pl.first("municipality_id"),
            pl.first("naselje_ime").alias("name_cyr"),
            pl.first("naselje_ime_lat").alias("name_lat"),
        )
        .rename({"settlement_id": "id"})
        .collect()
    )
    return munis, settlements


def build_streets(ulica: pl.DataFrame) -> pl.DataFrame:
    """Streets table from the official register (one row per ulica_maticni_broj)."""
    streets = ulica.select(
        pl.col("ulica_maticni_broj").alias("id"),
        pl.col("naselje_maticni_broj").alias("settlement_id"),
        pl.col("ulica_ime").alias("name_cyr"),
        pl.col("ulica_ime_lat").alias("name_lat"),
    )
    # name_norm on the unique street set (Python; ~96k rows).
    return streets.with_columns(
        pl.col("name_cyr")
        .map_elements(normalize_street, return_dtype=pl.String)
        .alias("name_norm")
    )


def settlements_from_ulica(ulica: pl.DataFrame) -> pl.DataFrame:
    """Distinct settlement rows referenced by the street register (some settlements
    have streets but no houses, so they are absent from the address-derived set)."""
    return ulica.select(
        pl.col("naselje_maticni_broj").alias("id"),
        pl.col("opstina_maticni_broj").alias("municipality_id"),
        pl.col("naselje_ime").alias("name_cyr"),
        pl.col("naselje_ime_lat").alias("name_lat"),
    ).unique(subset="id")


def _wkt_to_geojson(w: str) -> str | None:
    """Parse a UTM34N WKT line, lightly simplify, reproject to WGS84, dump GeoJSON."""
    try:
        g = shapely_wkt.loads(w)
    except Exception:
        return None
    if g.is_empty:
        return None
    g = g.simplify(STREET_GEOM_SIMPLIFY_M, preserve_topology=False)
    if g.is_empty or g.length == 0:  # degenerate (single/duplicate point) -> nothing to render
        return None
    g = shapely_transform(_TO_WGS84.transform, g)
    return json.dumps(mapping(g), separators=(",", ":"))


def build_street_geometry(ulica: pl.DataFrame, house_ids: set[str]) -> pl.DataFrame:
    """WGS84 line geometry for streets that have NO house numbers — exactly the
    streets a point-based polygon can't cover."""
    no_house = ulica.filter(~pl.col("ulica_maticni_broj").is_in(list(house_ids)))
    rows: list[dict[str, str]] = []
    for sid, w in zip(no_house["ulica_maticni_broj"], no_house["wkt"]):
        if not w:
            continue
        gj = _wkt_to_geojson(w)
        if gj is not None:
            rows.append({"street_id": sid, "geojson": gj})
    return pl.DataFrame(rows, schema={"street_id": pl.String, "geojson": pl.String})


def main() -> int:
    ap = argparse.ArgumentParser(description="Load + normalize the address + street register.")
    ap.add_argument(
        "--municipalities",
        help="Comma-separated opstina_ime_lat values to keep (dev subset). Default: all.",
    )
    args = ap.parse_args()

    config.ensure_artifacts()
    if not config.REGISTER_CSV.exists():
        sys.exit(f"Register CSV not found: {config.REGISTER_CSV}")
    if not config.ULICA_CSV.exists():
        sys.exit(f"Street register CSV not found: {config.ULICA_CSV}")

    wanted = (
        [m.strip().upper() for m in args.municipalities.split(",")]
        if args.municipalities else None
    )

    t0 = time.time()
    # Read all columns as strings to preserve id formatting; keep the full set for
    # reference-table names (opstina_ime etc.) then drop after.
    lf = pl.scan_csv(config.REGISTER_CSV, infer_schema_length=0)
    if wanted is not None:
        lf = lf.filter(pl.col("opstina_ime_lat").str.to_uppercase().is_in(wanted))

    # Single pass: addresses + carried name columns for reference tables.
    addr_full = build_addresses(lf)
    print(f"  active addresses: {addr_full.height:,}  ({time.time()-t0:.1f}s)")

    munis, settlements = build_muni_sett(addr_full)

    # Streets + settlements come from the official street register (same dev-subset
    # filter so a subset run stays internally consistent).
    ulica_lf = pl.scan_csv(config.ULICA_CSV, infer_schema_length=0).select(ULICA_COLS)
    if wanted is not None:
        ulica_lf = ulica_lf.filter(pl.col("opstina_ime_lat").str.to_uppercase().is_in(wanted))
    ulica = ulica_lf.collect()

    streets = build_streets(ulica)

    # Union settlements referenced by the street register that the addresses don't
    # cover (settlements with streets but no houses) — keeps streets.settlement_id FK valid.
    settlements = (
        pl.concat([settlements, settlements_from_ulica(ulica)], how="vertical_relaxed")
        .unique(subset="id", keep="first")
    )

    house_ids = set(addr_full["street_id"].to_list())
    street_geometry = build_street_geometry(ulica, house_ids)

    # City-municipality groups: add parent_id (members point to their rep so the UI can
    # hide them) and apply any rep display-name override.
    mr = munis.to_dicts()
    for r in mr:
        r["parent_id"] = config.parent_of(r["id"])
        nm = config.rep_name(r["id"])
        if nm:
            r["name_cyr"], r["name_lat"] = nm
    munis = pl.DataFrame(mr, infer_schema_length=None)
    print(
        f"  municipalities: {munis.height:,}  settlements: {settlements.height:,}  "
        f"streets: {streets.height:,}  (geometry for {street_geometry.height:,} no-house streets)"
    )

    addresses = addr_full.select(
        "id", "street_id", "settlement_id", "municipality_id",
        "house_num", "house_suffix", "house_raw", "lat", "lon", "x", "y",
    )

    addresses.write_parquet(config.ADDRESSES_PARQUET)
    munis.write_parquet(config.MUNICIPALITIES_PARQUET)
    settlements.write_parquet(config.SETTLEMENTS_PARQUET)
    streets.write_parquet(config.STREETS_PARQUET)
    street_geometry.write_parquet(config.STREET_GEOMETRY_PARQUET)
    print(f"  wrote artifacts in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
