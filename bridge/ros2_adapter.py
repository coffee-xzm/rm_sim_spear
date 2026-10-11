"""Independent ROS2 adapter for the versioned local simulation protocol.

The module is importable without ROS2.  ``build_node`` and ``main`` perform
the ROS imports only when the adapter process is started, which keeps the
MuJoCo/SPEAR process independent from the system ROS Python version.
"""

from __future__ import annotations

import argparse
import socket
from dataclasses import dataclass
from typing import Any, Sequence

from .codec import EnvelopeStream
from .messages import parse_frame_commit_payload


@dataclass(frozen=True)
class AdapterOptions:
    """Topic mapping for the standard ROS2 adapter."""

    socket_path: str
    node_name: str = "rm_sim_spear_adapter"
    namespace: str = ""
    tf_parent_frame: str = "world"
    imu_sensor: str | None = None
    joint_sensor: str | None = None
    joint_names: tuple[str, ...] = ()
    poll_period_s: float = 0.002

    def __post_init__(self) -> None:
        if not self.socket_path:
            raise ValueError("socket_path must be non-empty")
        if not self.node_name:
            raise ValueError("node_name must be non-empty")
        if not self.tf_parent_frame:
            raise ValueError("tf_parent_frame must be non-empty")
        if self.poll_period_s <= 0:
            raise ValueError("poll_period_s must be positive")
        if self.joint_sensor and self.joint_names and not all(self.joint_names):
            raise ValueError("joint_names must be non-empty")


def _stamp_parts(commit_payload: dict[str, Any]) -> tuple[int, int]:
    commit = parse_frame_commit_payload(commit_payload)
    return divmod(commit.sim_time_ns, 1_000_000_000)


def _set_stamp(stamp: Any, parts: tuple[int, int]) -> None:
    stamp.sec, stamp.nanosec = parts


def _require_ros2() -> dict[str, Any]:
    """Load ROS2 classes only inside the adapter process."""
    try:
        import rclpy
        from geometry_msgs.msg import TransformStamped
        from rclpy.node import Node
        from rclpy.qos import (
            DurabilityPolicy,
            QoSProfile,
            ReliabilityPolicy,
            qos_profile_sensor_data,
        )
        from rosgraph_msgs.msg import Clock
        from sensor_msgs.msg import Imu, JointState
        from sim_msgs.msg import FrameCommit
        from tf2_msgs.msg import TFMessage
    except ImportError as exc:
        raise RuntimeError(
            "ROS2 Humble packages are required only for bridge.ros2_adapter; "
            "source the ROS2 environment before starting the adapter"
        ) from exc
    return locals()


