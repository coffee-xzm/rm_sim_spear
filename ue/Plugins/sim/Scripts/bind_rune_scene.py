import json
import math
import os
import re
from pathlib import Path

import unreal


SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST = json.loads((SCRIPT_DIR / "rune_scene_manifest.json").read_text(encoding="utf-8"))
APPLY = os.getenv("RMSIM_BIND_APPLY", "0") == "1"


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


def set_subtree_movable(root):
    for actor in [root, *descendants(root)]:
        for component in actor.get_components_by_class(unreal.SceneComponent):
            component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)


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


def relative_transform(actor):
    component = actor.get_editor_property("root_component")
    return unreal.Transform(
        location=component.get_editor_property("relative_location"),
        rotation=component.get_editor_property("relative_rotation"),
        scale=component.get_editor_property("relative_scale3d"))


def find_generated(actors, label, actor_class):
    matches = [actor for actor in actors if actor.get_actor_label() == label]
    if len(matches) > 1:
        raise RuntimeError(f"duplicate generated actor label: {label}")
    if matches:
        if matches[0].get_class() != actor_class:
            raise RuntimeError(f"{label} has unexpected class")
        return matches[0]
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = subsystem.spawn_actor_from_class(actor_class, unreal.Vector(), unreal.Rotator())
    actor.set_actor_label(label)
    return actor


def build_arm(axis, armor_config):
    children = descendants(axis)
    fan = [item for item in children if re.fullmatch(r"fan1(?:_\d{3})?", item.get_actor_label())]
    flow = [item for item in children if item.get_actor_label().startswith("light_flow")]
    lights = [item for item in children if item.get_actor_label().startswith((
        "light_left", "light_right", "light_bottom", "light_up"))]
    horizontal_lights = [item for item in lights if item.get_actor_label().startswith(("light_left", "light_right"))]
    vertical_lights = [item for item in lights if item.get_actor_label().startswith(("light_bottom", "light_up"))]
    if len(fan) != 1 or len(flow) != 1 or len(lights) != 4:
        raise RuntimeError(
            f"{actor_path(axis)} expected fan=1 flow=1 lights=4, got "
            f"fan={len(fan)} flow={len(flow)} lights={len(lights)}")

    arm = unreal.RuneArmBinding()
    arm.set_editor_property("axis_actor", axis)
    arm.set_editor_property("armor_anchor_actor", fan[0])
    arm.set_editor_property("fan_actors", fan)
    arm.set_editor_property("flow_actors", flow)
    arm.set_editor_property("light_actors", lights)
    arm.set_editor_property("horizontal_light_actors", horizontal_lights)
    arm.set_editor_property("vertical_light_actors", vertical_lights)
    arm.set_editor_property("armor_radius_cm", armor_config["radius_cm"])
    arm.set_editor_property("armor_thickness_cm", armor_config["thickness_cm"])
    location = armor_config["local_location_cm"]
    rotation = armor_config["local_rotation_deg"]
    arm.set_editor_property("armor_local_transform", unreal.Transform(
        location=unreal.Vector(*location), rotation=unreal.Rotator(*rotation)))
    return arm


