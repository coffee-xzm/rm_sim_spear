import math
from dataclasses import dataclass


@dataclass(frozen=True)
class TargetGeometry:
    target_id: str
    radius_m: float


@dataclass(frozen=True)
class HitReport:
    run_id: str
    shot_id: str
    target_id: str
    commit_id: int
    ue_frame_id: int
    local_hit_position_m: tuple[float, float, float]


@dataclass(frozen=True)
class HitEvent:
    run_id: str
    shot_id: str
    target_id: str
    commit_id: int
    ue_frame_id: int
    local_hit_position_m: tuple[float, float, float]
    radial_distance_m: float
    ring: int


def calculate_ring(local_hit_position_m: tuple[float, float, float], radius_m: float) -> tuple[float, int] | None:
    """Return radial distance and ring for a hit in target-local coordinates."""
    if not math.isfinite(radius_m) or radius_m <= 0:
        return None
    if len(local_hit_position_m) != 3 or not all(math.isfinite(x) for x in local_hit_position_m):
        return None
    radial_distance_m = math.hypot(local_hit_position_m[0], local_hit_position_m[1])
    if radial_distance_m > radius_m:
        return None
    ring = max(1, min(10, 10 - math.floor(radial_distance_m / (radius_m / 10))))
    return radial_distance_m, ring


class ProjectileSubsystem:
    """Consumes engine collision reports and emits at-most-once hit events."""

    def __init__(self, targets: dict[str, TargetGeometry]):
        self._targets = dict(targets)
        self._processed_shots: set[tuple[str, str]] = set()

    def reset(self) -> None:
        self._processed_shots.clear()

    def handle_hit(self, report: HitReport) -> HitEvent | None:
        shot_key = (report.run_id, report.shot_id)
        if shot_key in self._processed_shots:
            return None
        target = self._targets.get(report.target_id)
        if target is None or target.target_id != report.target_id:
            return None
        result = calculate_ring(report.local_hit_position_m, target.radius_m)
        if result is None:
            return None
        self._processed_shots.add(shot_key)
        radial_distance_m, ring = result
        return HitEvent(
            run_id=report.run_id,
            shot_id=report.shot_id,
            target_id=report.target_id,
            commit_id=report.commit_id,
            ue_frame_id=report.ue_frame_id,
            local_hit_position_m=report.local_hit_position_m,
            radial_distance_m=radial_distance_m,
            ring=ring,
        )
