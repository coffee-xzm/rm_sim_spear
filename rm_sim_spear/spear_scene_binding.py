"""Apply a generated scene_binding_plan.json through SPEAR.

Call ``apply_scene_binding_plan(sp_instance.game, plan)`` from a SPEAR run
after the UE level and its scene actors have been loaded. Actor names and UE
asset paths are intentionally supplied by the JSON plan, not hard-coded here.
"""

from __future__ import annotations

from pathlib import Path
import json
import math
from typing import Any
from contextvars import ContextVar

from .pose_sync import PoseSynchronizer, make_spear_actor_applier


class _SceneActorNotFound(RuntimeError):
    """Internal marker distinguishing a missing actor from an RPC failure."""


_BINDING_CACHE: ContextVar[dict | None] = ContextVar("scene_binding_cache", default=None)


def load_plan(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _actor_attachment_path(game: Any, actor: Any) -> str:
    """Return the Outliner-style path formed by attached parent actors."""
    parts = []
    current = actor
    while current is not None:
        parts.append(game.unreal_service.get_stable_name_for_actor(actor=current))
        parent_handle = current.GetAttachParentActor(as_handle=True)
        if not parent_handle:
            break
        current = game.unreal_service.to_handle_or_unreal_object(
            obj=parent_handle,
            as_unreal_object=True,
        )
    return "/".join(reversed(parts))


def _find_actor_by_tag(game: Any, actor_tag: str) -> Any | None:
    """Return the single actor carrying a UE actor tag, or None.

    UE tags are set by the level/importer and behave identically in PIE and in
    a standalone ``-game`` world, so they are the reliable way to address
    importer-placed actors.  ``find_actors_by_tag_as_dict`` returns an empty
    mapping for no match instead of the singular lookup's native exception.
    """
    try:
        matches = game.unreal_service.find_actors_by_tag_as_dict(
            tag=actor_tag,
            uclass="AActor",
        )
    except (AttributeError, TypeError):
        # Lightweight test doubles and older SPEAR wrappers have no tag API;
        # fall through so the remaining lookups still get a chance.
        return None
    if not matches:
        return None
    if len(matches) > 1:
        # A tag must identify exactly one link actor; binding an arbitrary
        # match would silently drive the wrong visual.
        raise RuntimeError(
            f"ambiguous actor tag: {actor_tag!r} matched {len(matches)} actors")
    return next(iter(matches.values()))


def _find_actor(
    game: Any,
    name: str | None = None,
    actor_path: str | None = None,
    runtime_name: str | None = None,
    actor_tag: str | None = None,
) -> Any:
    """Find an actor by stable name, UE tag, then attached-actor path."""
    candidates = []
    for candidate in (actor_path, name):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        # The singular SPEAR lookup throws a native C++ exception when there
        # are zero matches, which poisons EngineService's error state. The map
        # form returns an empty mapping and is safe for an existence probe.
        matches = game.unreal_service.find_actors_by_name_as_dict(
            actor_name=candidate,
            uclass="AActor",
        )
        if matches:
            return next(iter(matches.values()))
    if actor_tag:
        # Prefer a tag over the name-shape heuristics below: an importer that
        # places one actor per link can tag them reliably, unlike editor labels
        # which become unstable UObject names in a standalone ``-game`` world.
        tagged = _find_actor_by_tag(game, actor_tag)
        if tagged is not None:
            return tagged
    # In a standalone game, imported Datasmith/Blender actors can lose their
    # editor label (for example ``body``) while retaining their UObject name
    # (for example ``blender_for_ue5_node_22``).  The map form with
    # include_unreal_name exposes both as ``stable:object`` without using the
    # singular lookup that poisons EngineService on an empty result.
    if runtime_name:
        all_actors = _runtime_actor_map(game)
        matches = [handle for key, handle in all_actors.items()
                   if str(key).rsplit(":", 1)[-1] == runtime_name]
        if len(matches) > 1:
            raise RuntimeError(f"ambiguous runtime actor name: {runtime_name!r}")
        if matches:
            return game.unreal_service.to_handle_or_unreal_object(
                obj=matches[0], as_unreal_object=True)
        # Merge Actors removes the source UObject but commonly preserves its
        # name as ``SM_MERGED_<source>``. Resolve that derived name from the
        # semantic source configured in the plan instead of storing the merged
        # asset's transient numeric suffix in the plan itself.
        merged_name = f"SM_MERGED_{runtime_name}"
        merged_matches = game.unreal_service.find_actors_by_name_as_dict(
            actor_name=merged_name,
            uclass="AActor",
        )
        if merged_matches:
            return next(iter(merged_matches.values()))
        for actor in game.unreal_service.find_actors_as_dict().values():
            try:
                stable_name = game.unreal_service.get_stable_name_for_actor(actor=actor)
            except (AttributeError, RuntimeError):
                continue
            if str(stable_name) == merged_name:
                return actor
    if actor_path:
        # SPEAR stable names use folder paths, while the UE Outliner also shows
        # attached actors as ``Parent/Child``. Resolve that hierarchy explicitly
        # when a plan uses the latter form (for example ``Scene/armor``).
        for actor in game.unreal_service.find_actors_as_dict().values():
            if _actor_attachment_path(game, actor) == actor_path:
                return actor
    description = actor_path or name or actor_tag or "<unnamed>"
    raise _SceneActorNotFound(f"scene actor not found: {description!r}")


def _runtime_actor_map(game: Any) -> dict[str, Any]:
    """Cache standalone UObject-name lookup for one binding transaction."""
    cache = _BINDING_CACHE.get()
    if cache is None:
        cache = {}
    actor_map = cache.get("runtime_actor_map")
    if actor_map is None:
        try:
            raw = game.unreal_service.find_actors_as_dict(
                include_unreal_name=True, as_handle=True)
        except TypeError:
            # Lightweight test doubles and older SPEAR wrappers expose only
            # the basic actor map; stable names are enough for fallback scans.
            raw = game.unreal_service.find_actors_as_dict()
        actor_map = {}
        for key, handle in raw.items():
            actor_map[str(key).rsplit(":", 1)[-1]] = handle
        cache["runtime_actor_map"] = actor_map
    return actor_map


def _find_runtime_actor(game: Any, runtime_name: str) -> Any:
    handle = _runtime_actor_map(game).get(runtime_name)
    if handle is None:
        raise _SceneActorNotFound(f"runtime actor not found: {runtime_name!r}")
    return game.unreal_service.to_handle_or_unreal_object(
        obj=handle, as_unreal_object=True)


def _resolve_runtime_children(
    game: Any,
    actor: Any,
    runtime_child_names: list[str] | None,
) -> list[Any]:
    """Discover children from the live hierarchy, including after UE merges.

    Prefer planned children when they still exist (this preserves an armor
    aggregate whose children are separate actors), but fall back to the live
    hierarchy when Merge Actors removed the importer names.
    """
    if runtime_child_names:
        resolved = []
        for name in runtime_child_names:
            try:
                resolved.append(_find_runtime_actor(game, name))
            except _SceneActorNotFound:
                continue
        if resolved:
            return resolved
    return _get_attached_actors(game, actor)


def link_application_order(plan: dict[str, Any], link_names: Any) -> list[str]:
    """Order link names so every parent is posed before its children.

    The imported anchors stay attached to their URDF parent (that is what gives
    the visuals their hierarchy), while each pose is applied as an absolute
    world location.  A child posed before its parent is therefore dragged by the
    parent's same-frame move: on the first frame, where the parent jumps from
    its imported level position to its offset world position, the error is the
    whole offset (measured 139.8 cm for ``armor_link_*`` under ``base_link``).
    Cycles and names missing from the plan fall back to a stable sort.
    """
    names = {str(name) for name in link_names}
    parents = {
        str(entry.get("name")): entry.get("parent")
        for entry in plan.get("links", [])
        if isinstance(entry, dict) and entry.get("name")
    }
    order: list[str] = []
    placed: set[str] = set()

    def visit(name: str, trail: frozenset[str]) -> None:
        if name in placed or name in trail:
            return
        parent = parents.get(name)
        if isinstance(parent, str) and parent in names and parent != name:
            visit(parent, trail | {name})
        if name not in placed:
            placed.add(name)
            order.append(name)

    for name in sorted(names):
        visit(name, frozenset())
    return order


def _find_or_spawn_anchor(game: Any, name: str, *, actor_tag: str | None = None) -> Any:
    """Return the actor anchoring a link, spawning a transient empty one.

    ``actor_tag`` lets a plan reuse an importer-placed, tagged level actor (the
    one that actually owns the link's visuals) instead of an empty anchor.
    """
    try:
        anchor = _find_actor(game, name, actor_tag=actor_tag)
        _ensure_actor_root(game, anchor, "RMSimRoot")
        return anchor
    except _SceneActorNotFound:
        anchor = game.unreal_service.spawn_actor(
            uclass="AActor",
            spawn_parameters={
                "SpawnCollisionHandlingOverride": "AlwaysSpawn",
                "ObjectFlags": ["RF_Transient"],
            },
        )
        game.unreal_service.set_stable_name_for_actor(actor=anchor, stable_name=name)
        _ensure_actor_root(game, anchor, "RMSimRoot")
        return anchor


def _try_find_actor(game: Any, name: str | None = None, actor_path: str | None = None) -> Any | None:
    try:
        return _find_actor(game, name=name, actor_path=actor_path)
    except RuntimeError:
        return None


def _link_anchor(game: Any, link_actors: dict[str, Any], link_name: str) -> Any:
    """Return the anchor already bound to a link, else find or spawn one.

    In ``link_hierarchy`` mode a link's anchor is the importer's tagged visual
    actor.  Armor and the camera must attach to that same actor: looking the
    link up again by name would spawn a second, empty anchor and the pose loop
    would drive that instead, leaving the imported visual behind.
    """
    anchor = link_actors.get(link_name)
    if anchor is not None:
        return anchor
    return _find_or_spawn_anchor(game, link_name)


def _spawn_spear_camera(game: Any, entry: dict[str, Any]) -> Any:
    class_path = entry.get("ue_class")
    if not class_path:
        raise RuntimeError("spear_sensor camera binding requires camera.ue_class")
    camera_class = game.unreal_service.load_class(uclass="AActor", name=class_path)
    if camera_class is None:
        raise RuntimeError(f"camera class not found: {class_path!r}")
    camera = game.unreal_service.spawn_actor(
        uclass=camera_class,
        spawn_parameters={"ObjectFlags": ["RF_Transient"]},
    )
    actor_name = entry.get("actor_name")
    if actor_name:
        game.unreal_service.set_stable_name_for_actor(actor=camera, stable_name=actor_name)
    return camera


def _find_component_from_map(game: Any, actor: Any, component_name: str) -> Any | None:
    """Find a component without SPEAR's assert-on-empty singular lookup."""
    components = game.unreal_service.get_components_as_dict(actor=actor)
    if component_name in components:
        return components[component_name]
    leaf_name = component_name.rsplit(".", 1)[-1]
    for path, component in components.items():
        if path.rsplit(".", 1)[-1] == leaf_name:
            return component
    return None


def _get_spear_capture_component(
    game: Any,
    camera: Any,
    entry: dict[str, Any],
    *,
    initialize: bool = True,
) -> Any:
    component_name = entry.get(
        "capture_component",
        "DefaultSceneRoot.final_tone_curve_hdr_",
    )
    component = _find_component_from_map(game, camera, component_name)
    if component is None:
        raise RuntimeError(
            f"SPEAR camera has no USpSceneCaptureComponent2D: {component_name!r}"
        )
    # Match the Rune SDK capture path.  The SDK publishes the post-tonemapped
    # LDR scene (SCS_FinalColorLDR); using SPEAR's default HDR tone-curve source
    # changes the render pipeline and can turn thin geometry into discontinuous
    # pixels at the target resolution.
    component.CaptureSource = "SCS_FinalColorLDR"
    component.bAlwaysPersistRenderingState = True
    # These calls register read_pixels/enqueue_copy SpFuncs. Keep this
    # idempotent because a reset may apply the same binding plan again. The
    # visual test can defer initialization until after the main capture target
    # is allocated; otherwise binding itself creates a second Vulkan target
    # before sequential capture has a chance to release it.
    if initialize:
        if not component.IsInitialized():
            component.Initialize()
        if not getattr(component, "_initialized_sp_funcs", False):
            component.initialize_sp_funcs()
    return component


def _handle_value(value: Any) -> Any:
    """Normalize an UnrealObject or SPEAR handle for equality checks."""
    value = getattr(value, "uobject", value)
    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)
    return value


