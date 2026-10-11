"""Versioned local messages exchanged with the ROS2 adapter.

This module deliberately has no ROS import.  The core process can therefore
serialize commit metadata in the SPEAR/MuJoCo Python 3.11 environment, while a
Humble adapter turns the payload into generated ROS messages in its own process.
"""

from typing import Any

from rm_sim_spear.model import FrameCommit, Snapshot

from .codec import decode_envelope, encode_envelope


_KIND = "frame_commit"
_SNAPSHOT_KIND = "snapshot"


def sim_time_parts(sim_time_ns: int) -> tuple[int, int]:
    """Convert non-negative nanoseconds to ROS ``Time`` fields."""
    _require_uint(sim_time_ns, "sim_time_ns")
    return divmod(sim_time_ns, 1_000_000_000)


def frame_commit_payload(commit: FrameCommit) -> dict[str, Any]:
    """Create the wire payload for ``sim_msgs/msg/FrameCommit``."""
    if not isinstance(commit, FrameCommit):
        raise TypeError("commit must be a FrameCommit")
    _require_nonempty_string(commit.run_id, "run_id")
    _require_uint(commit.commit_id, "commit_id", minimum=1)
    _require_uint(commit.sim_tick, "sim_tick", minimum=1)
    _require_uint(commit.sim_time_ns, "sim_time_ns")
    _require_uint(commit.physics_step, "physics_step")
    _require_uint(commit.state_revision, "state_revision")
    sec, nanosec = sim_time_parts(commit.sim_time_ns)
    return {
        "run_id": commit.run_id,
        "commit_id": commit.commit_id,
        "sim_tick": commit.sim_tick,
        "sim_time": {"sec": sec, "nanosec": nanosec},
        "sim_time_ns": commit.sim_time_ns,
        "physics_step": commit.physics_step,
        "state_revision": commit.state_revision,
    }


def encode_frame_commit(commit: FrameCommit) -> bytes:
    return encode_envelope(_KIND, frame_commit_payload(commit))


def snapshot_payload(snapshot: Snapshot) -> dict[str, Any]:
    """Serialize state metadata for the local adapter boundary.

    Images are intentionally excluded; they remain owned by SPEAR shared
    memory. This payload contains only bounded control/telemetry data.
    """
    if not isinstance(snapshot, Snapshot):
        raise TypeError("snapshot must be a Snapshot")
    poses: dict[str, Any] = {}
    for name, pose in snapshot.poses.items():
        if not isinstance(name, str) or not name:
            raise ValueError("pose names must be non-empty strings")
        _validate_numbers(pose.position_m, 3, f"poses[{name}].position_m")
        _validate_numbers(pose.quaternion_wxyz, 4, f"poses[{name}].quaternion_wxyz")
        poses[name] = {
            "position_m": list(pose.position_m),
            "quaternion_wxyz": list(pose.quaternion_wxyz),
        }
    sensors: dict[str, list[float]] = {}
    for name, values in snapshot.sensors.items():
        if not isinstance(name, str) or not name:
            raise ValueError("sensor names must be non-empty strings")
        _validate_numbers(values, len(values), f"sensors[{name}]")
        sensors[name] = list(values)
    return {
        "commit": frame_commit_payload(snapshot.commit),
        "poses": poses,
        "sensors": sensors,
    }


def encode_snapshot(snapshot: Snapshot) -> bytes:
    return encode_envelope(_SNAPSHOT_KIND, snapshot_payload(snapshot))


def parse_frame_commit_payload(payload: dict[str, Any]) -> FrameCommit:
    """Validate and decode one adapter payload.

    ``sim_time_ns`` is accepted as a convenience for local peers, but the ROS
    message's canonical representation is the ``sim_time`` pair. If both are
    present they must describe the same instant.
    """
    if not isinstance(payload, dict):
        raise ValueError("frame_commit payload must be an object")
    run_id = payload.get("run_id")
    commit_id = payload.get("commit_id")
    sim_tick = payload.get("sim_tick")
    physics_step = payload.get("physics_step")
    state_revision = payload.get("state_revision")
    _require_nonempty_string(run_id, "run_id")
    _require_uint(commit_id, "commit_id", minimum=1)
    _require_uint(sim_tick, "sim_tick", minimum=1)
    _require_uint(physics_step, "physics_step")
    _require_uint(state_revision, "state_revision")

    sim_time = payload.get("sim_time")
    if not isinstance(sim_time, dict):
        raise ValueError("sim_time must be an object")
    sec = sim_time.get("sec")
    nanosec = sim_time.get("nanosec")
    if not isinstance(sec, int) or isinstance(sec, bool) or sec < 0:
        raise ValueError("sim_time.sec must be a non-negative integer")
    if not isinstance(nanosec, int) or isinstance(nanosec, bool) or not 0 <= nanosec < 1_000_000_000:
        raise ValueError("sim_time.nanosec must be in [0, 1e9)")
    sim_time_ns = sec * 1_000_000_000 + nanosec
    if "sim_time_ns" in payload:
        _require_uint(payload["sim_time_ns"], "sim_time_ns")
        if payload["sim_time_ns"] != sim_time_ns:
            raise ValueError("sim_time and sim_time_ns disagree")
    return FrameCommit(
        run_id=run_id,
        commit_id=commit_id,
        sim_tick=sim_tick,
        sim_time_ns=sim_time_ns,
        physics_step=physics_step,
        state_revision=state_revision,
    )


def decode_frame_commit(buffer: bytes) -> tuple[FrameCommit, bytes] | None:
    decoded = decode_envelope(buffer)
    if decoded is None:
        return None
    envelope, remainder = decoded
    if envelope["kind"] != _KIND:
        raise ValueError(f"expected {_KIND!r}, got {envelope['kind']!r}")
    return parse_frame_commit_payload(envelope["payload"]), remainder


def _require_nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _require_uint(value: Any, field: str, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        if minimum:
            raise ValueError(f"{field} must be an integer >= {minimum}")
        raise ValueError(f"{field} must be a non-negative integer")


def _validate_numbers(values: Any, expected: int, field: str) -> None:
    import math

    try:
        values = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{field} must be a sequence") from exc
    if len(values) != expected:
        raise ValueError(f"{field} must contain {expected} values")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in values):
        raise ValueError(f"{field} must contain finite numbers")
