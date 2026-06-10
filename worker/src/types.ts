/** Cloudflare bindings available to the Worker. */
export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
}

/** UI script selection. */
export type Script = "cyr" | "lat";