def _get_attached_actors(game: Any, actor: Any) -> list[Any]:
    """Return direct attached actors without calling the reflected TArray UFunction.

    ``AActor::GetAttachedActors`` exposes a ``TArray<AActor*>&`` out parameter.
    SPEAR's generic UFunction bridge currently rejects that parameter's
    reflected name on this UE build, even though the native function is valid.
    The parent query has no out array, so derive the same direct-child set from
    the world's actor list instead.
    """
    parent_handle = _handle_value(actor)
    # A Datasmith level can contain thousands of actors. Cache the world map
    # for this binding transaction instead of rebuilding/converting it for
    # every child during recursive hierarchy traversal.
    cache = _BINDING_CACHE.get()
    if cache is None:
        cache = {}
    if "children" not in cache:
        children = {}
        for candidate in game.unreal_service.find_actors_as_dict().values():
            candidate_parent = candidate.GetAttachParentActor(as_handle=True)
            if candidate_parent:
                children.setdefault(_handle_value(candidate_parent), []).append(candidate)
        cache["children"] = children
    return list(cache["children"].get(parent_handle, ()))


def _copy_static_mesh_components(game: Any, source: Any, clone: Any) -> None:
    source_components = game.unreal_service.get_components_by_class(
        actor=source,
        uclass="UStaticMeshComponent",
    )
    clone_components = game.unreal_service.get_components_by_class(
        actor=clone,
        uclass="UStaticMeshComponent",
    )
    for source_component, clone_component in zip(source_components, clone_components):
        clone_component.SetMobility(NewMobility="Movable")
        mesh = source_component.get_property_value(
            property_name="StaticMesh",
            as_unreal_object=True,
        )
        if mesh is not None:
            clone_component.SetStaticMesh(NewMesh=mesh)

        try:
            material_count = source_component.call(
                function_name="GetNumMaterials",
                as_value=True,
            )
        except (AssertionError, RuntimeError):
            material_count = 0
        for material_index in range(int(material_count or 0)):
            material = source_component.call(
                function_name="GetMaterial",
                args={"ElementIndex": material_index},
                as_unreal_object=True,
            )
            if material is not None:
                clone_component.SetMaterial(
                    ElementIndex=material_index,
                    Material=material,
                )


