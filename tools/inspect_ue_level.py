"""Print stable labels and classes from a UE level for binding setup."""

import json
import sys
from pathlib import Path

import unreal


level = sys.argv[1] if len(sys.argv) > 1 else "/Game/creo_stp"
if not unreal.EditorLoadingAndSavingUtils.load_map(level):
    raise RuntimeError(f"failed to load level: {level}")

subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = subsystem.get_all_level_actors()
report = []
for actor in sorted(actors, key=lambda item: item.get_actor_label()):
    root = actor.get_editor_property("root_component")
    label = actor.get_actor_label()
    relevant = any(term in label.lower() for term in ("armor", "camera", "相机", "底盘", "总装"))
    report.append({
        "label": label,
        "name": actor.get_name(),
        "class": actor.get_class().get_path_name(),
        "mobility": str(root.get_editor_property("mobility")) if root else None,
        "tags": [str(tag) for tag in actor.get_editor_property("tags")],
        "parent": actor.get_attach_parent_actor().get_actor_label() if actor.get_attach_parent_actor() else None,
        "attached": [child.get_actor_label() for child in actor.get_attached_actors()] if relevant else [],
        "components": [component.get_name() for component in actor.get_components_by_class(unreal.ActorComponent)] if relevant else [],
    })
payload = {"level": level, "actor_count": len(report), "actors": report}
Path("/tmp/rm_sim_spear_ue_level_report.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
unreal.log(f"rm_sim_spear level inspection wrote {len(report)} actors")
