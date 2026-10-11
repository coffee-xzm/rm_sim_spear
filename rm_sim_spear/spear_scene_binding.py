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


def load_plan(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _find_actor(game: Any, name: str) -> Any:
    actor = game.unreal_service.find_actor_by_name(actor_name=name, uclass="AActor")
    if actor is None:
        raise RuntimeError(f"scene actor not found: {name!r}")
    return actor


def _find_or_spawn_anchor(game: Any, name: str) -> Any:
    try:
        return _find_actor(game, name)
    except Exception:
        anchor = game.unreal_service.spawn_actor(
            uclass="AActor",
            spawn_parameters={"SpawnCollisionHandlingOverride": "AlwaysSpawn"},
        )
        game.unreal_service.set_stable_name_for_actor(actor=anchor, stable_name=name)
        return anchor


def _attach_actor(actor: Any, parent: Any) -> None:
    # K2_AttachRootComponentToActor is exposed by SPEAR's UnrealObject wrapper.
    actor.call(
        function_name="K2_AttachRootComponentToActor",
        args={
            "InParentActor": parent,
            "LocationRule": "KeepRelative",
            "RotationRule": "KeepRelative",
            "ScaleRule": "KeepRelative",
            "bWeldSimulatedBodies": False,
        },
    )


def _set_material(game: Any, actor: Any, slot: int, asset_path: str | None) -> None:
    if not asset_path:
        return
    material = game.unreal_service.load_object(uclass="UMaterialInterface", name=asset_path)
    if material is None:
        raise RuntimeError(f"material asset not found: {asset_path!r}")
    components = game.unreal_service.get_components_by_class(actor=actor, uclass="UStaticMeshComponent")
    if not components:
        raise RuntimeError(f"armor actor has no UStaticMeshComponent: {actor}")
    for component in components:
        component.SetMaterial(ElementIndex=slot, Material=material)


def _attach_imu_marker(game: Any, camera: Any, imu: dict[str, Any]) -> Any:
    marker = game.unreal_service.create_scene_component_for_actor(
        uclass="USceneComponent",
        owner=camera,
        scene_component_name=imu["name"],
    )
    parent = camera.GetRootComponent()
    marker.K2_AttachToComponent(
        Parent=parent,
        LocationRule="KeepRelative",
        RotationRule="KeepRelative",
        ScaleRule="KeepRelative",
        bWeldSimulatedBodies=False,
    )
    xyz = imu["relative_position_m"]
    rpy = imu["relative_rpy_rad"]
    marker.SetRelativeLocation(NewLocation={"X": xyz[0] * 100.0, "Y": -xyz[1] * 100.0, "Z": xyz[2] * 100.0})
    marker.SetRelativeRotation(NewRotation={
        "Roll": -math.degrees(rpy[0]),
        "Pitch": -math.degrees(rpy[1]),
        "Yaw": math.degrees(rpy[2]),
    })
    return marker


def apply_scene_binding_plan(game: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Attach configured scene actors and return the runtime link map.

    The returned map is consumed by the MuJoCo-to-SPEAR pose loop. It maps a
    MuJoCo link name to the UE Actor that should receive its world transform.
    """
    if plan.get("warnings"):
        raise ValueError("binding plan has warnings: " + "; ".join(plan["warnings"]))

    link_actors: dict[str, Any] = {}
    for entry in plan.get("armor", []):
        link = _find_or_spawn_anchor(game, entry["link"])
        armor = _find_actor(game, entry["actor_name"])
        _attach_actor(armor, link)
        _set_material(game, armor, entry["material_slot"], entry.get("material_asset"))
        link_actors[entry["link"]] = link

    camera_entry = plan.get("camera")
    if camera_entry is not None:
        link = _find_or_spawn_anchor(game, camera_entry["link"])
        camera = _find_actor(game, camera_entry["actor_name"])
        _attach_actor(camera, link)
        _attach_imu_marker(game, camera, plan["imu"])
        link_actors[camera_entry["link"]] = link

    return link_actors
