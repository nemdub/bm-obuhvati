#!/usr/bin/env python3
"""Split a D1 .sql dump into ordered chunk files, one per ``wrangler d1 execute``.

A single execute of a large dump exceeds D1's per-execution CPU time limit ("D1 DB
exceeded its CPU time limit and was reset") — and a single DELETE of ~138k rows is enough
to trip it on its own, so chunking by statement *count* isn't enough. The preferred mode
splits on explicit ``-- CHUNK`` markers that the producer (stage06's partial import) emits
at FK-safe, work-bounded batch boundaries. If a file has no markers (e.g. the full dump),
it falls back to splitting every N statements (a line whose stripped text ends with ';').

Chunks are contiguous and ordered, so FK constraints hold across boundaries: a child row is
never inserted before its parent's chunk has run.

Usage:
  python3 split_sql.py <src.sql> <out_dir> <stmts_per_chunk>
Prints the number of chunk files written (chunk.00000.sql, chunk.00001.sql, ...).
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "-- CHUNK"


def main() -> int:
    src, out_dir, n = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = Path(src).read_text(encoding="utf-8").splitlines(keepends=True)
    use_markers = any(ln.strip() == MARKER for ln in lines)

    buf: list[str] = []
    stmts = 0
    idx = 0

    def flush() -> None:
        nonlocal buf, stmts, idx
        if any(ln.strip() for ln in buf):  # skip empty/whitespace-only chunks
            (out_dir / f"chunk.{idx:05d}.sql").write_text("".join(buf), encoding="utf-8")
            idx += 1
        buf = []
        stmts = 0

    if use_markers:
        for ln in lines:
            if ln.strip() == MARKER:
                flush()
            else:
                buf.append(ln)
        flush()
    else:
        for ln in lines:
            buf.append(ln)
            if ln.rstrip().endswith(";"):
                stmts += 1
                if stmts >= n:
                    flush()
        flush()
    print(idx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
