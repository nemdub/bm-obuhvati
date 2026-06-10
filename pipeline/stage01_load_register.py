#!/usr/bin/env python3
"""Stage 01 — load and normalize the address register.

Reads ``data/kucni_broj.csv`` (~2.48M rows), keeps active addresses, parses the
WKT point, reprojects UTM Zone 34N -> WGS84, and writes:

  artifacts/addresses.parquet        (id, street/settlement/municipality ids,
                                       house_num/suffix/raw, lat/lon, x/y)
  artifacts/municipalities.parquet   (id, name_cyr, name_lat)
  artifacts/settlements.parquet      (id, municipality_id, name_cyr, name_lat)
  artifacts/streets.parquet          (id, settlement_id, name_cyr, name_lat, name_norm)

Reference tables are deduplicated first, so the (slow, Python) street-name
normalization runs on ~100k unique streets rather than 2.48M rows.

Usage:
  python3 stage01_load_register.py
  python3 stage01_load_register.py --municipalities "ADA,BOR,SUBOTICA"   # dev subset
"""

from __future__ import annotations

import argparse
import sys
import time

import polars as pl

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
        "ulica_ime", "ulica_ime_lat",
    )


def build_reference_tables(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
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
    streets = (
        df.lazy()
        .group_by("street_id")
        .agg(
            pl.first("settlement_id"),
            pl.first("ulica_ime").alias("name_cyr"),
            pl.first("ulica_ime_lat").alias("name_lat"),
        )
        .rename({"street_id": "id"})
        .collect()
    )
    # name_norm on the deduped street set (Python; ~100k rows).
    streets = streets.with_columns(
        pl.col("name_cyr")
        .map_elements(normalize_street, return_dtype=pl.String)
        .alias("name_norm")
    )
    return munis, settlements, streets


def main() -> int:
    ap = argparse.ArgumentParser(description="Load + normalize the address register.")
    ap.add_argument(
        "--municipalities",
        help="Comma-separated opstina_ime_lat values to keep (dev subset). Default: all.",
    )
    args = ap.parse_args()

    config.ensure_artifacts()
    if not config.REGISTER_CSV.exists():
        sys.exit(f"Register CSV not found: {config.REGISTER_CSV}")

    t0 = time.time()
    # Read all columns as strings to preserve id formatting; keep the full set for
    # reference-table names (opstina_ime etc.) then drop after.
    lf = pl.scan_csv(config.REGISTER_CSV, infer_schema_length=0)

    if args.municipalities:
        wanted = [m.strip().upper() for m in args.municipalities.split(",")]
        lf = lf.filter(pl.col("opstina_ime_lat").str.to_uppercase().is_in(wanted))

    # Single pass: addresses + carried name columns for reference tables.
    addr_full = build_addresses(lf)
    print(f"  active addresses: {addr_full.height:,}  ({time.time()-t0:.1f}s)")

    munis, settlements, streets = build_reference_tables(addr_full)
    print(f"  municipalities: {munis.height:,}  settlements: {settlements.height:,}  streets: {streets.height:,}")

    addresses = addr_full.select(
        "id", "street_id", "settlement_id", "municipality_id",
        "house_num", "house_suffix", "house_raw", "lat", "lon", "x", "y",
    )

    addresses.write_parquet(config.ADDRESSES_PARQUET)
    munis.write_parquet(config.MUNICIPALITIES_PARQUET)
    settlements.write_parquet(config.SETTLEMENTS_PARQUET)
    streets.write_parquet(config.STREETS_PARQUET)
    print(f"  wrote artifacts in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
