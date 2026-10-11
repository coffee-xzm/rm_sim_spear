"""Offline tests for the rm_sim_rune shared-memory protocol reader.

These tests only exercise the pure encoding/decoding helpers; nothing here
touches ``/dev/shm`` or a running game.
"""

import struct
import sys
import unittest
from pathlib import Path

import numpy as np

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import read_rune_frames as rf  # noqa: E402


class PackCameraCommandTest(unittest.TestCase):
    def test_datagram_size_matches_protocol_static_assert(self) -> None:
        # rmsim_protocol.h asserts sizeof(FCommand) == 48.
        self.assertEqual(len(rf.pack_camera_command(0.0, 0.0, 0.0)), 48)

    def test_header_and_body_fields(self) -> None:
        datagram = rf.pack_camera_command(11.5, -25.0, 3.0, camera_follow=False, sequence=7, timestamp_ns=99)
        magic, version, message_type, size, sequence, timestamp_ns = struct.unpack_from("<IHHIQQ", datagram, 0)
        yaw, pitch, roll, follow = struct.unpack_from("<fffB", datagram, 28)
        self.assertEqual((magic, version, message_type, size), (rf.MAGIC, 1, rf.MESSAGE_TYPE_COMMAND, 48))
        self.assertEqual((sequence, timestamp_ns), (7, 99))
        self.assertAlmostEqual(yaw, 11.5)
        self.assertAlmostEqual(pitch, -25.0)
        self.assertAlmostEqual(roll, 3.0)
        self.assertEqual(follow, 0)

    def test_camera_follow_flag_round_trips(self) -> None:
        self.assertEqual(struct.unpack_from("<fffB", rf.pack_camera_command(0.0, 0.0, 0.0), 28)[3], 1)


class TransportHeaderTest(unittest.TestCase):
    def test_even_commit_means_complete(self) -> None:
        head = struct.pack("<IHHQ", rf.MAGIC, rf.TRANSPORT_VERSION, rf.TRANSPORT_HEADER_BYTES, 10856)
        parsed = rf.parse_transport_header(head)
        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["frame_sequence"], 5428)

    def test_odd_commit_means_writer_in_progress(self) -> None:
        head = struct.pack("<IHHQ", rf.MAGIC, rf.TRANSPORT_VERSION, rf.TRANSPORT_HEADER_BYTES, 10857)
        self.assertFalse(rf.parse_transport_header(head)["complete"])

    def test_zero_commit_is_not_a_frame(self) -> None:
        head = struct.pack("<IHHQ", rf.MAGIC, rf.TRANSPORT_VERSION, rf.TRANSPORT_HEADER_BYTES, 0)
        self.assertFalse(rf.parse_transport_header(head)["complete"])


class FrameHeaderTest(unittest.TestCase):
    def test_frame_header_matches_protocol_static_assert(self) -> None:
        # rmsim_protocol.h asserts sizeof(FImageFrame) == 48 and offsetof in the mapping == 16.
        self.assertEqual(rf.FRAME_HEADER_BYTES, 48)
        self.assertEqual(rf.TRANSPORT_HEADER_BYTES, 16)

    def test_geometry_fields(self) -> None:
        head = struct.pack(
            "<IHHIQQIIIII",
            rf.MAGIC, 1, rf.MESSAGE_TYPE_CAMERA, 48, 11930, 42,
            rf.IMAGE_WIDTH, rf.IMAGE_HEIGHT, rf.IMAGE_WIDTH * 4, rf.IMAGE_BYTES, rf.PIXEL_FORMAT_BGRA8)
        parsed = rf.parse_frame_header(head)
        self.assertEqual(parsed["sequence"], 11930)
        self.assertEqual((parsed["width"], parsed["height"]), (1440, 1080))
        self.assertEqual(parsed["data_bytes"], rf.IMAGE_BYTES)

    def test_mapping_size_is_header_plus_payload(self) -> None:
        self.assertEqual(rf.MAPPING_BYTES, 16 + 48 + 1440 * 1080 * 4)


class BgraToRgbTest(unittest.TestCase):
    def test_channel_order(self) -> None:
        bgra = bytearray(rf.IMAGE_WIDTH * rf.IMAGE_HEIGHT * 4)
        # first pixel: B=1, G=2, R=3, A=255
        bgra[0:4] = bytes((1, 2, 3, 255))
        image = rf.bgra_to_rgb(bytes(bgra))
        self.assertEqual(image.shape, (rf.IMAGE_HEIGHT, rf.IMAGE_WIDTH, 3))
        self.assertEqual(tuple(int(v) for v in image[0, 0]), (3, 2, 1))

    def test_output_is_decoupled_from_the_mapping(self) -> None:
        # grab_frame hands back a copy, not a view into the mapped file.
        raw = np.zeros((rf.IMAGE_HEIGHT, rf.IMAGE_WIDTH, 4), dtype=np.uint8)
        image = rf.bgra_to_rgb(raw.tobytes())
        raw[0, 0, 0] = 200
        self.assertEqual(int(image[0, 0, 2]), 0)


if __name__ == "__main__":
    unittest.main()