def _count_actor_tree_mesh_components(game: Any, actor: Any) -> int:
    """Count actual static-mesh components in an actor and its attached tree."""
    count = len(game.unreal_service.get_components_by_class(
        actor=actor,
        uclass="UStaticMeshComponent",
        include_from_child_actors=False,
    ))
    for child in _get_attached_actors(game, actor):
        count += _count_actor_tree_mesh_components(game, child)
    return count


def _count_cloned_tree_mesh_components(
    game: Any,
    actor: Any,
) -> int:
    """Count a freshly cloned tree using a fresh world hierarchy snapshot."""
    count = len(game.unreal_service.get_components_by_class(
        actor=actor,
        uclass="UStaticMeshComponent",
        include_from_child_actors=False,
    ))
    parent_handle = _handle_value(actor)
    children = []
    for candidate in game.unreal_service.find_actors_as_dict().values():
        candidate_parent = candidate.GetAttachParentActor(as_handle=True)
        if candidate_parent and _handle_value(candidate_parent) == parent_handle:
            children.append(candidate)
    for child in children:
        count += _count_actor_tree_mesh_components(game, child)
    return count


def _ue_vector(value: Any) -> dict[str, float]:
    """Normalize an FVector returned by SPEAR for a UE function argument."""
    return {
        "X": float(value.get("X", value.get("x", 0.0))),
        "Y": float(value.get("Y", value.get("y", 0.0))),
        "Z": float(value.get("Z", value.get("z", 0.0))),
    }