def build_node(options: AdapterOptions) -> Any:
    """Build a ROS2 node; importing this module itself does not need ROS2."""
    ros = _require_ros2()
    Node = ros["Node"]
    QoSProfile = ros["QoSProfile"]
    qos_profile_sensor_data = ros["qos_profile_sensor_data"]
    DurabilityPolicy = ros["DurabilityPolicy"]
    ReliabilityPolicy = ros["ReliabilityPolicy"]
    Clock = ros["Clock"]
    FrameCommit = ros["FrameCommit"]
    Imu = ros["Imu"]
    JointState = ros["JointState"]
    TFMessage = ros["TFMessage"]
    TransformStamped = ros["TransformStamped"]

    class AdapterNode(Node):
        def __init__(self) -> None:
            super().__init__(options.node_name, namespace=options.namespace)
            # Match rclcpp::ClockQoS: one best-effort sample, volatile.
            clock_qos = QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.VOLATILE,
            )
            command_qos = QoSProfile(
                depth=10,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            )
            self._clock_pub = self.create_publisher(Clock, "/clock", clock_qos)
            self._commit_pub = self.create_publisher(FrameCommit, "/sim/commit", command_qos)
            tf_qos = QoSProfile(
                depth=100,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            )
            self._tf_pub = self.create_publisher(TFMessage, "/tf", tf_qos)
            self._imu_pub = self.create_publisher(Imu, "/imu/data", qos_profile_sensor_data)
            self._joint_pub = self.create_publisher(JointState, "/joint_states", qos_profile_sensor_data)
            self._stream = EnvelopeStream()
            self._socket: socket.socket | None = None
            self._last_commit: tuple[str, int] | None = None
            self._timer = self.create_timer(options.poll_period_s, self._poll)

        def _connect(self) -> bool:
            if self._socket is not None:
                return True
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                sock.connect(options.socket_path)
            except (BlockingIOError, FileNotFoundError, ConnectionRefusedError):
                sock.close()
                return False
            self._socket = sock
            self.get_logger().info("connected to simulation bridge")
            return True

        def _poll(self) -> None:
            if not self._connect():
                return
            try:
                chunk = self._socket.recv(256 * 1024)  # type: ignore[union-attr]
            except BlockingIOError:
                return
            except OSError as exc:
                self.get_logger().warning(f"bridge socket closed: {exc}")
                self._close_socket()
                return
            if not chunk:
                self._close_socket()
                return
            try:
                envelopes = self._stream.feed(chunk)
                for envelope in envelopes:
                    self._dispatch(envelope)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                self.get_logger().error(f"invalid bridge envelope: {exc}")

        def _dispatch(self, envelope: dict[str, Any]) -> None:
            kind = envelope.get("kind")
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("bridge payload must be an object")
            if kind == "frame_commit":
                self._publish_commit(payload)
            elif kind == "snapshot":
                self._publish_snapshot(payload)
            elif kind == "reset":
                self._last_commit = None
            else:
                self.get_logger().warning(f"ignoring unknown bridge message kind {kind!r}")

        def _publish_commit(self, payload: dict[str, Any]) -> None:
            commit = parse_frame_commit_payload(payload)
            key = (commit.run_id, commit.commit_id)
            if key == self._last_commit:
                return
            self._last_commit = key
            msg = FrameCommit()
            msg.run_id = commit.run_id
            msg.commit_id = commit.commit_id
            msg.sim_tick = commit.sim_tick
            _set_stamp(msg.sim_time, (commit.sim_time_ns // 1_000_000_000, commit.sim_time_ns % 1_000_000_000))
            msg.physics_step = commit.physics_step
            msg.state_revision = commit.state_revision
            self._commit_pub.publish(msg)
            clock = Clock()
            _set_stamp(clock.clock, (commit.sim_time_ns // 1_000_000_000, commit.sim_time_ns % 1_000_000_000))
            self._clock_pub.publish(clock)

        def _publish_snapshot(self, payload: dict[str, Any]) -> None:
            commit_payload = payload.get("commit")
            if not isinstance(commit_payload, dict):
                raise ValueError("snapshot.commit must be an object")
            self._publish_commit(commit_payload)
            poses = payload.get("poses", {})
            if not isinstance(poses, dict):
                raise ValueError("snapshot.poses must be an object")
            stamp = _stamp_parts(commit_payload)
            tf = TFMessage()
            for child_frame, pose in poses.items():
                if not isinstance(child_frame, str) or not child_frame or not isinstance(pose, dict):
                    raise ValueError("snapshot pose has invalid shape")
                position = pose.get("position_m")
                quaternion = pose.get("quaternion_wxyz")
                if len(position) != 3 or len(quaternion) != 4:
                    raise ValueError("snapshot pose has invalid dimensions")
                transform = TransformStamped()
                _set_stamp(transform.header.stamp, stamp)
                transform.header.frame_id = options.tf_parent_frame
                transform.child_frame_id = child_frame
                transform.transform.translation.x = float(position[0])
                transform.transform.translation.y = float(position[1])
                transform.transform.translation.z = float(position[2])
                transform.transform.rotation.w = float(quaternion[0])
                transform.transform.rotation.x = float(quaternion[1])
                transform.transform.rotation.y = float(quaternion[2])
                transform.transform.rotation.z = float(quaternion[3])
                tf.transforms.append(transform)
            if tf.transforms:
                self._tf_pub.publish(tf)
            sensors = payload.get("sensors", {})
            if not isinstance(sensors, dict):
                raise ValueError("snapshot.sensors must be an object")
            if options.imu_sensor and options.imu_sensor in sensors:
                values = sensors[options.imu_sensor]
                if len(values) != 10:
                    raise ValueError("configured imu sensor must contain 10 values")
                imu = Imu()
                _set_stamp(imu.header.stamp, stamp)
                imu.header.frame_id = options.imu_sensor
                imu.orientation.w, imu.orientation.x, imu.orientation.y, imu.orientation.z = map(float, values[:4])
                imu.angular_velocity.x, imu.angular_velocity.y, imu.angular_velocity.z = map(float, values[4:7])
                imu.linear_acceleration.x, imu.linear_acceleration.y, imu.linear_acceleration.z = map(float, values[7:10])
                self._imu_pub.publish(imu)
            if options.joint_sensor and options.joint_sensor in sensors:
                values = sensors[options.joint_sensor]
                names = options.joint_names or tuple(f"joint_{i}" for i in range(len(values)))
                if len(names) != len(values):
                    raise ValueError("joint_names must match configured joint sensor length")
                joints = JointState()
                _set_stamp(joints.header.stamp, stamp)
                joints.name = list(names)
                joints.position = [float(value) for value in values]
                self._joint_pub.publish(joints)

        def _close_socket(self) -> None:
            if self._socket is not None:
                self._socket.close()
                self._socket = None

        def destroy_node(self) -> bool:
            self._close_socket()
            return super().destroy_node()

    return AdapterNode()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rm_sim_spear ROS2 adapter")
    parser.add_argument("socket_path")
    parser.add_argument("--node-name", default="rm_sim_spear_adapter")
    parser.add_argument("--namespace", default="")
    parser.add_argument("--tf-parent-frame", default="world")
    parser.add_argument("--imu-sensor")
    parser.add_argument("--joint-sensor")
    parser.add_argument("--joint-name", action="append", default=[])
    args = parser.parse_args(argv)
    options = AdapterOptions(
        socket_path=args.socket_path,
        node_name=args.node_name,
        namespace=args.namespace,
        tf_parent_frame=args.tf_parent_frame,
        imu_sensor=args.imu_sensor,
        joint_sensor=args.joint_sensor,
        joint_names=tuple(args.joint_name),
    )
    try:
        ros = _require_ros2()
    except RuntimeError as exc:
        parser.error(str(exc))
    rclpy = ros["rclpy"]
    rclpy.init(args=None)
    node = build_node(options)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
