"""Expand URDF scene binding rules into a deterministic link binding plan."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _link_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe every URDF link so a runtime binder can pose each one.

    A URDF importer that places one visual actor per link needs the joint that
    positions each link, not just the aggregate robot root.  Links stay in
    manifest order (the manifest already lists the root first), and the origin
    uses the same left-handed centimeter convention as the pose bridge:
    ``(100x, -100y, 100z)`` in UE, with the URDF rpy retained in radians.
    """
    joints_by_child = {joint["child"]: joint for joint in manifest.get("joints", [])}
    entries = []
    for link in manifest["links"]:
        joint = joints_by_child.get(link["name"])
        origin = joint.get("origin") if joint else None
        xyz = list((origin or {}).get("xyz") or (0.0, 0.0, 0.0))
        rpy = list((origin or {}).get("rpy") or (0.0, 0.0, 0.0))
        entries.append({
            "name": link["name"],
            # A link with no incoming joint is a root (or a disconnected link);
            # it sits at the world origin because nothing positions it.
            "parent": joint["parent"] if joint else None,
            "joint": joint["name"] if joint else None,
            # ``or 0.0`` folds the IEEE ``-0.0`` produced by the Y flip so the
            # written plan never contains a negative zero.
            "origin_ue_cm": [
                value or 0.0
                for value in (100.0 * xyz[0], -100.0 * xyz[1], 100.0 * xyz[2])
            ],
            "origin_rpy_rad": [float(value) for value in rpy],
        })
    return entries


def expand_plan(manifest: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    link_names = [link["name"] for link in manifest["links"]]
    bindings = config.get("scene_bindings", {})
    # Older override files predate the explicit robot binding.  Keep those
    # files usable: the level's aggregate body actor is the visual root for
    # the generated URDF unless a caller explicitly disables it with null.
    robot = bindings.get("robot", {
        "link": "base_link",
        "actor_name": "body",
        "actor_path": "Scene/body",
        "reset_relative_transform": True,
    })
    robot_entry = None
    if robot:
        root_link = robot.get("link", "base_link")
        if root_link in link_names:
            robot_entry = {
                "link": root_link,
                "actor_name": robot.get("actor_name", "body"),
                "actor_path": robot.get("actor_path"),
                "reset_relative_transform": bool(robot.get("reset_relative_transform", False)),
            }
            if robot.get("runtime_actor_name"):
                robot_entry["runtime_actor_name"] = robot["runtime_actor_name"]
            if robot.get("runtime_child_names"):
                robot_entry["runtime_child_names"] = list(robot["runtime_child_names"])
            # ``mode`` selects how the binder finds the robot visuals.  Only
            # copy it when configured so existing plans stay byte-identical.
            if robot.get("mode"):
                robot_entry["mode"] = robot["mode"]
            # The importer tags each link actor (for example
            # ``MuJoCoBody=base_link``) because UE tags behave the same in PIE
            # and in a standalone game, unlike editor labels.
            robot_entry["link_actor_tag"] = robot.get(
                "link_actor_tag", "MuJoCoBody={link}")
    armor_config = bindings.get("armor", {})
    # ``"armor": null`` and ``"enabled": false`` both switch the section off on
    # purpose, which is not the same as leaving the key out (that keeps the
    # legacy default regex).  A link whose visual now comes from a per-link
    # ``visual_source: ue_asset`` override must not also be bound to a scene
    # template actor: the importer already placed the real mesh under the link
    # anchor, so a clone would only duplicate it (and drag the template's own
    # group along).  No warning is emitted for a deliberate switch-off -- the
    # binder treats any warning as fatal.
    armor_enabled = armor_config is not None and armor_config.get("enabled", True) is not False
    armor_entries: list[dict[str, Any]] = []
    if armor_enabled:
        armor = armor_config
        armor_pattern = re.compile(armor.get("link_regex", r"^armor_link.*$"))
        armor_links = [name for name in link_names if armor_pattern.fullmatch(name)]
        for index, link_name in enumerate(armor_links, start=1):
            template = armor.get("actor_name_template", "armor_{index}")
            entry = {
                "link": link_name,
                "actor_name": template.format(index=index, link=link_name),
                "material_slot": int(armor.get("material_slot", 0)),
                "material_asset": armor.get("material_asset"),
            }
            if armor.get("runtime_actor_name"):
                entry["runtime_actor_name"] = armor["runtime_actor_name"]
            if armor.get("runtime_child_names"):
                entry["runtime_child_names"] = list(armor["runtime_child_names"])
            if armor.get("actor_path"):
                entry["actor_path"] = armor["actor_path"].format(index=index, link=link_name)
            if armor.get("clone_per_link"):
                entry["clone_per_link"] = True
                clone_template = armor.get("clone_name_template", "RMSim/armor_{index}")
                entry["clone_name"] = clone_template.format(index=index, link=link_name)
                entry["hide_template"] = bool(armor.get("hide_template", True))
            armor_entries.append(entry)

    camera = bindings.get("camera", {})
    camera_pattern = re.compile(camera.get("link_regex", r"^cam.*$"))
    camera_links = [name for name in link_names if camera_pattern.fullmatch(name)]
    camera_entry = None
    if camera_links:
        camera_entry = {
            "link": camera_links[0],
            "actor_name": camera.get("actor_name", "camera_sensor"),
            "ue_class": camera.get("ue_class"),
            "mode": camera.get("mode", "spear_sensor"),
        }
        if camera.get("actor_path"):
            camera_entry["actor_path"] = camera["actor_path"].format(link=camera_links[0])

    imu = bindings.get("imu", {})
    imu_entry = {
        "parent": camera_entry["link"] if camera_entry else None,
        "name": imu.get("name", "imu_marker"),
        "relative_position_m": list(imu.get("relative_position_m", [0.0, 0.0, 0.0])),
        "relative_rpy_rad": list(imu.get("relative_rpy_rad", [0.0, 0.0, 0.0])),
    }
    warnings = []
    if armor_enabled and not armor_entries:
        warnings.append("no links matched armor.link_regex")
    if camera_entry is None:
        warnings.append("no links matched camera.link_regex; camera and IMU cannot be attached")
    if camera_entry is not None and imu_entry["parent"] is None:
        warnings.append("IMU has no camera parent")
    return {
        "schema_version": 1,
        "robot": manifest["robot"],
        "manifest": manifest.get("source_urdf"),
        "robot_visual": robot_entry,
        "links": _link_entries(manifest),
        "armor": armor_entries,
        "camera": camera_entry,
        "imu": imu_entry,
        "warnings": warnings,
    }


def write_plan(manifest_path: str | Path, config_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    plan = expand_plan(manifest, config)
    Path(output_path).write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand UE scene binding rules for a URDF manifest")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = write_plan(args.manifest, args.config, args.output)
    print(f"{plan['robot']}: armor={len(plan['armor'])}, camera={plan['camera'] is not None}")
    for warning in plan["warnings"]:
        print(f"warning: {warning}")


if __name__ == "__main__":
    main()
