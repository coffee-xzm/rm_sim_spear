from collections import deque
from collections.abc import Mapping
from typing import Any, Callable, Protocol

from .model import Capture, ReadbackMetadata, Snapshot


class RenderPort(Protocol):
    def submit(self, snapshot: Snapshot) -> list[Capture]: ...

    def reset(self) -> None: ...


class MemoryRenderer:
    """Deterministic stand-in for delayed UE readback during core tests."""

    def __init__(self, delay_frames: int = 0, sensor_id: str = "default", modality: str = "rgb"):
        if delay_frames < 0:
            raise ValueError("delay_frames cannot be negative")
        _validate_sensor_identity(sensor_id, modality)
        self.delay_frames = delay_frames
        self.sensor_id = sensor_id
        self.modality = modality
        self._pending: deque[Capture] = deque()
        self._ue_frame_id = 0

    def submit(self, snapshot: Snapshot) -> list[Capture]:
        self._ue_frame_id += 1
        self._pending.append(Capture(
            run_id=snapshot.commit.run_id,
            commit_id=snapshot.commit.commit_id,
            ue_frame_id=self._ue_frame_id,
            image=f"frame:{snapshot.commit.commit_id}".encode("ascii"),
            sensor_id=self.sensor_id,
            modality=self.modality,
        ))
        if len(self._pending) <= self.delay_frames:
            return []
        return [self._pending.popleft()]

    def reset(self) -> None:
        self._pending.clear()
        self._ue_frame_id = 0


class CompositeRenderer:
    """Fan one Snapshot out to independent camera/modality ports."""

    def __init__(self, renderers: tuple[RenderPort, ...] | list[RenderPort]):
        self.renderers = tuple(renderers)
        if not self.renderers:
            raise ValueError("CompositeRenderer requires at least one renderer")
        streams = tuple(
            (
                getattr(renderer, "sensor_id", "default"),
                getattr(renderer, "modality", "rgb"),
            )
            for renderer in self.renderers
        )
        if len(set(streams)) != len(streams):
            raise ValueError("CompositeRenderer streams must be unique")
        self.capture_streams = frozenset(streams)

    def reset(self) -> None:
        for renderer in self.renderers:
            renderer.reset()

    def prime(self) -> None:
        for renderer in self.renderers:
            prime = getattr(renderer, "prime", None)
            if prime is not None:
                prime()

    def submit(self, snapshot: Snapshot) -> list[Capture]:
        captures: list[Capture] = []
        for renderer in self.renderers:
            captures.extend(renderer.submit(snapshot))
        return captures


