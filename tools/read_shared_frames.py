#!/usr/bin/env python3
"""Read live frames from a ``tools/run_physics_sim.py --publish-shm`` ring.

Why this exists next to ``tools/read_rune_frames.py``: the rune reader speaks the
SDK's own wire format, this one speaks ours.  Both exist because PNG is the
acceptance format, not the delivery format -- encoding a 1440x1080 frame costs
22-39 ms, more than the whole capture loop once the readback is pipelined, while
publishing it into shared memory costs a ~2 ms copy
(``docs/render_budget.md`` section 13).

Typical use::

    # in one terminal: run the sim and publish into a ring
    python -u tools/run_physics_sim.py --frames 200 ... --publish-shm rm_sim_spear_frames \\
        --save-images none --buffering triple --sync-pose

    # in another: discover what is publishing, then grab the newest frame
    python3 tools/read_shared_frames.py --list
    python3 tools/read_shared_frames.py --output /tmp/frame.png

    # measure the published rate (the ring is the delivery path, so this is the
    # rate a consumer can actually keep up with)
    python3 tools/read_shared_frames.py --fps-seconds 6

This tool only needs ``python3`` (numpy + cv2), not the ``spear-env`` conda
environment and not the engine's GPU budget.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rm_sim_spear.frame_publisher import FrameRingReader  # noqa: E402

DEFAULT_NAME = "rm_sim_spear_frames"


def list_rings() -> list[dict]:
    """Describe every ``/dev/shm`` mapping that carries our ring header."""
    found: list[dict] = []
    shm_dir = Path("/dev/shm")
    if not shm_dir.is_dir():
        return found
    for path in sorted(shm_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            reader = FrameRingReader(path.name)
        except Exception:  # noqa: BLE001 - a non-ring mapping is expected here
            continue
        try:
            header = reader.describe()
            found.append({
                "name": path.name,
                "width": header["width"],
                "height": header["height"],
                "channels": header["channels"],
                "slots": header["slot_count"],
                "latest_sequence": header["latest_sequence"],
                "bytes": reader.size,
            })
        finally:
            reader.close()
    return found


def open_reader(name: str, timeout_s: float) -> FrameRingReader:
    """Attach to a ring, waiting for the publisher to create it.

    The sim spends ~30 s loading the map before its first publish, so a consumer
    that starts first has to wait for the mapping instead of failing on it.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return FrameRingReader(name)
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"ring {name!r} did not appear within {timeout_s}s") from None
            time.sleep(0.05)


def measure_fps(name: str, seconds: float, timeout_s: float) -> float:
    """Measure how often the publisher completes a frame."""
    reader = open_reader(name, timeout_s)
    try:
        first = reader.describe()["latest_sequence"]
        start = time.monotonic()
        time.sleep(seconds)
        last = reader.describe()["latest_sequence"]
        elapsed = time.monotonic() - start
    finally:
        reader.close()
    return (last - first)/elapsed if elapsed > 0 else 0.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default=DEFAULT_NAME,
                        help=f"ring name in /dev/shm (default: {DEFAULT_NAME})")
    parser.add_argument("--list", action="store_true",
                        help="list every ring currently in /dev/shm and exit")
    parser.add_argument("--output", type=Path,
                        help="write the newest complete frame here as PNG")
    parser.add_argument("--fps-seconds", type=float, default=0.0,
                        help="measure the published frame rate for this many seconds")
    parser.add_argument("--timeout-seconds", type=float, default=5.0,
                        help="how long to wait for a frame (default: 5.0)")
    parser.add_argument("--after-sequence", type=int, default=None,
                        help="wait for a frame published after this sequence number")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report: dict[str, object] = {}

    if args.list:
        report["rings"] = list_rings()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0

    if args.fps_seconds > 0:
        report["published_fps"] = round(
            measure_fps(args.name, args.fps_seconds, args.timeout_seconds), 2)

    if args.output is not None or args.fps_seconds <= 0:
        reader = open_reader(args.name, args.timeout_seconds)
        try:
            image, meta = reader.read(
                after_sequence=args.after_sequence, timeout_s=args.timeout_seconds)
        finally:
            reader.close()
        report["frame"] = {key: meta[key] for key in (
            "sequence", "commit_id", "sim_time_ns", "width", "height", "channels")}
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(args.output), image):  # the ring carries BGR
                raise RuntimeError(f"failed to write {args.output}")
            report["output"] = str(args.output)

    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
