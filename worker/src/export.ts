/**
 * Per-municipality coverage-polygon exports in GeoJSON and KML.
 *
 * The GeoJSON shape mirrors data/exports/novi-beograd-automated.geojson (a FeatureCollection
 * of MultiPolygon features). The template also carries OKRUG (district) and RBR (a global
 * running index from RIK's master registry); neither is available in our pipeline/D1 data for
 * municipalities other than Novi Beograd, so those keys are omitted here. Every remaining
 * property is derived from the station register.
 */
import { tr, type Script } from "./translit";

/** One station's polygon plus the metadata needed to fill export properties. */
export interface ExportRow {
  station_id: number;
  number: number;
  name_cyr: string;
  name_lat: string;
  address_cyr: string;
  address_lat: string;
  geojson: string; // stored geometry, a GeoJSON MultiPolygon (occasionally Polygon)
}

/** Filesystem-safe slug from a Latin municipality name: "NOVI BEOGRAD" -> "novi-beograd". */
export function muniSlug(nameLat: string): string {
  return (
    nameLat
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "") // strip combining diacritics
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "municipality"
  );
}

/** Build the export properties for one station, honouring the active script. */
function featureProps(r: ExportRow, opstina: string, script: Script) {
  return {
    BR_BM: r.number,
    OPSTINA: opstina,
    NAZIV_BM: script === "lat" ? r.name_lat : tr(r.name_cyr, script),
    ADRESA_BM: script === "lat" ? r.address_lat : tr(r.address_cyr, script),
    Uparivanje: `${opstina}_${r.number}`,
  };
}

/** A FeatureCollection matching the template, omitting OKRUG/RBR. */
export function buildGeoJSON(rows: ExportRow[], muniNameCyr: string, script: Script) {
  const opstina = tr(muniNameCyr, script);
  return {
    type: "FeatureCollection",
    features: rows.map((r) => ({
      type: "Feature",
      properties: featureProps(r, opstina, script),
      geometry: JSON.parse(r.geojson),
    })),
  };
}

function xmlEscape(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** GeoJSON ring ([[lon,lat], ...]) -> KML coordinate tuple string ("lon,lat lon,lat ..."). */
function ringCoords(ring: number[][]): string {
  return ring.map((pt) => `${pt[0]},${pt[1]}`).join(" ");
}

/** One GeoJSON Polygon ([outerRing, ...holes]) -> a KML <Polygon> element. */
function polygonKml(poly: number[][][]): string {
  const [outer, ...holes] = poly;
  const inner = holes
    .map((h) => `<innerBoundaryIs><LinearRing><coordinates>${ringCoords(h)}</coordinates></LinearRing></innerBoundaryIs>`)
    .join("");
  return (
    `<Polygon>` +
    `<outerBoundaryIs><LinearRing><coordinates>${ringCoords(outer)}</coordinates></LinearRing></outerBoundaryIs>` +
    inner +
    `</Polygon>`
  );
}

/** GeoJSON geometry (MultiPolygon or Polygon) -> KML geometry, wrapping multiples in MultiGeometry. */
function geometryKml(geom: { type: string; coordinates: any }): string {
  if (geom.type === "Polygon") return polygonKml(geom.coordinates as number[][][]);
  // MultiPolygon
  const polys = (geom.coordinates as number[][][][]).map(polygonKml);
  return polys.length === 1 ? polys[0] : `<MultiGeometry>${polys.join("")}</MultiGeometry>`;
}

/** A KML document of one Placemark per station, with properties in ExtendedData. */
export function buildKML(rows: ExportRow[], muniNameCyr: string, script: Script): string {
  const opstina = tr(muniNameCyr, script);
  const placemarks = rows
    .map((r) => {
      const p = featureProps(r, opstina, script);
      const data = Object.entries(p)
        .map(([k, v]) => `<Data name="${k}"><value>${xmlEscape(String(v))}</value></Data>`)
        .join("");
      return (
        `<Placemark>` +
        `<name>${xmlEscape(String(p.NAZIV_BM))}</name>` +
        `<ExtendedData>${data}</ExtendedData>` +
        geometryKml(JSON.parse(r.geojson)) +
        `</Placemark>`
      );
    })
    .join("\n");
  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<kml xmlns="http://www.opengis.net/kml/2.2">\n` +
    `<Document>\n<name>${xmlEscape(opstina)}</name>\n${placemarks}\n</Document>\n</kml>\n`
  );
}
