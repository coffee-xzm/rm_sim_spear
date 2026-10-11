"""Run the MuJoCo + UE/SPEAR loop for one generated binding plan.

This is the runtime entry point.  It intentionally keeps scene creation in
the first SPEAR transaction and leaves per-frame work to Coordinator.
"""

from __future__ import annotations

import argparse
import csv
from itertools import count
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENGINE_IDLE_TIMEOUT_SECONDS = 180.0

import spear

from rm_sim_spear.arena import (
    resolve_arena_scene,
    resolve_world_offset_cm,
)
from rm_sim_spear.coordinator import Coordinator
from rm_sim_spear.mujoco_driver import (  # noqa: E402
    DEFAULT_SHELL_BODIES,
    MujocoDriver,
    PhysicsOptions,
)
from rm_sim_spear.render import SpearRenderPort
from rm_sim_spear.spear_metadata import SpearMetadataCallbacks
from rm_sim_spear.vram import low_vram_ini_args
from rm_sim_spear.spear_scene_binding import (
    apply_scene_binding_plan,
    load_plan,
    make_scene_pose_synchronizer,
)


def _object_name(obj) -> str | None:
    """Read a SPEAR object's name whether it is exposed as a property or a method."""
    name = getattr(obj, "get_name", None)
    if name is None:
        return None
    if callable(name):
        name = name()
    return str(name)


def _class_name(obj) -> str | None:
    """Read a SPEAR object's UE class name."""
    name = getattr(obj, "_class_name", None)
    return None if name is None else str(name)


def _find_metadata_component(game, camera, component_name: str):
    """Return the camera's readback metadata component, creating it if absent.

    Only the plural ``get_components_by_*`` entry points may be used here.  The
    singular ``get_component_by_class``/``get_component_by_name`` funnel through
    ``UnrealUtils::toItem``, which asserts when the result set is empty and
    leaves the whole engine in its error state, so the create fallback below
    could never run (observed as "Input vector is empty" in
    ``UnrealUtils.h:1415`` followed by a cascade of "In an error state" lines).
    The plural variants return a possibly-empty vector, which is the only safe
    way to ask "does this component already exist".
    """
    uclass = "URmSimSpearReadbackMetadataComponent"
    sp_func_uclass = "USpFuncComponent"

    def sp_func_count() -> int:
        return len(game.unreal_service.get_components_by_class(
            actor=camera, uclass=sp_func_uclass))

    sp_func_before = sp_func_count()
    source = "created"
    existing = game.unreal_service.get_components_by_class(actor=camera, uclass=uclass)
    if len(existing) > 0:
        component = existing[0]
        source = "actor_by_class"
    else:
        existing = game.unreal_service.get_components_by_name(
            actor=camera,
            component_name=component_name,
            uclass=uclass,
        )
        if len(existing) > 0:
            component = existing[0]
            source = "actor_by_name"
        else:
            component = game.unreal_service.create_scene_component_for_actor(
                uclass=uclass,
                owner=camera,
                scene_component_name=component_name,
            )
    captures = game.unreal_service.get_components_by_class(
        actor=camera, uclass="USpSceneCaptureComponent2D")
    print(json.dumps({
        "event": "metadata_component",
        "source": source,
        "name": _object_name(component),
        "sp_func_components_before": sp_func_before,
        "sp_func_components_after": sp_func_count(),
        # SPEAR's getSpFuncComponent() resolves an SpFuncComponent as a DIRECT
        # attach child of the owning component, so this list is what decides
        # whether initialize_sp_funcs() can see anything.  A component created
        # by create_scene_component_for_actor() reports [] here even though its
        # sp_func_component shows up in the actor's component list: SPEAR builds
        # it with CreateDefaultSubobject + SetupAttachment inside the
        # constructor, which only wires the attachment when the owner is
        # constructed as a class default object (i.e. from a Blueprint), not
        # when it is NewObject'd at runtime.  See docs/urdf_workflow.md.
        "metadata_children": [
            _class_name(item)
            for item in game.unreal_service.get_children_components_for_scene_component(
                parent=component, include_all_descendants=True)
        ],
        # A/B control: a Blueprint-created component does get its
        # sp_func_component as a direct attach child.
        "bp_capture_children": [
            _class_name(item)
            for item in game.unreal_service.get_children_components_for_scene_component(
                parent=captures[0], include_all_descendants=False)
        ] if captures else [],
    }, ensure_ascii=False, default=str), flush=True)
    return component


