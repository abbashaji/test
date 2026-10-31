#!/usr/bin/env python3
"""Validate the cell graph: cycles, references, path ownership, and parallel path overlaps.

Usage: python tools/validate_graph.py [--root .] [--waves]
Exit code 0 = OK, 1 = errors. Output is short on purpose.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprintlib import load_cells, load_config, validate, waves  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--waves", action="store_true", help="print which cells can run in parallel")
    a = ap.parse_args(argv)

    cfg = load_config(a.root)
    cells, load_errors = load_cells(Path(a.root) / "cells")
    errors, warnings = validate(cells, cfg)
    errors = load_errors + errors

    for w in warnings:
        print("WARN  " + w)
    if errors:
        for e in errors:
            print("ERROR " + e)
        print(f"INVALID: {len(errors)} error(s)")
        return 1
    print(f"OK: {len(cells)} cells, {len(warnings)} warning(s)")
    if a.waves:
        for lvl, ids in waves(cells).items():
            print(f"wave {lvl}: {' '.join(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
