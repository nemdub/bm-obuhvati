#!/usr/bin/env python3
"""Split a D1 .sql dump into chunk files of <= N statements, preserving order.

A single ``wrangler d1 execute --file`` of a large dump can exceed D1's per-execution
CPU time limit ("D1 DB exceeded its CPU time limit and was reset"). Splitting into smaller
ordered chunks keeps each execute under the limit. Because the chunks are contiguous slices
of the original (deletes-then-parents-then-children) ordering, FK constraints still hold
across chunk boundaries: a child row is never inserted before its parent's chunk has run.

A statement boundary is a line whose stripped text ends with ';' (the dumps end every
INSERT/DELETE with ';\\n'; value tuples inside a multi-row INSERT end with ',' or ')').

Usage:
  python3 split_sql.py <src.sql> <out_dir> <stmts_per_chunk>
Prints the number of chunk files written (chunk.00000.sql, chunk.00001.sql, ...).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    src, out_dir, n = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)
    buf: list[str] = []
    stmts = 0
    idx = 0

    def flush() -> None:
        nonlocal buf, stmts, idx
        if buf:
            (out_dir / f"chunk.{idx:05d}.sql").write_text("".join(buf), encoding="utf-8")
            idx += 1
            buf = []
            stmts = 0

    with open(src, encoding="utf-8") as f:
        for line in f:
            buf.append(line)
            if line.rstrip().endswith(";"):
                stmts += 1
                if stmts >= n:
                    flush()
    flush()
    print(idx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