def _ue_rotator(value: Any) -> dict[str, float]:
    """Normalize an FRotator returned by SPEAR for a UE function argument."""
    return {
        "Pitch": float(value.get("Pitch", value.get("pitch", 0.0))),
        "Yaw": float(value.get("Yaw", value.get("yaw", 0.0))),
        "Roll": float(value.get("Roll", value.get("roll", 0.0))),
    }


def _ensure_actor_root(game: Any, actor: Any, component_name: str) -> Any:
    # Read the UPROPERTY as a raw handle. Unlike the reflected K2 getter,
    # property conversion preserves a null UObject as handle 0 instead of
    # asserting while constructing an UnrealObject.
    try:
        root_handle = actor.get_property_value(
            property_name="RootComponent",
            as_handle=True,
        )
    except (AttributeError, AssertionError, RuntimeError):
        # Keep the fallback for lightweight test doubles and older wrappers.
        try:
            root_handle = actor.K2_GetRootComponent(as_handle=True)
        except AssertionError:
            root_handle = 0
    if root_handle:
        return game.unreal_service.to_handle_or_unreal_object(
            obj=root_handle,
            as_unreal_object=True,
        )
    return game.unreal_service.create_scene_component_for_actor(
        uclass="USceneComponent",
        owner=actor,
        scene_component_name=component_name,
    )


def _make_actor_movable(game: Any, actor: Any, component_name: str = "RMSimRoot") -> None:
    root = _ensure_actor_root(game, actor, component_name)
    if root is not None:
        root.SetMobility(NewMobility="Movable")


