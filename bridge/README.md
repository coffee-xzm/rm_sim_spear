# ROS2 Adapter Boundary

`bridge` is an independent process boundary. The simulation core does not import
ROS2 or `rclpy`; an adapter running with Ubuntu 22.04 ROS2 Humble translates the
versioned local protocol into standard ROS2 messages.

The local transport is a length-prefixed envelope:

```text
uint32 big-endian JSON byte length
JSON {"version": 1, "kind": "...", "payload": {...}}
```

Socket readers should feed received chunks to `bridge.codec.EnvelopeStream`;
it retains incomplete bytes and emits every complete envelope in order.
`bridge.transport.UnixEnvelopeChannel` wraps this pattern for a Unix-domain
socket and uses `sendall`, so the core does not assume one write equals one
message.
The simulation side can own an endpoint with `UnixEnvelopeServer(path)`, accept
the adapter connection, and close it to remove the socket path during shutdown.
`UnixBridgePublisher` provides typed `publish_commit()`, `publish_snapshot()`
and `publish_reset()` calls for the accepted channel.

`bridge.standard_messages` produces ROS-shaped dictionaries for `Clock`,
`TFMessage`, `Imu`, and `JointState`. The adapter converts these dictionaries
to generated ROS classes; all message headers use the `FrameCommit` simulation
time.

The core may send a `snapshot` envelope containing poses and scalar sensor
arrays. Image bytes are excluded from this envelope and stay in SPEAR shared
memory.

The adapter publishes:

- `/clock`: `rosgraph_msgs/msg/Clock`
- `/tf`: `tf2_msgs/msg/TFMessage`
- `/imu/data`: `sensor_msgs/msg/Imu` when an IMU sensor is configured
- `/joint_states`: `sensor_msgs/msg/JointState` when a joint sensor is configured
- `/sim/commit`: `sim_msgs/msg/FrameCommit` from `bridge/idl/sim_msgs`

Use ROS2 standard QoS profiles: `ClockQoS`, `SensorDataQoS`, the standard dynamic
and static TF profiles, and reliable keep-last depth 10 for control commands.
Static TF, odometry and a `sensor_msgs/msg/Image` compatibility publisher need
explicit sensor mappings and are outside this first adapter slice. Images
remain in SPEAR shared memory by default.

`FrameCommit.sim_time` is derived from `mujoco.data.time`; the adapter must not
replace it with wall-clock time. Every standard sensor message published for a
commit uses the same `sim_time` in its header. `run_id` changes on reset, so a
consumer can reject stale samples even when `commit_id` starts again at one.

The core serializes this contract with `bridge.messages.encode_frame_commit()`.
For strict SPEAR captures, `bind_frame_metadata(snapshot)` is required for
single-buffered business frames, while `metadata_enqueue_copy(snapshot)` is
required for buffered business frames. The existing no-argument SPEAR
`enqueue_copy()` has no place to record source identity and is only valid for
priming frames.

Each image stream also carries `sensor_id` and `modality` at the SPEAR boundary;
the adapter must preserve them when publishing camera metadata or compatibility
`sensor_msgs/Image` messages.

## Humble adapter

`bridge.ros2_adapter` is a separate process entry point. It imports `rclpy`
only when started, connects to the core's Unix socket, and publishes `/clock`,
`/sim/commit` and `/tf`. IMU and joint state publication is enabled by naming
the corresponding snapshot sensor fields:

```bash
source /opt/ros/humble/setup.bash
PYTHONPATH=/home/coffee/rm_sim_spear \
  /usr/bin/python3 -m bridge.ros2_adapter /tmp/rm_sim_spear.sock \
  --imu-sensor imu --joint-sensor joint_positions \
  --joint-name yaw --joint-name pitch
```

The adapter uses the simulation timestamp from each `FrameCommit`; it never
substitutes wall time. `--joint-name` may be omitted to generate stable
`joint_0`, `joint_1`, ... names from the configured sensor vector.
The adapter intentionally runs with the ROS2 system Python (Humble is Python
3.10); the core package remains in the SPEAR Python environment (Python 3.11).