class SpearRenderPort:
    """SPEAR transaction adapter with explicit delayed-readback ownership.

    ``readback_metadata`` must return ``ReadbackMetadata`` (or a mapping with
    the same fields) attached to the image by a SPEAR SpFunc. The adapter
    intentionally refuses to infer identity from the current transaction
    because buffered readback can return an older frame.
    """

    def __init__(
        self,
        instance: Any,
        capture_component: Any,
        apply_snapshot: Callable[[Snapshot], None],
        set_game_paused: Callable[[bool], None],
        readback_metadata: Callable[[Any], ReadbackMetadata | Mapping[str, Any] | None],
        buffering_mode: str = "SingleBuffered",
        metadata_enqueue_copy: Callable[[Snapshot | None], None] | None = None,
        bind_frame_metadata: Callable[[Snapshot], None] | None = None,
        metadata_reset: Callable[[], None] | None = None,
        capture_reset: Callable[[], None] | None = None,
        sensor_id: str = "default",
        modality: str = "rgb",
    ):
        if buffering_mode not in {"SingleBuffered", "DoubleBuffered", "TripleBuffered"}:
            raise ValueError("unsupported SPEAR buffering mode")
        _validate_sensor_identity(sensor_id, modality)
        self.instance = instance
        self.capture_component = capture_component
        self.apply_snapshot = apply_snapshot
        self.set_game_paused = set_game_paused
        self.readback_metadata = readback_metadata
        self.metadata_enqueue_copy = metadata_enqueue_copy
        self.bind_frame_metadata = bind_frame_metadata
        self.metadata_reset = metadata_reset
        # SPEAR's capture component owns GPU pending readbacks.  The core
        # cannot safely infer how to flush that queue, so the integration
        # supplies an explicit reset/reinitialize hook when one is needed.
        self.capture_reset = capture_reset
        self.sensor_id = sensor_id
        self.modality = modality
        self.buffering_mode = buffering_mode
        self._pending: dict[int, Snapshot] = {}
        self._initial_priming_frames = {"SingleBuffered": 0, "DoubleBuffered": 1, "TripleBuffered": 2}[buffering_mode]
        self._discard_reads = self._initial_priming_frames
        self._primed = buffering_mode == "SingleBuffered"
        self._metadata_reset_pending = False

    def reset(self) -> None:
        if (
            self._primed
            and self.buffering_mode != "SingleBuffered"
            and self.capture_reset is None
        ):
            raise RuntimeError(
                "buffered SPEAR reset requires capture_reset to clear GPU readbacks"
            )
        self._pending.clear()
        if self.capture_reset is not None:
            self.capture_reset()
        self._discard_reads = self._initial_priming_frames
        self._primed = self.buffering_mode == "SingleBuffered"
        self._metadata_reset_pending = self.metadata_reset is not None

    def prime(self) -> None:
        """Prime SPEAR's buffered readback before the first simulation commit."""
        if self._primed:
            return
        for _ in range(self._initial_priming_frames):
            with self.instance.begin_frame():
                self.set_game_paused(True)
                self._reset_metadata_in_frame()
                self._enqueue_copy(None)
            with self.instance.end_frame(single_step=True):
                pass
        self._primed = True

    def submit(self, snapshot: Snapshot) -> list[Capture]:
        if not self._primed:
            raise RuntimeError("call SpearRenderPort.prime() before buffered submits")
        if self.buffering_mode == "SingleBuffered" and self.bind_frame_metadata is None:
            raise RuntimeError(
                "single-buffered business submits require bind_frame_metadata; "
                "read_pixels() alone cannot prove commit identity"
            )
        data_bundle = None
        if self.buffering_mode == "SingleBuffered":
            self._pending[snapshot.commit.commit_id] = snapshot
        with self.instance.begin_frame():
            self.set_game_paused(False)
            self._reset_metadata_in_frame()
            if self.bind_frame_metadata is not None:
                self.bind_frame_metadata(snapshot)
            self.apply_snapshot(snapshot)
            if self.buffering_mode == "TripleBuffered":
                data_bundle = self.capture_component.read_pixels()
                self._enqueue_copy(snapshot)
            elif self.buffering_mode == "DoubleBuffered":
                self._enqueue_copy(snapshot)
            else:
                pass

        with self.instance.end_frame(single_step=True):
            if self.buffering_mode in {"SingleBuffered", "DoubleBuffered"}:
                data_bundle = self.capture_component.read_pixels()
            self.set_game_paused(True)

        capture = self._consume_readback(data_bundle)
        if self.buffering_mode != "SingleBuffered":
            self._pending[snapshot.commit.commit_id] = snapshot
        return [capture] if capture is not None else []

    def _reset_metadata_in_frame(self) -> None:
        if self._metadata_reset_pending:
            self.metadata_reset()
            self._metadata_reset_pending = False

    def _enqueue_copy(self, snapshot: Snapshot | None) -> None:
        if self.metadata_enqueue_copy is not None:
            self.metadata_enqueue_copy(snapshot)
            return
        if snapshot is not None:
            raise RuntimeError(
                "buffered business submits require metadata_enqueue_copy; "
                "the legacy enqueue_copy() cannot bind commit identity"
            )
        # Legacy no-argument enqueue is safe only for discarded priming frames.
        self.capture_component.enqueue_copy()

    def _consume_readback(self, data_bundle: Any) -> Capture | None:
        if data_bundle is None:
            raise RuntimeError("SPEAR did not return a readback after priming")
        metadata = self.readback_metadata(data_bundle)
        if self._discard_reads:
            self._discard_reads -= 1
            return None
        if isinstance(metadata, ReadbackMetadata):
            readback = metadata
        elif isinstance(metadata, Mapping):
            try:
                readback = ReadbackMetadata(
                    run_id=metadata.get("run_id"),
                    commit_id=metadata.get("commit_id"),
                    ue_frame_id=metadata["ue_frame_id"],
                    sensor_id=metadata.get("sensor_id", "default"),
                    modality=metadata.get("modality", "rgb"),
                )
            except (KeyError, TypeError) as exc:
                raise ValueError(
                    "readback_metadata must return run_id, commit_id and ue_frame_id"
                ) from exc
        else:
            if metadata is None:
                raise ValueError("business readback is missing metadata")
            raise TypeError(
                "readback_metadata must return ReadbackMetadata or a mapping"
            )
        if not isinstance(readback.run_id, str) or not readback.run_id:
            raise ValueError("business readback is missing run_id")
        if readback.commit_id is None:
            raise ValueError("business readback is missing commit_id")
        if not isinstance(readback.commit_id, int) or readback.commit_id < 1:
            raise ValueError("readback commit_id must be a positive integer")
        if not isinstance(readback.ue_frame_id, int) or readback.ue_frame_id < 0:
            raise ValueError("readback ue_frame_id must be a non-negative integer")
        _validate_sensor_identity(readback.sensor_id, readback.modality)
        if readback.sensor_id != self.sensor_id or readback.modality != self.modality:
            raise ValueError("SPEAR readback belongs to a different sensor or modality")
        try:
            snapshot = self._pending[readback.commit_id]
        except KeyError as exc:
            raise ValueError(
                "SPEAR readback refers to an unknown or already consumed commit_id"
            ) from exc
        if readback.run_id != snapshot.commit.run_id:
            raise ValueError("SPEAR readback belongs to a different simulation run")
        del self._pending[readback.commit_id]
        image = data_bundle["arrays"]["data"] if isinstance(data_bundle, dict) else data_bundle
        image_bytes = image.tobytes() if hasattr(image, "tobytes") else bytes(image)
        return Capture(
            snapshot.commit.run_id,
            snapshot.commit.commit_id,
            readback.ue_frame_id,
            image_bytes,
            readback.sensor_id,
            readback.modality,
        )


def _validate_sensor_identity(sensor_id: str, modality: str) -> None:
    if not isinstance(sensor_id, str) or not sensor_id:
        raise ValueError("sensor_id must be a non-empty string")
    if not isinstance(modality, str) or not modality:
        raise ValueError("modality must be a non-empty string")