def _make_actor_tree_movable(
    game: Any,
    actor: Any,
    runtime_child_names: list[str] | None = None,
) -> None:
    """Make an aggregate level actor attachable at runtime.

    Scene folders such as ``Scene/body`` often contain static child actors.
    UE rejects attaching that tree to a transient movable URDF anchor unless
    every child root has movable mobility.
    """
    _make_actor_movable(game, actor)
    children = _resolve_runtime_children(game, actor, runtime_child_names)
    for child in children:
        _make_actor_tree_movable(game, child)


def _clone_actor_tree(
    game: Any,
    source: Any,
    clone_name: str,
    runtime_child_names: list[str] | None = None,
) -> Any:
    """Clone an actor and its attached actor hierarchy at runtime.

    The source remains in the level.  Static mesh references and materials are
    shared assets; only actor/component instances are created.
    """
    existing = _try_find_actor(game, name=clone_name)
    if existing is not None:
        return existing

    location = _ue_vector(source.K2_GetActorLocation())
    rotation = _ue_rotator(source.K2_GetActorRotation())
    clone = game.unreal_service.spawn_actor(
        uclass=source.uclass,
        location=location,
        rotation=rotation,
        spawn_parameters={
            "SpawnCollisionHandlingOverride": "AlwaysSpawn",
            "ObjectFlags": ["RF_Transient"],
        },
    )
    game.unreal_service.set_stable_name_for_actor(actor=clone, stable_name=clone_name)
    _make_actor_movable(game, clone)
    clone.SetActorScale3D(NewScale3D=_ue_vector(source.GetActorScale3D()))
    _copy_static_mesh_components(game, source, clone)

    children = _resolve_runtime_children(game, source, runtime_child_names)
    for index, child in enumerate(children, start=1):
        child_clone = _clone_actor_tree(game, child, f"{clone_name}/part_{index}")
        _attach_actor(
            child_clone,
            clone,
            location_rule="KeepWorld",
            rotation_rule="KeepWorld",
            scale_rule="KeepWorld",
        )
    return clone


def _reset_actor_relative_transform(game: Any, actor: Any) -> None:
    root = _ensure_actor_root(game, actor, "RMSimRoot")
    if root is None:
        raise RuntimeError("cloned actor has no root component")
    root.ResetRelativeTransform()


def _attach_actor(
    actor: Any,
    parent: Any,
    *,
    location_rule: str = "KeepRelative",
    rotation_rule: str = "KeepRelative",
    scale_rule: str = "KeepRelative",
) -> None:
    # K2_AttachToActor uses the current UE attachment-rule signature. The
    # deprecated K2_AttachRootComponentToActor takes AttachLocationType and
    # rejects the rule names below at SPEAR's reflection boundary.
    actor.call(
        function_name="K2_AttachToActor",
        args={
            "ParentActor": parent,
            "SocketName": "",
            "LocationRule": location_rule,
            "RotationRule": rotation_rule,
            "ScaleRule": scale_rule,
            "bWeldSimulatedBodies": False,
        },
    )


def _hide_actor_tree(
    game: Any,
    actor: Any,
    runtime_child_names: list[str] | None = None,
) -> None:
    """Hide the map template so it cannot render beside its runtime clones."""
    actor.SetActorHiddenInGame(bNewHidden=True)
    children = _resolve_runtime_children(game, actor, runtime_child_names)
    for child in children:
        _hide_actor_tree(game, child)


def _set_material(game: Any, actor: Any, slot: int, asset_path: str | None) -> None:
    if not asset_path:
        return
    material = game.unreal_service.load_object(uclass="UMaterialInterface", name=asset_path)
    if material is None:
        raise RuntimeError(f"material asset not found: {asset_path!r}")
    components = game.unreal_service.get_components_by_class(
        actor=actor,
        uclass="UStaticMeshComponent",
        include_from_child_actors=True,
    )
    if not components:
        raise RuntimeError(f"armor actor has no UStaticMeshComponent: {actor}")
    for component in components:
        component.SetMaterial(ElementIndex=slot, Material=material)


def _attach_imu_marker(game: Any, camera: Any, imu: dict[str, Any]) -> Any:
    parent = _ensure_actor_root(game, camera, "RMSimCameraRoot")
    marker = _find_component_from_map(game, camera, imu["name"])
    if marker is None:
        marker = game.unreal_service.create_scene_component_for_object(
            uclass="USceneComponent",
            owner=camera,
            parent=parent,
            scene_component_name=imu["name"],
        )
    marker.K2_AttachToComponent(
        Parent=parent,
        SocketName="",
        LocationRule="KeepRelative",
        RotationRule="KeepRelative",
        ScaleRule="KeepRelative",
        bWeldSimulatedBodies=False,
    )
    xyz = imu["relative_position_m"]
    rpy = imu["relative_rpy_rad"]
    marker.K2_SetRelativeLocation(NewLocation={"X": xyz[0] * 100.0, "Y": -xyz[1] * 100.0, "Z": xyz[2] * 100.0})
    marker.K2_SetRelativeRotation(NewRotation={
        "Roll": -math.degrees(rpy[0]),
        "Pitch": -math.degrees(rpy[1]),
        "Yaw": math.degrees(rpy[2]),
    })
    return marker


