"""Export automated coverage polygons for one municipality as a GeoJSON whose feature
structure matches the volunteer-mapped files (data/volunteer-polygons/*.geojson).

Each feature gets these properties (same keys/order as the volunteer data):
    BR_BM, RBR, OKRUG, OPSTINA, NAZIV_BM, ADRESA_BM, Uparivanje

Reads the per-municipality R2 serving file `pipeline/artifacts/r2/polygons/m/<muni>.json`
(produced by the pipeline; bundles station metadata + geojson) and the municipality name
from municipalities.parquet. Only stations that have an automated polygon are emitted, so
the feature count can be lower than the number of polling stations.

Examples
--------
  # Novi Beograd, reproducing the volunteer file's national RBR serial (offset 549) and okrug:
  .venv/bin/python pipeline/export_automated_geojson.py 70181 \
      --okrug "ГРАД БЕОГРАД" --rbr-offset 549

  # Any municipality, minimal (RBR defaults to BR_BM, OKRUG left blank):
  .venv/bin/python pipeline/export_automated_geojson.py 80055 --out /tmp/foo.geojson
"""

from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path

import polars as pl

PIPELINE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PIPELINE_DIR.parent
ARTIFACTS = PIPELINE_DIR / "artifacts"


def slug(name: str) -> str:
    """Latin municipality name → filename slug (lowercase, spaces/punct → hyphens)."""
    out = []
    for ch in name.lower():
        out.append(ch if ch.isalnum() else "-")
    return "-".join(filter(None, "".join(out).split("-")))


def muni_name_cyr(muni_id: str) -> tuple[str, str]:
    """Return (name_cyr, name_lat) for the municipality id, from municipalities.parquet."""
    df = pl.read_parquet(ARTIFACTS / "municipalities.parquet").filter(pl.col("id") == muni_id)
    if df.height == 0:
        raise SystemExit(f"municipality id {muni_id!r} not found in municipalities.parquet")
    r = df.row(0, named=True)
    return r["name_cyr"], r["name_lat"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("muni_id", help="register municipality id, e.g. 70181 for Novi Beograd")
    ap.add_argument("--okrug", default="", help="okrug/district name for the OKRUG property (default: blank)")
    ap.add_argument("--rbr-offset", type=int, default=0,
                    help="RBR = BR_BM + offset; the volunteer files use a national serial "
                         "(Novi Beograd offset = 549). Default 0 → RBR == BR_BM.")
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default: data/exports/<muni-slug>-automated.geojson)")
    args = ap.parse_args()

    r2_file = ARTIFACTS / "r2" / "polygons" / "m" / f"{args.muni_id}.json"
    if not r2_file.exists():
        raise SystemExit(f"no polygon file for muni {args.muni_id}: {r2_file} (run the pipeline first)")

    name_cyr, name_lat = muni_name_cyr(args.muni_id)
    stations = json.loads(r2_file.read_text())["stations"]

    feats = []
    for s in sorted(stations, key=lambda s: s["number"]):
        n = s["number"]
        props = OrderedDict([
            ("BR_BM", n),
            ("RBR", n + args.rbr_offset),
            ("OKRUG", args.okrug),
            ("OPSTINA", name_cyr),
            ("NAZIV_BM", s.get("name_cyr")),
            ("ADRESA_BM", s.get("address_cyr")),
            ("Uparivanje", f"{name_cyr}_{n}"),
        ])
        feats.append({"type": "Feature", "properties": props, "geometry": json.loads(s["geojson"])})

    fc = {"type": "FeatureCollection", "features": feats}

    out = args.out or (ROOT_DIR / "data" / "exports" / f"{slug(name_lat)}-automated.geojson")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc, ensure_ascii=False))

    gtypes = sorted({ft["geometry"]["type"] for ft in feats})
    print(f"{name_cyr} (muni {args.muni_id}): wrote {len(feats)} features -> {out}")
    print(f"  geometry types: {gtypes}, size: {out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
