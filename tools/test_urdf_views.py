"""Read the main viewport and bound URDF camera in one SPEAR run.

This is the last visual integration check before adding control logic.  The
URDF is the generated MuJoCo model; the UE side uses the generated binding
plan, including the four runtime clones of the aggregate armor actor.
"""

from __future__ import annotations

import argparse
import json
from itertools import count
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENGINE_IDLE_TIMEOUT_SECONDS = 180.0

import cv2
import numpy as np
import spear
import math

from rm_sim_spear.capture_resolution import (
    DEFAULT_SUPERSAMPLE,
    NO_PIXEL_BUDGET,
    choose_internal_resolution,
)
from rm_sim_spear.model import FrameCommit, Pose, Snapshot
from rm_sim_spear.mujoco_driver import MujocoDriver
from rm_sim_spear.pose_sync import check_attachment, unverifiable_attachment_links
from rm_sim_spear.spear_scene_binding import (
    apply_scene_binding_plan,
    load_plan,
    make_scene_pose_synchronizer,
)
from rm_sim_spear.vram import (
    UeLogWatcher,
    VramSampler,
    device_local_budget_mb,
    evaluate_preflight,
    find_ue_processes,
    find_crash_signatures,
    format_preflight,
    kill_processes,
    low_vram_ini_args,
    query_vram,
    query_vram_processes,
    resolve_ue_log_path,
)

# Runtime cvar overrides applied inside the game world, logged back so a silent
# priority rejection in IConsoleManager cannot go unnoticed.
RUNTIME_MEMORY_CVARS = (
    "r.RayTracing",
    "r.DynamicGlobalIlluminationMethod",
    "r.ReflectionMethod",
    "r.Shadow.Virtual.Enable",
)


def _read_log_tail(path: Path, lines: int = 400) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return ""


def _spawn_main_view_sensor(game, viewport_desc, width: int, height: int, configure=None):
    """Create a temporary SPEAR sensor matching the current main viewport.

    ``configure`` runs after the capture properties that ``Initialize()`` reads
    are set and before it is called, so a caller can pick the readback pipeline
    (buffering mode, benchmark flags) without duplicating this spawn sequence.
    """
    spear.log("[test_urdf_views] spawning main-view camera sensor")
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
        stable_name="RMSim/main_view_camera",
    )
    component = game.unreal_service.get_component_by_name(
        actor=sensor,
        component_name="DefaultSceneRoot.final_tone_curve_hdr_",
        uclass="USpSceneCaptureComponent2D",
    )
    if component is None:
        raise RuntimeError("BP_CameraSensor has no USpSceneCaptureComponent2D")
    # Keep the temporary viewport capture identical to the Rune SDK camera.
    component.CaptureSource = "SCS_FinalColorLDR"
    component.bAlwaysPersistRenderingState = True
    spear.log("[test_urdf_views] aligning main-view camera")
    game.viewport_service.align_camera_with_viewport(
        camera_sensor=sensor,
        camera_components=component,
        viewport_desc=viewport_desc,
        widths=width,
        heights=height,
    )
    _copy_viewport_post_process(component, viewport_desc)
    if configure is not None:
        configure(component)
    spear.log("[test_urdf_views] initializing main-view capture component")
    component.Initialize()
    component.initialize_sp_funcs()
    spear.log("[test_urdf_views] main-view capture component ready")
    return sensor, component


def _set_capture_resolution(component, width: int, height: int, *, initialize: bool = True) -> None:
    """Render at ``width``x``height`` while the saved image keeps the output size."""
    if component.IsInitialized():
        component.Terminate()
        component._initialized_sp_funcs = False
    component.Width = width
    component.Height = height
    if initialize:
        component.Initialize()
        component.initialize_sp_funcs()


def _release_capture_component(component) -> None:
    """Release a capture target whether or not SPEAR SpFuncs were registered."""
    if getattr(component, "_initialized_sp_funcs", False):
        component.terminate_sp_funcs()
    if component.IsInitialized():
        component.Terminate()
    component._initialized_sp_funcs = False


def _warm_capture_history(instance, component, frames: int) -> None:
    """Render and discard a few frames so temporal AA has valid history."""
    for _ in range(max(0, int(frames))):
        with instance.begin_frame():
            component.bCaptureGpuNextRender = True
        with instance.end_frame(single_step=True):
            # Consume the readback before the next capture. This keeps the
            # render target and its temporal state alive without retaining
            # extra CPU images.
            component.read_pixels()


def _configure_capture_projection(component, viewport_desc, width: int, height: int) -> None:
    """Apply the viewport projection without moving a bound camera actor."""
    component.Width = width
    component.Height = height
    if viewport_desc["is_perspective"]:
        target_aspect = width / height
        half_fov = math.radians(viewport_desc["fov_degrees"]) / 2.0
        adjusted = 2.0 * math.degrees(math.atan(
            math.tan(half_fov) * target_aspect / viewport_desc["aspect_ratio"]))
        component.ProjectionType = "Perspective"
        component.FOVAngle = adjusted
    else:
        component.ProjectionType = "Orthographic"
        component.OrthoWidth = viewport_desc["ortho_width"]


def _as_image(data_bundle) -> np.ndarray:
    image = data_bundle["arrays"]["data"]
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] not in (3, 4):
        raise RuntimeError(f"unexpected camera image shape: {array.shape}")
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    return array.copy()


