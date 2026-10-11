"""Publish captured frames through a POSIX shared-memory ring.

Why this exists: PNG is the acceptance format here (lossless, and every
pixel-level A/B in ``docs/`` is measured on PNGs), but it is not a delivery
format -- ``cv2.imencode('.png')`` costs ~22 ms for 1440x1080 on this machine and
~39 ms under load, which is more than the whole capture loop once the readback is
pipelined (``docs/render_budget.md`` section 13).  Writing raw bytes instead costs
~2 ms.  Publishers and consumers are separate processes, so the frame has to
cross a process boundary without an encode: a shared-memory ring does exactly
that, and it is the same shape ``~/rm_sim_rune`` uses (a ``/dev/shm`` mapping
plus a commit sequence).

Layout of one mapping (all little-endian)::

    [ header 64 B ][ slot headers, 32 B each ][ slot payloads, H*W*C each ]

The header carries the geometry and ``latest_sequence``.  Each slot header
carries a commit sequence that is odd while the writer is copying and even once
the frame is complete, so a reader can detect a torn frame without a lock: read
the sequence, copy the payload, read the sequence again, retry if it changed.

This is deliberately single-writer.  Two writers would need a lock, and the
dataset loop has exactly one.
"""

from __future__ import annotations

import struct
import time
from multiprocessing import resource_tracker, shared_memory

import numpy as np

__all__ = [
    "DEFAULT_SLOTS",
    "HEADER_BYTES",
    "RING_MAGIC",
    "RING_VERSION",
    "SLOT_HEADER_BYTES",
    "FrameRingPublisher",
    "FrameRingReader",
    "ring_bytes",
]

RING_MAGIC = 0x524D5346  # "RMSF"
RING_VERSION = 1
HEADER_BYTES = 64
SLOT_HEADER_BYTES = 32
DEFAULT_SLOTS = 8

# magic, version, header_bytes, slot_count, width, height, channels, dtype_code,
# latest_sequence, latest_slot, frame_bytes, pad to 64
_HEADER = struct.Struct("<IIIIIIIIQII16x")
# sequence, commit_id, sim_time_ns, pad
_SLOT = struct.Struct("<QQQQ")

_DTYPE_UINT8 = 0


def _detach_from_resource_tracker(shm: shared_memory.SharedMemory) -> None:
    """Stop CPython's resource tracker from destroying a segment we only attached to.

    ``SharedMemory.__init__`` registers the segment with the resource tracker
    whether it created it or merely attached, and the tracker unlinks everything
    still registered when the process exits -- while ``close()`` does *not*
    unregister (checked in CPython 3.10 and 3.11).  A consumer that attaches and
    exits would therefore delete the running publisher's ring, which is exactly
    what ``tools/read_shared_frames.py --list`` does to every ``/dev/shm`` entry.
    """
    try:
        resource_tracker.unregister(shm._name, "shared_memory")
    except Exception:  # noqa: BLE001 - the tracker is advisory here
        pass


def ring_bytes(width: int, height: int, channels: int = 3, slots: int = DEFAULT_SLOTS) -> int:
    """Total mapping size for one geometry."""
    return HEADER_BYTES + slots*SLOT_HEADER_BYTES + slots*width*height*channels


