"""Expand URDF scene binding rules into a deterministic link binding plan."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def expand_plan(manifest: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    link_names = [link["name"] for link in manifest["links"]]
    bindings = config.get("scene_bindings", {})
    armor = bindings.get("armor", {})
    armor_pattern = re.compile(armor.get("link_regex", r"^armor_link.*$"))
    armor_links = [name for name in link_names if armor_pattern.fullmatch(name)]
    armor_entries = []
    for index, link_name in enumerate(armor_links, start=1):
        template = armor.get("actor_name_template", "armor_{index}")
        armor_entries.append({
            "link": link_name,
            "actor_name": template.format(index=index, link=link_name),
            "material_slot": int(armor.get("material_slot", 0)),
            "material_asset": armor.get("material_asset"),
        })

    camera = bindings.get("camera", {})
    camera_pattern = re.compile(camera.get("link_regex", r"^cam.*$"))
    camera_links = [name for name in link_names if camera_pattern.fullmatch(name)]
    camera_entry = None
    if camera_links:
        camera_entry = {
            "link": camera_links[0],
            "actor_name": camera.get("actor_name", "camera_sensor"),
            "ue_class": camera.get("ue_class"),
        }

    imu = bindings.get("imu", {})
    imu_entry = {
        "parent": camera_entry["link"] if camera_entry else None,
        "name": imu.get("name", "imu_marker"),
        "relative_position_m": list(imu.get("relative_position_m", [0.0, 0.0, 0.0])),
        "relative_rpy_rad": list(imu.get("relative_rpy_rad", [0.0, 0.0, 0.0])),
    }
    warnings = []
    if not armor_entries:
        warnings.append("no links matched armor.link_regex")
    if camera_entry is None:
        warnings.append("no links matched camera.link_regex; camera and IMU cannot be attached")
    if camera_entry is not None and imu_entry["parent"] is None:
        warnings.append("IMU has no camera parent")
    return {
        "schema_version": 1,
        "robot": manifest["robot"],
        "manifest": manifest.get("source_urdf"),
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
