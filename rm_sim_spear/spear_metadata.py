"""Python callbacks for the companion SPEAR metadata component."""

import json
from typing import Any

from .model import ReadbackMetadata, Snapshot


class SpearMetadataCallbacks:
    """Adapt a UE metadata component to :class:`SpearRenderPort` callbacks.

    The component exposes three SpFuncs: ``bind_frame_metadata``,
    ``enqueue_copy_metadata`` and ``readback_metadata``. Metadata is returned
    through the SpFunc ``info`` field, keeping image bytes in SPEAR shared
    memory.
    """

    def __init__(self, component: Any, sensor_id: str = "default", modality: str = "rgb"):
        if not isinstance(sensor_id, str) or not sensor_id:
            raise ValueError("sensor_id must be a non-empty string")
        if not isinstance(modality, str) or not modality:
            raise ValueError("modality must be a non-empty string")
        self.component = component
        self.sensor_id = sensor_id
        self.modality = modality

    def bind_frame_metadata(self, snapshot: Snapshot) -> None:
        self._call("bind_frame_metadata", snapshot)

    def enqueue_copy_metadata(self, snapshot: Snapshot | None) -> None:
        self._call("enqueue_copy_metadata", snapshot)

    def readback_metadata(self, _data_bundle: Any) -> ReadbackMetadata | None:
        result = self.component.readback_metadata()
        if not isinstance(result, dict):
            raise TypeError("metadata component must return a SPEAR data bundle")
        encoded = result.get("info", "")
        if not encoded:
            # Priming frames intentionally have no business identity.
            return None
        try:
            metadata = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("metadata component returned invalid JSON") from exc
        if not isinstance(metadata, dict):
            raise ValueError("metadata component JSON must be an object")
        return ReadbackMetadata(
            run_id=metadata.get("run_id"),
            commit_id=metadata.get("commit_id"),
            ue_frame_id=metadata["ue_frame_id"],
            sensor_id=metadata.get("sensor_id", self.sensor_id),
            modality=metadata.get("modality", self.modality),
        )

    def reset(self) -> None:
        """Discard metadata queued by a previous run."""
        self.component.reset_metadata()

    def _call(self, function_name: str, snapshot: Snapshot | None) -> None:
        payload = ""
        if snapshot is not None:
            payload = json.dumps(
                {
                    "run_id": snapshot.commit.run_id,
                    "commit_id": snapshot.commit.commit_id,
                    "sensor_id": self.sensor_id,
                    "modality": self.modality,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        getattr(self.component, function_name)(info=payload)
