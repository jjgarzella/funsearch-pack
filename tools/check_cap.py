#!/usr/bin/env python3
"""Independently check dumped F_3^n cap files using only Python's stdlib."""
import argparse
from pathlib import Path
import re
import sys


def check_cap(path):
    match = re.search(r"-n([1-9][0-9]*)\.txt$", path.name)
    if not match:
        raise ValueError("filename must end with -n<dimension>.txt")
    n = int(match.group(1))
    points = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            point = tuple(int(value) for value in line.split())
        except ValueError as exc:
            raise ValueError(f"line {line_number}: non-integer coordinate") from exc
        if len(point) != n or any(value not in (0, 1, 2) for value in point):
            raise ValueError(f"line {line_number}: expected {n} coordinates in {{0,1,2}}")
        points.append(point)
    if not points:
        raise ValueError("empty cap dump")
    members = set(points)
    if len(members) != len(points):
        raise ValueError("duplicate vectors")
    for i, x in enumerate(points):
        for y in points[:i]:
            z = tuple(-(a + b) % 3 for a, b in zip(x, y))
            if z in members:
                raise ValueError(f"line found: {x}, {y}, {z}")
    return n, len(points)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="cap files or directories of *.txt dumps")
    args = parser.parse_args(argv)
    files = []
    for path in args.paths:
        files.extend(sorted(path.glob("*.txt")) if path.is_dir() else [path])
    if not files:
        print("ERROR: no cap files found", file=sys.stderr)
        return 1
    valid = True
    for path in files:
        try:
            n, size = check_cap(path)
        except (OSError, ValueError) as exc:
            print(f"INVALID {path}: {exc}", file=sys.stderr)
            valid = False
        else:
            print(f"OK {path}: n={n}, size={size}")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
