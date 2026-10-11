import argparse
import json
from dataclasses import asdict

from .coordinator import Coordinator
from .mujoco_driver import MujocoDriver
from .render import MemoryRenderer


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a frame-consistent MuJoCo simulation")
    parser.add_argument("model", help="MJCF or URDF path accepted by MuJoCo")
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--steps-per-frame", type=int, default=1)
    parser.add_argument("--readback-delay", type=int, default=0)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")

    coordinator = Coordinator(
        MujocoDriver.from_xml_path(args.model, args.steps_per_frame),
        MemoryRenderer(args.readback_delay),
    )
    for _ in range(args.frames):
        result = coordinator.step()
        print(json.dumps({
            "commit": asdict(result.snapshot.commit),
            "poses": {name: asdict(pose) for name, pose in result.snapshot.poses.items()},
            "ready_captures": [asdict(obs.capture) | {"image": obs.capture.image.decode("ascii")}
                               for obs in result.observations],
        }))


if __name__ == "__main__":
    main()
