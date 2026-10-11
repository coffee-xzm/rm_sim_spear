from dataclasses import dataclass


@dataclass(frozen=True)
class FrameCommit:
    run_id: str
    commit_id: int
    sim_tick: int
    sim_time_ns: int
    physics_step: int
    state_revision: int


@dataclass(frozen=True)
class Pose:
    position_m: tuple[float, float, float]
    quaternion_wxyz: tuple[float, float, float, float]


@dataclass(frozen=True)
class Snapshot:
    commit: FrameCommit
    poses: dict[str, Pose]
    sensors: dict[str, tuple[float, ...]]


@dataclass(frozen=True)
class Capture:
    run_id: str
    commit_id: int
    ue_frame_id: int
    image: bytes
    sensor_id: str = "default"
    modality: str = "rgb"


@dataclass(frozen=True)
class ReadbackMetadata:
    """Identity attached to one SPEAR GPU readback buffer.

    ``run_id`` and ``commit_id`` are absent for buffers queued during renderer
    priming. A business readback must always carry both; positional queue
    order is not a valid source of frame identity.
    """

    run_id: str | None
    commit_id: int | None
    ue_frame_id: int
    sensor_id: str = "default"
    modality: str = "rgb"


@dataclass(frozen=True)
class Observation:
    snapshot: Snapshot
    capture: Capture
