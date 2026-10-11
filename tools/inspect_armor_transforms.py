"""Dump the four armor child actor transforms used by the scene binding."""

from __future__ import annotations

import json
from pathlib import Path

import unreal


LEVEL = "/Game/creo_stp"
OUTPUT = Path("/tmp/rm_sim_spear_armor_transforms.json")


def actor_path(actor):
    parts = []
    current = actor
    while current:
        parts.append(current.get_actor_label())
        current = current.get_attach_parent_actor()
    return "/".join(reversed(parts))


if not unreal.EditorLoadingAndSavingUtils.load_map(LEVEL):
    raise RuntimeError(f"failed to load level: {LEVEL}")

subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
wanted = {"前哨站_body36_2", "前哨站_body37_2", "前哨站_body38_2", "前哨站_body398_2"}
result = []
for actor in subsystem.get_all_level_actors():
    if actor.get_actor_label() not in wanted:
        continue
    location = actor.get_actor_location()
    rotation = actor.get_actor_rotation()
    result.append({
        "label": actor.get_actor_label(),
        "path": actor_path(actor),
        "location_cm": [location.x, location.y, location.z],
        "rotation_deg": [rotation.roll, rotation.pitch, rotation.yaw],
        "class": actor.get_class().get_path_name(),
    })
result.sort(key=lambda item: item["label"])
OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
unreal.log(f"rm_sim_spear wrote {len(result)} armor transforms to {OUTPUT}")
if len(result) != 4:
    raise RuntimeError(f"expected four armor children, found {len(result)}")
