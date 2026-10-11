import json
import math
import os
import re
from pathlib import Path

import unreal


SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST = json.loads((SCRIPT_DIR / "rune_scene_manifest.json").read_text(encoding="utf-8"))


def actor_path(actor):
    parts = []
    current = actor
    while current:
        parts.append(current.get_actor_label())
        current = current.get_attach_parent_actor()
    return "/".join(reversed(parts))


def descendants(actor):
    result = []
    pending = list(actor.get_attached_actors())
    while pending:
        item = pending.pop(0)
        result.append(item)
        pending.extend(item.get_attached_actors())
    return result


def dot(left, right):
    return left.x * right.x + left.y * right.y + left.z * right.z


def local_position(root, actor):
    delta = actor.get_actor_location() - root.get_actor_location()
    return unreal.Vector(
        dot(delta, root.get_actor_forward_vector()),
        dot(delta, root.get_actor_right_vector()),
        dot(delta, root.get_actor_up_vector()))


def polar_angle(config, position):
    axis = config["rotation_axis_local"]
    dominant = max(range(3), key=lambda index: abs(axis[index]))
    if dominant == 0:
        return math.atan2(position.z, position.y)
    if dominant == 1:
        return math.atan2(position.z, position.x)
    return math.atan2(position.y, position.x)


def require_tag(actor, tag, errors):
    tags = [str(item) for item in actor.get_editor_property("tags")]
    if tag not in tags:
        errors.append(f"{actor_path(actor)} missing tag {tag}")


unreal.EditorLoadingAndSavingUtils.load_map(MANIFEST["level"])
actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = actor_subsystem.get_all_level_actors()
path_groups = {}
for actor in actors:
    path_groups.setdefault(actor_path(actor), []).append(actor)
by_path = {path: group[0] for path, group in path_groups.items() if len(group) == 1}

report = {
    "schema_version": MANIFEST["schema_version"],
    "level": MANIFEST["level"],
    "actor_count": len(actors),
    "bindings": {},
    "duplicate_paths": {path: len(group) for path, group in path_groups.items() if len(group) > 1},
    "errors": [],
}
report["errors"].extend(f"duplicate actor path: {path}" for path in report["duplicate_paths"])

for team in ("red", "blue"):
    config = MANIFEST[team]
    required = [config["visual_root"], config["center_root"], *config["axis_paths"]]
    missing = [path for path in required if path not in by_path]
    team_report = {"paths": required, "missing": missing, "arms": []}
    report["bindings"][team] = team_report
    report["errors"].extend(f"{team}: missing {path}" for path in missing)
    root = by_path.get(config["visual_root"])
    if not root:
        continue
    root_component = root.get_editor_property("root_component")
    team_report["root_mobility"] = str(root_component.get_editor_property("mobility"))
    if root_component.get_editor_property("mobility") != unreal.ComponentMobility.MOVABLE:
        report["errors"].append(f"{team}: visual root must be Movable")
    center = by_path.get(config["center_root"])
    if center:
        center_component = center.get_editor_property("root_component")
        team_report["center_mobility"] = str(center_component.get_editor_property("mobility"))
        if center_component.get_editor_property("mobility") != unreal.ComponentMobility.MOVABLE:
            report["errors"].append(f"{team}: center root must be Movable")
    team_title = team.title()
    require_tag(root, f"RMSim.Rune.{team_title}.Root", report["errors"])
    axes = [by_path[path] for path in config["axis_paths"] if path in by_path]
    axis_fans = []
    for axis in axes:
        fan_roots = [item for item in descendants(axis)
                     if re.fullmatch(r"fan1(?:_\d{3})?", item.get_actor_label())]
        axis_fans.append((axis, fan_roots))
    axis_fans.sort(key=lambda pair: polar_angle(config, local_position(root, pair[1][0]))
                   if pair[1] else 0.0)
    if len(axes) != 5:
        report["errors"].append(f"{team}: expected 5 axis actors, got {len(axes)}")
    for index, (axis, fan_roots) in enumerate(axis_fans):
        children = descendants(axis)
        fan = fan_roots
        flow = [item for item in children if item.get_actor_label().startswith("light_flow")]
        lights = [item for item in children if item.get_actor_label().startswith((
            "light_left", "light_right", "light_bottom", "light_up"))]
        if len(fan) != 1 or len(flow) != 1 or len(lights) != 4:
            report["errors"].append(
                f"{team}: {actor_path(axis)} expected fan=1 flow=1 lights=4, "
                f"got fan={len(fan)} flow={len(flow)} lights={len(lights)}")
        require_tag(axis, f"RMSim.Rune.{team_title}.Arm.{index}.Axis", report["errors"])
        for role, role_actors in (("Armor", fan), ("Fan", fan), ("Flow", flow), ("Light", lights)):
            for role_actor in role_actors:
                require_tag(role_actor, f"RMSim.Rune.{team_title}.Arm.{index}.{role}", report["errors"])
        relative = local_position(root, fan[0] if fan else axis)
        team_report["arms"].append({
            "index": index,
            "axis": actor_path(axis),
            "fan": [actor_path(item) for item in fan],
            "flow": [actor_path(item) for item in flow],
            "lights": [actor_path(item) for item in lights],
            "relative_location": [relative.x, relative.y, relative.z],
        })

camera_matches = [actor for actor in actors if actor.get_actor_label() == MANIFEST["fixed_camera"]]
report["fixed_camera_matches"] = [actor.get_path_name() for actor in camera_matches]
if len(camera_matches) != 1:
    report["errors"].append(f"expected one fixed camera, got {len(camera_matches)}")

output = Path(os.getenv("RMSIM_BIND_REPORT", unreal.Paths.project_saved_dir() + "RuneBindingReport.json"))
output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print("RMSIM_BIND_REPORT " + json.dumps(report, ensure_ascii=False))
if report["errors"]:
    raise RuntimeError("Rune scene inspection failed")
