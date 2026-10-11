import unittest
import json
import tempfile
import socket
from pathlib import Path

from rm_sim_spear import Coordinator, MujocoDriver
from rm_sim_spear import HitReport, ProjectileSubsystem, TargetGeometry, calculate_ring
from bridge.codec import EnvelopeStream, decode_envelope, encode_envelope
from bridge.messages import decode_frame_commit, encode_frame_commit, encode_snapshot, sim_time_parts
from bridge.standard_messages import clock_payload, imu_payload, joint_state_payload, tf_payload
from bridge.publisher import UnixBridgePublisher
from bridge.transport import UnixEnvelopeChannel, UnixEnvelopeServer
from rm_sim_spear.render import CompositeRenderer, MemoryRenderer, SpearRenderPort
from rm_sim_spear.spear_metadata import SpearMetadataCallbacks
from rm_sim_spear.model import Capture, FrameCommit, ReadbackMetadata, Snapshot


MODEL = Path(__file__).resolve().parents[1] / "examples" / "free_fall.xml"


class CoordinatorTests(unittest.TestCase):
    def make_coordinator(self, delay=0):
        return Coordinator(MujocoDriver.from_xml_path(MODEL, 10), MemoryRenderer(delay))

    def test_delayed_capture_pairs_with_original_snapshot(self):
        sim = self.make_coordinator(delay=2)
        first = sim.step()
        second = sim.step()
        third = sim.step()
        self.assertEqual(first.observations, ())
        self.assertEqual(second.observations, ())
        observation = third.observations[0]
        self.assertEqual(observation.capture.commit_id, first.snapshot.commit.commit_id)
        self.assertEqual(observation.snapshot.poses, first.snapshot.poses)
        self.assertNotEqual(observation.snapshot.poses, third.snapshot.poses)

    def test_reset_restarts_time_and_discards_old_capture(self):
        sim = self.make_coordinator(delay=1)
        before = sim.step()
        sim.reset()
        after = sim.step()
        self.assertNotEqual(before.snapshot.commit.run_id, after.snapshot.commit.run_id)
        self.assertEqual(after.snapshot.commit.sim_tick, 1)
        self.assertEqual(after.observations, ())
        self.assertEqual(before.snapshot.poses, after.snapshot.poses)

    def test_multiple_streams_pair_independently_with_one_snapshot(self):
        streams = (("front", "rgb"), ("front", "depth"))
        renderer = _ScheduledRenderer()
        sim = Coordinator(_StubPhysics(), renderer, capture_streams=streams)
        first = sim.step().snapshot
        renderer.ready = [
            Capture(first.commit.run_id, 1, 11, b"depth", "front", "depth"),
        ]
        second = sim.step()
        self.assertEqual(second.observations[0].snapshot, first)
        renderer.ready = [
            Capture(first.commit.run_id, 1, 10, b"rgb", "front", "rgb"),
        ]
        third = sim.step()
        self.assertEqual(third.observations[0].snapshot, first)
        self.assertEqual(sim._pending.keys(), {2, 3})

    def test_composite_renderer_produces_independent_camera_streams(self):
        renderer = CompositeRenderer((
            MemoryRenderer(sensor_id="front", modality="rgb"),
            MemoryRenderer(sensor_id="front", modality="depth"),
        ))
        sim = Coordinator(_StubPhysics(), renderer)
        result = sim.step()
        self.assertEqual(
            {(item.capture.sensor_id, item.capture.modality) for item in result.observations},
            {("front", "rgb"), ("front", "depth")},
        )
        self.assertEqual({item.capture.commit_id for item in result.observations}, {1})

    def test_multiple_streams_reject_duplicate_unknown_and_old_run(self):
        renderer = _ScheduledRenderer()
        sim = Coordinator(
            _StubPhysics(), renderer,
            capture_streams=(("front", "rgb"), ("front", "depth")),
        )
        first = sim.step().snapshot.commit
        renderer.ready = [Capture(first.run_id, 1, 1, b"rgb", "front", "rgb")]
        sim.step()
        renderer.ready = [Capture(first.run_id, 1, 2, b"again", "front", "rgb")]
        with self.assertRaisesRegex(ValueError, "duplicate capture stream"):
            sim.step()
        renderer.ready = [Capture(first.run_id, 1, 3, b"unknown", "rear", "rgb")]
        with self.assertRaisesRegex(ValueError, "unknown or duplicate capture stream"):
            sim.step()
        sim.reset()
        renderer.ready = [Capture(first.run_id, 1, 4, b"stale", "front", "depth")]
        with self.assertRaisesRegex(ValueError, "different run"):
            sim.step()

    def test_free_fall_and_replay(self):
        sim = self.make_coordinator()
        results = [sim.step() for _ in range(10)]
        height = results[-1].snapshot.poses["ball"].position_m[2]
        self.assertAlmostEqual(height, 1 - 0.5 * 9.81 * 0.1**2, delta=0.001)
        self.assertEqual(results[-1].snapshot.commit.sim_time_ns, 100_000_000)
        self.assertEqual(results[-1].snapshot.commit.physics_step, 100)
        sim.reset()
        replay = [sim.step() for _ in range(10)]
        self.assertEqual(
            [result.snapshot.poses for result in results],
            [result.snapshot.poses for result in replay],
        )

    def test_invalid_control_does_not_advance(self):
        sim = self.make_coordinator()
        with self.assertRaises(ValueError):
            sim.step([1.0])
        result = sim.step()
        self.assertEqual(result.snapshot.commit.sim_tick, 1)
        self.assertEqual(result.snapshot.commit.physics_step, 10)

    def test_projectile_ring_and_duplicate_hit(self):
        self.assertEqual(calculate_ring((0.0, 0.0, 0.0), 0.05), (0.0, 10))
        self.assertEqual(calculate_ring((0.005, 0.0, 0.0), 0.05), (0.005, 9))
        self.assertIsNone(calculate_ring((0.051, 0.0, 0.0), 0.05))
        projectile = ProjectileSubsystem({"armor-0": TargetGeometry("armor-0", 0.05)})
        report = HitReport("run", "shot-0", "armor-0", 3, 8, (0.0, 0.0, 0.0))
        event = projectile.handle_hit(report)
        self.assertIsNotNone(event)
        self.assertEqual(event.ring, 10)
        self.assertIsNone(projectile.handle_hit(report))

    def test_projectile_rejects_unknown_or_invalid_target(self):
        projectile = ProjectileSubsystem({"armor-0": TargetGeometry("armor-0", 0.05)})
        unknown = HitReport("run", "shot-0", "missing", 1, 1, (0.0, 0.0, 0.0))
        invalid = HitReport("run", "shot-1", "armor-0", 1, 1, (float("nan"), 0.0, 0.0))
        self.assertIsNone(projectile.handle_hit(unknown))
        self.assertIsNone(projectile.handle_hit(invalid))

    def test_versioned_bridge_envelope_handles_fragmentation_and_batching(self):
        wire = encode_envelope("frame_commit", {"commit_id": 4, "sim_tick": 4})
        first = decode_envelope(wire[:3])
        self.assertIsNone(first)
        decoded, remainder = decode_envelope(wire + encode_envelope("reset", {"run_id": "x"}))
        self.assertEqual(decoded["kind"], "frame_commit")
        self.assertEqual(decoded["payload"]["commit_id"], 4)
        decoded_reset, remainder = decode_envelope(remainder)
        self.assertEqual(decoded_reset["kind"], "reset")
        self.assertEqual(remainder, b"")

    def test_envelope_stream_decodes_arbitrary_socket_chunks(self):
        wire = encode_envelope("a", {"value": 1}) + encode_envelope("b", {"value": 2})
        stream = EnvelopeStream()
        messages = []
        for offset in range(0, len(wire), 3):
            messages.extend(stream.feed(wire[offset:offset + 3]))
        self.assertEqual([message["kind"] for message in messages], ["a", "b"])
        self.assertEqual(stream.pending_bytes, 0)

    def test_unix_envelope_channel_transfers_versioned_messages(self):
        wire = encode_envelope("reset", {"run_id": "next"})
        sender_socket = _FakeSocket()
        sender = UnixEnvelopeChannel(sender_socket)
        sender.send("reset", {"run_id": "next"})
        self.assertEqual(sender_socket.sent, wire)

        receiver_socket = _FakeSocket([wire[:4], wire[4:]])
        receiver = UnixEnvelopeChannel(receiver_socket, recv_bytes=4)
        self.assertEqual(receiver.receive(), [])
        self.assertEqual(receiver.receive(), [{
            "version": 1,
            "kind": "reset",
            "payload": {"run_id": "next"},
        }])

    def test_unix_envelope_server_owns_and_removes_socket_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.sock")
            try:
                server = UnixEnvelopeServer(path)
            except PermissionError as exc:
                self.skipTest(f"Unix socket bind is unavailable: {exc}")
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.connect(path)
            channel = server.accept()
            client_socket.sendall(encode_envelope("ping", {"value": 1}))
            self.assertEqual(channel.receive()[0]["kind"], "ping")
            channel.close()
            client_socket.close()
            server.close()
            self.assertFalse(Path(path).exists())

    def test_frame_commit_wire_message_preserves_ros_time(self):
        commit = FrameCommit("run", 4, 4, 2_000_000_007, 40, 4)
        self.assertEqual(sim_time_parts(commit.sim_time_ns), (2, 7))
        wire = encode_frame_commit(commit)
        decoded = decode_frame_commit(wire[:5])
        self.assertIsNone(decoded)
        decoded, remainder = decode_frame_commit(wire)
        self.assertEqual(decoded, commit)
        self.assertEqual(remainder, b"")

    def test_typed_bridge_publisher_emits_snapshot_and_reset_envelopes(self):
        fake_socket = _FakeSocket()
        publisher = UnixBridgePublisher(UnixEnvelopeChannel(fake_socket))
        snapshot = _snapshot(3)
        publisher.publish_frame(snapshot)
        publisher.publish_reset("next")
        first, remainder = decode_envelope(fake_socket.sent)
        second, remainder = decode_envelope(remainder)
        self.assertEqual(first["kind"], "snapshot")
        self.assertEqual(first["payload"]["commit"]["commit_id"], 3)
        self.assertEqual(second["kind"], "reset")
        self.assertEqual(second["payload"], {"run_id": "next"})
        self.assertEqual(remainder, b"")

    def test_frame_commit_rejects_inconsistent_or_invalid_time(self):
        commit = FrameCommit("run", 1, 1, 1, 1, 1)
        payload = encode_frame_commit(commit)
        envelope, _ = decode_envelope(payload)
        envelope["payload"]["sim_time"]["nanosec"] = 2
        with self.assertRaises(ValueError):
            from bridge.messages import parse_frame_commit_payload
            parse_frame_commit_payload(envelope["payload"])

    def test_standard_message_payloads_share_commit_time_and_ros_quaternion_order(self):
        commit = FrameCommit("run", 2, 2, 1_000_000_003, 2, 2)
        tf = tf_payload(commit, {"base": _pose()})
        self.assertEqual(tf["transforms"][0]["header"]["stamp"], {"sec": 1, "nanosec": 3})
        self.assertEqual(tf["transforms"][0]["transform"]["rotation"], {"x": 0.2, "y": 0.3, "z": 0.4, "w": 0.9})
        imu = imu_payload(commit, "imu", (0.9, 0.1, 0.2, 0.3), (1, 2, 3), (4, 5, 6))
        self.assertEqual(imu["header"]["stamp"], {"sec": 1, "nanosec": 3})
        self.assertEqual(clock_payload(commit)["clock"], {"sec": 1, "nanosec": 3})
        joints = joint_state_payload(commit, ["yaw"], [0.5])
        self.assertEqual(joints["header"]["stamp"], {"sec": 1, "nanosec": 3})

    def test_snapshot_wire_message_excludes_images_and_keeps_state(self):
        snapshot = _snapshot(3)
        wire = encode_snapshot(snapshot)
        envelope, remainder = decode_envelope(wire)
        self.assertEqual(envelope["kind"], "snapshot")
        self.assertEqual(envelope["payload"]["commit"]["commit_id"], 3)
        self.assertEqual(envelope["payload"]["poses"]["base"]["position_m"], [1.0, 2.0, 3.0])
        self.assertEqual(envelope["payload"]["sensors"], {})
        self.assertEqual(remainder, b"")

    def test_standard_message_payloads_reject_bad_shapes(self):
        commit = FrameCommit("run", 1, 1, 0, 0, 1)
        with self.assertRaises(ValueError):
            imu_payload(commit, "imu", (1, 0, 0, 0), (1, 2), (0, 0, 0))
        with self.assertRaises(ValueError):
            joint_state_payload(commit, ["a"], [0], [1, 2])

    def test_spear_double_buffer_pairs_after_explicit_prime(self):
        instance = _FakeInstance()
        component = _FakeCaptureComponent()
        port = SpearRenderPort(
            instance=instance,
            capture_component=component,
            apply_snapshot=lambda snapshot: None,
            set_game_paused=lambda paused: None,
            readback_metadata=_metadata_from_bundle,
            metadata_enqueue_copy=lambda snapshot: component.enqueue_copy(
                None if snapshot is None else snapshot.commit
            ),
            metadata_reset=lambda: None,
            buffering_mode="DoubleBuffered",
        )
        port.prime()
        first = _snapshot(1)
        second = _snapshot(2)
        self.assertEqual(port.submit(first), [])
        observations = port.submit(second)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].commit_id, 1)
        self.assertEqual(observations[0].image, b"image-2")
        with self.assertRaisesRegex(RuntimeError, "capture_reset"):
            port.reset()
        self.assertEqual(observations[0].ue_frame_id, 2)

    def test_spear_single_buffer_requires_and_uses_frame_binding(self):
        instance = _FakeInstance()
        component = _FakeSingleCaptureComponent()
        port = SpearRenderPort(
            instance=instance,
            capture_component=component,
            apply_snapshot=lambda snapshot: None,
            set_game_paused=lambda paused: None,
            readback_metadata=_metadata_from_bundle,
        )
        with self.assertRaises(RuntimeError):
            port.submit(_snapshot(1))

        port = SpearRenderPort(
            instance=instance,
            capture_component=component,
            apply_snapshot=lambda snapshot: None,
            set_game_paused=lambda paused: None,
            readback_metadata=_metadata_from_bundle,
            bind_frame_metadata=component.bind,
        )
        captures = port.submit(_snapshot(1))
        self.assertEqual(captures[0].commit_id, 1)
        self.assertEqual(captures[0].ue_frame_id, 1)

    def test_spear_rejects_readback_from_another_sensor(self):
        instance = _FakeInstance()
        component = _FakeSingleCaptureComponent()
        component.sensor_id = "depth"
        port = SpearRenderPort(
            instance=instance,
            capture_component=component,
            apply_snapshot=lambda snapshot: None,
            set_game_paused=lambda paused: None,
            readback_metadata=_metadata_from_bundle,
            bind_frame_metadata=component.bind,
            sensor_id="rgb",
            modality="rgb",
        )
        with self.assertRaises(ValueError):
            port.submit(_snapshot(1))

    def test_spear_metadata_callbacks_drain_priming_before_business_frame(self):
        component = _FakeMetadataComponent()
        callbacks = SpearMetadataCallbacks(component, "front", "rgb")
        snapshot = _snapshot(7)
        callbacks.enqueue_copy_metadata(None)
        callbacks.enqueue_copy_metadata(snapshot)
        self.assertIsNone(callbacks.readback_metadata(None))
        metadata = callbacks.readback_metadata(None)
        self.assertEqual(metadata.run_id, "run")
        self.assertEqual(metadata.commit_id, 7)
        self.assertEqual(metadata.sensor_id, "front")
        self.assertEqual(metadata.modality, "rgb")

    def test_coordinator_primes_buffered_renderer_after_reset(self):
        instance = _FakeInstance()
        component = _FakeCaptureComponent()
        port = SpearRenderPort(
            instance=instance,
            capture_component=component,
            apply_snapshot=lambda snapshot: None,
            set_game_paused=lambda paused: None,
            readback_metadata=_metadata_from_bundle,
            metadata_enqueue_copy=lambda snapshot: component.enqueue_copy(
                None if snapshot is None else snapshot.commit
            ),
            buffering_mode="DoubleBuffered",
            capture_reset=component._queued.clear,
        )
        coordinator = Coordinator(_StubPhysics(), port)
        first = coordinator.step()
        self.assertEqual(first.observations, ())
        second = coordinator.step()
        self.assertEqual(second.observations[0].capture.commit_id, 1)
        coordinator.reset()
        after_reset = coordinator.step()
        self.assertEqual(after_reset.observations, ())

    def test_spear_reset_calls_capture_reset_before_repriming(self):
        instance = _FakeInstance()
        component = _FakeCaptureComponent()
        reset_calls = []
        port = SpearRenderPort(
            instance=instance,
            capture_component=component,
            apply_snapshot=lambda snapshot: None,
            set_game_paused=lambda paused: None,
            readback_metadata=_metadata_from_bundle,
            metadata_enqueue_copy=lambda snapshot: component.enqueue_copy(
                None if snapshot is None else snapshot.commit
            ),
            buffering_mode="DoubleBuffered",
            capture_reset=lambda: (reset_calls.append(True), component._queued.clear()),
        )
        port.prime()
        self.assertEqual(port.submit(_snapshot(1)), [])
        port.reset()
        self.assertEqual(reset_calls, [True])
        port.prime()
        self.assertEqual(port.submit(_snapshot(1)), [])
        capture = port.submit(_snapshot(2))[0]
        self.assertEqual(capture.commit_id, 1)

    def test_bridge_rejects_negative_unsigned_fields(self):
        commit = FrameCommit("run", 1, 1, -1, 0, 1)
        with self.assertRaises(ValueError):
            encode_frame_commit(commit)

    def test_spear_triple_buffer_has_two_commit_delay(self):
        instance = _FakeInstance()
        component = _FakeCaptureComponent()
        port = SpearRenderPort(
            instance=instance,
            capture_component=component,
            apply_snapshot=lambda snapshot: None,
            set_game_paused=lambda paused: None,
            readback_metadata=_metadata_from_bundle,
            metadata_enqueue_copy=lambda snapshot: component.enqueue_copy(
                None if snapshot is None else snapshot.commit
            ),
            buffering_mode="TripleBuffered",
        )
        port.prime()
        self.assertEqual(port.submit(_snapshot(1)), [])
        self.assertEqual(port.submit(_snapshot(2)), [])
        captures = port.submit(_snapshot(3))
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0].commit_id, 1)
        self.assertEqual(captures[0].image, b"image-3")


