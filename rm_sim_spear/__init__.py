"""Frame-consistent simulation core.

The public names are loaded lazily so the standalone bridge can import the
small data model without importing MuJoCo.  This matters for the independent
ROS2 adapter, which runs in the system ROS environment rather than the SPEAR
Python environment.
"""

__all__ = [
    "Capture",
    "Coordinator",
    "CompositeRenderer",
    "FrameCommit",
    "FrameResult",
    "HitEvent",
    "HitReport",
    "MujocoDriver",
    "MemoryRenderer",
    "Observation",
    "Pose",
    "ReadbackMetadata",
    "ProjectileSubsystem",
    "Snapshot",
    "SpearRenderPort",
    "SpearMetadataCallbacks",
    "TargetGeometry",
    "calculate_ring",
]


def __getattr__(name):
    if name in {"Capture", "FrameCommit", "Observation", "Pose", "ReadbackMetadata", "Snapshot"}:
        from . import model
        return getattr(model, name)
    if name in {"Coordinator", "FrameResult"}:
        from .coordinator import Coordinator, FrameResult
        return {"Coordinator": Coordinator, "FrameResult": FrameResult}[name]
    if name == "MujocoDriver":
        from .mujoco_driver import MujocoDriver
        return MujocoDriver
    if name in {"HitEvent", "HitReport", "ProjectileSubsystem", "TargetGeometry", "calculate_ring"}:
        from . import projectile
        return getattr(projectile, name)
    if name in {"CompositeRenderer", "MemoryRenderer", "SpearRenderPort"}:
        from . import render
        return getattr(render, name)
    if name == "SpearMetadataCallbacks":
        from .spear_metadata import SpearMetadataCallbacks
        return SpearMetadataCallbacks
    raise AttributeError(name)
