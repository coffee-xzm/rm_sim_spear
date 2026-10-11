"""Deterministic observation-camera poses for the physics view.

Why this is its own module (and not inline in ``tools/run_physics_sim.py``): the
framing rule is a measured decision, not plumbing.  A camera 40-60 cm above the
floor looking 14 degrees down puts the ground at extreme grazing incidence, and
a scene capture has no temporal AA to hide what that does to thin geometry and
minified textures -- the field markings read as dashed lines and the near floor
as a washed-out haze (docs/rendering.md sections 12.6 and 12.10).  At 1.7 m the
same capture is clean.

``~/rm_sim_rune``'s packaged build looks clean for the same reason: its pawn
camera sits about 2 m up with a mild pitch, and the scene capture it publishes
(1440x1080, single sample, ``SCS_FinalColorLDR``) is the same mechanism ours is.
"""

from __future__ import annotations

import math

import numpy as np

# Defaults chosen from the camera-height ladder in docs/rendering.md section
# 12.6 (60 cm = mush, 150/250 cm = legible) plus a measured robot footprint at
# the delivered 1440x1080: 320/170 leaves the robot 134x76 px, 200/170 gives
# 188x134 px with a steeper (cleaner) 35 degree pitch.  Both are dash-free; the
# closer one is what a "watch the robot advance" frame needs.
STANDOFF_CM = 200.0
HEIGHT_CM = 170.0
AIM_HEIGHT_CM = 30.0

# Azimuth of the standoff, as (x, y) sign pattern.  This is the (+1, -1)
# diagonal the earlier 40 cm camera already used, so every robot-framed frame in
# docs/rendering.md stays comparable and the sightline is the one we know is
# clear.  Kept as a unit vector so the standoff is a real distance.
AZIMUTH = (1.0 / math.sqrt(2.0), -1.0 / math.sqrt(2.0))


def robot_view_pose(
    base_location_cm,
    *,
    standoff_cm: float = STANDOFF_CM,
    height_cm: float = HEIGHT_CM,
    aim_height_cm: float = AIM_HEIGHT_CM,
):
    """Return ``(position, target)`` for a camera watching the posed ``base_link``.

    ``standoff_cm`` is the horizontal distance from the robot (a distance, not a
    per-axis offset, so the azimuth and the aim stay fixed while it changes),
    ``height_cm`` is measured up from the robot's own origin, and
    ``aim_height_cm`` is the height above that origin the camera looks at.  The
    pitch is whatever those three imply, because the camera always aims at the
    robot.  Units are centimetres, in Unreal's
    world frame, exactly like the MuJoCo poses the caller applies.
    """
    base = np.asarray(base_location_cm, dtype=float)
    if base.shape != (3,):
        raise ValueError(f"base_location_cm must have 3 components, got {base!r}")
    position = base + np.array(
        (AZIMUTH[0] * float(standoff_cm), AZIMUTH[1] * float(standoff_cm), float(height_cm)))
    target = base + np.array((0.0, 0.0, float(aim_height_cm)))
    return position, target


#
# Interactive free camera
#
# ``tools/run_physics_sim.py --interactive`` reuses the keyboard handler that
# already ships with ``tools/test_urdf_views.py`` instead of growing a second
# one.  That handler keeps a single mutable ``{location, pitch, yaw, roll}``
# dict, so a world-fixed ``(position, target)`` pair has to be converted into it
# and back.  The two functions below are that conversion, kept here (rather than
# next to the loop) so they can be tested without importing a tool script.
#
# The convention is the handler's own, not a generic one: it rebuilds the
# forward vector as ``[cos(p)cos(y), cos(p)sin(y), sin(p)]``, so pitch is
# measured from the horizontal plane and yaw from +x, both in Unreal axes.
#


def free_camera_state(position, target) -> dict[str, object]:
    """Seed the key handler's camera state from a world-fixed look-at pair."""
    position = np.asarray(position, dtype=float)
    offset = np.asarray(target, dtype=float) - position
    if position.shape != (3,) or offset.shape != (3,):
        raise ValueError("position and target must both have 3 components")
    horizontal = float(np.hypot(offset[0], offset[1]))
    return {
        # Copy: the handler translates in place (`location += forward * d`), and
        # the caller still holds `position` as the live sensor pose.
        "location": np.array(position, dtype=float),
        "pitch": float(np.degrees(np.arctan2(offset[2], horizontal))),
        "yaw": float(np.degrees(np.arctan2(offset[1], offset[0]))),
        "roll": 0.0,
    }


def free_camera_pose(camera_state, distance_cm: float):
    """Turn a moved camera state back into the sensor's ``(position, target)``.

    The aim distance is preserved rather than recomputed: ``_set_camera_look_at``
    only reads the direction, but the jitter path (``--jitter-pixels``) builds an
    offset from the whole pair, so keeping the original standoff keeps the two
    paths consistent.
    """
    yaw = math.radians(float(camera_state["yaw"]))
    pitch = math.radians(float(camera_state["pitch"]))
    forward = np.array(
        [math.cos(pitch) * math.cos(yaw),
         math.cos(pitch) * math.sin(yaw),
         math.sin(pitch)],
        dtype=float,
    )
    location = np.asarray(camera_state["location"], dtype=float)
    return location, location + forward * float(distance_cm)
