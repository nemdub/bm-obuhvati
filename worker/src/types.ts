/** Cloudflare bindings available to the Worker. */
export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  /** Per-municipality coverage-polygon GeoJSON blobs (polygons/m/<muniId>.json) +
   *  polygons/summary.json. Polygons are static between recomputes and byte-heavy, so
   *  they live in object storage instead of D1. */
  POLY: R2Bucket;
}

/** UI script selection. */
export type Script = "cyr" | "lat";