def _camera_actor(game, entry):
    identifier = entry.get("actor_path") or entry.get("actor_name")
    return game.unreal_service.find_actor_by_name(
        actor_name=identifier,
        uclass="AActor",
    )


def _get_game_world(instance, config):
    """Start PIE for an editor launch, then return the live game world.

    A standalone launch (``EDITOR_LAUNCH_MODE: game``) has no editor world at
    all: asking for it asserts inside SPEAR, so the game world is fetched
    directly, exactly like ``tools/test_urdf_views.py`` does for ``--standalone``.
    """
    standalone = (
        str(getattr(config.SPEAR.INSTANCE, "EDITOR_LAUNCH_MODE", "editor")) == "game"
    )
    if str(config.SPEAR.LAUNCH_MODE) == "editor" and not standalone:
        editor = instance.get_editor(
            warm_up=False,
            wait_for_engine_idle=True,
            wait_for_engine_idle_max_time_seconds=ENGINE_IDLE_TIMEOUT_SECONDS,
            wait_for_engine_idle_log_interval_seconds=1.0,
        )
        with instance.begin_frame():
            level_editor_subsystem = editor.unreal_service.get_editor_subsystem(
                uclass="ULevelEditorSubsystem",
            )
            level_editor_subsystem.EditorRequestBeginPlay()
        # get_game() performs worker-thread world queries immediately after
        # this transaction, so leave the engine in the normal idle state.
        # ``single_step=True`` would set the frame state to
        # "request_begin_next_frame", which those queries reject with
        # "Unexpected frame state" (see docs/architecture.md section 4.1).
        with instance.end_frame():
            pass
    return instance.get_game(
        warm_up=False,
        wait_for_world_initialized=True,
        wait_for_world_initialized_max_time_seconds=30.0,
        wait_for_world_initialized_sleep_time_seconds=1.0,
        wait_for_engine_idle=True,
        wait_for_engine_idle_max_time_seconds=ENGINE_IDLE_TIMEOUT_SECONDS,
        wait_for_engine_idle_log_interval_seconds=1.0,
    )


def _write_capture(
    directory: Path | None,
    capture,
    width: int,
    height: int,
) -> dict[str, object]:
    """Persist the raw readback and a previewable PNG when its shape is known."""
    if directory is None:
        return {"saved": False}
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"commit_{capture.commit_id:08d}"
    raw_path = stem.with_suffix(".bin")
    raw_path.write_bytes(capture.image)
    result: dict[str, object] = {
        "saved": True,
        "raw_path": str(raw_path),
    }
    expected_pixels = width * height
    if len(capture.image) not in {expected_pixels * 3, expected_pixels * 4}:
        result["png_error"] = (
            f"expected {expected_pixels * 3} or {expected_pixels * 4} bytes, "
            f"got {len(capture.image)}"
        )
        return result
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        result["png_error"] = f"OpenCV/numpy unavailable: {exc}"
        return result
    channels = len(capture.image) // expected_pixels
    image = np.frombuffer(capture.image, dtype=np.uint8).reshape(height, width, channels)
    png_path = stem.with_suffix(".png")
    if not cv2.imwrite(str(png_path), image):
        result["png_error"] = f"cv2.imwrite failed for {png_path}"
        return result
    result["png_path"] = str(png_path)
    return result


