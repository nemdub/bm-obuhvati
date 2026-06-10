"""Official municipality boundary polygons, loaded for clipping and the UI overlay.

``data/opstine.geojson`` carries opština-level boundaries keyed by ``id_od`` (matični
broj, same code as the register's municipality_id). Big cities without city-municipality
subdivisions (Novi Sad, Subotica, Kragujevac, ...) live in a companion ``gradovi`` layer;
when ``data/gradovi.geojson`` is present it fills only the ids the opština layer lacks —
the opština layer wins where both exist, since a city polygon would wrongly cover all of
its city-municipalities (Beograd, Niš, Požarevac, Užice, Vranje members).

Geometries are returned in UTM 34N (meters) and validity-fixed: the source data contains
non-noded self-intersections that make raw intersection()/difference() raise GEOSException.
"""

from __future__ import annotations

import json

import polars as pl
import shapely
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform
from pyproj import Transformer

import config

_TO_UTM = Transformer.from_crs(config.WGS84, config.UTM_34N, always_xy=True)

# Candidate code properties for the gradovi layer (schema confirmed when the file lands).
_CODE_KEYS = ("id_od", "id_grad", "sifra", "maticni_broj", "code")


def _to_utm_valid(geom_json: dict) -> BaseGeometry:
    g = shp_transform(lambda xs, ys: _TO_UTM.transform(xs, ys), shape(geom_json))
    return shapely.make_valid(g.buffer(0))


def _norm_name(s: str) -> str:
    s = s.upper().strip()
    return s.removeprefix("GRAD ").strip()


def load_muni_boundaries() -> dict[str, BaseGeometry]:
    """{municipality_id: boundary polygon (UTM 34N, valid)} for register municipalities."""
    valid_ids = set(pl.read_parquet(config.MUNICIPALITIES_PARQUET)["id"].to_list())

    out: dict[str, BaseGeometry] = {}
    with config.OPSTINE_GEOJSON.open(encoding="utf-8") as f:
        gj = json.load(f)
    for feat in gj["features"]:
        code = str(feat["properties"].get("id_od", ""))
        if code in valid_ids:  # drops Kosovo features absent from the register
            out[code] = _to_utm_valid(feat["geometry"])

    if config.GRADOVI_GEOJSON.exists():
        # Latin display name -> id, only for munis still missing a polygon (name fallback
        # in case the gradovi layer carries no usable code property).
        name_to_id = {
            _norm_name(r["name_lat"]): r["id"]
            for r in pl.read_parquet(config.MUNICIPALITIES_PARQUET).iter_rows(named=True)
            if r["id"] not in out
        }
        with config.GRADOVI_GEOJSON.open(encoding="utf-8") as f:
            gj = json.load(f)
        for feat in gj["features"]:
            props = feat["properties"]
            code = next((str(props[k]) for k in _CODE_KEYS if props.get(k) not in (None, "", "0")), None)
            if code not in valid_ids:
                code = name_to_id.get(_norm_name(str(props.get("name", ""))))
            if code and code in valid_ids and code not in out:
                out[code] = _to_utm_valid(feat["geometry"])

    return out
