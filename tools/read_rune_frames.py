#!/usr/bin/env python3
"""Read live camera frames from the ``~/rm_sim_rune`` packaged Linux build.

The rune project ships a cooked standalone (``package_linux.sh`` produces
``/tmp/rm_sim_package/Linux/rm_sim.sh``) that renders through a plain
``USceneCaptureComponent2D`` at 1440x1080 -- the same resolution and the same
capture mechanism we use -- and publishes each frame as BGRA8 through POSIX
shared memory, accepting camera commands over UDP.  See
``SimUnreal/Plugins/sim/Source/ThirdParty/rmsim_sdk/include/rmsim_protocol.h``
for the wire format this tool implements.

Why this exists: it is the reference side of the comparison in
``docs/rendering.md`` section 12.  The rune build renders the *same* map
(``Content/creo_stp.umap``, which our ``ue/Content`` is a copy of) at the *same*
output resolution with every renderer feature left at its default, and it costs
us no GPU budget to observe.  Diffing our own capture against it separates "our
renderer settings are wrong" from "our camera/geometry/framing is the problem".

Typical use::

    # dump the newest frame the running game has published
    python3 tools/read_rune_frames.py --output /tmp/rune_now.png

    # measure the live capture rate (shared memory is the delivery path)
    python3 tools/read_rune_frames.py --fps-seconds 6

    # point the rune camera down 25 degrees, grab, then put it back
    python3 tools/read_rune_frames.py --camera 0,-25,0 --output /tmp/rune_down.png
    python3 tools/read_rune_frames.py --camera 0,0,0

The camera command only survives until the next command; the game's own mouse
input also overwrites the rotation, so restore it (or leave the game alone) when
you are done.
"""

from __future__ import annotations

import argparse
import json
import mmap
import socket
import struct
import sys
import time
from pathlib import Path

import cv2  # noqa: E402
import numpy as np  # noqa: E402

# --- wire format: rmsim_protocol.h -------------------------------------------

MAGIC = 0x524D5344  # "RMSD"
PROTOCOL_VERSION = 1
TRANSPORT_VERSION = 2
MESSAGE_TYPE_CAMERA = 1
MESSAGE_TYPE_IMU = 2
MESSAGE_TYPE_COMMAND = 3
PIXEL_FORMAT_BGRA8 = 1

IMAGE_WIDTH = 1440
IMAGE_HEIGHT = 1080
IMAGE_BYTES = IMAGE_WIDTH * IMAGE_HEIGHT * 4
IMU_CAPACITY = 512

TRANSPORT_HEADER_BYTES = 16
FRAME_HEADER_BYTES = 48
IMU_SAMPLE_BYTES = 64
MAPPING_BYTES = TRANSPORT_HEADER_BYTES + FRAME_HEADER_BYTES + IMAGE_BYTES

CAMERA_SHM = "/dev/shm/rmsim_camera_v2"
IMU_SHM = "/dev/shm/rmsim_imu_v1"
COMMAND_ENDPOINT = ("127.0.0.1", 39001)

_TRANSPORT = struct.Struct("<IHHQ")
_FRAME = struct.Struct("<IHHIQQIIIII")
_COMMAND = struct.Struct("<IHHIQQfffB7x")
_IMU = struct.Struct("<IHHIQQ9f")


def pack_camera_command(
    yaw: float,
    pitch: float,
    roll: float,
    *,
    camera_follow: bool = True,
    sequence: int = 0,
    timestamp_ns: int = 0,
) -> bytes:
    """Pack an ``FCommand`` datagram (``RunePlayerController::PlayerTick``)."""
    return _COMMAND.pack(
        MAGIC,
        PROTOCOL_VERSION,
        MESSAGE_TYPE_COMMAND,
        _COMMAND.size,
        sequence,
        timestamp_ns,
        float(yaw),
        float(pitch),
        float(roll),
        1 if camera_follow else 0,
    )


def parse_transport_header(head: bytes) -> dict[str, int]:
    """Parse ``FImageTransportHeader``.

    ``CommitSequence`` is odd while the publisher writes and even once the frame
    is complete; its committed value is ``frame_sequence * 2``.
    """
    magic, version, header_bytes, commit_sequence = _TRANSPORT.unpack_from(head, 0)
    return {
        "magic": magic,
        "version": version,
        "header_bytes": header_bytes,
        "commit_sequence": commit_sequence,
        "complete": commit_sequence != 0 and commit_sequence % 2 == 0,
        "frame_sequence": commit_sequence // 2,
    }


def parse_frame_header(head: bytes) -> dict[str, int]:
    """Parse the ``FImageFrame`` that immediately follows the transport header."""
    fields = _FRAME.unpack_from(head, 0)
    keys = (
        "magic",
        "version",
        "type",
        "size",
        "sequence",
        "timestamp_ns",
        "width",
        "height",
        "stride",
        "data_bytes",
        "pixel_format",
    )
    return dict(zip(keys, fields))


def bgra_to_rgb(buffer: bytes) -> np.ndarray:
    """Convert one BGRA8 frame payload into an ``(H, W, 3)`` RGB array."""
    bgra = np.frombuffer(buffer, dtype=np.uint8).reshape(IMAGE_HEIGHT, IMAGE_WIDTH, 4)
    return bgra[:, :, 2::-1].copy()