def apply_scene_binding_plan(
    game: Any,
    plan: dict[str, Any],
    *,
    capture_components: dict[str, Any] | None = None,
    binding_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a plan using a hierarchy index scoped to this transaction."""
    token = _BINDING_CACHE.set({})
    try:
        return _apply_scene_binding_plan(
            game,
            plan,
            capture_components=capture_components,
            binding_diagnostics=binding_diagnostics,
        )
    finally:
        _BINDING_CACHE.reset(token)


def _derive_runtime_asset_path(robot_visual: dict[str, Any]) -> str | None:
    """Best-effort asset path for a runtime-spawned robot visual.

    A standalone (``-game``) world is loaded from the map, not copied from the
    editor world, so an editor-time Merge Actors result can be absent there.
    Merge Actors names the new actor after the asset it creates, which puts the
    asset at ``/Game/<actor_name>``; ``robot_visual.asset_path`` overrides that
    when the asset lives elsewhere.
    """
    explicit = robot_visual.get("asset_path")
    if explicit:
        return str(explicit)
    name = robot_visual.get("actor_name")
    if name and not str(name).startswith("/"):
        return f"/Game/{name}"
    return None


def _spawn_runtime_static_mesh_actor(game: Any, asset_path: str, stable_name: str) -> Any:
    """Load a StaticMesh asset and spawn a movable actor for it.

    This is the standalone fallback for a robot visual that exists as an asset
    but not as a level actor. Performed inside a SPEAR transaction, so it must
    stay limited to the entry points the rest of the binder already uses.
    """
    mesh = game.unreal_service.load_object(
        uclass="UStaticMesh",
        name=asset_path,
        as_unreal_object=True,
    )
    if mesh is None:
        raise RuntimeError(f"robot visual asset could not be loaded: {asset_path!r}")
    # Resolve the native class by path: spawn_actor's string form goes through
    # SPEAR's static class-descriptor registry, which does not contain every
    # engine class.
    actor_class = game.unreal_service.load_class(
        uclass="AActor",
        name="/Script/Engine.StaticMeshActor",
    )
    actor = game.unreal_service.spawn_actor(
        uclass=actor_class,
        spawn_parameters={"ObjectFlags": ["RF_Transient"]},
        as_unreal_object=True,
    )
    components = game.unreal_service.get_components_by_class(
        actor=actor,
        uclass="UStaticMeshComponent",
    )
    if not components:
        raise RuntimeError("runtime-spawned robot visual has no UStaticMeshComponent")
    for component in components:
        component.SetMobility(NewMobility="Movable")
        component.SetStaticMesh(NewMesh=mesh)
    game.unreal_service.set_stable_name_for_actor(actor=actor, stable_name=stable_name)
    return actor


def _bind_single_mesh_robot_visual(
    game: Any,
    robot_visual: dict[str, Any],
    link_actors: dict[str, Any],
    binding_diagnostics: dict[str, Any] | None,
) -> None:
    """Bind one aggregate scene actor (or its runtime asset) as the robot visual."""
    root_link = _find_or_spawn_anchor(game, robot_visual["link"])
    robot_visual_source = "scene_actor_override"
    try:
        visual_actor = _find_actor(
            game,
            name=robot_visual.get("actor_name"),
            actor_path=robot_visual.get("actor_path"),
            runtime_name=robot_visual.get("runtime_actor_name"),
        )
    except _SceneActorNotFound:
        # Standalone game: the editor-time merged actor is not in the map.
        # The asset itself is runtime content, so load and spawn it.
        asset_path = _derive_runtime_asset_path(robot_visual)
        if asset_path is None:
            raise
        visual_actor = _spawn_runtime_static_mesh_actor(
            game,
            asset_path,
            stable_name="RMSim/robot_visual",
        )
        robot_visual_source = "runtime_spawn"
    _make_actor_tree_movable(
        game, visual_actor, robot_visual.get("runtime_child_names"))
    robot_mesh_count = _count_actor_tree_mesh_components(game, visual_actor)
    if robot_mesh_count == 0 and robot_visual.get("runtime_actor_name"):
        # The semantic Scene/body folder may remain as an empty organizer
        # after Merge Actors. Retry the source UObject suffix so the
        # merged mesh can become the visual root without hard-coding its
        # generated numeric asset name in the binding plan.
        try:
            merged_actor = _find_actor(
                game,
                runtime_name=robot_visual["runtime_actor_name"],
            )
            merged_mesh_count = _count_actor_tree_mesh_components(game, merged_actor)
            if merged_mesh_count:
                visual_actor = merged_actor
                _make_actor_tree_movable(
                    game, visual_actor, robot_visual.get("runtime_child_names"))
                robot_mesh_count = merged_mesh_count
        except _SceneActorNotFound:
            pass
    if robot_mesh_count == 0:
        raise RuntimeError(
            "robot visual actor has no static mesh in its attached hierarchy: "
            f"{robot_visual.get('actor_path') or robot_visual.get('actor_name')!r}; "
            "select the current mesh actor in scene_bindings.robot after UE Merge Actors"
        )
    _attach_actor(visual_actor, root_link)
    if robot_visual.get("reset_relative_transform"):
        _reset_actor_relative_transform(game, visual_actor)
    link_actors[robot_visual["link"]] = root_link
    if binding_diagnostics is not None:
        binding_diagnostics["robot_visual_actor"] = visual_actor
        binding_diagnostics["robot_visual_mesh_components"] = robot_mesh_count
        binding_diagnostics["robot_visual_source"] = robot_visual_source


def _bind_link_hierarchy_robot_visual(
    game: Any,
    plan: dict[str, Any],
    robot_visual: dict[str, Any],
    link_actors: dict[str, Any],
    binding_diagnostics: dict[str, Any] | None,
) -> None:
    """Bind one importer-placed actor per URDF link as the robot visual.

    When an importer places and tags a visual actor per link there is no single
    merged robot actor to find (the merged editor label does not survive into a
    standalone ``-game`` world at all).  Each link actor *is* the anchor the
    pose synchroniser drives, so every MuJoCo body keeps a pose target even
    when the link carries no armor or camera.
    """
    links = plan.get("links") or []
    if not links:
        raise RuntimeError(
            "link_hierarchy robot visual requires a non-empty 'links' list "
            "in the binding plan")
    tag_template = robot_visual.get("link_actor_tag")
    anchors: dict[str, Any] = {}
    for entry in links:
        link_name = entry["name"]
        actor_tag = tag_template.format(link=link_name) if tag_template else None
        anchor = _find_or_spawn_anchor(game, link_name, actor_tag=actor_tag)
        # Imported actors usually arrive with static mobility; the pose loop
        # moves each anchor every frame and UE refuses to move static roots.
        _make_actor_tree_movable(game, anchor)
        anchors[link_name] = anchor
    mesh_counts = {
        name: _count_actor_tree_mesh_components(game, anchor)
        for name, anchor in anchors.items()
    }
    robot_mesh_count = sum(mesh_counts.values())
    if robot_mesh_count == 0:
        missing = ", ".join(sorted(mesh_counts))
        raise RuntimeError(
            "link_hierarchy robot visual found no UStaticMeshComponent for any "
            f"link anchor: {missing}; import the link visuals first or fix "
            f"robot_visual.link_actor_tag ({tag_template!r})"
        )
    link_actors.update(anchors)
    if binding_diagnostics is not None:
        binding_diagnostics["robot_visual_source"] = "imported_link_hierarchy"
        binding_diagnostics["robot_visual_actor"] = None
        binding_diagnostics["robot_visual_mesh_components"] = robot_mesh_count


def _apply_scene_binding_plan(
    game: Any,
    plan: dict[str, Any],
    *,
    capture_components: dict[str, Any] | None = None,
    binding_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach configured scene actors and return the runtime link map.

    The returned map is consumed by the MuJoCo-to-SPEAR pose loop. It maps a
    MuJoCo link name to the UE Actor that should receive its world transform.
    """
    if plan.get("warnings"):
        raise ValueError("binding plan has warnings: " + "; ".join(plan["warnings"]))

    link_actors: dict[str, Any] = {}
    bound_actor_keys: dict[str, str] = {}
    hidden_templates: set[str] = set()
    robot_visual = plan.get("robot_visual")
    if robot_visual:
        # Plans without a mode keep the original single merged visual path.
        if robot_visual.get("mode") == "link_hierarchy":
            _bind_link_hierarchy_robot_visual(
                game, plan, robot_visual, link_actors, binding_diagnostics)
        else:
            _bind_single_mesh_robot_visual(
                game, robot_visual, link_actors, binding_diagnostics)
    for entry in plan.get("armor", []):
        link = _link_anchor(game, link_actors, entry["link"])
        if entry.get("clone_per_link"):
            template = _find_actor(
                game,
                name=entry.get("actor_name"),
                actor_path=entry.get("actor_path"),
                runtime_name=entry.get("runtime_actor_name"),
            )
            clone_name = entry.get("clone_name")
            if not clone_name:
                raise ValueError(f"clone_per_link entry has no clone_name: {entry!r}")
            armor = _clone_actor_tree(
                game, template, clone_name, entry.get("runtime_child_names"))
            if entry.get("hide_template", True):
                template_key = entry.get("actor_path") or entry.get("actor_name")
                if template_key not in hidden_templates:
                    _hide_actor_tree(
                        game, template, entry.get("runtime_child_names"))
                    hidden_templates.add(template_key)
        else:
            armor = _find_actor(
                game,
                name=entry.get("actor_name"),
                actor_path=entry.get("actor_path"),
            )
            actor_key = entry.get("actor_path") or entry.get("actor_name")
            if actor_key in bound_actor_keys and bound_actor_keys[actor_key] != entry["link"]:
                raise ValueError(
                    f"actor {actor_key!r} is assigned to multiple links: "
                    f"{bound_actor_keys[actor_key]!r}, {entry['link']!r}"
                )
            bound_actor_keys[actor_key] = entry["link"]
        _attach_actor(armor, link)
        if entry.get("clone_per_link"):
            _reset_actor_relative_transform(game, armor)
        _set_material(game, armor, entry["material_slot"], entry.get("material_asset"))
        link_actors[entry["link"]] = link
        if binding_diagnostics is not None:
            binding_diagnostics.setdefault("armor_actors", []).append(armor)
            binding_diagnostics.setdefault("armor_clone_mesh_components", []).append(
                _count_cloned_tree_mesh_components(
                    game,
                    armor,
                ))

    camera_entry = plan.get("camera")
    if camera_entry is not None:
        link = _link_anchor(game, link_actors, camera_entry["link"])
        mode = camera_entry.get("mode", "spear_sensor")
        if mode not in {"spear_sensor", "cinecamera"}:
            raise ValueError(f"unsupported camera binding mode: {mode!r}")
        camera = _try_find_actor(
            game,
            name=camera_entry.get("actor_name"),
            actor_path=camera_entry.get("actor_path"),
        )
        if camera is None:
            if mode == "cinecamera":
                raise RuntimeError(
                    "cinecamera binding requires an existing camera actor: "
                    f"{camera_entry.get('actor_path') or camera_entry.get('actor_name')!r}"
                )
            camera = _spawn_spear_camera(game, camera_entry)
        if mode == "spear_sensor":
            capture_component = _get_spear_capture_component(
                game,
                camera,
                camera_entry,
                initialize=False,
            )
            if capture_components is not None:
                capture_components["camera"] = capture_component
        _attach_actor(camera, link)
        _attach_imu_marker(game, camera, plan["imu"])
        link_actors[camera_entry["link"]] = link

    return link_actors


def get_scene_camera_capture_component(game: Any, plan: dict[str, Any]) -> Any:
    """Return the initialized SPEAR capture component for a bound camera.

    ``apply_scene_binding_plan`` may have spawned the sensor, so callers can
    obtain the component afterwards using the same generated plan.  A
    ``cinecamera`` binding has only a regular ``CameraComponent`` and therefore
    cannot be used as a ``SpearRenderPort`` capture source.
    """
    entry = plan.get("camera")
    if entry is None:
        raise RuntimeError("binding plan has no camera")
    if entry.get("mode", "spear_sensor") != "spear_sensor":
        raise RuntimeError("cinecamera binding has no SPEAR SceneCapture component")
    camera = _find_actor(
        game,
        name=entry.get("actor_name"),
        actor_path=entry.get("actor_path"),
    )
    return _get_spear_capture_component(game, camera, entry)


def make_scene_pose_synchronizer(
    link_actors: dict[str, Any],
    spear_math: Any,
    *,
    strict: bool = False,
    world_offset_cm: tuple[float, float, float] = (0.0, 0.0, 0.0),
    order: Any = None,
    use_send_async: bool = True,
) -> PoseSynchronizer:
    """Create the runtime MuJoCo-to-SPEAR link pose synchronizer."""

    if order is None:
        order = link_application_order({"links": []}, link_actors)
    return PoseSynchronizer(
        link_actors,
        make_spear_actor_applier(spear_math, use_send_async=use_send_async),
        strict=strict,
        world_offset_cm=world_offset_cm,
        order=order,
    )
