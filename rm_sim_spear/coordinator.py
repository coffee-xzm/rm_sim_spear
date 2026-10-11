from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from .model import Capture, FrameCommit, Observation, Snapshot
from .mujoco_driver import MujocoDriver
from .render import RenderPort


@dataclass(frozen=True)
class FrameResult:
    snapshot: Snapshot
    observations: tuple[Observation, ...]


class Coordinator:
    def __init__(
        self,
        physics: MujocoDriver,
        renderer: RenderPort,
        capture_streams: Sequence[tuple[str, str]] | None = None,
    ):
        self.physics = physics
        self.renderer = renderer
        if capture_streams is None:
            capture_streams = getattr(renderer, "capture_streams", None)
            if capture_streams is None:
                capture_streams = ((
                    getattr(renderer, "sensor_id", "default"),
                    getattr(renderer, "modality", "rgb"),
                ),)
        self.capture_streams = frozenset(capture_streams)
        if len(self.capture_streams) != len(capture_streams) or not self.capture_streams:
            raise ValueError("capture_streams must be non-empty and unique")
        if any(not sensor_id or not modality for sensor_id, modality in self.capture_streams):
            raise ValueError("capture stream identities must be non-empty")
        self._run_id = ""
        self._sim_tick = 0
        self._pending: dict[int, Snapshot] = {}
        self._awaiting: dict[int, set[tuple[str, str]]] = {}
        self.reset()

    def reset(self) -> None:
        self.physics.reset()
        self.renderer.reset()
        self._run_id = uuid4().hex
        self._sim_tick = 0
        self._pending.clear()
        self._awaiting.clear()
        # Buffered SPEAR readback needs non-business frames to fill its GPU
        # queue.  Prime after every reset so those frames can never be paired
        # with a real Snapshot.  MemoryRenderer and other synchronous ports do
        # not need to implement this optional hook.
        prime = getattr(self.renderer, "prime", None)
        if prime is not None:
            prime()

    def step(self, control: Sequence[float] = ()) -> FrameResult:
        self.physics.step(control)
        self._sim_tick += 1
        commit = FrameCommit(
            run_id=self._run_id,
            commit_id=self._sim_tick,
            sim_tick=self._sim_tick,
            sim_time_ns=self.physics.sim_time_ns,
            physics_step=self.physics.physics_step,
            state_revision=self._sim_tick,
        )
        snapshot = Snapshot(commit, self.physics.read_poses(), self.physics.read_sensors())
        self._pending[commit.commit_id] = snapshot
        self._awaiting[commit.commit_id] = set(self.capture_streams)
        captures = self.renderer.submit(snapshot)
        observations = tuple(self._pair(capture) for capture in captures)
        return FrameResult(snapshot, observations)

    def _pair(self, capture: Capture) -> Observation:
        if capture.run_id != self._run_id:
            raise ValueError("capture belongs to a different run")
        snapshot = self._pending.get(capture.commit_id)
        if snapshot is None:
            raise ValueError("unknown or completed capture commit_id")
        remaining = self._awaiting[capture.commit_id]
        stream = (capture.sensor_id, capture.modality)
        if stream not in remaining:
            raise ValueError("unknown or duplicate capture stream for commit_id")
        remaining.remove(stream)
        if not remaining:
            del self._awaiting[capture.commit_id]
            del self._pending[capture.commit_id]
        return Observation(snapshot, capture)
