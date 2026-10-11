"""ROS standard-message-shaped payloads without importing ROS."""

import math
from collections.abc import Iterable, Sequence
from typing import Any

from rm_sim_spear.model import FrameCommit, Pose

from .messages import sim_time_parts


def clock_payload(commit: FrameCommit) -> dict[str, Any]:
    sec, nanosec = sim_time_parts(commit.sim_time_ns)
    return {"clock": {"sec": sec, "nanosec": nanosec}}


def tf_payload(
    commit: FrameCommit,
    poses: dict[str, Pose],
    parent_frame: str = "world",
) -> dict[str, Any]:
    if not parent_frame:
        raise ValueError("parent_frame must be non-empty")
    stamp = _stamp(commit)
    transforms = []
    for child_frame, pose in poses.items():
        if not child_frame:
            raise ValueError("child frame names must be non-empty")
        _validate_pose(pose)
        w, x, y, z = pose.quaternion_wxyz
        transforms.append({
            "header": {"stamp": stamp, "frame_id": parent_frame},
            "child_frame_id": child_frame,
            "transform": {
                "translation": {
                    "x": pose.position_m[0],
                    "y": pose.position_m[1],
                    "z": pose.position_m[2],
                },
                "rotation": {"x": x, "y": y, "z": z, "w": w},
            },
        })
    return {"transforms": transforms}


def imu_payload(
    commit: FrameCommit,
    frame_id: str,
    orientation_wxyz: Sequence[float],
    angular_velocity: Sequence[float],
    linear_acceleration: Sequence[float],
) -> dict[str, Any]:
    if not frame_id:
        raise ValueError("frame_id must be non-empty")
    _validate_vector(orientation_wxyz, 4, "orientation_wxyz")
    _validate_vector(angular_velocity, 3, "angular_velocity")
    _validate_vector(linear_acceleration, 3, "linear_acceleration")
    w, x, y, z = orientation_wxyz
    return {
        "header": {"stamp": _stamp(commit), "frame_id": frame_id},
        "orientation": {"x": x, "y": y, "z": z, "w": w},
        "angular_velocity": _vector3(angular_velocity),
        "linear_acceleration": _vector3(linear_acceleration),
    }


def joint_state_payload(
    commit: FrameCommit,
    names: Sequence[str],
    positions: Sequence[float],
    velocities: Sequence[float] = (),
    efforts: Sequence[float] = (),
) -> dict[str, Any]:
    if len(names) != len(positions):
        raise ValueError("names and positions must have equal length")
    if velocities and len(velocities) != len(names):
        raise ValueError("velocities must be empty or match names")
    if efforts and len(efforts) != len(names):
        raise ValueError("efforts must be empty or match names")
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("joint names must be non-empty strings")
    _validate_vector(positions, len(names), "positions")
    _validate_vector(velocities, len(names), "velocities", allow_empty=True)
    _validate_vector(efforts, len(names), "efforts", allow_empty=True)
    return {
        "header": {"stamp": _stamp(commit), "frame_id": ""},
        "name": list(names),
        "position": list(positions),
        "velocity": list(velocities),
        "effort": list(efforts),
    }


def _stamp(commit: FrameCommit) -> dict[str, int]:
    sec, nanosec = sim_time_parts(commit.sim_time_ns)
    return {"sec": sec, "nanosec": nanosec}


def _validate_pose(pose: Pose) -> None:
    _validate_vector(pose.position_m, 3, "position_m")
    _validate_vector(pose.quaternion_wxyz, 4, "quaternion_wxyz")


def _validate_vector(
    values: Iterable[float],
    expected: int,
    field: str,
    allow_empty: bool = False,
) -> None:
    values = tuple(values)
    if allow_empty and not values:
        return
    if len(values) != expected:
        raise ValueError(f"{field} must contain {expected} values")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in values):
        raise ValueError(f"{field} must contain finite numbers")


def _vector3(values: Sequence[float]) -> dict[str, float]:
    return {"x": values[0], "y": values[1], "z": values[2]}
