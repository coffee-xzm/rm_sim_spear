from pathlib import Path
from typing import Sequence

import mujoco

from .model import Pose


class MujocoDriver:
    def __init__(self, model: mujoco.MjModel, steps_per_frame: int):
        if steps_per_frame < 1:
            raise ValueError("steps_per_frame must be positive")
        self.model = model
        self.data = mujoco.MjData(model)
        self.steps_per_frame = steps_per_frame
        self.physics_step = 0
        mujoco.mj_forward(self.model, self.data)

    @classmethod
    def from_xml_path(cls, path: str | Path, steps_per_frame: int) -> "MujocoDriver":
        return cls(mujoco.MjModel.from_xml_path(str(path)), steps_per_frame)

    @property
    def sim_time_ns(self) -> int:
        return round(self.data.time * 1_000_000_000)

    def reset(self) -> None:
        mujoco.mj_resetData(self.model, self.data)
        mujoco.mj_forward(self.model, self.data)
        self.physics_step = 0

    def step(self, control: Sequence[float] = ()) -> None:
        if len(control) != self.model.nu:
            raise ValueError(f"expected {self.model.nu} actuator controls, got {len(control)}")
        if self.model.nu:
            self.data.ctrl[:] = control
        for _ in range(self.steps_per_frame):
            mujoco.mj_step(self.model, self.data)
        self.physics_step += self.steps_per_frame

    def read_poses(self) -> dict[str, Pose]:
        return {
            self.model.body(body_id).name: Pose(
                position_m=tuple(float(x) for x in self.data.xpos[body_id]),
                quaternion_wxyz=tuple(float(x) for x in self.data.xquat[body_id]),
            )
            for body_id in range(1, self.model.nbody)
        }

    def read_sensors(self) -> dict[str, tuple[float, ...]]:
        return {
            self.model.sensor(sensor_id).name: tuple(
                float(x) for x in self.data.sensordata[
                    self.model.sensor_adr[sensor_id]:
                    self.model.sensor_adr[sensor_id] + self.model.sensor_dim[sensor_id]
                ]
            )
            for sensor_id in range(self.model.nsensor)
        }
