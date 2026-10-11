"""Tests for the shared-memory frame ring (rm_sim_spear.frame_publisher)."""

from __future__ import annotations

import struct
import sys
import unittest
import uuid
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rm_sim_spear import frame_publisher as fp


def _unique_name() -> str:
    return f"rm_sim_test_{uuid.uuid4().hex[:12]}"


class FrameRingTests(unittest.TestCase):
    def test_round_trip_preserves_pixels_and_metadata(self):
        name = _unique_name()
        publisher = fp.FrameRingPublisher(name, width=6, height=4, channels=3, slots=3)
        try:
            reader = fp.FrameRingReader(name)
            try:
                for index in range(5):
                    pixels = np.full((4, 6, 3), index + 1, dtype=np.uint8)
                    sequence = publisher.publish(
                        pixels, commit_id=100 + index, sim_time_ns=index*1_000_000)
                    self.assertEqual(sequence, index + 1)
                    image, meta = reader.read(after_sequence=index)
                    np.testing.assert_array_equal(image, pixels)
                    self.assertEqual(meta["sequence"], index + 1)
                    self.assertEqual(meta["commit_id"], 100 + index)
                    self.assertEqual(meta["sim_time_ns"], index*1_000_000)
                    self.assertEqual((meta["width"], meta["height"]), (6, 4))
            finally:
                reader.close()
        finally:
            publisher.close()
            publisher.unlink()

    def test_ring_wraps_and_latest_sequence_wins(self):
        name = _unique_name()
        publisher = fp.FrameRingPublisher(name, width=2, height=2, channels=3, slots=2)
        try:
            reader = fp.FrameRingReader(name)
            try:
                for index in range(5):
                    publisher.publish(np.full((2, 2, 3), index, dtype=np.uint8),
                                      commit_id=index)
                image, meta = reader.read()
                self.assertEqual(meta["sequence"], 5)
                # slot 5 % 2 == 1 holds the newest frame
                np.testing.assert_array_equal(image, np.full((2, 2, 3), 4, dtype=np.uint8))
            finally:
                reader.close()
        finally:
            publisher.close()
            publisher.unlink()

    def test_reader_never_returns_a_half_written_slot(self):
        # Hand-built ring: an odd sequence means "writer is mid-copy".  The reader
        # must not hand that payload back even though the mapping is visible.
        name = _unique_name()
        width = height = channels = 2
        slots = 1
        shm = shared_memory.SharedMemory(
            name=name, create=True, size=fp.ring_bytes(width, height, channels, slots))
        try:
            fp._HEADER.pack_into(
                shm.buf, 0, fp.RING_MAGIC, fp.RING_VERSION, fp.HEADER_BYTES, slots,
                width, height, channels, 0, 1, 0, width*height*channels)
            fp._SLOT.pack_into(shm.buf, fp.HEADER_BYTES, 1, 7, 0, 0)  # odd = in progress
            data_offset = fp.HEADER_BYTES + slots*fp.SLOT_HEADER_BYTES
            shm.buf[data_offset:data_offset + width*height*channels] = b"\xff" * (width*height*channels)

            reader = fp.FrameRingReader(name)
            try:
                with self.assertRaises(TimeoutError):
                    reader.read(timeout_s=0.1, poll_s=0.01)
                fp._SLOT.pack_into(shm.buf, fp.HEADER_BYTES, 2, 7, 0, 0)  # complete now
                image, meta = reader.read(timeout_s=0.5)
                self.assertEqual(meta["sequence"], 1)
                self.assertEqual(meta["commit_id"], 7)
                self.assertTrue(np.all(image == 255))
            finally:
                reader.close()
        finally:
            shm.close()
            shm.unlink()

    def test_publish_rejects_other_geometry_and_dtypes(self):
        name = _unique_name()
        publisher = fp.FrameRingPublisher(name, width=4, height=3, channels=3)
        try:
            with self.assertRaises(ValueError):
                publisher.publish(np.zeros((3, 4, 3), dtype=np.uint8)[:, :2])  # wrong width
            with self.assertRaises(ValueError):
                publisher.publish(np.zeros((3, 4, 3), dtype=np.float32))
        finally:
            publisher.close()
            publisher.unlink()

    def test_stale_mapping_with_another_geometry_is_replaced(self):
        name = _unique_name()
        first = fp.FrameRingPublisher(name, width=4, height=4, channels=3, slots=2)
        first.close()
        second = fp.FrameRingPublisher(name, width=8, height=8, channels=3, slots=2)
        try:
            reader = fp.FrameRingReader(name)
            try:
                self.assertEqual((reader.width, reader.height), (8, 8))
            finally:
                reader.close()
        finally:
            second.close()
            second.unlink()

    def test_names_with_slashes_are_rejected(self):
        with self.assertRaises(ValueError):
            fp.FrameRingPublisher("/dev/shm/nope", width=2, height=2)


if __name__ == "__main__":
    unittest.main()
