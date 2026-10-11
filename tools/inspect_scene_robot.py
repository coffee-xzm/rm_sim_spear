"""Read-only report for the scene actors used by the URDF binding plan."""

import json
from pathlib import Path

import unreal


LEVEL = "/Game/creo_stp"
OUTPUT = Path("/tmp/rm_sim_spear_scene_robot.json")

if not unreal.EditorLoadingAndSavingUtils.load_map(LEVEL):
    raise RuntimeError(f"failed to load level: {LEVEL}")

subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
wanted = {"Scene", "body", "armor"}
rows = []
for actor in subsystem.get_all_level_actors():
    label = actor.get_actor_label()
    parent = actor.get_attach_parent_actor()
    parent_label = parent.get_actor_label() if parent else None
    try:
        folder = str(actor.get_folder_path())
    except Exception:
        folder = None
    if label not in wanted and parent_label not in {"Scene", "body", "armor"} and folder not in {"Scene", "Scene/body", "Scene/armor"}:
        continue
    location = actor.get_actor_location()
    scale = actor.get_actor_scale3d()
    origin, extent = actor.get_actor_bounds(only_colliding_components=False)
    components = []
    for component in actor.get_components_by_class(unreal.ActorComponent):
        row = {"name": component.get_name(), "class": component.get_class().get_path_name()}
        if isinstance(component, unreal.SceneComponent):
            row["mobility"] = str(component.get_editor_property("mobility"))
            row["visible"] = bool(component.get_editor_property("visible"))
            row["relative_location"] = [component.relative_location.x, component.relative_location.y, component.relative_location.z]
        if isinstance(component, unreal.StaticMeshComponent):
            mesh = component.get_editor_property("static_mesh")
            row["mesh"] = mesh.get_path_name() if mesh else None
        components.append(row)
    rows.append({
        "label": label,
        "name": actor.get_name(),
        "class": actor.get_class().get_path_name(),
        "parent": parent_label,
        "hidden_in_game": bool(actor.get_editor_property("hidden")) if hasattr(actor, "get_editor_property") else None,
        "editor_only": bool(actor.get_editor_property("is_editor_only_actor")) if hasattr(actor, "get_editor_property") else None,
        "folder": folder,
        "location": [location.x, location.y, location.z],
        "scale": [scale.x, scale.y, scale.z],
        "bounds_origin": [origin.x, origin.y, origin.z],
        "bounds_extent": [extent.x, extent.y, extent.z],
        "components": components,
    })

OUTPUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
unreal.log(f"wrote {len(rows)} scene robot actor rows to {OUTPUT}")