def _snapshot(commit_id: int) -> Snapshot:
    commit = FrameCommit("run", commit_id, commit_id, commit_id * 1_000_000, commit_id, commit_id)
    return Snapshot(commit, {"base": _pose()}, {})


def _metadata_from_bundle(bundle):
    if bundle["metadata"] is None:
        return None
    return ReadbackMetadata(**bundle["metadata"])


def _pose():
    from rm_sim_spear.model import Pose
    return Pose((1.0, 2.0, 3.0), (0.9, 0.2, 0.3, 0.4))


class _FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakeSocket:
    def __init__(self, chunks=()):
        self.sent = b""
        self._chunks = list(chunks)

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        pass


class _ScheduledRenderer:
    def __init__(self):
        self.ready = []

    def reset(self):
        self.ready = []

    def submit(self, snapshot):
        ready, self.ready = self.ready, []
        return ready


class _FakeInstance:
    def begin_frame(self):
        return _FakeContext()

    def end_frame(self, single_step=False):
        return _FakeContext()


class _FakeCaptureComponent:
    def __init__(self):
        self._next = 0
        self._queued: list[tuple[int, object | None]] = []

    def enqueue_copy(self, commit=None):
        self._next += 1
        self._queued.append((self._next, commit))

    def read_pixels(self):
        if len(self._queued) <= 1:
            return None
        image_id, commit = self._queued.pop(0)
        metadata = {
            "run_id": commit.run_id,
            "commit_id": commit.commit_id,
            "ue_frame_id": image_id,
        } if commit is not None else None
        return {
            "arrays": {"data": f"image-{image_id}".encode()},
            "metadata": metadata,
        }


