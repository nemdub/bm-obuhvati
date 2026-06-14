#!/usr/bin/env python3
"""Helpers for ``recompute.sh --only-dirty``.

Bridges the Worker's per-station ``dirty`` flag to the pipeline's municipality-scoped
recompute. The dirty snapshot (artifacts/dirty_snapshot.json, written by fetch_overrides.sh
at fetch time) looks like:

  [{"station_id": 8043800001, "updated_at": "2026-06-11 12:00:00"}, ...]

Two subcommands:
  munis      print the comma-separated group_rep municipality ids those stations belong to
             (the value passed to stage04/05 --municipalities). Prints nothing if none.
  clear-sql  write artifacts/clear_dirty.sql with one race-safe ``dirty=0`` UPDATE per
             station, and print how many. The UPDATE is guarded on the snapshot's
             updated_at, so a station re-edited DURING the recompute (markDirty bumps
             updated_at) won't match and correctly stays dirty for the next pass.

Usage:
  python3 dirty_scope.py munis
  python3 dirty_scope.py clear-sql
"""

from __future__ import annotations

import json
import sys

import polars as pl

import config


def _snapshot() -> list[dict]:
    if not config.DIRTY_SNAPSHOT_JSON.exists():
        return []
    return json.loads(config.DIRTY_SNAPSHOT_JSON.read_text())


def _rep_of_station() -> dict[int, str]:
    # Resolve from the PRISTINE snapshot (fallback: canonical) so removed stations — dropped
    # from canonical by stage03c — still map to their municipality and stay in the recompute
    # scope (their muni must be re-tessellated to drop them from the R2 polygon blob).
    src = config.STATIONS_PRISTINE_PARQUET if config.STATIONS_PRISTINE_PARQUET.exists() else config.STATIONS_PARQUET
    st = pl.read_parquet(src).select("id", "municipality_id")
    rep = {int(s): config.group_rep(str(m)) for s, m in zip(st["id"], st["municipality_id"])}
    # Added stations aren't in any parquet yet; resolve their synthetic ids from the export.
    if config.ADDED_STATIONS_JSON.exists():
        try:
            for a in json.loads(config.ADDED_STATIONS_JSON.read_text()) or []:
                rep[config.ADDED_STATION_BASE + int(a["id"])] = config.group_rep(str(a["municipality_id"]))
        except json.JSONDecodeError:
            pass
    return rep


def cmd_munis() -> None:
    snap = _snapshot()
    if not snap:
        return
    rep = _rep_of_station()
    reps = sorted({rep[int(r["station_id"])] for r in snap if int(r["station_id"]) in rep})
    if reps:
        print(",".join(reps))


def cmd_clear_sql() -> None:
    snap = _snapshot()
    n = 0
    with config.CLEAR_DIRTY_SQL.open("w", encoding="utf-8") as f:
        for r in snap:
            ts = r.get("updated_at")
            if ts is None:
                continue  # nothing to guard against; leave it dirty (conservative)
            sid = int(r["station_id"])
            ts = str(ts).replace("'", "''")
            f.write(
                f"UPDATE station_status SET dirty=0 "
                f"WHERE station_id={sid} AND dirty=1 AND updated_at='{ts}';\n"
            )
            n += 1
    print(n)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "munis":
        cmd_munis()
    elif cmd == "clear-sql":
        cmd_clear_sql()
    else:
        sys.exit("usage: dirty_scope.py {munis|clear-sql}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