class FrameRingPublisher:
    """Write frames into a shared-memory ring, one slot per published frame.

    ``create`` builds a fresh mapping and unlinks any stale one of a different
    size (a crashed run leaves one behind, and POSIX shared memory survives its
    creator).  Consumers attach with :class:`FrameRingReader`, which is also what
    ``tools/read_shared_frames.py`` uses.
    """

    def __init__(
        self,
        name: str,
        width: int,
        height: int,
        channels: int = 3,
        slots: int = DEFAULT_SLOTS,
        create: bool = True,
    ):
        if not name or "/" in name:
            raise ValueError("shared-memory name must be non-empty and contain no '/'")
        if width < 1 or height < 1 or channels < 1:
            raise ValueError("frame geometry must be positive")
        if slots < 1:
            raise ValueError("slots must be positive")
        self.name = name
        self.width = int(width)
        self.height = int(height)
        self.channels = int(channels)
        self.slots = int(slots)
        self.frame_bytes = self.width*self.height*self.channels
        self.size = ring_bytes(self.width, self.height, self.channels, self.slots)

        if not create:
            self._shm = shared_memory.SharedMemory(name=name, create=False)
        else:
            self._shm = self._open_or_recreate(name)

        self._buffer = self._shm.buf
        self._sequence = 0
        if create:
            self._initialize_header()

    def _open_or_recreate(self, name: str) -> shared_memory.SharedMemory:
        try:
            return shared_memory.SharedMemory(name=name, create=True, size=self.size)
        except FileExistsError:
            # A previous run left the mapping (or crashed); it is only reusable if
            # the size matches, otherwise the geometry changed and it must go.
            existing = shared_memory.SharedMemory(name=name, create=False)
            if existing.size == self.size:
                return existing
            existing.close()
            existing.unlink()
            return shared_memory.SharedMemory(name=name, create=True, size=self.size)

    def _initialize_header(self) -> None:
        _HEADER.pack_into(
            self._buffer, 0,
            RING_MAGIC, RING_VERSION, HEADER_BYTES, self.slots,
            self.width, self.height, self.channels, _DTYPE_UINT8,
            0, 0, self.frame_bytes,
        )
        for slot in range(self.slots):
            _SLOT.pack_into(self._buffer, self._slot_header_offset(slot), 0, 0, 0, 0)

    def _slot_header_offset(self, slot: int) -> int:
        return HEADER_BYTES + slot*SLOT_HEADER_BYTES

    def _slot_data_offset(self, slot: int) -> int:
        return (HEADER_BYTES + self.slots*SLOT_HEADER_BYTES
                + slot*self.frame_bytes)

    def publish(self, image: np.ndarray, *, commit_id: int = 0, sim_time_ns: int = 0) -> int:
        """Copy one frame into the ring and return its published sequence number.

        The copy is the only per-frame cost here (a 1440x1080 BGRA8 frame is
        ~6 MB, measured at 1.6-2.3 ms including the file-less write path).
        """
        array = np.ascontiguousarray(image)
        if array.dtype != np.uint8:
            raise ValueError(f"only uint8 frames are supported, got {array.dtype}")
        if array.shape != (self.height, self.width, self.channels):
            raise ValueError(
                f"frame shape {array.shape} does not match the ring's "
                f"{(self.height, self.width, self.channels)}")

        slot = self._sequence % self.slots
        sequence = self._sequence + 1
        header_offset = self._slot_header_offset(slot)
        data_offset = self._slot_data_offset(slot)

        # Odd sequence = being written; a reader that sees this retries.
        _SLOT.pack_into(self._buffer, header_offset, 2*sequence - 1, commit_id, sim_time_ns, 0)
        self._buffer[data_offset:data_offset + self.frame_bytes] = array.tobytes()
        _SLOT.pack_into(self._buffer, header_offset, 2*sequence, commit_id, sim_time_ns, 0)
        _HEADER.pack_into(
            self._buffer, 0,
            RING_MAGIC, RING_VERSION, HEADER_BYTES, self.slots,
            self.width, self.height, self.channels, _DTYPE_UINT8,
            sequence, slot, self.frame_bytes,
        )
        self._sequence = sequence
        return sequence

    def close(self) -> None:
        """Close the mapping; ``unlink`` is up to the caller (``FrameRingReader``
        may still be attached, and POSIX keeps the mapping alive until then)."""
        self._buffer = None
        self._shm.close()

    def unlink(self) -> None:
        """Remove the mapping from ``/dev/shm``; attached readers keep it alive.

        ``close()`` already unregisters the segment with CPython's resource
        tracker, which is what stops the tracker from unlinking it a second time
        when this process exits, so this is a plain unlink.
        """
        self._shm.unlink()


