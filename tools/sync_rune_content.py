#!/usr/bin/env python3
"""Copy the UE Content tree from rm_sim_rune into this workspace.

The copy is intentionally additive: existing files in the destination are
updated, unrelated destination files are preserved, and the source's nested
Git metadata is never copied.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def default_source() -> Path:
    return Path.home() / "rm_sim_rune" / "SimUnreal" / "Content"


def default_destination() -> Path:
    return Path(__file__).resolve().parents[1] / "ue" / "Content"


def copy_content(source: Path, destination: Path, *, dry_run: bool = False) -> tuple[int, int]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"source Content directory does not exist: {source}")
    if destination == source:
        raise ValueError("source and destination must be different")

    files = 0
    bytes_copied = 0
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".git" in relative.parts:
            continue
        target = destination / relative
        if path.is_dir():
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        files += 1
        bytes_copied += path.stat().st_size
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return files, bytes_copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source())
    parser.add_argument("--destination", type=Path, default=default_destination())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files, total_bytes = copy_content(args.source, args.destination, dry_run=args.dry_run)
    mode = "would copy" if args.dry_run else "copied"
    print(f"{mode} {files} files ({total_bytes / 1024**3:.2f} GiB)")
    print(f"source: {args.source.expanduser().resolve()}")
    print(f"destination: {args.destination.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
