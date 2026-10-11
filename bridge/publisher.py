"""Typed send helpers for the simulation side of the ROS2 bridge."""

from rm_sim_spear.model import FrameCommit, Snapshot

from .messages import frame_commit_payload, snapshot_payload
from .transport import UnixEnvelopeChannel


class UnixBridgePublisher:
    """Publish core state without exposing envelope names to the caller."""

    def __init__(self, channel: UnixEnvelopeChannel):
        self.channel = channel

    def publish_commit(self, commit: FrameCommit) -> None:
        self.channel.send("frame_commit", frame_commit_payload(commit))

    def publish_snapshot(self, snapshot: Snapshot) -> None:
        self.channel.send("snapshot", snapshot_payload(snapshot))

    def publish_frame(self, snapshot: Snapshot, include_commit: bool = False) -> None:
        """Send one snapshot, optionally sending the low-latency commit first."""
        if include_commit:
            self.publish_commit(snapshot.commit)
        self.publish_snapshot(snapshot)

    def publish_reset(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        self.channel.send("reset", {"run_id": run_id})