class FrameRingReader:
    """Read frames out of a shared-memory ring published by another process."""

    def __init__(self, name: str):
        self.name = name
        self._shm = shared_memory.SharedMemory(name=name, create=False)
        _detach_from_resource_tracker(self._shm)
        try:
            self._buffer = self._shm.buf
            header = self.describe()
            self.width = header["width"]
            self.height = header["height"]
            self.channels = header["channels"]
            self.slots = header["slot_count"]
            self.frame_bytes = header["frame_bytes"]
            expected = ring_bytes(self.width, self.height, self.channels, self.slots)
            if self._shm.size < expected:
                raise RuntimeError(
                    f"ring {name!r} is {self._shm.size} bytes, expected at least {expected}")
        except BaseException:
            # Attaching registers the segment with CPython's resource tracker,
            # and the tracker unlinks everything still registered when the
            # process exits.  A reader that rejects a mapping (the --list scan
            # tries every /dev/shm entry) must therefore release its handle
            # before raising, or a later exit would delete another process's
            # ring.
            self._buffer = None
            self._shm.close()
            raise

    @property
    def size(self) -> int:
        """Size of the underlying mapping in bytes."""
        return self._shm.size

    def describe(self) -> dict:
        fields = _HEADER.unpack_from(self._buffer, 0)
        header = {
            "magic": fields[0], "version": fields[1], "header_bytes": fields[2],
            "slot_count": fields[3], "width": fields[4], "height": fields[5],
            "channels": fields[6], "dtype_code": fields[7],
            "latest_sequence": fields[8], "latest_slot": fields[9],
            "frame_bytes": fields[10],
        }
        if header["magic"] != RING_MAGIC:
            raise RuntimeError(f"ring {self.name!r} has no RMSF header (magic={header['magic']:#x})")
        if header["version"] != RING_VERSION:
            raise RuntimeError(f"ring {self.name!r} has version {header['version']}, expected {RING_VERSION}")
        if header["dtype_code"] != _DTYPE_UINT8:
            raise RuntimeError(f"ring {self.name!r} has unsupported dtype code {header['dtype_code']}")
        return header

    def read(self, *, after_sequence: int | None = None, timeout_s: float = 3.0,
             poll_s: float = 0.002) -> tuple[np.ndarray, dict]:
        """Return a complete frame, waiting for one newer than ``after_sequence``.

        The payload is copied out of the mapping before returning, so the writer
        may overwrite that slot immediately afterwards.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            header = self.describe()
            sequence = header["latest_sequence"]
            fresh = after_sequence is None or sequence > after_sequence
            if sequence > 0 and fresh:
                frame = self._read_slot(header["latest_slot"], header)
                if frame is not None:
                    return frame
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"no new frame in {timeout_s}s on {self.name!r} "
                    f"(latest_sequence={sequence})")
            time.sleep(poll_s)

    def _read_slot(self, slot: int, header: dict) -> tuple[np.ndarray, dict] | None:
        header_offset = HEADER_BYTES + slot*SLOT_HEADER_BYTES
        data_offset = HEADER_BYTES + self.slots*SLOT_HEADER_BYTES + slot*self.frame_bytes
        before = _SLOT.unpack_from(self._buffer, header_offset)
        if before[0] == 0 or before[0] % 2 == 1:
            return None  # never written, or the writer is mid-copy
        payload = bytes(self._buffer[data_offset:data_offset + self.frame_bytes])
        after = _SLOT.unpack_from(self._buffer, header_offset)
        if before != after:
            return None  # torn: retry
        image = np.frombuffer(payload, dtype=np.uint8).reshape(
            self.height, self.width, self.channels)
        return image, {
            "name": self.name,
            "sequence": before[0] // 2,
            "commit_id": before[1],
            "sim_time_ns": before[2],
            "width": self.width,
            "height": self.height,
            "channels": self.channels,
            "latest_sequence": header["latest_sequence"],
        }

    def close(self) -> None:
        self._buffer = None
        self._shm.close()