def grab_frame(
    *,
    path: str = CAMERA_SHM,
    timeout_s: float = 3.0,
    poll_s: float = 0.004,
    after_commit: int | None = None,
) -> tuple[np.ndarray, dict[str, int]]:
    """Return the newest complete frame, optionally waiting for a newer commit.

    Frames are copied out of the mapping before the lock-free publisher can
    overwrite them; the transport is the same one ``librmsim_sdk`` consumes.
    """
    with open(path, "rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            deadline = time.monotonic() + timeout_s
            while True:
                transport = parse_transport_header(mapping[:TRANSPORT_HEADER_BYTES])
                fresh = after_commit is None or transport["commit_sequence"] != after_commit
                if transport["complete"] and fresh:
                    frame = parse_frame_header(
                        mapping[TRANSPORT_HEADER_BYTES:TRANSPORT_HEADER_BYTES + FRAME_HEADER_BYTES])
                    start = TRANSPORT_HEADER_BYTES + FRAME_HEADER_BYTES
                    payload = mapping[start:start + frame["data_bytes"]]
                    if frame["width"] == IMAGE_WIDTH and frame["height"] == IMAGE_HEIGHT:
                        return bgra_to_rgb(payload), {**transport, **frame}
                    raise RuntimeError(f"unexpected frame geometry: {frame['width']}x{frame['height']}")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"no new frame in {timeout_s}s (commit={transport['commit_sequence']})")
                time.sleep(poll_s)


def read_imu(path: str = IMU_SHM) -> dict[str, float]:
    """Read the newest IMU sample (the rune build derives it from the view point)."""
    with open(path, "rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            sequence = struct.unpack_from("<Q", mapping, 0)[0]
            offset = 8 + (sequence % IMU_CAPACITY) * IMU_SAMPLE_BYTES
            fields = _IMU.unpack_from(mapping, offset)
    keys = (
        "magic",
        "version",
        "type",
        "size",
        "sequence",
        "timestamp_ns",
        "accel_forward",
        "accel_left",
        "accel_up",
        "gyro_roll",
        "gyro_pitch",
        "gyro_yaw",
        "yaw",
        "pitch",
        "roll",
    )
    return dict(zip(keys, fields))


def send_camera_command(yaw: float, pitch: float, roll: float, *, endpoint=COMMAND_ENDPOINT) -> None:
    """Send one camera pose to the running game (non-blocking UDP, fire and forget)."""
    datagram = pack_camera_command(yaw, pitch, roll, timestamp_ns=time.time_ns())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(datagram, endpoint)


def measure_fps(seconds: float, *, path: str = CAMERA_SHM) -> float:
    """Measure how often the game completes a published capture."""
    with open(path, "rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            first = parse_transport_header(mapping[:TRANSPORT_HEADER_BYTES])["frame_sequence"]
            start = time.monotonic()
            time.sleep(seconds)
            last = parse_transport_header(mapping[:TRANSPORT_HEADER_BYTES])["frame_sequence"]
            elapsed = time.monotonic() - start
    return (last - first) / elapsed if elapsed > 0 else 0.0


def _parse_triple(text: str) -> tuple[float, float, float]:
    parts = [piece for piece in text.replace(" ", "").split(",") if piece != ""]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"expected 'yaw,pitch,roll', got {text!r}")
    return float(parts[0]), float(parts[1]), float(parts[2])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, help="write the newest complete frame here as PNG")
    parser.add_argument("--camera", type=_parse_triple, metavar="YAW,PITCH,ROLL",
                        help="send this control rotation to the running game before grabbing")
    parser.add_argument("--settle-seconds", type=float, default=1.2,
                        help="wait this long after --camera before grabbing (default: 1.2)")
    parser.add_argument("--fps-seconds", type=float, default=0.0,
                        help="measure the published capture rate for this many seconds")
    parser.add_argument("--imu", action="store_true", help="print the newest IMU sample")
    parser.add_argument("--timeout-seconds", type=float, default=3.0, help="frame wait timeout (default: 3.0)")
    parser.add_argument("--camera-shm", default=CAMERA_SHM, help=f"camera mapping (default: {CAMERA_SHM})")
    parser.add_argument("--imu-shm", default=IMU_SHM, help=f"IMU mapping (default: {IMU_SHM})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report: dict[str, object] = {}

    if args.imu:
        report["imu"] = read_imu(args.imu_shm)

    if args.fps_seconds > 0:
        report["capture_fps"] = round(measure_fps(args.fps_seconds, path=args.camera_shm), 2)

    if args.camera is not None:
        yaw, pitch, roll = args.camera
        send_camera_command(yaw, pitch, roll)
        report["camera_command"] = {"yaw": yaw, "pitch": pitch, "roll": roll}
        time.sleep(args.settle_seconds)

    if args.output is not None or (args.camera is None and not args.imu and args.fps_seconds <= 0):
        image, frame = grab_frame(path=args.camera_shm, timeout_s=args.timeout_seconds)
        report["frame"] = {
            "sequence": frame["sequence"],
            "timestamp_ns": frame["timestamp_ns"],
            "width": frame["width"],
            "height": frame["height"],
            "data_bytes": frame["data_bytes"],
        }
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(args.output), image[:, :, ::-1]):  # cv2 writes BGR
                raise RuntimeError(f"failed to write {args.output}")
            report["output"] = str(args.output)

    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