def _viewer_display_reason() -> str | None:
    """Return why the MuJoCo viewer cannot come up here, or ``None`` if it can.

    ``mujoco.viewer.launch_passive`` does not fail politely.  With no display
    GLFW cannot initialise, MuJoCo's FatalError handler prints
    ``ERROR: could not initialize GLFW`` and exits(1) from C, so no
    ``try/except`` in this process ever sees an exception (measured
    2026-10-10).  The check therefore has to happen *before* the call, which is
    why this is a preflight rather than a ``try`` around ``open_viewer()``.

    A set ``DISPLAY`` is treated as usable on purpose: under ``xvfb-run`` the
    viewer really does open and ``sync()`` works, but the process can then hang
    at exit on ``X_GLXSwapBuffers``/``GLXBadDrawable``.  That case cannot be
    told apart cheaply here, so it is documented instead of guessed at
    (docs/urdf_workflow.md, "生成清单").
    """
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    return "neither DISPLAY nor WAYLAND_DISPLAY is set"


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Split out of ``main()`` so the offline suite can assert the defaults
    (notably ``--mujoco-viewer``, which is on) without launching Unreal.
    """
    # allow_abbrev is off so a flag that has been *removed* errors instead of
    # being silently swallowed as a prefix of a surviving one.  Measured: with
    # it on, `--no-arena` (deleted with pipeline/arena_scene.py) parses as
    # `--no-arena-scene-mirror-y` and quietly flips a mirror setting.
    parser = argparse.ArgumentParser(description="Run rm_sim_spear through SPEAR",
                                     allow_abbrev=False)
    parser.add_argument("--user-config", type=Path, required=True)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("generated/urdf/总装/scene_binding_plan.json"),
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("generated/urdf/总装/mujoco.urdf"),
    )
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--steps-per-frame", type=int, default=1)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--sensor-id", default="front_camera")
    parser.add_argument("--modality", default="rgb")
    parser.add_argument("--save-dir", type=Path)
    parser.add_argument(
        "--standalone", action="store_true",
        help="pass -game to UnrealEditor: no editor viewport and no PIE world",
    )
    parser.add_argument(
        "--low-vram", action="store_true",
        help="apply the -ini:Engine:[SystemSettings] preset while config loads",
    )
    parser.add_argument(
        "--trace", type=Path,
        help="write one CSV row per frame with the MuJoCo body poses that were pushed",
    )
    parser.add_argument(
        "--drive-wheels", type=float,
        help="wheel velocity command in rad/s; enables the free base, the ground "
             "plane and the simplified collision model (the URDF alone is welded to world)",
    )
    parser.add_argument(
        "--drive-joints",
        default="link_1_joint,link_2_joint,link_3_joint,link_4_joint",
        help="joints exposed as velocity actuators when --drive-wheels is used",
    )
    parser.add_argument("--joint-damping", type=float, default=2.0)
    parser.add_argument(
        "--shell-bodies", default=",".join(DEFAULT_SHELL_BODIES),
        help="bodies that additionally get one box collider (their mesh AABB) on "
             "top of the four wheel cylinders.  Keep it to bodies whose AABB stays "
             "above the wheels: including link_5/link_7 makes the robot rest on the "
             "gimbal.  Empty string keeps only the wheels",
    )
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
        help="place the MuJoCo origin inside the level.  Without --arena-scene "
             "the default is 0 0 -140, which is where the level's floor sits "
             "(measured, see docs/rendering.md section 10).  With --arena-scene "
             "it is required: the export carries its own origin, so no default "
             "is meaningful -- tools/check_arena_scene.py computes the real value",
    )
    parser.add_argument(
        "--forever", action="store_true",
        help="loop until interrupted instead of stopping after --frames; this is "
             "the interactive editor mode (the PIE world is left paused when the "
             "loop stops, so the picture freezes even though the editor stays live)",
    )
    parser.add_argument("--physics-timestep", type=float, default=0.001)
    parser.add_argument(
        "--metadata-component",
        default="rm_sim_spear_readback_metadata",
        help="camera component path/name when it is already in the Blueprint",
    )
    parser.add_argument(
        "--mujoco-viewer", action=argparse.BooleanOptionalAction, default=True,
        help="open mujoco.viewer.launch_passive next to the UE loop and sync it "
             "once per frame, so the physics can be watched -- and bodies "
             "dragged -- alongside the rendered image.  ON BY DEFAULT, because "
             "this is the interactive entry point and the viewer is where the "
             "collision model is visible.  Headless runs must pass "
             "--no-mujoco-viewer: with no DISPLAY the viewer exits the process "
             "from C (uncatchable), and under xvfb it opens but can hang at "
             "exit",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive (or use --forever)")
    if args.forever:
        # --frames is meaningless in this mode; keep the trace bounded instead.
        args.frames = None
    if args.width < 1 or args.height < 1:
        parser.error("--width and --height must be positive")

    if not args.plan.is_file():
        parser.error(f"binding plan does not exist: {args.plan}")
    if not args.model.is_file():
        parser.error(f"MuJoCo URDF does not exist: {args.model}")
    print(f"[prepare] binding plan: {args.plan}")
    print(f"[prepare] MuJoCo URDF: {args.model}")
    if args.save_dir is None:
        print("[capture] --save-dir 未指定，图像只回读到内存，不会保存文件")

    plan = load_plan(args.plan)
    config = spear.get_config(user_config_files=[str(args.user_config.resolve())])
    config.defrost()
    if args.standalone:
        config.SPEAR.INSTANCE.EDITOR_LAUNCH_MODE = "game"
    if args.low_vram:
        for key, value in low_vram_ini_args().items():
            config.SPEAR.INSTANCE.COMMAND_LINE_ARGS[key] = value
    config.freeze()
    spear.configure_system(config=config)
    instance = spear.Instance(config=config)
    game = _get_game_world(instance, config)

    capture_components = {}
    with instance.begin_frame():
        link_actors = apply_scene_binding_plan(
            game,
            plan,
            capture_components=capture_components,
        )
        camera_entry = plan.get("camera")
        if camera_entry is None:
            raise RuntimeError("binding plan has no camera")
        camera = _camera_actor(game, camera_entry)
        capture_component = capture_components["camera"]
        viewport_desc = game.viewport_service.get_current_viewport_desc()
        game.viewport_service.align_camera_with_viewport(
            camera_sensor=camera,
            camera_components=capture_component,
            viewport_desc=viewport_desc,
            widths=args.width,
            heights=args.height,
            post_processing_components=capture_component,
        )
        # apply_scene_binding_plan() deliberately hands back the capture
        # component uninitialized (it binds with initialize=False), and the
        # read_pixels/enqueue_copy SpFuncs are only registered by Initialize().
        # Order matters, exactly as upstream documents it: align the projection
        # first so the render target is created at the final resolution, then
        # Initialize(), then let SPEAR discover the functions.
        capture_component.Initialize()
        if not getattr(capture_component, "_initialized_sp_funcs", False):
            capture_component.initialize_sp_funcs()
        metadata_component = _find_metadata_component(
            game,
            camera,
            args.metadata_component,
        )
        # Drive the component's own lifecycle, exactly like
        # capture_component.Initialize() above: it attaches its inner
        # SpFuncComponent (SPEAR resolves SpFuncs by walking AttachChildren) and
        # registers the four metadata SpFuncs.  Doing it from Python instead of
        # relying on BeginPlay() is what makes a runtime-created component work.
        metadata_component.Initialize()
        # get_unreal_object() resolves the class default object through a
        # game-thread entry point, so the handle has to be built here rather
        # than after the transaction; SetGamePaused() itself is called from
        # inside SpearRenderPort's own transactions.
        gameplay_statics = game.get_unreal_object(uclass="UGameplayStatics")
    with instance.end_frame():
        pass
    # initialize_sp_funcs() is itself a game-thread entry point -- SPEAR only
    # accepts those in "with begin_frame()" or "with end_frame()" blocks, so it
    # needs a transaction (calling it bare leaves the frame state at "idle" and
    # asserts).
    with instance.begin_frame():
        metadata_component.initialize_sp_funcs()
    with instance.end_frame():
        pass
    required_metadata_funcs = {
        "bind_frame_metadata",
        "enqueue_copy_metadata",
        "readback_metadata",
        "reset_metadata",
    }
    available_metadata_funcs = set(getattr(metadata_component, "_sp_func_names", ()) or ())
    print(json.dumps({
        "event": "metadata_sp_funcs",
        "available": sorted(available_metadata_funcs),
    }, ensure_ascii=False), flush=True)
    if not required_metadata_funcs.issubset(available_metadata_funcs):
        missing = sorted(required_metadata_funcs - available_metadata_funcs)
        raise RuntimeError(
            "metadata component is not active; add "
            "RmSimSpearReadbackMetadataComponent to the camera Blueprint "
            f"(missing SpFuncs: {missing})"
        )
    print("[scene] armor/link/camera binding complete")
    print("[capture] USpSceneCaptureComponent2D metadata callbacks ready")

    metadata = SpearMetadataCallbacks(
        metadata_component,
        sensor_id=args.sensor_id,
        modality=args.modality,
    )
    def set_game_paused(paused: bool) -> None:
        gameplay_statics.SetGamePaused(bPaused=paused)

    wheel_joints = tuple(name for name in args.drive_joints.split(",") if name)
    shell_bodies = tuple(name for name in args.shell_bodies.split(",") if name)

    arena_scene = resolve_arena_scene(args.arena_scene)
    world_offset_cm = resolve_world_offset_cm(args.world_offset_cm, arena_scene)
    if arena_scene is not None:
        if args.arena_scene_spawn_m is None:
            raise SystemExit(
                "--arena-scene is somebody else's export and carries no spawn "
                "block, so --arena-scene-spawn-m X Y Z is required (in the "
                "scene's own coordinates)")
        if args.world_offset_cm is None:
            raise SystemExit(
                "--arena-scene carries its own origin, so --world-offset-cm is "
                "required: without it the pose bridge puts the robot at the "
                "scene's origin, which is 16.5 m from where Unreal draws the "
                "level.  Run tools/check_arena_scene.py to get the value")
        # On the field, standing still.
        if args.drive_wheels is None:
            args.drive_wheels = 0.0
        root_pos_m = tuple(float(value) for value in args.arena_scene_spawn_m)
    else:
        root_pos_m = (0.0, 0.0, 0.0)
    pose_sync = make_scene_pose_synchronizer(
        link_actors, spear.math, world_offset_cm=world_offset_cm,
    )

    physics_options = None
    if args.drive_wheels is not None:
        physics_options = PhysicsOptions(
            free_base=True,
            driven_joints=wheel_joints,
            floor=arena_scene is None,
            joint_damping=args.joint_damping,
            simple_collision=True,
            shell_bodies=shell_bodies,
            timestep=args.physics_timestep,
            arena_scene=arena_scene,
            arena_scene_mirror_y=args.arena_scene_mirror_y,
            arena_scene_drop_geoms=tuple(args.arena_scene_drop),
            root_pos_m=root_pos_m,
        )
    physics = MujocoDriver.from_xml_path(
        args.model, args.steps_per_frame, physics=physics_options)
    if args.mujoco_viewer:
        reason = _viewer_display_reason()
        if reason is not None:
            # Deliberately fatal rather than "warn and continue": the whole
            # point of the default is that the window shows up, so a silent
            # downgrade would hide the very thing this flag exists for.
            raise SystemExit(
                f"--mujoco-viewer is on by default, but {reason}; "
                "headless runs must pass --no-mujoco-viewer"
            )
        physics.open_viewer()
        print(json.dumps({"event": "mujoco_viewer", "opened": True},
                         ensure_ascii=False), flush=True)
    renderer = SpearRenderPort(
        instance=instance,
        capture_component=capture_component,
        apply_snapshot=pose_sync.apply_snapshot,
        set_game_paused=set_game_paused,
        readback_metadata=metadata.readback_metadata,
        bind_frame_metadata=metadata.bind_frame_metadata,
        metadata_enqueue_copy=metadata.enqueue_copy_metadata,
        metadata_reset=metadata.reset,
        on_reset=pose_sync.reset,
        sensor_id=args.sensor_id,
        modality=args.modality,
        buffering_mode="SingleBuffered",
    )
    coordinator = Coordinator(
        physics,
        renderer,
        capture_streams=((args.sensor_id, args.modality),),
    )

    control = (0.0,) * physics.model.nu
    if physics_options is not None and args.drive_wheels is not None:
        control = (float(args.drive_wheels),) * physics.model.nu
        print(json.dumps({
            "event": "physics",
            "free_base": True,
            # Not the constant it used to be: "floor" said True even with an
            # arena, which is exactly the case where there is no plane floor.
            # A log alone should answer "which world did this run stand on".
            "floor": arena_scene is None,
            "simple_collision": True,
            "shell_bodies": list(shell_bodies),
            "arena_scene": None if arena_scene is None else str(arena_scene),
            "arena_scene_mirror_y": bool(args.arena_scene_mirror_y),
            "arena_scene_drop_geoms": list(args.arena_scene_drop),
            "world_offset_cm": list(world_offset_cm),
            "driven_joints": list(wheel_joints),
            "wheel_speed_rad_s": args.drive_wheels,
            "nq": physics.model.nq,
            "nv": physics.model.nv,
            "nu": physics.model.nu,
        }, ensure_ascii=False), flush=True)
    trace_rows = []
    completed = 0
    frames = count(1) if args.frames is None else range(1, args.frames + 1)
    try:
        for frame_index in frames:
            if args.mujoco_viewer and not physics.viewer_running():
                # Closing the viewer window is the operator saying stop -- the
                # same contract tools/run_physics_sim.py gives its --interactive
                # window, so there is no second thing to learn.
                print(json.dumps({"event": "mujoco_viewer", "opened": False,
                                  "stopped_at": frame_index - 1},
                                 ensure_ascii=False), flush=True)
                break
            result = coordinator.step(control)
            if args.mujoco_viewer:
                # Same pattern as SPEAR's examples/mujoco_interop and as
                # tools/run_physics_sim.py: one sync() per iteration, right
                # after the physics step.  A body dragged in the window writes
                # into mj_data, so the next frame's poses already carry it and
                # UE mirrors it with no extra plumbing.
                physics.sync_viewer()
            # Record what the simulation actually did, so "the robot advances
            # under physics" is checkable from numbers and not only from pixels.
            poses = physics.read_poses()
            base = poses.get("base_link")
            row = {
                "frame": frame_index + 1,
                "sim_time_s": round(physics.data.time, 6),
                "base_link_x": round(base.position_m[0], 6) if base else None,
                "base_link_y": round(base.position_m[1], 6) if base else None,
                "base_link_z": round(base.position_m[2], 6) if base else None,
                "base_link_qw": round(base.quaternion_wxyz[0], 6) if base else None,
                "base_link_qx": round(base.quaternion_wxyz[1], 6) if base else None,
                "base_link_qy": round(base.quaternion_wxyz[2], 6) if base else None,
                "base_link_qz": round(base.quaternion_wxyz[3], 6) if base else None,
                "wheel_1_angle": round(float(physics.data.qpos[physics.model.jnt_qposadr[
                    physics.model.joint("link_1_joint").id]]), 6)
                    if physics.model.njnt and physics.model.joint("link_1_joint").id >= 0 else None,
                "max_body_speed": round(float(max(abs(physics.data.qvel))), 6)
                    if physics.data.qvel.size else 0.0,
            }
            if args.trace is not None:
                trace_rows.append(row)
            for observation in result.observations:
                capture = observation.capture
                saved = _write_capture(args.save_dir, capture, args.width, args.height)
                print(json.dumps({
                    "commit_id": capture.commit_id,
                    "ue_frame_id": capture.ue_frame_id,
                    "sensor_id": capture.sensor_id,
                    "modality": capture.modality,
                    "image_bytes": len(capture.image),
                    "image_readback": True,
                    **saved,
                }, ensure_ascii=False))
            completed = frame_index
    except KeyboardInterrupt:
        pass
    finally:
        # launch_passive() owns a daemon thread and a window; close it here so
        # a window cannot outlive the loop it was mirroring.  instance.close()
        # is deliberately still not called -- the editor is left standing.
        physics.close_viewer()
    print(json.dumps({
        "event": "loop_done",
        "frames": completed,
        "paused_at_exit": True,
        "note": "the UE game world is left paused at the last commit, so the "
                "simulation picture freezes while the editor stays interactive; "
                "use --forever to keep stepping until you interrupt",
    }, ensure_ascii=False), flush=True)
    if args.trace is not None and trace_rows:
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        with args.trace.open("w", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trace_rows[0]))
            writer.writeheader()
            writer.writerows(trace_rows)
        print(json.dumps({"trace": str(args.trace), "rows": len(trace_rows)}, ensure_ascii=False),
              flush=True)


if __name__ == "__main__":
    main()
