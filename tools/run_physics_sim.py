"""Advance the robot under MuJoCo physics and capture one view per frame.

Why this runner exists next to ``tools/run_spear_sim.py``: the render port
(``rm_sim_spear.render.SpearRenderPort``) refuses a business readback without
UE-side frame metadata (``run_id``/``commit_id``/``ue_frame_id``), and the
standalone ``-game`` world does not expose the metadata component.  This runner
therefore reads the capture back directly (``bCaptureGpuNextRender`` +
``read_pixels()``), the same mechanism ``tools/test_urdf_views.py`` already uses.

It renders **one** view that matches the player viewport (the "editor window"
view), pushes every MuJoCo link pose into the UE link anchors each frame - with
the world offset that puts the MuJoCo origin on the level's floor - and writes
one PNG plus one CSV row per frame.

For throughput runs the loop has four cost knobs, all measured in ``trace.csv``
and written up in docs/render_budget.md section 13: ``--save-images`` keeps the
~22 ms (1440x1080) PNG encode off the loop, ``--buffering triple`` replaces the
per-frame GPU/CPU flush of ``read_pixels()`` with a pipelined enqueue/read pair,
``--sync-pose`` calls the actors directly instead of ``send_async`` (a queued
call costs the transaction a whole extra engine frame, ~10.5 ms, regardless of
how many links are posed), and ``--width/--height`` plus ``--supersample`` set
the internal pixel count (at 512x384 the capture is nearly free, at 1440x1080 it
is +8..12 ms; engine feature flags are worth +-4 ms).  Measured best case:
512x384 = 11.3 ms/frame, 1024x768 = 14.8 ms, 1440x1080 = 19.2..23.7 ms, against
108.5 ms for the original single-buffered + synchronous-PNG loop.
With ``--buffering triple`` the image read in iteration k belongs to iteration
``k - _BUFFERED_LAG[mode]``, so the loop runs that many extra drain iterations
and pairs every row with the pose it was rendered with.

``--interactive`` turns the same loop into a viewer you can fly: each frame goes
to an OpenCV window, and ``W/S/A/D/E/Q`` + ``I/K``/``J/L`` move the world-fixed
capture camera while the physics keeps stepping.  It shares the key handler with
``tools/test_urdf_views.py`` (``views._move_debug_camera``) and the pose
conversion with ``rm_sim_spear.view_pose``; it is restricted to ``--view fixed``
and ``--view main`` because the bound camera rides ``cam_link_8``.  Unlike
``test_urdf_views.py --continuous``, which never calls ``physics.step()`` and so
only ever shows one frozen pose, this is how you watch the robot actually move.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import deque
from dataclasses import replace
from itertools import count
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

ENGINE_IDLE_TIMEOUT_SECONDS = 180.0

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import spear  # noqa: E402

import test_urdf_views as views  # noqa: E402  (verified capture helpers)
from rm_sim_spear.arena import (  # noqa: E402
    resolve_arena_scene,
    resolve_world_offset_cm,
)
from rm_sim_spear.capture_resolution import choose_internal_resolution  # noqa: E402
from rm_sim_spear.frame_publisher import FrameRingPublisher  # noqa: E402
from rm_sim_spear.image_writer import AsyncImageWriter  # noqa: E402
from rm_sim_spear import view_pose  # noqa: E402
from rm_sim_spear.launch import resolve_game_executable as _resolve_game_executable  # noqa: E402
from rm_sim_spear.lens import ROBOT_CAMERA_LENS, Lens, rescaled_fov_deg  # noqa: E402
from rm_sim_spear.model import FrameCommit, Snapshot  # noqa: E402
from rm_sim_spear.pose_sync import pose_due, to_unreal_pose  # noqa: E402
from rm_sim_spear.mujoco_driver import (  # noqa: E402
    DEFAULT_SHELL_BODIES,
    MujocoDriver,
    PhysicsOptions,
)
from rm_sim_spear.view_pose import robot_view_pose  # noqa: E402
from rm_sim_spear.spear_scene_binding import (  # noqa: E402
    apply_scene_binding_plan,
    link_application_order,
    load_plan,
    make_scene_pose_synchronizer,
)
from rm_sim_spear.skeletal_robot import (  # noqa: E402
    compare_bone_writes,
    make_skeletal_pose_synchronizer,
    missing_bones,
    read_bone_world_locations,
    spawn_poseable_robot,
)
from rm_sim_spear.vram import VramSampler, low_vram_ini_args  # noqa: E402

DEFAULT_WHEELS = ("link_1_joint", "link_2_joint", "link_3_joint", "link_4_joint")


def _jitter_offsets(count, radius_px):
    """Deterministic sub-pixel sample offsets on a disc (golden-angle spiral).

    Scene captures do not jitter for us: ``USceneCaptureComponent2D`` sets
    ``ShowFlags.TemporalAA = false`` in its legacy init
    (``SceneCaptureComponent.cpp:685``), so consecutive capture frames come out
    byte-identical.  We therefore generate the offsets ourselves and average
    the reads, which is what turns single-sample aliasing into real coverage.
    """
    if count <= 1 or radius_px <= 0.0:
        return [(0.0, 0.0)]*max(1, count)
    golden_angle = np.pi*(3.0 - np.sqrt(5.0))
    offsets = []
    for index in range(count):
        radius = radius_px*np.sqrt((index + 0.5)/count)
        angle = index*golden_angle
        offsets.append((float(radius*np.cos(angle)), float(radius*np.sin(angle))))
    return offsets


def _set_camera_pose(sensor, position_cm, target_cm, *, fov_x_deg, width, height, offset_px):
    """Aim the sensor, shifted by a sub-pixel offset.

    One pixel spans ``fov_x/width`` horizontally, so the offset is applied as a
    small rotation of the forward vector inside the camera basis.
    """
    position = np.asarray(position_cm, dtype=float)
    forward = np.asarray(target_cm, dtype=float) - position
    forward /= np.linalg.norm(forward)
    up_world = np.asarray((0.0, 0.0, 1.0), dtype=float)
    right = np.cross(up_world, forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    if offset_px != (0.0, 0.0):
        fov_y_deg = 2.0*np.degrees(np.arctan(
            np.tan(np.radians(fov_x_deg)/2.0)*height/width))
        d_right = np.tan(np.radians(fov_x_deg)*offset_px[0]/width)
        d_up = np.tan(np.radians(fov_y_deg)*offset_px[1]/height)
        forward = forward + right*d_right + up*d_up
        forward /= np.linalg.norm(forward)
        right = np.cross(up_world, forward)
        right /= np.linalg.norm(right)
        up = np.cross(forward, right)
    rotation = np.column_stack((forward, right, up))
    sensor.K2_SetActorLocationAndRotation(
        NewLocation={"X": float(position[0]), "Y": float(position[1]), "Z": float(position[2])},
        NewRotation=spear.math.to_spear_rotator_from_numpy_matrix(numpy_matrix=rotation),
    )


# A pipelined readback returns an image that is a fixed number of loop
# iterations old.  The lag is not a free parameter: ``enqueue_copy`` runs in
# ``begin_frame``, i.e. before that frame's capture is rendered, so the copy sees
# the *previous* frame's render target, and the staging-to-CPU copy lands one
# enqueue later.  Measured against a single-buffered run of the same command
# (same deterministic MuJoCo steps, frame-for-frame PNG diff): triple buffering
# is 3 iterations behind with the read placed before the enqueue, double
# buffering 1.  Pairing each row with the pose it was rendered with is what keeps
# "which commit does this image belong to" provable even though the standalone
# ``-game`` world has no metadata component.
_BUFFERED_LAG = {"single": 0, "double": 1, "triple": 3}

_BUFFERING_MODE_NAMES = {
    "single": "SingleBuffered",
    # SPEAR allocates the scene view extension that drives user scene textures
    # and the buffered staging copies for DoubleBuffered and TripleBuffered only.
    "double": "DoubleBuffered",
    "triple": "TripleBuffered",
}


def _input_settings_report(game, config) -> dict:
    """Report the mouse-capture settings this run asked the engine for.

    Reading the live ``UInputSettings`` default object is not possible here:
    ``UInputSettings`` is not in SPEAR's static-class-description whitelist, so
    the name lookup cannot produce a class handle (observed 2026-10-09, both the
    direct name and ``find_static_classes_as_dict``).  What is both available and
    decisive is the launch command line: the engine applies
    ``-DefaultViewportMouseCaptureMode`` *after* reading the ini
    (``GameViewportClient.cpp`` constructor), and SPEAR logs the full command
    line, so a run log proves the capture mode it was launched with.  The other
    half of the fix, ``ue/Config/DefaultInput.ini``, is the only place that can
    clear ``bCaptureMouseOnLaunch``.
    """
    wanted = {"norelativemousemode", "defaultviewportmousecapturemode"}
    return {"requested_launch_args": {
        str(key): ("null" if value is None else str(value))
        for key, value in dict(config.SPEAR.INSTANCE.COMMAND_LINE_ARGS).items()
        if str(key).lower() in wanted
    }}


def _configure_capture_pipeline(component, args) -> None:
    """Apply the readback pipeline knobs that must be set before ``Initialize()``.

    ``BufferingMode`` decides which RHI readbacks ``Initialize()`` allocates, so
    it cannot be changed later; the two measurement flags are set here as well so
    every view path (fixed/main/bound) gets the same pipeline.
    """
    component.BufferingMode = _BUFFERING_MODE_NAMES[args.buffering]
    if args.print_engine_frame_time:
        component.bPrintFrameTimeEveryFrame = True
    if args.no_readback:
        # Developer flag from SPEAR: keeps the returned bundle's shape but skips
        # the GPU-to-CPU copy, which is how the render cost is measured alone.
        component.bReadPixelData = False


def _capture_transaction(instance, component, buffering: str, body=None, capture=True):
    """Run one capture transaction and return its readback bundle.

    ``body`` runs inside ``begin_frame`` and is where the pose and camera work
    goes.  The modes differ only in where the readback sits relative to the
    frame it captures:

    - ``single``: SPEAR's default; ``read_pixels`` in ``end_frame`` flushes the
      renderer and locks each staging buffer, so the frame cannot be pipelined.
    - ``double``: enqueue in ``begin_frame``, read in ``end_frame``.  Nothing is
      flushed, but the read still waits for this frame's GPU copy.
    - ``triple``: read the previous enqueue in ``begin_frame``, then hand this
      frame's capture to the ring, so no iteration waits on the GPU.  The image
      therefore belongs ``_BUFFERED_LAG[mode]`` iterations back.

    ``capture=False`` (the ``--no-capture`` measurement flag) runs the same
    transaction with no capture request and no readback at all, which is how the
    engine's own per-frame cost is separated from the scene capture.

    Priming uses this same function with no body, which is what keeps the three
    modes from drifting apart.
    """
    begin_start = time.perf_counter()
    if not capture:
        body_ms = 0.0
        with instance.begin_frame():
            if body is not None:
                inner_start = time.perf_counter()
                body()
                body_ms = 1000.0*(time.perf_counter() - inner_start)
        begin_ms = 1000.0*(time.perf_counter() - begin_start)
        with instance.end_frame(single_step=True):
            pass
        end_ms = 1000.0*(time.perf_counter() - begin_start) - begin_ms
        # ``body_ms`` is the body alone, as in the capturing paths.  It used to
        # be reported as an alias of ``begin_ms`` here, which hid the one column
        # that says whether the transaction did any work at all -- that alias is
        # what let a "no poses were sent" bug survive a whole measurement series
        # (docs/render_budget.md 15.4).
        return None, {
            "begin_ms": begin_ms, "end_ms": end_ms,
            "body_ms": body_ms, "read_ms": 0.0, "enqueue_ms": 0.0,
        }
    if buffering == "triple":
        body_ms = read_ms = enqueue_ms = 0.0
        with instance.begin_frame():
            if body is not None:
                inner_start = time.perf_counter()
                body()
                body_ms = 1000.0*(time.perf_counter() - inner_start)
            component.bCaptureGpuNextRender = True
            # The read returns what the ring finished lag iterations ago; the
            # enqueue hands this frame's capture to the ring without waiting.
            inner_start = time.perf_counter()
            bundle = component.read_pixels()
            read_ms = 1000.0*(time.perf_counter() - inner_start)
            inner_start = time.perf_counter()
            component.enqueue_copy()
            enqueue_ms = 1000.0*(time.perf_counter() - inner_start)
        begin_ms = 1000.0*(time.perf_counter() - begin_start)
        with instance.end_frame(single_step=True):
            pass
        end_ms = 1000.0*(time.perf_counter() - begin_start) - begin_ms
        return bundle, {
            "begin_ms": begin_ms, "end_ms": end_ms,
            "body_ms": body_ms, "read_ms": read_ms, "enqueue_ms": enqueue_ms,
        }
    with instance.begin_frame():
        if body is not None:
            body()
        component.bCaptureGpuNextRender = True
        if buffering == "double":
            component.enqueue_copy()
    begin_ms = 1000.0*(time.perf_counter() - begin_start)
    with instance.end_frame(single_step=True):
        bundle = component.read_pixels()
    end_ms = 1000.0*(time.perf_counter() - begin_start) - begin_ms
    return bundle, {
        "begin_ms": begin_ms, "end_ms": end_ms,
        "body_ms": 0.0, "read_ms": 0.0, "enqueue_ms": 0.0,
    }


def _prime_buffered_capture(instance, component, buffering: str, frames: int) -> None:
    """Fill the pipeline depth so the first real read returns a real frame.

    Double buffering needs one frame (its first enqueue has no staged data yet),
    triple buffering needs its ``_BUFFERED_LAG``; the images are discarded.
    """
    for _ in range(max(0, int(frames))):
        _capture_transaction(instance, component, buffering)


def _reconstruct(image, width, height, *, filter_name, sigma_px):
    """Reduce a supersampled buffer to the output size.

    ``area`` is a box filter (crisp); ``gaussian`` widens the reconstruction
    filter, which trades sharpness for smoothness - the acceptable side of the
    trade for thin-line artefacts.  Blurring only helps here, before the output
    grid is sampled; blurring the finished image cannot remove aliasing that is
    already baked into its pixels.
    """
    if image.shape[1] == width and image.shape[0] == height:
        return image
    if filter_name == "gaussian" and sigma_px > 0.0:
        scale = image.shape[1]/float(width)
        image = cv2.GaussianBlur(image, (0, 0), sigma_px*scale)
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def resolve_game_executable(path) -> Path:
    """Validate ``--game-executable``: the ``.sh`` UAT archived for a packaged build.

    Thin wrapper so callers and tests keep one name; the rule itself lives in
    ``rm_sim_spear.launch`` next to the other pure helpers.
    """
    return _resolve_game_executable(path)


def _viewport_camera_pose(viewport_desc):
    """Return the player viewport pose as a world-fixed (position, target) pair.

    The editor window is the one view whose framing and exposure we already
    trust, so the diagnostic sensor starts there and then holds still while the
    robot moves through the frame.  Unreal's camera x-axis is forward
    (see examples/mujoco_interop/run.py for the same convention).
    """
    location = {str(key).lower(): float(value) for key, value in viewport_desc["camera_location"].items()}
    position = np.array([location["x"], location["y"], location["z"]], dtype=float)
    rotation = np.asarray(
        spear.math.to_numpy_matrix_from_spear_rotator(spear_rotator=viewport_desc["camera_rotation"]),
        dtype=float)
    forward = rotation[:, 0]
    forward /= float(np.linalg.norm(forward))
    return position, position + forward*500.0


def _hide_default_pawn_mesh(game) -> list[dict]:
    """Hide the engine placeholder sphere that ``ADefaultPawn`` owns.

    ``ADefaultPawn`` (``Engine/Source/Runtime/Engine/Private/DefaultPawn.cpp``)
    creates ``MeshComponent0`` with static mesh ``/Engine/EngineMeshes/Sphere``,
    no material, and ``bOwnerNoSee = true``.  The owning player therefore never
    sees it in their own viewport, but a ``USceneCaptureComponent2D`` belongs to
    no player, so every capture renders a 70 cm untextured checkerboard sphere
    at the PlayerStart.  Measured: the pawn sits at the level origin with actor
    bounds +/-35 cm, matching the ``+34.3 cm`` the ``sp_world_position_`` probe
    read at the origin column in docs/rendering.md section 10, and rune has no
    such sphere because it supplies its own camera-only pawn (``ARunePawn``).

    Hiding it does not touch the pawn's camera, so ``--view main`` still frames
    the player viewport.  Delivery-level fix (a project GameMode whose
    DefaultPawnClass is camera-only) needs an editor change plus a re-cook.

    ``SetActorHiddenInGame(bNewHidden=True)`` rather than a component-level
    ``SetVisibility``: it propagates to the primitive component and dirties its
    render state, and it is the same call the armor template hide already uses.
    A reflected ``SetVisibility`` needs every parameter by exact name --
    ``isSubsetOf(keys(args), keys(property_descs))`` aborts and poisons
    EngineService, which then fails every later frame.
    """
    hidden = []
    actors = game.unreal_service.find_actors_by_class(
        uclass="ADefaultPawn", as_unreal_object=True)
    for actor in actors or []:
        actor.SetActorHiddenInGame(bNewHidden=True)
        row = _link_dump(actor)
        row["mesh_components"] = len(game.unreal_service.get_components_by_class(
            actor=actor, uclass="UStaticMeshComponent",
            include_from_child_actors=True) or [])
        hidden.append(row)
    return hidden


def _tagged_actor_bounds(game, tag, uclass="AStaticMeshActor"):
    """Union of world bounds over every actor carrying ``tag``.

    Used to answer "where is the robot and how big is it" from the actor set the
    importer tagged, instead of guessing from the URDF or a single anchor.
    """
    actors = list(game.unreal_service.find_actors_by_tag(
        tag=tag, uclass=uclass, as_unreal_object=True))
    low = np.array([np.inf, np.inf, np.inf])
    high = np.array([-np.inf, -np.inf, -np.inf])
    for actor in actors:
        dump = _link_dump(actor)
        location = np.asarray(dump["location_cm"], dtype=float)
        extent = np.asarray(dump.get("bounds_extent_cm", [0.0, 0.0, 0.0]), dtype=float)
        low = np.minimum(low, location - extent)
        high = np.maximum(high, location + extent)
    if not actors:
        return {"tag": tag, "count": 0}
    return {
        "tag": tag,
        "count": len(actors),
        "min_cm": [float(value) for value in low],
        "max_cm": [float(value) for value in high],
        "center_cm": [float(value) for value in 0.5*(low + high)],
        "size_cm": [float(value) for value in (high - low)],
    }


def _hide_tagged_actors(game, tag):
    """Hide every actor carrying ``tag``; returns a per-class breakdown.

    Two passes, not one.  The URDF importer puts a ``RMSimRobot=`` tag on every
    visual actor it places, but the ``--instanced-visuals`` level instead hangs
    those 951 visuals on the 13 link anchors as ISM *components*, and the
    anchors are plain ``AActor``s that no ``AStaticMeshActor`` query will ever
    return.  Hiding only the first match therefore left the robot fully visible,
    which is exactly the trap docs/render_budget.md §18.4 fell into.
    ``AActor::SetActorHiddenInGame`` calls ``UpdateComponentVisibility``, so
    hiding an anchor does take its instanced components with it.
    """
    seen = {}
    for uclass in ("AStaticMeshActor", "AActor"):
        for actor in game.unreal_service.find_actors_by_tag(
                tag=tag, uclass=uclass, as_unreal_object=True):
            seen.setdefault(_handle_value(actor), (actor, uclass))
    for actor, _ in seen.values():
        actor.SetActorHiddenInGame(bNewHidden=True)
    return {
        "actors": len(seen),
        "by_class": {uclass: sum(1 for _, klass in seen.values() if klass == uclass)
                     for uclass in ("AStaticMeshActor", "AActor")},
    }


def _handle_value(value):
    """Normalize an UnrealObject or SPEAR handle for equality checks.

    Mirrors ``rm_sim_spear.spear_scene_binding._handle_value``: the UObject
    pointer comes back either as an int or as a ``0x``-prefixed string.
    """
    value = getattr(value, "uobject", value)
    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)
    return value


def _link_dump(actor):
    """World location plus actor bounds, for binding diagnostics."""
    dump = {"location_cm": [float(value) for value in views._actor_world_location(actor)]}
    try:
        # An anchor that the pose loop drives with absolute world poses must not
        # stay attached: an attached actor's move is resolved against its parent,
        # so its world location comes out one parent-height off.  Recording the
        # parent handle lets the caller name it (SPEAR exposes no object name).
        dump["attach_parent_handle"] = actor.GetAttachParentActor(as_handle=True)
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        dump["attach_parent_error"] = type(exc).__name__
    try:
        # Root mobility decides whether a move is honoured at all; a Static
        # anchor silently keeps its imported location.
        root = actor.K2_GetRootComponent(as_unreal_object=True)
        if root is not None:
            dump["root_mobility"] = str(root.Mobility.get())
            relative = root.RelativeLocation.get()
            dump["root_relative_location_cm"] = (
                [float(relative.get(key, 0.0)) for key in ("X", "Y", "Z")]
                if isinstance(relative, dict) else None)
            dump["root_absolute_location"] = [
                bool(root.bAbsoluteLocation.get()),
                bool(root.bAbsoluteRotation.get()),
                bool(root.bAbsoluteScale.get()),
            ]
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        dump["root_error"] = type(exc).__name__
    try:
        scale = actor.GetActorScale3D()
        dump["scale"] = [float(scale.get(key, 0.0)) for key in ("x", "y", "z")]
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        dump["scale_error"] = type(exc).__name__
    try:
        dump["hidden"] = int(actor.bHidden.get())
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        dump["hidden_error"] = type(exc).__name__
    try:
        bounds = actor.GetActorBounds(bOnlyCollidingComponents=False, as_dict=True)
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        dump["bounds_error"] = type(exc).__name__
        return dump
    origin = bounds.get("Origin") or {}
    extent = bounds.get("BoxExtent") or {}
    dump["bounds_origin_cm"] = [float(origin.get(key, 0.0)) for key in ("x", "y", "z")]
    dump["bounds_extent_cm"] = [float(extent.get(key, 0.0)) for key in ("x", "y", "z")]
    return dump


def _resolve_view_pose(args, viewport_desc, base_location_cm):
    """Resolve the world-fixed sensor pose.

    Default: stand off the *posed* ``base_link`` anchor, read after the first
    snapshot.  The anchor's spawn location is the level origin, not the robot:
    MuJoCo poses are applied absolute (with ``world_offset_cm``), so resolving
    the camera before the first snapshot framed the level origin instead of the
    robot.  Fallbacks: the player viewport pose, or an explicit CLI pose.
    """
    if args.camera_position_cm is not None:
        position = np.asarray(args.camera_position_cm, dtype=float)
        if args.camera_target_cm is None:
            target = position + np.array((100.0, 0.0, 0.0))
        else:
            target = np.asarray(args.camera_target_cm, dtype=float)
        source = "cli"
    elif args.frame_robot:
        if base_location_cm is None:
            raise RuntimeError("--frame-robot needs posed link anchors")
        # The standoff rules live in rm_sim_spear.view_pose: 40-60 cm above the
        # floor is a grazing view whose ground reads as haze and whose thin
        # markings read as dashes once the capture's TAA is off
        # (docs/rendering.md section 12.6).
        position, target = robot_view_pose(
            base_location_cm,
            standoff_cm=(args.camera_standoff_cm if args.camera_standoff_cm is not None
                         else view_pose.STANDOFF_CM),
            height_cm=(args.camera_height_cm if args.camera_height_cm is not None
                       else view_pose.HEIGHT_CM),
            aim_height_cm=(args.camera_aim_height_cm if args.camera_aim_height_cm is not None
                           else view_pose.AIM_HEIGHT_CM),
        )
        source = "robot"
    else:
        position, target = _viewport_camera_pose(viewport_desc)
        source = "viewport"
    print(json.dumps({
        "event": "view_pose",
        "source": source,
        "position_cm": [float(value) for value in position],
        "target_cm": [float(value) for value in target],
    }, ensure_ascii=False), flush=True)
    return position, target


def _resolve_capture_lens(args, view: str) -> Lens | None:
    """Pick the lens for this run, or None to keep the viewport projection.

    The camera the binding plan puts on ``cam_link_8`` *is* the robot's camera,
    so ``--view bound`` always gets the real lens (8 mm on a 1/1.8" sensor); the
    world-fixed ``fixed``/``main`` cameras are viewport-aligned debug views and
    keep UE's 90 degree default unless ``--robot-lens`` asks for the lens by
    hand - which is also how the two lenses get compared from one pose.
    """
    use_lens = args.robot_lens if args.robot_lens is not None else (view == "bound")
    if not use_lens:
        return None
    else:
        lens = ROBOT_CAMERA_LENS
        fstop = lens.fstop if args.lens_fstop is None else float(args.lens_fstop)
        focus_distance_cm = (lens.focus_distance_cm if args.lens_focus_cm is None
                             else float(args.lens_focus_cm))
        if (fstop, focus_distance_cm) == (lens.fstop, lens.focus_distance_cm):
            return lens
        else:
            return replace(lens, fstop=fstop, focus_distance_cm=focus_distance_cm)


def parse_args(argv=None):
    # allow_abbrev is off so a flag that has been *removed* errors instead of
    # being silently swallowed as a prefix of a surviving one.  Measured: with
    # it on, `--no-arena` (deleted with pipeline/arena_scene.py) parses as
    # `--no-arena-scene-mirror-y` and quietly flips a mirror setting.
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--user-config", type=Path, default=REPO_ROOT / "tools/spear_user_config.yaml")
    parser.add_argument("--game-executable", type=Path, default=None,
                        help="drive a cooked packaged build (the .sh that "
                             "tools/package_linux.sh archived) instead of "
                             "UnrealEditor -game; set SPEAR.LAUNCH_MODE=game")
    parser.add_argument("--rpc-timeout-seconds", type=float, default=None,
                        help="SPEAR.INSTANCE.CLIENT_INTERNAL_TIMEOUT_SECONDS; a packaged "
                             "build streams its map out of a pak and stalls on PSO "
                             "creation, so the 2.0 s default can time out during "
                             "get_game() (upstream documents this knob for exactly "
                             "that: 'set CLIENT_INTERNAL_TIMEOUT_SECONDS to be overly "
                             "conservative')")
    parser.add_argument("--plan", type=Path,
                        default=REPO_ROOT / "generated/urdf/总装/scene_binding_plan.json")
    parser.add_argument("--model", type=Path,
                        default=REPO_ROOT / "generated/urdf/总装/mujoco.urdf")
    parser.add_argument("--frames", type=int, default=None,
                        help="frames to run; default 60, or until X/Esc when "
                             "--interactive is on")
    parser.add_argument("--interactive", action="store_true",
                        help="show each captured frame in an OpenCV window and fly "
                             "the capture camera with test_urdf_views' keys "
                             "(W/S/A/D/E/Q, I/K pitch, J/L yaw, X/Esc quit). Physics "
                             "keeps stepping, so this is how you watch the robot move "
                             "from wherever you fly to. Requires --view fixed or main: "
                             "the bound camera rides cam_link_8 and cannot be flown. "
                             "Implies --save-images none unless you say otherwise")
    parser.add_argument("--camera-speed-cm", type=float, default=25.0,
                        help="--interactive camera translation per key press (Unreal cm)")
    parser.add_argument("--camera-turn-deg", type=float, default=3.0,
                        help="--interactive camera pitch/yaw change per key press")
    parser.add_argument("--display-ms", type=int, default=30,
                        help="--interactive cv2.waitKey timeout in ms; this also caps "
                             "the display rate, since the loop waits here each frame")
    parser.add_argument("--steps-per-frame", type=int, default=5)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--supersample", type=float, default=1.0,
                        help="render the capture at N x the output size and box-downsample; "
                             "the only knob that puts more samples on thin distant lines "
                             "(r.ScreenPercentage does not apply to scene captures)")
    parser.add_argument("--max-internal-pixels", type=int, default=0,
                        help="ceiling on the internal target; only ever trims the "
                             "supersampling, never renders below the output resolution")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/rm_sim_spear_physics"))
    parser.add_argument("--drive-wheels", type=float, default=6.0,
                        help="wheel velocity command in rad/s")
    parser.add_argument("--drive-joints", default=",".join(DEFAULT_WHEELS))
    parser.add_argument("--joint-damping", type=float, default=2.0)
    parser.add_argument("--shell-bodies", default=",".join(DEFAULT_SHELL_BODIES),
                        help="bodies that additionally get one box collider (mesh AABB) "
                             "on top of the four wheel cylinders. Empty keeps only wheels")
    parser.add_argument("--settle-frames", type=int, default=20,
                        help="frames of zero control before the wheels are driven, "
                             "so the robot lands on its wheels first")
    parser.add_argument("--drive-gain", type=float, default=1.0,
                        help="velocity-servo gain; 10.0 overflows QACC, 5.0 is stable "
                             "and gives ~5.6 cm of travel in a 0.6 s drive window")
    parser.add_argument("--joint-armature", type=float, default=0.01)
    parser.add_argument("--physics-timestep", type=float, default=0.001)
    parser.add_argument(
        "--arena-scene", type=Path, default=None,
        help="the MJCF world exported by the Blender blender_mujoco_terrain "
             "addon.  Opt-in by path; there is no default for somebody else's "
             "export.  See docs/rmuc26_asset_review.md for the two traps in "
             "that format",
    )
    parser.add_argument(
        "--arena-scene-mirror-y", action=argparse.BooleanOptionalAction,
        default=False,
        help="negate y while importing --arena-scene (default off).  The addon "
             "does export the level reflected in y relative to Unreal, but the "
             "pose bridge already applies that same reflection, so the file is "
             "already in the frame the bridge wants; mirroring it here negates y "
             "a second time.  Run tools/check_arena_scene.py on a new export "
             "instead of assuming -- it reports which setting that file needs",
    )
    parser.add_argument(
        "--arena-scene-drop", action="append", default=[], metavar="GEOM",
        help="delete this geom from --arena-scene; repeatable.  The addon "
             "approximates every plain 'extra collider' by a single convex hull, "
             "and one of those can cover the whole field",
    )
    parser.add_argument(
        "--arena-scene-spawn-m", type=float, nargs=3, default=None,
        metavar=("X", "Y", "Z"),
        help="where the robot starts, in the coordinates of --arena-scene.  The "
             "export has no spawn block, so this cannot be defaulted.  Required "
             "with --arena-scene",
    )
    parser.add_argument(
        "--world-offset-cm", type=float, nargs=3, default=None,
        metavar=("X", "Y", "Z"),
        help="place the MuJoCo origin inside the level; without an arena the "
             "default is 0 0 -140, where the level's floor sits (measured).  "
             "MuJoCo's own ground is an invisible plane at the model origin, so "
             "this value *is* the floor as far as the renderer is concerned: "
             "-160 buries the robot 20 cm")
    parser.add_argument("--low-vram", action="store_true",
                        help="drop Lumen/ray tracing/virtual shadows while config loads; "
                             "this is for logic-only runs, never for visual acceptance")
    parser.add_argument("--ini", action="append", default=None, metavar="NAME=VALUE",
                        help="engine setting applied while config loads, i.e. "
                             "-ini:Engine:[SystemSettings]:NAME=VALUE; repeatable, so one "
                             "renderer lever can be measured at a time")
    parser.add_argument("--window-size", type=int, nargs=2, default=None, metavar=("W", "H"),
                        help="standalone window (main viewport) size; defaults to the user "
                             "config's resx/resy. This is the screen-space cost driver, "
                             "unlike --width/--height which only size the capture target")
    parser.add_argument("--jitter-pixels", type=float, default=0.0,
                        help="sub-pixel radius for the per-sample camera jitter; with "
                             "--average-frames N>1 this is real temporal supersampling "
                             "(the engine does not jitter scene captures for us)")
    parser.add_argument("--downsample-filter", default="area",
                        help="reconstruction filter when reducing a supersampled "
                             "buffer: area (box) or gaussian")
    parser.add_argument("--downsample-sigma-px", type=float, default=0.6,
                        help="gaussian sigma in *output* pixels for --downsample-filter "
                             "gaussian")
    parser.add_argument("--blur-sigma-px", type=float, default=0.0,
                        help="blur the saved image by this sigma (in output pixels); "
                             "kept separate from --downsample-filter because blurring an "
                             "already-sampled image cannot remove aliasing")
    parser.add_argument("--warmup-frames", type=int, default=4,
                        help="frames rendered and discarded before the first saved frame "
                             "so temporal AA has history (the editor viewport always has it)")
    parser.add_argument("--average-frames", type=int, default=1,
                        help="read N consecutive frames per saved image and average them; "
                             "temporal supersampling converges thin features the way a "
                             "displayed viewport does, at zero VRAM cost")
    parser.add_argument("--publish-shm", default=None, metavar="NAME",
                        help="also publish every frame to a POSIX shared-memory ring "
                             "(/dev/shm/NAME), which is the zero-encode delivery path: "
                             "PNG costs ~22-39 ms/frame at 1440x1080 while the ring costs "
                             "a ~2 ms copy. Read it with tools/read_shared_frames.py; "
                             "combine with --save-images none for throughput runs")
    parser.add_argument("--save-images", choices=("async", "sync", "none"), default=None,
                        help="async encodes PNGs on a writer thread (the loop only pays "
                             "the queue hand-off), sync writes them inline as before, "
                             "none skips saving; trace.csv reports the cost either way. "
                             "Default: async, or none under --interactive (an unbounded "
                             "run would otherwise fill the output directory)")
    parser.add_argument("--buffering", choices=("single", "double", "triple"), default="single",
                        help="readback pipeline: single is flush-and-wait every frame "
                             "(SPEAR's default, needed for --average-frames jitter), "
                             "double enqueues then reads the same frame without a flush, "
                             "triple is fully pipelined (each mode's image lag is "
                             "recorded in _BUFFERED_LAG)")
    parser.add_argument("--console-command", action="append", default=[], dest="console_commands",
                        metavar="CMD",
                        help="execute this UE console command on iteration "
                             "--console-command-at (repeatable). This is how the "
                             "engine's own frame breakdown is pulled into the log, "
                             "e.g. --console-command 'stat dumpframe -ms=0.2 -depth=8'")
    parser.add_argument("--console-command-at", type=int, default=3, metavar="N",
                        help="iteration that runs --console-command (default 3, i.e. "
                             "after the scene has settled)")
    parser.add_argument("--print-engine-frame-time", action="store_true",
                        help="ask the capture component to log its own average frame "
                             "interval every 100 frames, i.e. the engine frame rate "
                             "without the Python loop")
    parser.add_argument("--sync-pose", action="store_true",
                        help="apply poses with direct (blocking) UFunction calls "
                             "instead of send_async; measured to remove the extra "
                             "engine frame a queued call costs the transaction")
    parser.add_argument("--pose-every", type=int, default=1, metavar="N",
                        help="apply the link poses on iterations 1, N+1, 2N+1, ... "
                             "(default 1 = every frame). Applying poses at all costs "
                             "the engine frame ~+10.6 ms whatever the calling "
                             "convention, so N=2 is the throughput lever when the "
                             "camera is not the thing being tested")
    parser.add_argument("--detach-links", action="store_true",
                        help="detach each link anchor from its URDF parent anchor "
                             "before the loop (visual meshes stay attached to their "
                             "own anchor). The imported hierarchy is preserved only "
                             "by attachment, so every pose write also re-runs "
                             "UpdateChildTransforms over the whole subtree; writing "
                             "all links as absolute world poses does not need that")
    parser.add_argument("--mujoco-viewer", action="store_true",
                        help="open mujoco.viewer.launch_passive alongside the UE loop "
                             "and sync it once per iteration, so bodies can be dragged "
                             "and the motion mirrors into UE (same pattern as SPEAR's "
                             "examples/mujoco_interop). Off by default: the GUI owns a "
                             "thread, needs a display, and takes the mouse")
    parser.add_argument("--pose-frozen", action="store_true",
                        help="measurement aid: always write the first snapshot's "
                             "poses, so the same transforms are re-issued every "
                             "frame (separates the per-frame write cost from the "
                             "cost of the actors actually moving)")
    parser.add_argument("--pose-bones", default=None, metavar="ASSET",
                        help="skeletal measurement path: load this "
                             "/Game/.../SkeletalMesh asset, spawn a "
                             "UPoseableMeshComponent for it and drive one bone per "
                             "bound link instead of moving the link actors. Pairs "
                             "with --no-pose-sync, which then leaves the imported "
                             "actors untouched so the two write paths can be "
                             "measured separately")
    parser.add_argument("--pose-links", default=None, metavar="LIST",
                        help="restrict the per-link pose RPCs to this comma-separated "
                             "list (default: every bound link); a measurement and "
                             "debug aid, the other links then keep their last pose")
    parser.add_argument("--skel-no-shadow", action="store_true",
                        help="measurement flag for --pose-bones: turn off dynamic "
                             "shadow casting on the skinned component, to separate "
                             "its own render cost from the shadow passes it adds")
    parser.add_argument("--no-bone-sync", action="store_true",
                        help="measurement flag for --pose-bones: spawn the skinned "
                             "robot but never write its bones, so the cost of "
                             "having a skinned mesh and the cost of posing one are "
                             "separable")
    parser.add_argument("--no-pose-sync", action="store_true",
                        help="measurement flag: skip the per-link pose RPCs, so the "
                             "cost of the transaction itself is separable from the "
                             "cost of the calls made inside it")
    parser.add_argument("--no-capture", action="store_true",
                        help="measurement flag: run the pose/camera transactions "
                             "without requesting a capture or reading pixels back, "
                             "so the engine's own per-frame cost is separable from "
                             "the scene capture (saved images are skipped)")
    parser.add_argument("--no-readback", action="store_true",
                        help="skip the GPU-to-CPU pixel copy (SPEAR's benchmark flag): "
                             "saved images are meaningless, frame_ms is render-only")
    parser.add_argument("--vram-poll-seconds", type=float, default=1.0,
                        help="nvidia-smi sampling interval; the peak is what matters")
    parser.add_argument("--vram-csv", type=Path, default=None,
                        help="sampling log path, default <output-dir>/vram.csv")
    parser.add_argument("--fully-load-textures", action=argparse.BooleanOptionalAction, default=None,
                        help="force every used texture to its top mip before capturing; "
                             "defaults to on unless --low-vram is set")
    parser.add_argument("--hide-tag", action="append", default=None, metavar="TAG",
                        help="hide every AStaticMeshActor with TAG before the loop; "
                             "repeatable. Use it to measure what a content subset costs")
    parser.add_argument("--dump-robot-bounds", action="store_true",
                        help="print the union world bounds of every RMSimRobot-tagged "
                             "visual, i.e. where the imported robot actually is")
    parser.add_argument("--dump-binding", action="store_true",
                        help="print each link anchor's world location and bounds after the "
                             "first snapshot; use this instead of trusting tags/diagnostics "
                             "whenever the robot is missing from a frame")
    parser.add_argument("--aim-links", default="base_link",
                        help="comma-separated link anchors whose mean world position the "
                             "robot-relative camera stands off; use the armor links to "
                             "frame the armor instead of the chassis")
    parser.add_argument("--dump-armor", action="store_true",
                        help="print each armor clone's location/bounds and the template it "
                             "came from, right after binding")
    parser.add_argument("--hide-pawn-mesh", action=argparse.BooleanOptionalAction, default=True,
                        help="hide ADefaultPawn's MeshComponent0 (the engine's "
                             "/Engine/EngineMeshes/Sphere with no material) before "
                             "capturing; the pawn sets bOwnerNoSee, so the player's own "
                             "viewport never draws it but every scene capture does "
                             "(default: on). --no-hide-pawn-mesh reproduces it")
    parser.add_argument("--frame-robot", action=argparse.BooleanOptionalAction, default=True,
                        help="stand the fixed camera off the bound base_link anchor "
                             "(default); --no-frame-robot uses the player viewport pose")
    parser.add_argument("--camera-standoff-cm", type=float, default=None,
                        help="horizontal distance from the robot for the default "
                             "framing (default: rm_sim_spear.view_pose.STANDOFF_CM, "
                             "200 cm); the camera always aims at "
                             "--camera-aim-height-cm, so the pitch follows from "
                             "standoff and height")
    parser.add_argument("--camera-height-cm", type=float, default=None,
                        help="camera height above the robot's own origin for the "
                             "default framing (default: rm_sim_spear.view_pose.HEIGHT_CM, "
                             "170 cm). The robot origin sits on the level floor "
                             "(z about -140 cm), so this is also roughly the height "
                             "above the floor: below ~100 the ground goes grazing and "
                             "the single-sample capture shows haze plus dashed "
                             "markings (docs/rendering.md section 12.6)")
    parser.add_argument("--camera-aim-height-cm", type=float, default=None,
                        help="height above the robot's origin that the default "
                             "framing looks at (default: 30)")
    parser.add_argument("--camera-position-cm", type=float, nargs=3, default=None,
                        metavar=("X", "Y", "Z"),
                        help="explicit world-fixed sensor position; overrides the "
                             "robot-relative default")
    parser.add_argument("--camera-target-cm", type=float, nargs=3, default=None,
                        metavar=("X", "Y", "Z"),
                        help="world-fixed sensor look-at target; only used together with "
                             "--camera-position-cm")
    parser.add_argument("--view", choices=("bound", "main", "fixed"), default="bound",
                        help="bound: the URDF camera created by the binding plan; "
                             "main: a temporary sensor aligned to the player viewport; "
            "fixed: a world-fixed diagnostic camera (the bound camera rides the "
            "robot, so it can never show the chassis advancing)")
    parser.add_argument("--robot-lens", action=argparse.BooleanOptionalAction, default=None,
                        help="give the capture the robot camera's physical lens "
                             "(8 mm on a 1/1.8\" sensor: FOVAngle and circle depth of "
                             "field from rm_sim_spear/lens.py) instead of the viewport's "
                             "90 degree default; default: on for --view bound, off for the "
                             "viewport-aligned debug views. --no-robot-lens on --view "
                             "bound is the 90 degree reference of the lens A/B "
                             "(docs/rendering.md section 13)")
    parser.add_argument("--lens-fstop", type=float, default=None,
                        help="aperture used for the depth of field; default: the real "
                             "f-number in rm_sim_spear.lens.ROBOT_CAMERA_LENS (f/2.4)")
    parser.add_argument("--lens-focus-cm", type=float, default=None,
                        help="focus distance in cm for the depth of field; default: the "
                             "value assumed in rm_sim_spear.lens.ROBOT_CAMERA_LENS (200); "
                             "the real assembly is the only thing that can settle it")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    # Resolve the world first: every world_offset_cm use below is absolute, so
    # the arena decision has to be made once, here, and not at the point where
    # the MuJoCo model happens to be built.
    args.arena_scene = resolve_arena_scene(args.arena_scene)
    # Resolving invents a default for the no-arena case, so "did the operator
    # pass it" has to be read before that happens.
    offset_requested = args.world_offset_cm is not None
    args.world_offset_cm = resolve_world_offset_cm(
        args.world_offset_cm, args.arena_scene)
    if args.arena_scene is not None:
        if args.arena_scene_spawn_m is None:
            raise SystemExit(
                "--arena-scene is somebody else's export and carries no spawn "
                "block, so --arena-scene-spawn-m X Y Z is required (in the "
                "scene's own coordinates)")
        if not offset_requested:
            raise SystemExit(
                "--arena-scene carries its own origin, so --world-offset-cm is "
                "required: without it the pose bridge puts the robot at the "
                "scene's origin, which is 16.5 m from where Unreal draws the "
                "level.  Run tools/check_arena_scene.py to get the value")
        args.root_pos_m = tuple(float(v) for v in args.arena_scene_spawn_m)
    else:
        args.root_pos_m = (0.0, 0.0, 0.0)
    if args.fully_load_textures is None:
        # Sharp textures and the low-VRAM preset pull on the same budget, so the
        # preset wins when it is asked for explicitly.
        args.fully_load_textures = not args.low_vram
    if args.buffering == "triple" and args.average_frames > 1:
        # Jitter moves the camera between the enqueue and the read, so the
        # pipelined read would return a different sub-pixel offset than the one
        # the averaging code believes it is summing.
        raise SystemExit("--buffering triple cannot be combined with --average-frames > 1")
    if args.interactive:
        # The bound capture is parented to cam_link_8, so flying it would move the
        # robot's own eye and destroy the one thing that view is for.  `fixed` and
        # `main` are both transient world-fixed sensors, which is what a free
        # camera needs.
        if args.view == "bound":
            raise SystemExit(
                "--interactive cannot fly --view bound: the bound camera rides "
                "cam_link_8. Use --view fixed (robot standoff, follows nothing) or "
                "--view main (viewport-aligned)")
        if args.no_capture:
            raise SystemExit("--interactive needs the capture on (drop --no-capture)")
        if args.pose_frozen:
            # The frozen-pose aid exists to prove the capture cost, and the free
            # camera exists to look at a moving robot; they cancel out.
            raise SystemExit("--interactive conflicts with --pose-frozen")
    if args.save_images is None:
        args.save_images = "none" if args.interactive else "async"
    internal = choose_internal_resolution(
        args.width, args.height, args.supersample, args.max_internal_pixels)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # The ring carries what a consumer would otherwise read out of a PNG: the
    # display image (uint8 BGR) at the output resolution.
    publisher = None
    if args.publish_shm:
        publisher = FrameRingPublisher(
            args.publish_shm, args.width, args.height, channels=3)

    config = spear.get_config(user_config_files=[str(args.user_config.resolve())])
    config.defrost()
    # Standalone: no editor viewport and no PIE world (and no metadata component).
    config.SPEAR.INSTANCE.EDITOR_LAUNCH_MODE = "game"
    if args.game_executable is not None:
        # Drive a cooked packaged build instead of `UnrealEditor -game`. That is
        # the ~/rm_sim_rune delivery shape: no editor modules, no editor world,
        # 21.4 fps vs 11.4 fps at the same 1440x1080 single-sample capture
        # (docs/rendering.md section 12). EDITOR_LAUNCH_MODE above is only read
        # for editor launches, so leaving it set is harmless.
        config.SPEAR.LAUNCH_MODE = "game"
        config.SPEAR.INSTANCE.GAME_EXECUTABLE = str(resolve_game_executable(args.game_executable))
    if args.rpc_timeout_seconds is not None:
        config.SPEAR.INSTANCE.CLIENT_INTERNAL_TIMEOUT_SECONDS = float(args.rpc_timeout_seconds)
    if args.low_vram:
        for key, value in low_vram_ini_args().items():
            config.SPEAR.INSTANCE.COMMAND_LINE_ARGS[key] = value
    for item in args.ini or []:
        name, _, value = str(item).partition("=")
        if not name or not _:
            raise SystemExit(f"--ini expects NAME=VALUE, got {item!r}")
        # Config load is the only layer that moves the startup peak: a runtime
        # set_cvar lands after the map is already resident (see docs/rendering.md).
        config.SPEAR.INSTANCE.COMMAND_LINE_ARGS[
            f"ini:Engine:[SystemSettings]:{name}"
        ] = value
    if args.window_size is not None:
        config.SPEAR.INSTANCE.COMMAND_LINE_ARGS["resx"] = int(args.window_size[0])
        config.SPEAR.INSTANCE.COMMAND_LINE_ARGS["resy"] = int(args.window_size[1])
    config.freeze()

    # Sample from before the engine launch: the map-load peak is what a tight
    # budget dies on, not the steady state.
    sampler = VramSampler(
        csv_path=args.vram_csv or (args.output_dir / "vram.csv"),
        poll_seconds=args.vram_poll_seconds,
    )
    sampler.start()
    if publisher is not None:
        print(json.dumps({
            "event": "publish_shm",
            "name": args.publish_shm,
            "path": f"/dev/shm/{args.publish_shm}",
            "width": args.width,
            "height": args.height,
            "channels": 3,
            "slots": publisher.slots,
            "reader": "tools/read_shared_frames.py",
        }, ensure_ascii=False), flush=True)
    print(json.dumps({
        "event": "internal_capture",
        "output": f"{args.width}x{args.height}",
        "internal": f"{internal.width}x{internal.height}",
        "detail": internal.describe(),
    }, ensure_ascii=False), flush=True)
    spear.configure_system(config=config)

    instance = spear.Instance(config=config)
    physics = None
    skeletal_sync = None
    skeletal_component = None
    # A one-element list, not a bool: the check runs inside the nested
    # ``_capture_body`` closure, which can mutate a captured container but
    # cannot rebind a name owned by ``main``.
    bone_check_done: list[int] = []
    try:
        game = views._get_game_world(instance, config)
        plan = load_plan(args.plan)
        capture_components: dict = {}
        binding_diagnostics: dict = {}
        with instance.begin_frame():
            link_actors = apply_scene_binding_plan(
                game, plan, capture_components=capture_components,
                binding_diagnostics=binding_diagnostics)
            viewport_desc = game.viewport_service.get_current_viewport_desc()
            detached = []
            if args.detach_links:
                for name, actor in sorted(link_actors.items()):
                    try:
                        if not actor.GetAttachParentActor(as_handle=True):
                            continue
                        actor.K2_DetachFromActor(
                            LocationRule="KeepWorld",
                            RotationRule="KeepWorld",
                            ScaleRule="KeepWorld")
                        detached.append(name)
                    except Exception as exc:  # noqa: BLE001 - diagnostics only
                        print(json.dumps({"event": "detach_failed", "link": name,
                                          "error": f"{type(exc).__name__}: {exc}"},
                                         ensure_ascii=False), flush=True)
        with instance.end_frame(single_step=True):
            pass
        print(json.dumps({"event": "bound", "links": sorted(link_actors)}, ensure_ascii=False),
              flush=True)
        if args.detach_links:
            print(json.dumps({"event": "detached_links", "count": len(detached),
                              "links": detached}, ensure_ascii=False), flush=True)
        if args.pose_bones:
            # The skeleton is spawned at runtime rather than placed by an editor
            # script, so this path never touches the saved level.  One bone per
            # bound link, written in the plan's parent-before-child order.
            skeletal_diagnostics: dict = {}
            with instance.begin_frame():
                _, skeletal_component = spawn_poseable_robot(
                    game,
                    asset_path=args.pose_bones,
                    stable_name="RMSimSkeletalRobot",
                    cast_shadow=not args.skel_no_shadow,
                    diagnostics=skeletal_diagnostics)
            with instance.end_frame(single_step=True):
                pass
            bone_order = link_application_order(plan, link_actors)
            available = [str(name) for name in (skeletal_diagnostics.get("bones") or [])]
            absent = missing_bones(bone_order, available) if available else []
            print(json.dumps({
                "event": "skeletal_bound",
                "asset": args.pose_bones,
                "bones_written": bone_order,
                "skeleton_bones": available,
                "missing_bones": absent,
            }, ensure_ascii=False), flush=True)
            if absent and available:
                raise SystemExit(
                    f"--pose-bones {args.pose_bones}: skeleton is missing {absent}; "
                    "the run would silently measure a robot that never moves")
            skeletal_sync = make_skeletal_pose_synchronizer(
                skeletal_component,
                bone_order,
                spear_math=spear.math,
                world_offset_cm=tuple(args.world_offset_cm))
        # Record the projection a viewport-aligned capture *would* inherit.
        # ``--view fixed`` never calls align_camera_with_viewport (it takes the
        # blueprint's FOVAngle), so this is the source value, not a reading of
        # the component; the component itself is reported as
        # {"event": "capture_lens"} once the capture exists.
        viewport_fov = viewport_desc.get("fov_degrees") if viewport_desc.get("is_perspective") else None
        capture_aspect = internal.width/internal.height
        viewport_aspect = float(viewport_desc.get("aspect_ratio") or capture_aspect)
        print(json.dumps({
            "event": "capture_projection",
            "viewport_fov_degrees": None if viewport_fov is None else float(viewport_fov),
            "viewport_aspect_ratio": viewport_aspect,
            "capture_aspect_ratio": capture_aspect,
            "viewport_rescaled_fov_degrees": (
                None if viewport_fov is None else rescaled_fov_deg(
                    float(viewport_fov), viewport_aspect, capture_aspect)),
            "view": args.view,
        }, ensure_ascii=False), flush=True)
        if args.hide_pawn_mesh:
            with instance.begin_frame():
                print(json.dumps({
                    "event": "pawn_mesh",
                    "hidden": _hide_default_pawn_mesh(game),
                }, ensure_ascii=False), flush=True)
            with instance.end_frame(single_step=True):
                pass

        if args.dump_armor:
            with instance.begin_frame():
                print(json.dumps({
                    "event": "armor_dump",
                    "armor_links": [name for name in sorted(link_actors)
                                    if name.startswith("armor_link")],
                    "clones": [_link_dump(actor) for actor in binding_diagnostics.get("armor_actors", [])],
                    "clone_mesh_components": binding_diagnostics.get("armor_clone_mesh_components"),
                    "robot_visual_source": binding_diagnostics.get("robot_visual_source"),
                }, ensure_ascii=False), flush=True)
            with instance.end_frame(single_step=True):
                pass

        if args.dump_robot_bounds or args.hide_tag:
            with instance.begin_frame():
                if args.dump_robot_bounds:
                    for tag in ("RMSimRobot=总装", "RMSimRobot="):
                        print(json.dumps({
                            "event": "robot_bounds",
                            **_tagged_actor_bounds(game, tag),
                        }, ensure_ascii=False), flush=True)
                for tag in args.hide_tag or []:
                    print(json.dumps({
                        "event": "hidden",
                        "tag": tag,
                        **_hide_tagged_actors(game, tag),
                    }, ensure_ascii=False), flush=True)
            with instance.end_frame(single_step=True):
                pass

        position = target = None
        # Interactive free camera: seeded from the first resolved look-at pair,
        # then driven by test_urdf_views' key handler.  `camera_dirty` is what
        # makes the sensor re-aim on the frames after a key press; without it the
        # pose RPC stays gated on `position is None` and only ever runs once.
        camera_state = None
        camera_dirty = False
        quit_requested = False
        aim_distance_cm = 0.0
        with instance.begin_frame():
            # A scripted camera renders whatever mip is already resident, so thin
            # geometry reads as dashed lines unless the top mips are forced in.
            # The merged CAD map has thousands of textures, so this spends the
            # remaining Vulkan budget on sharpness; docs/rendering.md records the
            # measured tradeoff.
            game.console_service.set_cvar(
                name="r.Streaming.FramesForFullUpdate",
                value=0 if args.fully_load_textures else 2,
            )
            game.console_service.set_cvar(
                name="r.Streaming.FullyLoadUsedTextures",
                value=1 if args.fully_load_textures else 0,
            )
        with instance.end_frame(single_step=True):
            pass
        print(json.dumps({
            "event": "input_settings",
            **_input_settings_report(game, config),
        }, ensure_ascii=False), flush=True)
        print(json.dumps({
            "event": "capture_settings",
            "fully_load_textures": bool(args.fully_load_textures),
            "low_vram": bool(args.low_vram),
        }, ensure_ascii=False), flush=True)

        if args.view == "fixed":
            # A world-fixed camera: place a transient sensor at an absolute pose
            # and never attach it, so the robot visibly moves across the frames.
            with instance.begin_frame():
                sensor_class = game.unreal_service.load_class(
                    uclass="AActor",
                    name="/SpContent/Blueprints/BP_CameraSensor.BP_CameraSensor_C",
                )
                sensor = game.unreal_service.spawn_actor(
                    uclass=sensor_class,
                    spawn_parameters={"ObjectFlags": ["RF_Transient"]},
                )
                game.unreal_service.set_stable_name_for_actor(
                    actor=sensor,
                    stable_name="RMSim/physics_view_camera",
                )
                component = game.unreal_service.get_component_by_name(
                    actor=sensor,
                    component_name="DefaultSceneRoot.final_tone_curve_hdr_",
                    uclass="USpSceneCaptureComponent2D",
                )
                if component is None:
                    raise RuntimeError("BP_CameraSensor has no USpSceneCaptureComponent2D")
                component.CaptureSource = "SCS_FinalColorLDR"
                component.bAlwaysPersistRenderingState = True
                component.Width = internal.width
                component.Height = internal.height
                # Without the viewport's post-process the capture keeps the
                # component's default exposure and the frame comes out near
                # black (measured mean 9.4/255 before this copy was added).
                if not views._copy_player_camera_post_process(game, component):
                    views._copy_viewport_post_process(component, viewport_desc)
                _configure_capture_pipeline(component, args)
                component.Initialize()
                component.initialize_sp_funcs()
                if args.camera_position_cm is not None:
                    # An explicit pose is known before the robot has been posed.
                    position, target = _resolve_view_pose(args, viewport_desc, None)
                    views._set_camera_look_at(sensor, tuple(position), tuple(target))
            with instance.end_frame(single_step=True):
                pass
        elif args.view == "main":
            with instance.begin_frame():
                sensor, component = views._spawn_main_view_sensor(
                    game, viewport_desc, internal.width, internal.height,
                    configure=lambda capture: _configure_capture_pipeline(capture, args))
                views._copy_player_camera_post_process(game, component)
            with instance.end_frame(single_step=True):
                pass
            if args.camera_position_cm is not None:
                position, target = _resolve_view_pose(args, viewport_desc, None)
                # RPCs must run inside a transaction; calling this outside one is
                # what poisoned SPEAR's frame state in the first main-view attempt.
                with instance.begin_frame():
                    views._set_camera_look_at(sensor, tuple(position), tuple(target))
                with instance.end_frame(single_step=True):
                    pass
        else:
            # The bound URDF camera is created by the binding plan. It is the one
            # capture path that already produced frames in a standalone world, so
            # the physics evidence uses it instead of a viewport-aligned sensor.
            component = capture_components.get("camera")
            if component is None:
                raise RuntimeError("binding plan produced no camera capture component")
            with instance.begin_frame():
                if not component.IsInitialized():
                    # The binding plan deliberately leaves the sensor
                    # uninitialized, so this is the last point where the readback
                    # pipeline can still be chosen.
                    _configure_capture_pipeline(component, args)
                    component.Initialize()
                    component.initialize_sp_funcs()
                component.Width = internal.width
                component.Height = internal.height
                component.CaptureSource = "SCS_FinalColorLDR"
                component.bAlwaysPersistRenderingState = True
                if not views._copy_player_camera_post_process(game, component):
                    views._copy_viewport_post_process(component, viewport_desc)
            with instance.end_frame(single_step=True):
                pass
        # The lens goes on last: it has to override both the projection the
        # capture inherited (viewport or blueprint default) and the depth of
        # field the post-process copy brought along.  Reading FOVAngle back is
        # the only place that reports what the capture actually uses
        # (docs/rendering.md section 13).
        lens = _resolve_capture_lens(args, args.view)
        with instance.begin_frame():
            if lens is not None:
                lens_report = views.apply_capture_lens(
                    component, lens, internal.width, internal.height)
            else:
                lens_report = {}
            try:
                capture_fov_deg = float(component.FOVAngle.get())
            except (AssertionError, AttributeError, KeyError, RuntimeError, TypeError):
                capture_fov_deg = None
        with instance.end_frame(single_step=True):
            pass
        print(json.dumps({
            "event": "capture_lens",
            "view": args.view,
            "lens": None if lens is None else lens.describe(),
            "fov_angle_degrees": capture_fov_deg,
            "requested_fov_angle_degrees": lens_report.get("requested_fov_angle_degrees"),
            "depth_of_field_applied": lens_report.get("depth_of_field_applied"),
        }, ensure_ascii=False), flush=True)
        # Let temporal AA build history before the first saved frame: the editor
        # viewport always has it, a fresh scene capture does not.
        views._warm_capture_history(instance, component, max(0, args.warmup_frames))

        wheels = tuple(name for name in args.drive_joints.split(",") if name)
        physics = MujocoDriver.from_xml_path(
            args.model,
            args.steps_per_frame,
            physics=PhysicsOptions(
                free_base=True,
                driven_joints=wheels,
                floor=args.arena_scene is None,
                joint_damping=args.joint_damping,
                drive_gain=args.drive_gain,
                joint_armature=args.joint_armature,
                simple_collision=True,
                shell_bodies=tuple(name for name in args.shell_bodies.split(",") if name),
                timestep=args.physics_timestep,
                arena_scene=args.arena_scene,
                arena_scene_mirror_y=args.arena_scene_mirror_y,
                arena_scene_drop_geoms=tuple(args.arena_scene_drop),
                root_pos_m=args.root_pos_m,
            ),
        )
        if args.mujoco_viewer:
            physics.open_viewer()
            print(json.dumps({"event": "mujoco_viewer", "opened": True},
                             ensure_ascii=False), flush=True)
        pose_actors = link_actors
        if args.pose_links:
            wanted = [name for name in str(args.pose_links).split(",") if name]
            missing = [name for name in wanted if name not in link_actors]
            if missing:
                raise SystemExit(f"--pose-links not bound: {missing}")
            pose_actors = {name: link_actors[name] for name in wanted}
        # Parents before children: the imported anchors are attached to their
        # URDF parent, so a child posed first is dragged by the parent's move.
        pose_sync = make_scene_pose_synchronizer(
            pose_actors, spear.math, world_offset_cm=tuple(args.world_offset_cm),
            order=link_application_order(plan, pose_actors),
            use_send_async=not args.sync_pose)
        control = (float(args.drive_wheels),) * physics.model.nu
        print(json.dumps({
            "event": "physics",
            "nq": physics.model.nq,
            "nv": physics.model.nv,
            "nu": physics.model.nu,
            "world_offset_cm": list(args.world_offset_cm),
            "arena_scene": None if args.arena_scene is None else str(args.arena_scene),
            "root_pos_m": list(args.root_pos_m),
            "wheel_speed_rad_s": args.drive_wheels,
        }, ensure_ascii=False), flush=True)

        # Triple buffering reads the image the capture pipeline produced LAG
        # iterations ago, so run that many extra iterations at the end (without
        # stepping physics) to drain the last frames out of the ring.
        lag = 0 if args.no_capture else _BUFFERED_LAG[args.buffering]
        if args.buffering != "single" and not args.no_capture:
            # Double buffering needs one priming frame (its first enqueue has no
            # staging data yet), triple buffering needs `lag` to fill its ring.
            _prime_buffered_capture(instance, component, args.buffering, lag if lag else 1)
        image_writer = AsyncImageWriter() if args.save_images == "async" else None

        run_id = uuid4().hex
        rows = []
        pending_frames: deque = deque()
        offset = tuple(float(value) for value in args.world_offset_cm)
        # `--interactive` has no frame budget: it runs until the window says quit.
        # `--frames` still wins when it is given explicitly, which is also how the
        # interactive path is tested headlessly (no key ever arrives there).
        max_frames = args.frames
        if max_frames is None and not args.interactive:
            max_frames = 60
        iterations = (count(1) if max_frames is None
                      else range(1, max_frames + lag + 1))
        frame_start = time.perf_counter()
        for iteration in iterations:
            frame_start = time.perf_counter()
            if args.mujoco_viewer and not physics.viewer_running():
                print(json.dumps({"event": "mujoco_viewer", "opened": False,
                                  "stopped_at": iteration - 1},
                                 ensure_ascii=False), flush=True)
                break
            if quit_requested:
                # The operator asked to stop.  Skipping the lag drain costs the
                # last `lag` frames of trace.csv, which is not worth stepping the
                # physics they just walked away from.
                break
            stepping = max_frames is None or iteration <= max_frames
            poses = None
            snapshot = None
            if stepping:
                # Land on the wheels before commanding them: driving from the very
                # first frame with the default pose spikes QACC and diverges.
                physics.step(
                    (0.0,) * physics.model.nu if iteration <= args.settle_frames else control)
                if args.mujoco_viewer:
                    # SPEAR's interop example syncs right after stepping; the drag
                    # perturbations land in mj_data, so read_poses() below already
                    # sees them.
                    physics.sync_viewer()
                poses = physics.read_poses()
                snapshot = Snapshot(
                    commit=FrameCommit(
                        run_id=run_id,
                        commit_id=iteration,
                        sim_tick=iteration,
                        sim_time_ns=physics.sim_time_ns,
                        physics_step=physics.physics_step,
                        state_revision=iteration,
                    ),
                    poses=poses,
                    sensors={},
                )
            physics_ms = 1000.0*(time.perf_counter() - frame_start)
            capture_start = time.perf_counter()
            frames_read: list[np.ndarray] = []
            begin_ms = end_ms = convert_ms = 0.0
            body_ms = read_ms = enqueue_ms = 0.0
            sample_offsets = _jitter_offsets(
                max(1, args.average_frames), max(0.0, args.jitter_pixels))
            for _read_index in range(max(1, args.average_frames)):
                # Bind the index as a default argument: a closure over the loop
                # variable would see the last value by the time it runs.
                frozen_poses = None

                def _capture_body(read_index=_read_index):
                    # The aim step below resolves and writes these two; without
                    # the declaration they would be body-locals and the read
                    # above them would raise UnboundLocalError.
                    nonlocal position, target, frozen_poses, camera_dirty
                    if stepping and args.jitter_pixels > 0.0 and args.view in ("fixed", "main"):
                        fov_x_deg = float(viewport_desc.get("fov_degrees") or 90.0)
                        try:
                            fov_x_deg = float(component.FOVAngle.get())
                        except Exception:  # noqa: BLE001 - fall back to the viewport FOV
                            pass
                        _set_camera_pose(
                            sensor, position, target,
                            fov_x_deg=fov_x_deg, width=internal.width, height=internal.height,
                            offset_px=sample_offsets[read_index],
                        )
                    if args.console_commands and iteration == args.console_command_at and read_index == 0:
                        for command in args.console_commands:
                            game.unreal_service.execute_console_command(command=command)
                    if stepping and read_index == 0 and pose_due(iteration, args.pose_every) and (
                            (skeletal_sync is not None and not args.no_bone_sync)
                            or not args.no_pose_sync):
                        # One snapshot per saved frame: the readback for the extra
                        # samples must not re-apply the same commit.
                        applied = snapshot
                        if args.pose_frozen:
                            # Measurement aid: keep writing the first snapshot's
                            # poses, so every frame re-issues the same transforms.
                            # This separates "cost of writing an actor transform
                            # per frame" from "cost of the actor actually moving".
                            if frozen_poses is None:
                                frozen_poses = dict(snapshot.poses)
                            applied = replace(snapshot, poses=frozen_poses)
                        if skeletal_sync is not None and not args.no_bone_sync:
                            skeletal_sync.apply_snapshot(applied)
                        if not args.no_pose_sync:
                            pose_sync.apply_snapshot(applied)
                        if (skeletal_sync is not None and skeletal_component is not None
                                and not args.no_pose_sync
                                and not bone_check_done and iteration == 1):
                            # Read the bones back out of the engine and compare
                            # them with the link anchors, which went through the
                            # same to_unreal_pose(). On a correct glTF bind pose
                            # the two agree; a mirrored axis or a missing global
                            # transform shows up here as centimetres. Runs once,
                            # on a frame the warmup already excludes. Gated on
                            # the actors actually being posed: with
                            # --no-pose-sync they never move and the comparison
                            # would just measure the world offset.
                            bone_check_done.append(1)
                            locations, read_error = read_bone_world_locations(
                                skeletal_component, bone_order, spear.math)
                            anchor_locations = {
                                name: views._actor_world_location(actor)
                                for name, actor in link_actors.items()
                            }
                            payload = {"event": "bone_vs_actor", "read_error": read_error}
                            payload.update(compare_bone_writes(locations, anchor_locations))
                            print(json.dumps(payload, ensure_ascii=False), flush=True)
                            if payload["matched"] >= 3 and payload["max_delta_cm"] > 2.0:
                                raise SystemExit(
                                    "skeletal bind does not reproduce the link anchors: "
                                    f"max delta {payload['max_delta_cm']} cm on "
                                    f"{payload['worst_link']}")
                    if stepping and args.dump_binding and iteration == 1:
                        tagged = {}
                        for tag in ("RMSimLink=base_link", "RMSimLink=link_1"):
                            try:
                                actors = game.unreal_service.find_actors_by_tag(
                                    tag=tag,
                                    uclass="AStaticMeshActor",
                                    as_unreal_object=True,
                                )
                            except Exception as exc:  # noqa: BLE001 - diagnostics only
                                tagged[tag] = {"error": type(exc).__name__}
                                continue
                            tagged[tag] = {
                                "count": len(actors),
                                "sample": [_link_dump(actor) for actor in list(actors)[:3]],
                            }
                        # ``link_dumps`` must not reuse the name ``rows``: that
                        # list holds the per-frame trace rows and is appended to
                        # after this block, so shadowing it made every
                        # ``--dump-binding`` run die on frame 2 with
                        # "'dict' object has no attribute 'append'".
                        link_dumps = {name: _link_dump(actor)
                                      for name, actor in sorted(link_actors.items())}
                        handle_to_name = {_handle_value(actor): name
                                          for name, actor in link_actors.items()}
                        for row in link_dumps.values():
                            row["attach_parent"] = handle_to_name.get(
                                _handle_value(row.pop("attach_parent_handle", None)))
                        fed = {}
                        for name in link_dumps:
                            pose = snapshot.poses.get(name)
                            if pose is not None:
                                fed[name] = [
                                    round(float(v), 2) for v in to_unreal_pose(
                                        pose, world_offset_cm=tuple(args.world_offset_cm)
                                    ).location_cm]
                        print(json.dumps({
                            "event": "binding_dump",
                            "links": link_dumps,
                            "fed_poses_cm": fed,
                            "tagged_visuals": tagged,
                        }, ensure_ascii=False), flush=True)
                    if (stepping and read_index == 0 and args.view in ("fixed", "main")
                            and (position is None or camera_dirty)):
                        if position is not None:
                            # A free-camera re-aim: the pair is already resolved,
                            # only the sensor is behind it.
                            views._set_camera_look_at(sensor, tuple(position), tuple(target))
                            camera_dirty = False
                        else:
                            # The robot-relative and viewport poses need an applied
                            # snapshot: the anchor's location only reflects the robot
                            # after apply_snapshot(), and side effects are observable
                            # within the same transaction.
                            aim = [name for name in str(args.aim_links).split(",") if name]
                            missing = [name for name in aim if name not in link_actors]
                            if missing:
                                raise RuntimeError(f"--aim-links not bound: {missing}")
                            # Prefer the committed MuJoCo pose over reading the
                            # anchor back: a read only sees the move if the pose
                            # calls of *this* transaction have already run on the
                            # game thread, which is true for send_async but not for
                            # the blocking UFunction path (--sync-pose).  The anchor
                            # lands exactly on the fed pose (docs/rendering.md 8.4),
                            # so the two sources agree when both are available.
                            base_location = np.mean(
                                [np.asarray(
                                    to_unreal_pose(
                                        snapshot.poses[name],
                                        world_offset_cm=tuple(args.world_offset_cm),
                                    ).location_cm
                                    if name in snapshot.poses
                                    else views._actor_world_location(link_actors[name]),
                                    dtype=float)
                                 for name in aim], axis=0)
                            position, target = _resolve_view_pose(args, viewport_desc, base_location)
                            views._set_camera_look_at(sensor, tuple(position), tuple(target))
                bundle, transaction_ms = _capture_transaction(
                    instance, component, args.buffering, _capture_body,
                    capture=not args.no_capture)
                begin_ms += transaction_ms["begin_ms"]
                end_ms += transaction_ms["end_ms"]
                body_ms += transaction_ms["body_ms"]
                read_ms += transaction_ms["read_ms"]
                enqueue_ms += transaction_ms["enqueue_ms"]
                if bundle is not None:
                    convert_start = time.perf_counter()
                    frames_read.append(views._as_image(bundle).astype(np.float32))
                    convert_ms += 1000.0*(time.perf_counter() - convert_start)
            capture_ms = 1000.0*(time.perf_counter() - capture_start)
            if stepping:
                pending_frames.append({
                    "frame": iteration,
                    "poses": poses,
                    "sim_time_s": physics.data.time,
                    "max_qvel": float(abs(physics.data.qvel).max()),
                    "physics_ms": physics_ms,
                    "begin_ms": begin_ms,
                    "end_ms": end_ms,
                    "body_ms": body_ms,
                    "read_ms": read_ms,
                    "enqueue_ms": enqueue_ms,
                    "convert_ms": convert_ms,
                    "capture_ms": capture_ms,
                })
            if iteration - lag < 1:
                # Still filling the ring; this readback predates frame 1.
                continue
            record = pending_frames.popleft()
            png_path = args.output_dir / f"frame_{record['frame']:04d}.png"
            save_ms = 0.0
            published_sequence = 0
            if frames_read:
                image = (frames_read[0] if len(frames_read) == 1
                         else np.mean(frames_read, axis=0))
                image = _reconstruct(
                    image, args.width, args.height,
                    filter_name=str(args.downsample_filter),
                    sigma_px=float(args.downsample_sigma_px),
                )
                if args.blur_sigma_px > 0.0:
                    image = cv2.GaussianBlur(image, (0, 0), float(args.blur_sigma_px))
                display_image = views._display_image(image.astype(np.uint8))
                save_start = time.perf_counter()
                if publisher is not None:
                    published_sequence = publisher.publish(
                        display_image,
                        commit_id=record["frame"],
                        sim_time_ns=round(record["sim_time_s"]*1e9),
                    )
                if image_writer is not None:
                    image_writer.submit(png_path, display_image)
                elif args.save_images == "sync":
                    cv2.imwrite(str(png_path), display_image)
                save_ms = 1000.0*(time.perf_counter() - save_start)
                if args.interactive:
                    if camera_state is None and position is not None:
                        camera_state = view_pose.free_camera_state(position, target)
                        aim_distance_cm = float(np.linalg.norm(
                            np.asarray(target, dtype=float) - np.asarray(position, dtype=float)))
                        print(json.dumps({
                            "event": "interactive",
                            "view": args.view,
                            "camera_speed_cm": args.camera_speed_cm,
                            "camera_turn_deg": args.camera_turn_deg,
                            "keys": "W/S/A/D/E/Q move, I/K pitch, J/L yaw, X/Esc quit",
                        }, ensure_ascii=False), flush=True)
                    try:
                        cv2.imshow("physics capture", display_image)
                        key = cv2.waitKey(max(1, args.display_ms)) & 0xFF
                    except cv2.error as exc:
                        # A headless run would otherwise wait on keys that can
                        # never arrive, in a loop with no frame budget: fatal.
                        raise SystemExit(f"--interactive needs a usable display: {exc}")
                    if key in (ord("x"), 27):
                        quit_requested = True
                    elif camera_state is not None and views._move_debug_camera(
                            camera_state,
                            key,
                            distance_cm=args.camera_speed_cm,
                            angle_deg=args.camera_turn_deg):
                        position, target = view_pose.free_camera_pose(camera_state, aim_distance_cm)
                        camera_dirty = True
            base = record["poses"]["base_link"]
            row = {
                "frame": record["frame"],
                "sim_time_s": round(record["sim_time_s"], 6),
                "base_link_x_cm": round(100.0 * base.position_m[0] + offset[0], 3),
                "base_link_y_cm": round(-100.0 * base.position_m[1] + offset[1], 3),
                "base_link_z_cm": round(100.0 * base.position_m[2] + offset[2], 3),
                "max_qvel": round(record["max_qvel"], 6),
                "phase": "settle" if record["frame"] <= args.settle_frames else "drive",
                # The wall-clock frame time the sim actually pays, split into the
                # three costs the loop has levers for.  It is the iteration that
                # completed this frame's image; under triple buffering that is
                # `lag` iterations after the frame was rendered, and the physics
                # and capture columns are the ones measured when it was.
                "frame_ms": round(1000.0*(time.perf_counter() - frame_start), 3),
                "physics_ms": round(record["physics_ms"], 3),
                # capture_ms is the whole readback window, split into its three
                # parts: begin_frame (pose RPCs, camera aim, read, enqueue),
                # end_frame (frame advance plus, for single/double, the readback
                # wait) and the float conversion of the returned image.
                "begin_ms": round(record["begin_ms"], 3),
                "end_ms": round(record["end_ms"], 3),
                # begin_frame split further: the pose/camera work, the ring
                # read, and the capture enqueue.
                "body_ms": round(record["body_ms"], 3),
                "read_ms": round(record["read_ms"], 3),
                "enqueue_ms": round(record["enqueue_ms"], 3),
                "convert_ms": round(record["convert_ms"], 3),
                "capture_ms": round(record["capture_ms"], 3),
                "save_ms": round(save_ms, 3),
                "png": (str(png_path)
                        if frames_read and args.save_images != "none" else ""),
                # Ring sequence carrying this frame, 0 when --publish-shm is off.
                "shm_sequence": published_sequence,
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        if image_writer is not None:
            image_writer.close()
            print(json.dumps({"event": "image_writer", **image_writer.summary()},
                             ensure_ascii=False), flush=True)

        trace = args.output_dir / "trace.csv"
        if rows:
            with trace.open("w", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        else:
            # Reachable when --interactive is quit before the first frame
            # completes, or when a lag-only drain finds the ring empty.
            trace = None
        print(json.dumps({"event": "done", "frames": len(rows),
                          "trace": None if trace is None else str(trace)},
                         ensure_ascii=False), flush=True)
        return 0
    finally:
        if publisher is not None:
            # Unlink on the way out so a later reader cannot attach to a dead
            # run's mapping; an already-attached reader keeps its mapping.
            publisher.close()
            publisher.unlink()
            print(json.dumps({"event": "publish_shm_closed",
                              "name": args.publish_shm}, ensure_ascii=False), flush=True)
        if args.mujoco_viewer and physics is not None:
            physics.close_viewer()
        sampler.stop()
        print(json.dumps({"event": "vram", **sampler.summary()}, ensure_ascii=False),
              flush=True)
        instance.close(force=True)


if __name__ == "__main__":
    raise SystemExit(main())