class _FakeSingleCaptureComponent:
    def __init__(self):
        self._commit = None
        self._ue_frame_id = 0
        self.sensor_id = "default"

    def bind(self, snapshot):
        self._commit = snapshot.commit

    def read_pixels(self):
        self._ue_frame_id += 1
        return {
            "arrays": {"data": f"image-{self._ue_frame_id}".encode()},
            "metadata": {
                "run_id": self._commit.run_id,
                "commit_id": self._commit.commit_id,
                "ue_frame_id": self._ue_frame_id,
                "sensor_id": self.sensor_id,
                "modality": "rgb",
            },
        }


class _FakeMetadataComponent:
    def __init__(self):
        self._next_ue_frame_id = 100
        self._queue = []

    def bind_frame_metadata(self, info=""):
        self._queue.append((info, self._next_ue_frame_id))
        self._next_ue_frame_id += 1

    def enqueue_copy_metadata(self, info=""):
        self._queue.append((info, self._next_ue_frame_id))
        self._next_ue_frame_id += 1

    def readback_metadata(self):
        info, ue_frame_id = self._queue.pop(0)
        if not info:
            return {"info": ""}
        metadata = json.loads(info)
        metadata["ue_frame_id"] = ue_frame_id
        return {"info": json.dumps(metadata)}


class _StubPhysics:
    def __init__(self):
        self.physics_step = 0
        self.sim_time_ns = 0

    def reset(self):
        self.physics_step = 0
        self.sim_time_ns = 0

    def step(self, control=()):
        self.physics_step += 1
        self.sim_time_ns += 1_000_000

    def read_poses(self):
        return {}

    def read_sensors(self):
        return {}


if __name__ == "__main__":
    unittest.main()