def _downsample(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if image.shape[1] == width and image.shape[0] == height:
        return image
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def _display_image(image: np.ndarray) -> np.ndarray:
    if image.shape[2] == 4:
        return image[:, :, :3]
    return image


def _viewport_pose(viewport_desc: dict) -> dict[str, object]:
    """Copy a viewport pose into a mutable, pause-safe debug-camera state."""
    location = viewport_desc["camera_location"]
    rotation = viewport_desc["camera_rotation"]
    location = {str(key).lower(): value for key, value in location.items()}
    rotation = {str(key).lower(): value for key, value in rotation.items()}
    return {
        "location": np.array(
            [float(location["x"]), float(location["y"]), float(location["z"])],
            dtype=float,
        ),
        "pitch": float(rotation.get("pitch", 0.0)),
        "yaw": float(rotation.get("yaw", 0.0)),
        "roll": float(rotation.get("roll", 0.0)),
    }


def _apply_debug_camera_pose(camera: object, state: dict[str, object]) -> None:
    """Move the temporary capture actor directly, even while UE is paused."""
    location = np.asarray(state["location"], dtype=float)
    camera.K2_SetActorLocationAndRotation(
        NewLocation={
            "X": float(location[0]),
            "Y": float(location[1]),
            "Z": float(location[2]),
        },
        NewRotation={
            "Pitch": float(state["pitch"]),
            "Yaw": float(state["yaw"]),
            "Roll": float(state["roll"]),
        },
    )


def _move_debug_camera(state: dict[str, object], key: int, *, distance_cm: float,
                       angle_deg: float) -> bool:
    """Apply one keyboard command to a pause-safe free camera.

    The old implementation injected DefaultPawn axes.  Those axes call
    movement code that is only consumed by a Pawn tick, so they appear to do
    nothing while the game world is paused.  Updating this transient capture
    actor directly keeps simulation and rendering pause semantics independent.
    """
    if key < 0 or key == 255:
        return False
    try:
        command = chr(key).lower()
    except (TypeError, ValueError):
        return False
    location = np.asarray(state["location"], dtype=float)
    yaw = math.radians(float(state["yaw"]))
    pitch = math.radians(float(state["pitch"]))
    forward = np.array(
        [math.cos(pitch) * math.cos(yaw),
         math.cos(pitch) * math.sin(yaw),
         math.sin(pitch)],
        dtype=float,
    )
    right = np.array([-math.sin(yaw), math.cos(yaw), 0.0], dtype=float)
    if command == "w":
        location += forward * distance_cm
    elif command == "s":
        location -= forward * distance_cm
    elif command == "a":
        location -= right * distance_cm
    elif command == "d":
        location += right * distance_cm
    elif command == "e":
        location[2] += distance_cm
    elif command == "q":
        location[2] -= distance_cm
    elif command == "i":
        state["pitch"] = float(np.clip(float(state["pitch"]) + angle_deg, -89.0, 89.0))
    elif command == "k":
        state["pitch"] = float(np.clip(float(state["pitch"]) - angle_deg, -89.0, 89.0))
    elif command == "j":
        state["yaw"] = float(state["yaw"]) - angle_deg
    elif command == "l":
        state["yaw"] = float(state["yaw"]) + angle_deg
    else:
        return False
    state["location"] = location
    return True


def _copy_viewport_post_process(component, viewport_desc) -> None:
    """Match the player camera's post-process volume on a scene capture."""
    volumes = viewport_desc.get("post_process_volumes", ())
    if volumes:
        component.PostProcessSettings = volumes[0].Settings.get()
        component.PostProcessBlendWeight = 1.0


def _copy_player_camera_post_process(game, component) -> bool:
    """Copy the fully blended PlayerCameraManager post process to a capture.

    Rune's SDK path copies ``FMinimalViewInfo::PostProcessSettings`` after
    ``SetCameraView``.  Copying only the first level volume is insufficient for
    a bound URDF camera when several volumes or camera overrides are active.
    """
    try:
        gameplay_statics = game.get_unreal_object(uclass="UGameplayStatics")
        controller = gameplay_statics.GetPlayerController(PlayerIndex=0)
        pov = controller.PlayerCameraManager.ViewTarget.POV.get()
    except (AssertionError, AttributeError, KeyError, RuntimeError, TypeError):
        return False
    if not isinstance(pov, dict):
        return False
    normalized = {str(key).lower(): value for key, value in pov.items()}
    settings = normalized.get("postprocesssettings")
    if settings is None:
        settings = normalized.get("post_process_settings")
    if settings is None:
        return False
    component.PostProcessSettings = settings
    weight = normalized.get("postprocessblendweight")
    if weight is None:
        weight = normalized.get("post_process_blend_weight")
    if weight is not None:
        component.PostProcessBlendWeight = float(weight)
    return True


def apply_capture_lens(component, lens, width: int, height: int) -> dict[str, object]:
    """Give a scene capture a physical lens: projection plus circle depth of field.

    A capture otherwise inherits the viewport projection, which in standalone
    ``-game`` is UE's 90 degree default - a wide debug lens, not the ~48 degree
    lens on the robot (``rm_sim_spear/lens.py``, ``docs/rendering.md`` section
    13).  Depth of field is merged into whatever the post-process copy produced,
    so this has to run *after* ``_copy_viewport_post_process`` /
    ``_copy_player_camera_post_process``; the merge is case-insensitive because
    the settings struct comes back with engine-cased keys from ``.get()``.
    """
    aspect = float(width)/float(height)
    fov_deg = lens.fov_angle_deg(aspect)
    component.ProjectionType = "Perspective"
    component.FOVAngle = fov_deg
    overrides = lens.ue_post_process_overrides()
    try:
        settings = component.PostProcessSettings.get()
    except (AssertionError, AttributeError, KeyError, RuntimeError, TypeError):
        settings = None
    if isinstance(settings, dict):
        engine_keys = {str(key).lower(): key for key in settings}
        for name, value in overrides.items():
            settings[engine_keys.get(name.lower(), name)] = value
        component.PostProcessSettings = settings
        depth_of_field_applied = True
    else:
        depth_of_field_applied = False
    return {
        "requested_fov_angle_degrees": fov_deg,
        "depth_of_field_applied": depth_of_field_applied,
    }


def _set_camera_look_at(camera, position_cm, target_cm=(0.0, 0.0, 0.0)) -> dict[str, object]:
    """Place a UE camera at a deterministic robot-centred diagnostic pose."""
    position = np.asarray(position_cm, dtype=float)
    target = np.asarray(target_cm, dtype=float)
    forward = target - position
    forward /= np.linalg.norm(forward)
    up_world = np.asarray((0.0, 0.0, 1.0), dtype=float)
    right = np.cross(up_world, forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    rotation = np.column_stack((forward, right, up))
    camera.K2_SetActorLocationAndRotation(
        NewLocation={"X": float(position[0]), "Y": float(position[1]), "Z": float(position[2])},
        NewRotation=spear.math.to_spear_rotator_from_numpy_matrix(numpy_matrix=rotation),
    )
    # Return the same pose in the representation used by the pause-safe
    # keyboard controller.  This avoids querying the actor again through RPC.
    return {
        "location": position.copy(),
        "pitch": float(math.degrees(math.atan2(forward[2], np.linalg.norm(forward[:2])))),
        "yaw": float(math.degrees(math.atan2(forward[1], forward[0]))),
        "roll": 0.0,
    }


def _get_game_world(instance, config):
    """Start PIE when using an editor launch, then return the live game world."""
    uses_editor_pie = (
        str(config.SPEAR.LAUNCH_MODE) == "editor"
        and str(config.SPEAR.INSTANCE.EDITOR_LAUNCH_MODE) == "editor"
    )
    if uses_editor_pie:
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


def _end_editor_play(instance, config) -> bool:
    """End PIE before closing a launched Unreal Editor process.

    ``Instance.close()`` requests process exit.  In editor launch mode that
    request can race ``UEditorEngine::EndPlayMap()`` while the level viewport
    is being destroyed, which is a known crash path in UE 5.8.  End PIE through
    the editor subsystem first so the game world is torn down while the editor
    is still fully alive.
    """
    if not (
        str(config.SPEAR.LAUNCH_MODE) == "editor"
        and str(config.SPEAR.INSTANCE.EDITOR_LAUNCH_MODE) == "editor"
    ):
        return True
    try:
        spear.log("[test_urdf_views] requesting EditorRequestEndPlay")
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
            level_editor_subsystem.EditorRequestEndPlay()
        # The following world-registry query requires the normal idle state.
        with instance.end_frame():
            pass
        # EndPlay is asynchronous. Do not ask the process to exit until SPEAR's
        # world registry confirms that the PIE world is gone.
        for _ in range(60):
            world_descs = instance.world_registry_service.get_world_descs()
            if not any(desc.is_game_world and desc.is_playing for desc in world_descs.values()):
                spear.log("[test_urdf_views] PIE world ended")
                return True
            instance.step(num_frames=1)
        spear.log("[test_urdf_views] PIE world did not end within 60 frames")
        return False
    except Exception as exc:
        spear.log(f"[test_urdf_views] failed to end editor play session: {exc}")
        return False


def _save_frame(output_dir: Path, index: int, main_image: np.ndarray, bound_image: np.ndarray) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    main_stem = output_dir / f"main_view_{index:06d}"
    bound_stem = output_dir / f"bound_camera_{index:06d}"
    montage_stem = output_dir / f"comparison_{index:06d}"
    np.save(main_stem.with_suffix(".npy"), main_image)
    np.save(bound_stem.with_suffix(".npy"), bound_image)
    main_path = main_stem.with_suffix(".png")
    bound_path = bound_stem.with_suffix(".png")
    montage_path = montage_stem.with_suffix(".png")
    cv2.imwrite(str(main_path), main_image)
    cv2.imwrite(str(bound_path), bound_image)
    montage = np.concatenate(
        [_display_image(main_image), _display_image(bound_image)],
        axis=1,
    )
    cv2.imwrite(str(montage_path), montage)
    return {
        "main_png": str(main_path),
        "bound_png": str(bound_path),
        "comparison_png": str(montage_path),
        "main_npy": str(main_stem.with_suffix(".npy")),
        "bound_npy": str(bound_stem.with_suffix(".npy")),
    }


def _save_single_frame(
    output_dir: Path,
    index: int,
    view_name: str,
    image: np.ndarray,
) -> dict[str, str]:
    """Save one active continuous-mode view without creating a montage."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_dir / f"{view_name}_{index:06d}"
    np.save(stem.with_suffix(".npy"), image)
    png_path = stem.with_suffix(".png")
    cv2.imwrite(str(png_path), image)
    return {
        f"{view_name}_png": str(png_path),
        f"{view_name}_npy": str(stem.with_suffix(".npy")),
    }



def _actor_world_location(actor) -> tuple[float, float, float]:
    """Read an actor's world location in Unreal centimeters.

    SPEAR returns an FVector as a dict, with either capitalized or lower-case
    keys depending on the conversion path.
    """
    location = actor.K2_GetActorLocation()
    if isinstance(location, dict):
        return (
            float(location.get("X", location.get("x", 0.0))),
            float(location.get("Y", location.get("y", 0.0))),
            float(location.get("Z", location.get("z", 0.0))),
        )
    return (float(location.x), float(location.y), float(location.z))


def _resolve_binding_target(game, target):
    """Accept either a live actor or a stable name for a bound target."""
    if isinstance(target, str):
        matches = game.unreal_service.find_actors_by_name_as_dict(
            actor_name=target,
            uclass="AActor",
        )
        if not matches:
            raise RuntimeError(f"binding target not found by stable name: {target!r}")
        return next(iter(matches.values()))
    return target


def _imported_armor_targets(instance, game, plan, link_actors) -> dict[str, object]:
    """Map ``armor_link_*`` to an imported visual actor that must follow it.

    With ``scene_bindings.armor.enabled=false`` there is no clone to verify:
    the armor visual is a per-link ``ue_asset`` actor that
    ``tools/import_urdf_visuals.py`` attached to the link anchor and tagged
    ``RMSimLink=<link>``.  One representative visual per link is enough -- the
    verifier only asserts that the anchor-to-visual offset survives a pose
    change.  The tag lookup is game-thread work, so it needs a transaction.
    """
    cloned_links = {entry["link"] for entry in plan.get("armor", [])}
    wanted = [
        link for link in sorted(link_actors)
        if link not in cloned_links and link.startswith("armor_link")
    ]
    if not wanted:
        return {}
    targets: dict[str, object] = {}
    with instance.begin_frame():
        for link in wanted:
            try:
                matches = game.unreal_service.find_actors_by_tag(
                    tag=f"RMSimLink={link}",
                    uclass="AStaticMeshActor",
                    as_unreal_object=True,
                )
            except (AttributeError, TypeError):
                # Lightweight doubles have no tag API; skip instead of failing
                # the whole verification run.
                break
            actor = next(iter(matches), None) if matches else None
            if actor is not None:
                targets[link] = actor
    with instance.end_frame(single_step=True):
        pass
    return targets


def _measure_bindings(instance, game, targets, link_actors) -> dict[str, tuple[tuple[float, float, float], ...]]:
    """Read anchor/attached world locations inside one SPEAR transaction.

    Game-thread work is rejected outside begin_frame()/end_frame(), so this has
    to be a transaction even though it only reads state.
    """
    with instance.begin_frame():
        measured = {
            link: (
                _actor_world_location(link_actors[link]),
                _actor_world_location(_resolve_binding_target(game, target)),
            )
            for link, target in targets.items()
        }
    with instance.end_frame():
        pass
    return measured


def _verify_link_attachment(instance, game, gameplay_statics, pose_sync, physics,
                            targets, link_actors, run_id, log) -> dict:
    """Prove every bound visual follows its own link anchor under a pose change.

    The poses are deliberately perturbed instead of simulated, so the result
    does not depend on MuJoCo dynamics: an attached actor must keep a constant
    offset to its anchor while the anchor moves.
    """
    baseline = _measure_bindings(instance, game, targets, link_actors)
    poses_a = physics.read_poses()
    poses_b = dict(poses_a)
    for index, link in enumerate(sorted(targets)):
        pose = poses_b.get(link)
        if pose is None:
            continue
        offset_m = 0.05 * (index + 1)
        poses_b[link] = Pose(
            position_m=(pose.position_m[0], pose.position_m[1], pose.position_m[2] + offset_m),
            quaternion_wxyz=pose.quaternion_wxyz,
        )

    def apply_poses(poses: dict, commit_id: int) -> None:
        snapshot = Snapshot(
            FrameCommit(
                run_id=run_id,
                commit_id=commit_id,
                sim_tick=commit_id,
                sim_time_ns=0,
                physics_step=1,
                state_revision=commit_id,
            ),
            poses,
            {},
        )
        with instance.begin_frame():
            gameplay_statics.SetGamePaused(bPaused=False)
            pose_sync.apply_snapshot(snapshot)
        with instance.end_frame(single_step=True):
            pass
        with instance.begin_frame():
            gameplay_statics.SetGamePaused(bPaused=True)
        with instance.end_frame():
            pass

    apply_poses(poses_b, 900001)
    perturbed = _measure_bindings(instance, game, targets, link_actors)
    checks = check_attachment(baseline, perturbed)
    unverifiable = unverifiable_attachment_links(baseline, perturbed)
    report = {
        "event": "link_attachment_verification",
        "checks": [check.as_dict() for check in checks],
        "unverifiable_links": list(unverifiable),
        "all_ok": all(check.ok for check in checks) and not unverifiable and bool(checks),
    }
    print(json.dumps(report, ensure_ascii=False), flush=True)
    # Leave the world in the measured baseline pose for any later capture.
    apply_poses(poses_a, 900002)
    if not report["all_ok"]:
        raise RuntimeError(f"link attachment verification failed: {report}")
    log(f"[verify] {len(checks)} bound links follow their anchors")
    return report



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-config", type=Path, default=Path("tools/spear_user_config.yaml"))
    parser.add_argument("--plan", type=Path, default=Path("generated/urdf/总装/scene_binding_plan.json"))
    parser.add_argument("--model", type=Path, default=Path("generated/urdf/总装/mujoco.urdf"))
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument(
        "--continuous", action="store_true",
        help="持续采集并显示；按 x 或 Esc 退出，物理保持暂停以便调试视角",
    )
    parser.add_argument(
        "--dual-view", action="store_true",
        help="持续模式同时保留主视角和绑定相机；默认持续模式一次只渲染一个视角",
    )
    parser.add_argument("--steps-per-frame", type=int, default=1)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument(
        "--supersample", type=float, default=DEFAULT_SUPERSAMPLE,
        help="render-target scale relative to --width/--height; 1.0 matches the "
             "Rune SDK (capture target == output), 2.0 is true supersampling. "
             "A --max-internal-pixels budget only trims it, never below 1.0",
    )
    parser.add_argument(
        "--max-internal-pixels", type=int, default=NO_PIXEL_BUDGET,
        help="optional ceiling on pixels per capture target (readback/CPU budget, "
             "not the GPU memory lever); 0 = no ceiling",
    )
    parser.add_argument(
        "--bound-max-internal-pixels", type=int,
        help="bound-camera pixel budget; may exceed the main budget with sequential capture",
    )
    parser.add_argument(
        "--keep-expensive-rendering", action="store_true",
        help="keep Lumen/ray tracing/virtual shadows; high resolutions may exhaust VRAM",
    )
    parser.add_argument(
        "--fully-load-textures", action="store_true",
        help="force all used textures resident and wait for engine idle (high VRAM use)",
    )
    parser.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=30.0,
        help="SPEAR RPC timeout used during heavy PIE scene binding",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/rm_sim_spear_views"))
    parser.add_argument(
        "--no-save", action="store_true",
        help="continuous mode只显示，不把每一帧写入磁盘",
    )
    parser.add_argument("--display", action="store_true", help="show an OpenCV window while capturing")
    parser.add_argument("--display-ms", type=int, default=100)
    parser.add_argument(
        "--capture-warmup-frames", type=int, default=4,
        help="discarded captures used to warm temporal AA after target initialization",
    )
    parser.add_argument(
        "--continuous-fps", type=float, default=15.0,
        help="continuous-mode frame-rate cap; limits GPU readback pressure",
    )
    parser.add_argument(
        "--camera-speed-cm", type=float, default=50.0,
        help="continuous-mode camera translation per key press (Unreal cm)",
    )
    parser.add_argument(
        "--camera-turn-deg", type=float, default=3.0,
        help="continuous-mode camera pitch/yaw change per key press",
    )
    parser.add_argument(
        "--sequential-capture", action="store_true",
        help="capture main and bound views one at a time to reduce Vulkan peak memory",
    )
    parser.add_argument(
        "--standalone", action="store_true",
        help="用 UnrealEditor -game 独立运行，不启动编辑器/PIE（降低显存占用）",
    )
    parser.add_argument(
        "--low-vram", action="store_true",
        help="用 -ini:Engine:[SystemSettings] 在关卡加载前落低显存渲染预设（PIE 内改 cvar 已经太晚）",
    )
    parser.add_argument(
        "--min-free-vram-mb", type=int, default=1200,
        help="启动前 nvidia-smi 空闲显存低于该值时直接中止；0 表示关闭该闸门",
    )
    parser.add_argument(
        "--vram-poll-seconds", type=float, default=1.0,
        help="显存采样周期；0 表示不采样",
    )
    parser.add_argument(
        "--vram-csv", type=Path,
        help="显存采样记录路径，默认 <output-dir>/vram.csv",
    )
    parser.add_argument(
        "--ue-log", type=Path,
        help="崩溃诊断用的 Unreal 日志，默认 <project>/Saved/Logs/<project>.log",
    )
    parser.add_argument(
        "--kill-stale-editor", action="store_true",
        help="启动前 SIGKILL 掉残留的 UnrealEditor（否则 SPEAR 会连到旧实例）",
    )
    parser.add_argument(
        "--no-fail-fast", action="store_true",
        help="日志或进程已经显示 UE 崩溃时，不要立即退出，而是继续等 RPC 超时",
    )
    parser.add_argument(
        "--window-size", type=int, nargs=2, metavar=("WIDTH", "HEIGHT"),
        help="覆盖 UE 窗口启动尺寸（resx/resy）；独立运行时让窗口本身按该分辨率显示",
    )
    parser.add_argument(
        "--verify-armor-links", action="store_true",
        help="采集后用扰动位姿验证每个装甲克隆与机器人视觉仍跟随各自 link 锚点",
    )
    parser.add_argument(
        "--dump-actors", action="store_true",
        help="绑定前打印当前运行时世界的 Actor 稳定名（只读诊断）",
    )
    parser.add_argument(
        "--robot-focus-position", type=float, nargs=3, metavar=("X", "Y", "Z"),
        default=(250.0, -300.0, 180.0),
        help="--robot-focus 的相机位置（UE cm），默认 (250,-300,180)",
    )
    parser.add_argument(
        "--keep-default-view", action="store_true",
        help="不自动把相机移到机器人（默认：PlayerStart 缺失、相机与机器人同原点时自动移动）",
    )
    parser.add_argument(
        "--robot-focus", action="store_true",
        help="把主视角移动到原点机器人，用于单独验收 URDF 视觉绑定",
    )
    args = parser.parse_args()
    single_view = bool(args.continuous and not args.dual_view)
    if (args.frames < 1 and not args.continuous) or args.steps_per_frame < 1:
        parser.error("--frames and --steps-per-frame must be positive")
    if args.width < 1 or args.height < 1:
        parser.error("--width and --height must be positive")
    if args.supersample < 1.0:
        parser.error("--supersample must be at least 1")
    if args.rpc_timeout_seconds <= 0:
        parser.error("--rpc-timeout-seconds must be positive")
    if args.capture_warmup_frames < 0:
        parser.error("--capture-warmup-frames must be non-negative")
    if args.continuous_fps <= 0:
        parser.error("--continuous-fps must be positive")
    if args.max_internal_pixels < 0 or (
        args.bound_max_internal_pixels is not None and args.bound_max_internal_pixels < 0
    ):
        parser.error("internal pixel budgets must be non-negative (0 = no ceiling)")
    if args.min_free_vram_mb < 0:
        parser.error("--min-free-vram-mb must be non-negative")
    if args.vram_poll_seconds < 0:
        parser.error("--vram-poll-seconds must be non-negative")
    if args.window_size is not None and (args.window_size[0] < 1 or args.window_size[1] < 1):
        parser.error("--window-size values must be positive")
    for path, label in ((args.user_config, "user config"), (args.plan, "binding plan"), (args.model, "MuJoCo URDF")):
        if not path.is_file():
            parser.error(f"{label} does not exist: {path}")

    def log(message: str) -> None:
        print(message, flush=True)

    plan = load_plan(args.plan)
    # Armor can be bound two ways: the plan clones a scene template per link
    # (``armor`` entries) or the armor visual is a per-link ``ue_asset`` actor
    # imported under the link anchor (``scene_bindings.armor.enabled=false``).
    # Either way the armor links themselves must be in ``links``, so require
    # those instead of requiring clones.
    armor_link_names = [
        entry["name"] for entry in plan.get("links", [])
        if str(entry.get("name", "")).startswith("armor_link")
    ]
    if not plan.get("armor") and not armor_link_names:
        parser.error("binding plan has neither armor entries nor armor_link_* links")
    if plan.get("camera", {}).get("mode", "spear_sensor") != "spear_sensor":
        parser.error("this visual test requires camera.mode=spear_sensor")

    log(f"[1/6] loading config: {args.user_config}")
    config = spear.get_config(user_config_files=[str(args.user_config.resolve())])
    config.defrost()
    if args.standalone:
        # There is no packaged project executable in this checkout. SPEAR's
        # editor launcher can still pass -game to UnrealEditor, producing a
        # standalone game process without the editor viewport and PIE world.
        config.SPEAR.INSTANCE.EDITOR_LAUNCH_MODE = "game"
    if args.window_size is not None:
        window_width, window_height = args.window_size
        config.SPEAR.INSTANCE.COMMAND_LINE_ARGS["resx"] = window_width
        config.SPEAR.INSTANCE.COMMAND_LINE_ARGS["resy"] = window_height
        log(f"[1/6] UE window size: {window_width}x{window_height}")
    if args.low_vram:
        # The map is rendered before the first SPEAR transaction, and the
        # engine's own on-failure dump shows the device-local heap within
        # ~100 MiB of the driver budget, so the renderer footprint must be cut
        # while config is still loading. -ini:Engine:[SystemSettings]:* is
        # applied before the default map is loaded; -ExecCmds is applied after
        # it (verified in the run log) and would not affect the map-load peak.
        ini_args = low_vram_ini_args()
        for key, value in ini_args.items():
            config.SPEAR.INSTANCE.COMMAND_LINE_ARGS[key] = value
        log(f"[1/6] low-VRAM preset: {len(ini_args)} -ini:Engine:[SystemSettings] overrides")
        for key in sorted(ini_args):
            log(f"[1/6]   -{key}={ini_args[key]}")
    config.SPEAR.INSTANCE.CLIENT_INTERNAL_TIMEOUT_SECONDS = max(
        float(config.SPEAR.INSTANCE.CLIENT_INTERNAL_TIMEOUT_SECONDS),
        args.rpc_timeout_seconds,
    )
    config.freeze()
    log(
        "[1/6] SPEAR RPC timeout: "
        f"{config.SPEAR.INSTANCE.CLIENT_INTERNAL_TIMEOUT_SECONDS:.1f}s"
    )

    ue_log_path = resolve_ue_log_path(
        project_file=(
            str(config.SPEAR.INSTANCE.EDITOR_UPROJECT)
            if str(config.SPEAR.LAUNCH_MODE) == "editor"
            else None
        ),
        explicit=args.ue_log,
    )
    log(f"[1/6] UE log for crash diagnosis: {ue_log_path}")

    # A leftover Unreal process is not just wasted VRAM: SPEAR's client connects
    # to whichever RPC server is already listening, so a stale instance makes
    # the next launch attach to the wrong process and assert on the PID check.
    project_file = str(config.SPEAR.INSTANCE.EDITOR_UPROJECT) if str(config.SPEAR.LAUNCH_MODE) == "editor" else None
    stale = find_ue_processes(project_file)
    if stale:
        for process in stale:
            log(f"[1/6] stale Unreal process: pid={process.pid} {process.cmdline[:120]}")
        if not args.kill_stale_editor:
            print(json.dumps({
                "event": "stale_ue_process",
                "processes": [process.as_dict() for process in stale],
                "hint": "re-run with --kill-stale-editor to SIGKILL them",
            }, ensure_ascii=False), flush=True)
            raise SystemExit(2)
        survivors = kill_processes([process.pid for process in stale])
        if survivors:
            raise SystemExit(f"failed to kill stale Unreal processes: {survivors}")
        log(f"[1/6] killed {len(stale)} stale Unreal process(es)")

    decision = evaluate_preflight(
        query_vram(),
        args.min_free_vram_mb,
        query_vram_processes(),
    )
    for line in format_preflight(decision).splitlines():
        log(f"[1/6] {line}")
    if not decision.ok:
        print(json.dumps(
            {"event": "vram_preflight_failed", **decision.as_dict()},
            ensure_ascii=False,
        ), flush=True)
        raise SystemExit(2)

    sampler = VramSampler(
        csv_path=args.vram_csv or (args.output_dir / "vram.csv"),
        poll_seconds=args.vram_poll_seconds,
    )
    watcher = UeLogWatcher(log_path=ue_log_path)

    def _on_ue_crash(signature) -> None:
        report = {
            "event": "ue_crash_detected",
            "signature": signature.name,
            "detail": signature.detail,
            "ue_log": str(ue_log_path) if ue_log_path is not None else None,
            "vram": sampler.summary(),
            "ue_log_tail": list(watcher.tail[-20:]),
        }
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not args.no_fail_fast:
            # The engine died on the render thread; the pending RPC will only
            # surface as a timeout. Do not hold the terminal for that long.
            os._exit(4)

    watcher.on_failure = _on_ue_crash
    sampler.start()
    watcher.start()

    spear.configure_system(config=config)
    log("[2/6] connecting to SPEAR/Unreal")
    instance = spear.Instance(config=config)

    def _ue_process_alive() -> bool:
        """Local liveness check for the launched Unreal process.

        Deliberately avoids any SPEAR RPC: the point is to notice that the
        engine is gone while one of our RPCs is still pending.
        """
        try:
            process = instance._process
            if process is None:
                return True
            return bool(process.is_running()) and process.status() != "zombie"
        except Exception:  # noqa: BLE001 - diagnostics only
            return False

    watcher.process_probe = _ue_process_alive
    game = None
    debug_camera_state = None
    main_sensor = None
    main_component = None
    bound_component = None
    main_sensor_alive = False
    try:
        spear.log("[test_urdf_views] getting game world")
        game = _get_game_world(instance, config)
        capture_components = {}
        binding_diagnostics = {}
        log("[3/6] binding URDF links, armor clones, and bound camera")
        with instance.begin_frame():
            spear.log("[test_urdf_views] binding scene")
            if not args.keep_expensive_rendering:
                # The map enables Lumen, ray tracing and virtual shadows. Two
                # simultaneous SceneCapture targets otherwise allocate several
                # hundred MiB transient Vulkan buffers at 1440x1080. These
                # overrides land after the map is already resident, so they are
                # a secondary lever: use --low-vram for the startup peak.
                for name, value in {
                    "r.RayTracing": 0,
                    "r.DynamicGlobalIlluminationMethod": 0,
                    "r.ReflectionMethod": 0,
                    "r.Shadow.Virtual.Enable": 0,
                }.items():
                    game.console_service.set_cvar(name=name, value=value)
                # Read back so a silently rejected set (UE ignores a write with
                # lower priority than the current ECVF_SetBy* provenance) is
                # visible in the run log instead of being assumed to work.
            # Read back regardless of the flag: with --keep-expensive-rendering
            # the expected values are 1, which is the evidence that the run
            # really kept the project's Lumen/ray tracing/virtual shadow setup.
            for name in RUNTIME_MEMORY_CVARS:
                try:
                    effective = game.console_service.get_cvar_as(name=name, as_type=int)
                except Exception as exc:  # noqa: BLE001 - diagnostics only
                    effective = f"unavailable ({type(exc).__name__})"
                log(f"[3/6] runtime cvar check: {name}={effective}")
            # A merged CAD map can contain thousands of textures. Forcing all
            # used textures resident consumes the remaining Vulkan budget before
            # the first capture; leave streaming enabled for normal debug runs.
            game.console_service.set_cvar(
                name="r.Streaming.FramesForFullUpdate",
                value=0 if args.fully_load_textures else 2,
            )
            game.console_service.set_cvar(
                name="r.Streaming.FullyLoadUsedTextures",
                value=1 if args.fully_load_textures else 0,
            )
            gameplay_statics = game.get_unreal_object(uclass="UGameplayStatics")
            spear.log("[test_urdf_views] applying scene binding plan")
            if args.dump_actors:
                actor_map = game.unreal_service.find_actors_as_dict(
                    include_unreal_name=True,
                )
                labels = sorted(str(name) for name in actor_map)
                relevant = [
                    name for name in labels
                    if any(term in name.lower() for term in ("scene", "body", "armor", "前哨", "camera"))
                ]
                log(json.dumps({
                    "event": "runtime_actors",
                    "count": len(labels),
                    "relevant": relevant[:200],
                    "sample": labels[:50],
                }, ensure_ascii=False))
            link_actors = apply_scene_binding_plan(
                game,
                plan,
                capture_components=capture_components,
                binding_diagnostics=binding_diagnostics,
            )
            spear.log("[test_urdf_views] scene binding plan applied")
            robot_visual_actor = binding_diagnostics.get("robot_visual_actor")
            robot_mesh_count = int(binding_diagnostics.get("robot_visual_mesh_components", 0))
            armor_mesh_counts = list(binding_diagnostics.get("armor_clone_mesh_components", []))
            bound_component = capture_components["camera"]
            viewport_desc = game.viewport_service.get_current_viewport_desc()
            # Where is the default player camera, and where is the robot we just
            # bound? Without this the "is the robot visible at startup" question
            # can only be answered by guessing at a dark image.
            _pov = _viewport_pose(viewport_desc)
            _robot_actor = binding_diagnostics.get("robot_visual_actor")
            _robot_loc = _actor_world_location(_robot_actor) if _robot_actor is not None else None
            if _robot_loc is not None:
                _delta = np.asarray(_robot_loc, dtype=float) - np.asarray(_pov["location"], dtype=float)
                _distance_cm = float(np.linalg.norm(_delta))
                log(json.dumps({
                    "event": "robot_visibility",
                    "player_pov_cm": [float(v) for v in _pov["location"]],
                    "player_pov_yaw_pitch": [float(_pov["yaw"]), float(_pov["pitch"])],
                    "robot_visual_cm": [float(v) for v in _robot_loc],
                    "distance_cm": _distance_cm,
                }, ensure_ascii=False))
                # This map has no PlayerStart, so the default pawn (and the
                # player camera) spawns at the world origin, which is also where
                # the robot is bound. A capture camera inside the robot renders
                # the arena and no robot, so fall back to the robot view unless
                # the caller explicitly wants the untouched default.
                if (not args.robot_focus and not args.keep_default_view
                        and _distance_cm < 100.0):
                    args.robot_focus = True
                    log(json.dumps({
                        "event": "auto_robot_focus",
                        "reason": "player camera starts inside the robot",
                        "distance_cm": _distance_cm,
                    }, ensure_ascii=False))
            log(json.dumps({
                "event": "viewport_pose",
                "camera_location": {str(k).lower(): v for k, v in viewport_desc["camera_location"].items()},
                "camera_rotation": {str(k).lower(): v for k, v in viewport_desc["camera_rotation"].items()},
                "aspect_ratio": viewport_desc.get("aspect_ratio"),
            }, ensure_ascii=False, default=str))
            main_resolution = choose_internal_resolution(
                args.width, args.height, args.supersample, args.max_internal_pixels)
            bound_budget = (
                args.max_internal_pixels
                if args.bound_max_internal_pixels is None
                else args.bound_max_internal_pixels
            )
            sequential_capture = bool(
                args.sequential_capture
                or (not args.continuous and args.width * args.height > 800_000)
            )
            if args.continuous and args.sequential_capture:
                raise ValueError("--sequential-capture cannot be used with --continuous")
            if bound_budget != args.max_internal_pixels and not sequential_capture:
                raise ValueError("--bound-max-internal-pixels requires --sequential-capture")
            bound_resolution = choose_internal_resolution(
                args.width, args.height, args.supersample, bound_budget)
            log(
                f"[3/6] internal capture: main {main_resolution.describe()}, "
                f"bound {bound_resolution.describe()}, "
                f"output {args.width}x{args.height}"
            )
            if sequential_capture or single_view:
                # Free the bound target before allocating the temporary main
                # target; only the component properties survive Terminate().
                # Continuous single-view mode keeps the inactive component
                # uninitialized and activates it only after a key switch.
                _release_capture_component(bound_component)
            _set_capture_resolution(
                bound_component,
                bound_resolution.width,
                bound_resolution.height,
                initialize=not (sequential_capture or single_view),
            )
            _configure_capture_projection(
                bound_component, viewport_desc,
                bound_resolution.width, bound_resolution.height)
            player_pp_copied = _copy_player_camera_post_process(game, bound_component)
            main_sensor, main_component = _spawn_main_view_sensor(
                game,
                viewport_desc,
                main_resolution.width,
                main_resolution.height,
            )
            main_sensor_alive = True
            _copy_viewport_post_process(bound_component, viewport_desc)
            # Prefer the fully blended camera state, matching Rune's SDK;
            # volume settings remain the fallback for maps without a camera
            # post-process override.
            player_pp_copied = _copy_player_camera_post_process(game, bound_component) or player_pp_copied
            debug_camera_state = _viewport_pose(viewport_desc)
            if args.robot_focus:
                debug_camera_state = _set_camera_look_at(
                    main_sensor,
                    tuple(float(component) for component in args.robot_focus_position),
                )
                log(
                    "[3/6] robot-focused diagnostic view enabled at "
                    f"{tuple(args.robot_focus_position)}"
                )
            spear.log("[test_urdf_views] camera alignment complete")
            if sequential_capture:
                # Keep only one SceneCapture render target alive at a time.
                # The editor viewport and PIE world already consume a large
                # part of an 8 GiB Vulkan heap at 1440x1080.
                bound_component.SetVisibility(bNewVisibility=False)
                log("[3/6] sequential capture enabled (one SceneCapture target at a time)")
            elif single_view:
                bound_component.SetVisibility(bNewVisibility=False)
                log("[3/6] continuous single-view mode enabled (press 1/2/Tab to switch)")
        with instance.end_frame():
            pass

        # Do not wait indefinitely for editor asset streaming. A few frames are
        # enough to initialize scene capture targets and temporal post-processing.
        log("[4/6] warming up scene capture")
        if args.fully_load_textures:
            try:
                game.async_loading_service.wait_for_engine_idle(
                    max_time_seconds=30.0,
                    log_interval_seconds=1.0,
                )
            except AssertionError:
                log("[4/6] warning: UE is still streaming assets; continuing with capture")
        else:
            log("[4/6] texture streaming left enabled; skipping full engine-idle wait")
        instance.step(num_frames=30)

        physics = MujocoDriver.from_xml_path(args.model, args.steps_per_frame)
        pose_names = set(physics.read_poses())
        binding_names = set(link_actors)
        missing_pose_links = sorted(binding_names - pose_names)
        if missing_pose_links:
            raise RuntimeError(
                "MuJoCo model does not expose bound links as bodies: "
                + ", ".join(missing_pose_links)
                + "; regenerate mujoco.urdf with fusestatic=false"
            )
        pose_sync = make_scene_pose_synchronizer(link_actors, spear.math)
        run_id = uuid4().hex
        zero_control = (0.0,) * physics.model.nu
        print(json.dumps({
            "event": "ready",
            "urdf": str(args.model),
            "armor_entries": len(plan["armor"]),
            "robot_visual_bound": bool(plan.get("robot_visual")),
            "robot_visual_actor": (plan.get("robot_visual") or {}).get("actor_path") or (plan.get("robot_visual") or {}).get("actor_name"),
            "bound_camera_link": plan["camera"]["link"],
            "main_view_capture": True,
            "bound_camera_capture": True,
            "binding_links": sorted(binding_names),
            "pose_links": sorted(pose_names),
            "binding_pose_coverage": len(binding_names & pose_names) / len(binding_names),
            "robot_visual_pose_applied": bool(plan.get("robot_visual", {}).get("link") in pose_names),
            "robot_visual_source": binding_diagnostics.get("robot_visual_source"),
            "robot_visual_mesh_components": robot_mesh_count,
            "armor_clone_mesh_components": armor_mesh_counts,
            "visual_binding_render_ready": robot_mesh_count > 0 and all(armor_mesh_counts),
            "bound_camera_player_post_process": bool(player_pp_copied),
        }, ensure_ascii=False), flush=True)

        frame_indices = count(1) if args.continuous else range(1, args.frames + 1)
        next_continuous_frame = time.monotonic()
        active_view = "main"
        warned_bound_keys = False
        for frame_index in frame_indices:
            if args.continuous:
                now = time.monotonic()
                if now < next_continuous_frame:
                    time.sleep(next_continuous_frame - now)
                next_continuous_frame = time.monotonic()
            if single_view:
                log(f"[5/6] reading frame {frame_index}: {active_view} view")
            else:
                log(f"[5/6] reading frame {frame_index}: main view + bound camera")
            snapshot = None
            if not args.continuous:
                physics.step(zero_control)
                snapshot = Snapshot(
                    FrameCommit(
                        run_id=run_id,
                        commit_id=frame_index,
                        sim_tick=frame_index,
                        sim_time_ns=physics.sim_time_ns,
                        physics_step=physics.physics_step,
                        state_revision=frame_index,
                    ),
                    physics.read_poses(),
                    physics.read_sensors(),
                )
            with instance.begin_frame():
                if snapshot is not None:
                    gameplay_statics.SetGamePaused(bPaused=False)
                    pose_sync.apply_snapshot(snapshot)
                if args.continuous and debug_camera_state is not None:
                    # Do not read the live viewport or drive the Pawn here:
                    # both paths are inert while the game world is paused.
                    _apply_debug_camera_pose(main_sensor, debug_camera_state)
                # SPEAR's capture path queues an explicit GPU readback for
                # the render that follows this transaction.  This avoids
                # consuming the previous frame's target after camera/pose
                # changes and lets temporal history accumulate normally.
                active_component = (
                    main_component if active_view == "main" else bound_component
                ) if single_view else main_component
                active_component.bCaptureGpuNextRender = True
                if not sequential_capture and not single_view:
                    bound_component.bCaptureGpuNextRender = True
            with instance.end_frame(single_step=True):
                if single_view:
                    active_image = _as_image(active_component.read_pixels())
                    main_image = active_image if active_view == "main" else None
                    bound_image = active_image if active_view == "bound" else None
                else:
                    main_image = _as_image(main_component.read_pixels())
                if not sequential_capture and not single_view:
                    bound_image = _as_image(bound_component.read_pixels())

            if sequential_capture:
                # Terminate and destroy the temporary viewport sensor before
                # rendering the bound camera.  This releases its target and
                # view state instead of merely hiding it.
                with instance.begin_frame():
                    _release_capture_component(main_component)
                    game.unreal_service.destroy_actor(actor=main_sensor)
                    main_sensor_alive = False
                    bound_component.Initialize()
                    bound_component.initialize_sp_funcs()
                    bound_component.SetVisibility(bNewVisibility=True)
                with instance.end_frame(single_step=True):
                    pass
                _warm_capture_history(
                    instance,
                    bound_component,
                    args.capture_warmup_frames,
                )
                with instance.begin_frame():
                    bound_component.bCaptureGpuNextRender = True
                with instance.end_frame(single_step=True):
                    bound_image = _as_image(bound_component.read_pixels())
            elif single_view:
                # Only the selected capture has a live render target.  The
                # inactive component is terminated on a key switch below.
                pass
            with instance.begin_frame():
                gameplay_statics.SetGamePaused(bPaused=True)
            with instance.end_frame():
                pass

            if single_view:
                active_image = _downsample(active_image, args.width, args.height)
                paths = {} if args.no_save else _save_single_frame(
                    args.output_dir, frame_index, active_view, active_image)
                print(json.dumps({
                    "event": "frame",
                    "frame": frame_index,
                    "active_view": active_view,
                    "image_shape": list(active_image.shape),
                    "image_dtype": str(active_image.dtype),
                    **paths,
                }, ensure_ascii=False), flush=True)
            else:
                main_image = _downsample(main_image, args.width, args.height)
                bound_image = _downsample(bound_image, args.width, args.height)
                paths = {} if args.no_save else _save_frame(
                    args.output_dir, frame_index, main_image, bound_image)
                print(json.dumps({
                    "event": "frame",
                    "frame": frame_index,
                    "main_view_shape": list(main_image.shape),
                    "main_view_dtype": str(main_image.dtype),
                    "bound_camera_shape": list(bound_image.shape),
                    "bound_camera_dtype": str(bound_image.dtype),
                    **paths,
                }, ensure_ascii=False), flush=True)
            if args.display:
                try:
                    if single_view:
                        cv2.imshow(f"{active_view} view", _display_image(active_image))
                    else:
                        cv2.imshow("main view | bound camera", np.concatenate(
                            [_display_image(main_image), _display_image(bound_image)], axis=1))
                    key = cv2.waitKey(max(1, args.display_ms)) & 0xFF
                    if args.continuous and debug_camera_state is not None:
                        if _move_debug_camera(
                            debug_camera_state,
                            key,
                            distance_cm=args.camera_speed_cm,
                            angle_deg=args.camera_turn_deg,
                        ):
                            # The free camera *is* the main capture actor.  The
                            # bound camera belongs to the robot, so flying it
                            # would defeat the point of that view; say so once
                            # instead of letting the keys look dead.
                            if single_view and active_view != "main" and not warned_bound_keys:
                                warned_bound_keys = True
                                log("[display] free-camera keys move the main view; press 1 to see it")
                            with instance.begin_frame():
                                _apply_debug_camera_pose(main_sensor, debug_camera_state)
                            with instance.end_frame():
                                pass
                    if single_view and key in (ord("1"), ord("2"), 9):
                        requested_view = (
                            "main" if key == ord("1")
                            else "bound" if key == ord("2")
                            else "bound" if active_view == "main" else "main"
                        )
                        if requested_view != active_view:
                            old_component = main_component if active_view == "main" else bound_component
                            new_component = main_component if requested_view == "main" else bound_component
                            with instance.begin_frame():
                                _release_capture_component(old_component)
                                old_component.SetVisibility(bNewVisibility=False)
                                new_component.Initialize()
                                new_component.initialize_sp_funcs()
                                new_component.SetVisibility(bNewVisibility=True)
                            with instance.end_frame(single_step=True):
                                pass
                            _warm_capture_history(
                                instance,
                                new_component,
                                args.capture_warmup_frames,
                            )
                            active_view = requested_view
                            log(f"[display] switched to {active_view} view")
                    if args.continuous and key in (ord("x"), 27):
                        break
                except cv2.error as exc:
                    print(f"[display] OpenCV window unavailable: {exc}", file=sys.stderr)
                    args.display = False
            if args.continuous and args.display:
                pass
            if args.continuous:
                next_continuous_frame += 1.0 / args.continuous_fps
                if next_continuous_frame < time.monotonic():
                    next_continuous_frame = time.monotonic()
        if args.verify_armor_links:
            targets = {
                entry["link"]: entry["clone_name"]
                for entry in plan.get("armor", [])
                if entry.get("clone_name")
            }
            targets.update(_imported_armor_targets(instance, game, plan, link_actors))
            robot_visual_actor = binding_diagnostics.get("robot_visual_actor")
            robot_visual_link = (plan.get("robot_visual") or {}).get("link")
            if robot_visual_actor is not None and robot_visual_link:
                targets[robot_visual_link] = robot_visual_actor
            _verify_link_attachment(
                instance,
                game,
                gameplay_statics,
                pose_sync,
                physics,
                targets,
                link_actors,
                run_id,
                log,
            )
    finally:
        crashed = sys.exc_info()[0] is not None
        watcher.disarm()
        watcher.stop()
        sampler.stop()
        vram_summary = dict(sampler.summary())
        # The engine's own budget report is the ceiling that actually matters:
        # it is well below the 8188 MiB heap size because the desktop session
        # shares the card, and it moves between runs.
        budget_mb = None
        if ue_log_path is not None and ue_log_path.is_file():
            try:
                budget_mb = device_local_budget_mb(ue_log_path.read_text(errors="replace"))
            except OSError:
                budget_mb = None
        vram_summary["device_local_budget_mb"] = budget_mb
        if crashed:
            # Report the evidence before touching Unreal again: the cleanup
            # below can itself block on an RPC timeout when the engine is gone.
            tail = watcher.tail_text() or (
                _read_log_tail(ue_log_path) if ue_log_path is not None else ""
            )
            signatures = find_crash_signatures(tail)
            print(json.dumps({
                "event": "run_failed",
                "ue_log": str(ue_log_path) if ue_log_path is not None else None,
                "signatures": [signature.as_dict() for signature in signatures],
                "vram": vram_summary,
            }, ensure_ascii=False), flush=True)
        log(f"[6/6] vram: {json.dumps(vram_summary, ensure_ascii=False)}")
        log("[6/6] cleaning up temporary main-view sensor")
        if main_sensor is not None and game is not None and main_sensor_alive:
            try:
                with instance.begin_frame():
                    pass
                with instance.end_frame():
                    if main_component is not None:
                        _release_capture_component(main_component)
                    game.unreal_service.destroy_actor(actor=main_sensor)
            except Exception as exc:  # noqa: BLE001 - keep the original error
                spear.log(f"[test_urdf_views] failed to release main-view sensor: {exc}")
        if game is not None and bound_component is not None:
            try:
                with instance.begin_frame():
                    _release_capture_component(bound_component)
                with instance.end_frame():
                    pass
            except Exception as exc:
                spear.log(f"[test_urdf_views] failed to release bound capture: {exc}")
        # SPEAR's editor example drops the game-world handle before requesting
        # EndPlay because the game world is invalidated by that request.
        game = None
        play_ended = _end_editor_play(instance, config)
        if args.display:
            cv2.destroyAllWindows()
        # A failed EndPlay must not be followed by graceful editor shutdown:
        # UE 5.8 can dereference its level viewport during that second teardown.
        instance.close(force=(crashed or (str(config.SPEAR.LAUNCH_MODE) == "editor" and not play_ended)))


if __name__ == "__main__":
    main()