def build_side(controller, team_name, config, by_path, visual_profile):
    root = by_path[config["visual_root"]]
    center = by_path[config["center_root"]]
    set_subtree_movable(root)
    set_subtree_movable(center)
    axes = [by_path[path] for path in config["axis_paths"]]
    axis_arm_pairs = [(axis, build_arm(axis, MANIFEST["armor"])) for axis in axes]
    axis_arm_pairs.sort(key=lambda pair: polar_angle(
        config, local_position(root, pair[1].get_editor_property("armor_anchor_actor"))))
    axes = [pair[0] for pair in axis_arm_pairs]
    arms = [pair[1] for pair in axis_arm_pairs]
    binding = unreal.RuneSideBinding()
    binding.set_editor_property("visual_root_actor", root)
    binding.set_editor_property("center_root_actor", center)
    binding.set_editor_property("rotation_axis_local", unreal.Vector(*config["rotation_axis_local"]))
    binding.set_editor_property("rotation_sign", config["rotation_sign"])
    binding.set_editor_property("baseline_relative_transform", relative_transform(root))
    binding.set_editor_property("arms", arms)
    controller.set_editor_property("binding", binding)
    controller.set_editor_property("team_color", getattr(unreal.TeamColor, team_name.upper()))
    controller.set_editor_property("visual_profile", visual_profile)
    controller.set_editor_property("tags", [unreal.Name(f"RMSim.Rune.{team_name.title()}.Controller")])

    root_tags = list(root.get_editor_property("tags"))
    root_tag = unreal.Name(f"RMSim.Rune.{team_name.title()}.Root")
    if root_tag not in root_tags:
        root_tags.append(root_tag)
        root.set_editor_property("tags", root_tags)
    for index, axis in enumerate(axes):
        tags = list(axis.get_editor_property("tags"))
        tag = unreal.Name(f"RMSim.Rune.{team_name.title()}.Arm.{index}.Axis")
        if tag not in tags:
            tags.append(tag)
            axis.set_editor_property("tags", tags)
        roles = {
            "Armor": [arms[index].get_editor_property("armor_anchor_actor")],
            "Fan": arms[index].get_editor_property("fan_actors"),
            "Flow": arms[index].get_editor_property("flow_actors"),
            "Light": arms[index].get_editor_property("light_actors"),
        }
        for role, role_actors in roles.items():
            for role_actor in role_actors:
                role_tags = list(role_actor.get_editor_property("tags"))
                role_tag = unreal.Name(f"RMSim.Rune.{team_name.title()}.Arm.{index}.{role}")
                if role_tag not in role_tags:
                    role_tags.append(role_tag)
                    role_actor.set_editor_property("tags", role_tags)


unreal.EditorLoadingAndSavingUtils.load_map(MANIFEST["level"])
actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = actor_subsystem.get_all_level_actors()
path_groups = {}
for actor in actors:
    path_groups.setdefault(actor_path(actor), []).append(actor)
duplicate_paths = [path for path, group in path_groups.items() if len(group) > 1]
if duplicate_paths:
    raise RuntimeError("duplicate actor paths: " + ", ".join(duplicate_paths))
by_path = {path: group[0] for path, group in path_groups.items()}
required = []
for team in ("red", "blue"):
    required.extend([MANIFEST[team]["visual_root"], MANIFEST[team]["center_root"], *MANIFEST[team]["axis_paths"]])
missing = [path for path in required if path not in by_path]
if missing:
    raise RuntimeError("missing binding paths: " + ", ".join(missing))

cameras = [actor for actor in actors if actor.get_actor_label() == MANIFEST["fixed_camera"]]
if len(cameras) != 1:
    raise RuntimeError(f"expected one fixed camera, got {len(cameras)}")

# Validate every arm before creating or modifying generated actors.
validated_arms = {}
for team in ("red", "blue"):
    validated_arms[team] = [build_arm(by_path[path], MANIFEST["armor"])
                            for path in MANIFEST[team]["axis_paths"]]

if not APPLY:
    print(json.dumps({"dry_run": True, "level": MANIFEST["level"], "paths": required,
                      "actor_count": len(actors),
                      "arm_counts": {team: len(arms) for team, arms in validated_arms.items()},
                      "errors": []}, ensure_ascii=False))
else:
    rune_class = unreal.load_class(None, "/Script/sim.Rune")
    director_class = unreal.load_class(None, "/Script/sim.RuneSceneDirector")
    if not rune_class or not director_class:
        raise RuntimeError("sim runtime classes are not loaded")
    visual_profile = unreal.load_asset(MANIFEST["visual_profile"])
    if not visual_profile:
        raise RuntimeError(f"missing visual profile: {MANIFEST['visual_profile']}")

    red = find_generated(actors, "RMSim_RedRuneController", rune_class)
    if red not in actors:
        actors.append(red)
    blue = find_generated(actors, "RMSim_BlueRuneController", rune_class)
    if blue not in actors:
        actors.append(blue)
    director = find_generated(actors, "RMSim_RuneSceneDirector", director_class)
    build_side(red, "red", MANIFEST["red"], by_path, visual_profile)
    build_side(blue, "blue", MANIFEST["blue"], by_path, visual_profile)

    director.set_editor_property("red_rune", red)
    director.set_editor_property("blue_rune", blue)
    director.set_editor_property("fixed_camera", cameras[0])
    director.set_editor_property("tags", [unreal.Name("RMSim.Rune.Director")])

    unreal.EditorLevelLibrary.save_current_level()
    print(json.dumps({"applied": True, "red": red.get_path_name(), "blue": blue.get_path_name(),
                      "director": director.get_path_name()}, ensure_ascii=False))
