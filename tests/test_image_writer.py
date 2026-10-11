"""Tests for the off-loop PNG writer (rm_sim_spear.image_writer)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rm_sim_spear.image_writer import AsyncImageWriter


class AsyncImageWriterTests(unittest.TestCase):
    def test_keeps_each_submitted_image_in_submission_order(self):
        # Guards the pipeline failure mode: under triple buffering the writer is
        # fed one frame per loop iteration, so a reordered or reused buffer would
        # silently label a frame with another frame's pixels.
        with tempfile.TemporaryDirectory() as tmp:
            writer = AsyncImageWriter(queue_size=2)
            for index in range(6):
                writer.submit(
                    Path(tmp) / f"frame_{index:04d}.png",
                    np.full((8, 8, 3), index, dtype=np.uint8),
                )
            writer.close()

            self.assertEqual(writer.summary()["images"], 6)
            for index in range(6):
                written = cv2.imread(str(Path(tmp) / f"frame_{index:04d}.png"))
                self.assertIsNotNone(written, f"frame {index} was not written")
                np.testing.assert_array_equal(
                    written, np.full((8, 8, 3), index, dtype=np.uint8))

    def test_summary_reports_mean_encode_time_only_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = AsyncImageWriter()
            writer.submit(Path(tmp) / "one.png", np.zeros((4, 4, 3), dtype=np.uint8))
            writer.close()
            summary = writer.summary()
            self.assertEqual(summary["images"], 1)
            self.assertIsNotNone(summary["encode_ms_mean"])

    def test_empty_writer_reports_no_mean(self):
        writer = AsyncImageWriter()
        writer.close()
        self.assertIsNone(writer.summary()["encode_ms_mean"])

    def test_unwritable_destination_surfaces_from_close(self):
        # cv2.imwrite returns False for a missing directory; the writer has to
        # turn that into an error instead of silently dropping frames.
        missing = Path(tempfile.gettempdir()) / "rm_sim_no_such_dir" / "frame.png"
        writer = AsyncImageWriter()
        writer.submit(missing, np.zeros((4, 4, 3), dtype=np.uint8))
        with self.assertRaises(RuntimeError):
            writer.close()

    def test_rejects_non_positive_queue_size(self):
        with self.assertRaises(ValueError):
            AsyncImageWriter(queue_size=0)


if __name__ == "__main__":
    unittest.main()
