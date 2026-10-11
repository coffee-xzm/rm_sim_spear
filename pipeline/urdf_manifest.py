"""Parse URDF files into a UE/MuJoCo integration manifest.

The manifest is deliberately engine independent.  UE Editor Python can use it
to build a link hierarchy, while the runtime bridge uses the same link names to
pair UE actors with MuJoCo bodies.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _floats(value: str | None, count: int, default: tuple[float, ...]) -> list[float]:
    if not value:
        return list(default)
    values = [float(item) for item in value.replace(",", " ").split()]
    if len(values) != count:
        raise ValueError(f"expected {count} values, got {len(values)}: {value!r}")
    return values


def _origin(element: ET.Element | None) -> dict[str, list[float]]:
    return {
        "xyz": _floats(element.get("xyz") if element is not None else None, 3, (0.0, 0.0, 0.0)),
        "rpy": _floats(element.get("rpy") if element is not None else None, 3, (0.0, 0.0, 0.0)),
    }


def _geometry(element: ET.Element | None) -> dict[str, Any] | None:
    if element is None:
        return None
    child = next(iter(element), None)
    if child is None:
        return None
    kind = child.tag
    result: dict[str, Any] = {"type": kind}
    if kind == "mesh":
        result["filename"] = child.get("filename")
        if child.get("scale"):
            result["scale"] = _floats(child.get("scale"), 3, (1.0, 1.0, 1.0))
    elif kind == "box":
        result["size"] = _floats(child.get("size"), 3, (0.0, 0.0, 0.0))
    elif kind == "cylinder":
        result["radius"] = float(child.get("radius", "0"))
        result["length"] = float(child.get("length", "0"))
    elif kind == "sphere":
        result["radius"] = float(child.get("radius", "0"))
    else:
        result["attributes"] = dict(child.attrib)
    return result


def _mesh_path(urdf_path: Path, filename: str | None) -> Path | None:
    if not filename:
        return None
    if filename.startswith("package://"):
        # package://pkg/path is resolved against the URDF directory first. A
        # package workspace can be supplied later by the UE importer.
        relative = filename.removeprefix("package://").split("/", 1)[-1]
    else:
        relative = filename.removeprefix("file://")
    return (urdf_path.parent / relative).resolve()


def _visual_or_collision(urdf_path: Path, element: ET.Element, index: int) -> dict[str, Any]:
    geometry = _geometry(element.find("geometry"))
    mesh_file = geometry.get("filename") if geometry and geometry.get("type") == "mesh" else None
    mesh_path = _mesh_path(urdf_path, mesh_file)
    material = element.find("material")
    color = material.find("color") if material is not None else None
    return {
        "name": element.get("name") or f"visual_{index:03d}",
        "origin": _origin(element.find("origin")),
        "geometry": geometry,
        "material": {
            "name": material.get("name") if material is not None else None,
            "rgba": _floats(color.get("rgba") if color is not None else None, 4, (0.7, 0.7, 0.7, 1.0)),
        },
        "mesh_path": str(mesh_path) if mesh_path is not None else None,
        "mesh_exists": mesh_path.is_file() if mesh_path is not None else None,
    }


def parse_urdf(path: str | Path) -> dict[str, Any]:
    urdf_path = Path(path).expanduser().resolve()
    root = ET.parse(urdf_path).getroot()
    if root.tag != "robot":
        raise ValueError(f"{urdf_path} is not a URDF robot document")

    links: list[dict[str, Any]] = []
    for link in root.findall("link"):
        inertial = link.find("inertial")
        inertia = inertial.find("inertia") if inertial is not None else None
        links.append({
            "name": link.get("name"),
            "visuals": [_visual_or_collision(urdf_path, item, i) for i, item in enumerate(link.findall("visual"))],
            "collisions": [_visual_or_collision(urdf_path, item, i) for i, item in enumerate(link.findall("collision"))],
            "inertial": None if inertial is None else {
                "mass_kg": float(inertial.find("mass").get("value", "0")) if inertial.find("mass") is not None else 0.0,
                "origin": _origin(inertial.find("origin")),
                "inertia": None if inertia is None else {key: float(inertia.get(key, "0")) for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")},
            },
        })

    joints: list[dict[str, Any]] = []
    child_links: set[str] = set()
    for joint in root.findall("joint"):
        parent = joint.find("parent")
        child = joint.find("child")
        if parent is None or child is None:
            raise ValueError(f"joint {joint.get('name')} has no parent or child")
        child_name = child.get("link")
        child_links.add(child_name)
        axis = joint.find("axis")
        limit = joint.find("limit")
        joints.append({
            "name": joint.get("name"),
            "type": joint.get("type"),
            "parent": parent.get("link"),
            "child": child_name,
            "origin": _origin(joint.find("origin")),
            "axis": _floats(axis.get("xyz") if axis is not None else None, 3, (1.0, 0.0, 0.0)),
            "limit": None if limit is None else {key: float(limit.get(key, "0")) for key in ("lower", "upper", "effort", "velocity") if limit.get(key) is not None},
        })

    link_names = {item["name"] for item in links}
    roots = sorted(link_names - child_links)
    if len(roots) != 1:
        raise ValueError(f"URDF must have exactly one root link, found {roots}")
    missing_meshes = sorted({item["mesh_path"] for link in links for group in ("visuals", "collisions") for item in link[group] if item["mesh_exists"] is False})
    return {
        "schema_version": 1,
        "robot": root.get("name") or urdf_path.stem,
        "source_urdf": str(urdf_path),
        "units": {"urdf_length": "m", "ue_length": "cm"},
        "root_link": roots[0],
        "links": links,
        "joints": joints,
        "validation": {
            "link_count": len(links),
            "joint_count": len(joints),
            "missing_meshes": missing_meshes,
            "unsupported_joint_types": sorted({item["type"] for item in joints if item["type"] not in {"fixed", "revolute", "continuous", "prismatic", "floating", "planar"}}),
            "mujoco_incompatible_meshes": [],
        },
    }


def _absolute_mesh_tree(path: Path) -> ET.Element:
    root = ET.parse(path).getroot()
    for mesh in root.findall(".//mesh"):
        filename = mesh.get("filename")
        resolved = _mesh_path(path, filename)
        if resolved is not None:
            mesh.set("filename", str(resolved))
    return root


def find_mujoco_incompatible_meshes(path: str | Path) -> list[str]:
    """Return mesh basenames that MuJoCo cannot decode, without editing assets."""
    try:
        import mujoco
    except ImportError:
        return []

    urdf_path = Path(path).expanduser().resolve()
    root = _absolute_mesh_tree(urdf_path)
    incompatible: list[str] = []
    for _ in range(128):
        with tempfile.TemporaryDirectory(prefix="rm_sim_spear_urdf_") as temp_dir:
            temp_path = Path(temp_dir) / "robot.urdf"
            ET.ElementTree(root).write(temp_path, encoding="utf-8", xml_declaration=True)
            try:
                mujoco.MjModel.from_xml_path(str(temp_path))
                return incompatible
            except Exception as exc:
                match = re.search(r"mesh file '([^']+)'", str(exc))
                if match is None:
                    return incompatible
                bad_path = Path(match.group(1))
                bad_name = bad_path.name
                if bad_name in incompatible:
                    return incompatible
                incompatible.append(bad_name)
                removed = 0
                for link in root.findall("link"):
                    for tag in ("visual", "collision"):
                        for item in list(link.findall(tag)):
                            mesh = item.find("./geometry/mesh")
                            if mesh is not None and Path(mesh.get("filename", "")).name == bad_name:
                                link.remove(item)
                                removed += 1
                if removed == 0:
                    return incompatible
    return incompatible


def write_mujoco_urdf(path: str | Path, output_path: str | Path, ignored_meshes: set[str]) -> Path:
    """Write a self-contained local URDF with ignored mesh references removed."""
    source = Path(path).expanduser().resolve()
    root = _absolute_mesh_tree(source)
    for link in root.findall("link"):
        for tag in ("visual", "collision"):
            for item in list(link.findall(tag)):
                mesh = item.find("./geometry/mesh")
                if mesh is not None and Path(mesh.get("filename", "")).name in ignored_meshes:
                    link.remove(item)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
    return destination


def _safe_name(value: str) -> str:
    # Keep Unicode robot names; only path separators and control punctuation
    # are unsafe here. This makes generated directories stable for the assets.
    return re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_") or "robot"


def make_override_template(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "robot": manifest["robot"],
        "manifest": "manifest.json",
        "default_visual_source": "urdf",
        "ignored_meshes": manifest["validation"].get("mujoco_incompatible_meshes", []),
        "scene_bindings": {
            "armor": {
                "link_regex": "^armor_link.*$",
                "actor_name_template": "armor_{index}",
                "material_slot": 0,
                "material_asset": None,
            },
            "camera": {
                "link_regex": "^cam.*$",
                "actor_name": "camera_sensor",
                "ue_class": "/SpContent/Blueprints/BP_CameraSensor.BP_CameraSensor_C",
            },
            "imu": {
                "parent": "camera",
                "name": "imu_marker",
                "relative_position_m": [0.0, 0.0, 0.0],
                "relative_rpy_rad": [0.0, 0.0, 0.0],
            },
        },
        "links": {
            link["name"]: {
                "visual_source": "urdf",
                "ue_asset": None,
                "local_transform": {"xyz_m": [0.0, 0.0, 0.0], "rpy_rad": [0.0, 0.0, 0.0]},
                "runtime_bindings": [],
            }
            for link in manifest["links"]
        },
    }


def write_outputs(urdf_path: str | Path, output_dir: str | Path) -> tuple[Path, Path]:
    manifest = parse_urdf(urdf_path)
    manifest["validation"]["mujoco_incompatible_meshes"] = find_mujoco_incompatible_meshes(urdf_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    override_path = output / "ue_overrides.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    override_path.write_text(json.dumps(make_override_template(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_mujoco_urdf(urdf_path, output / "mujoco.urdf", set(manifest["validation"]["mujoco_incompatible_meshes"]))
    return manifest_path, override_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate UE/MuJoCo manifests from URDF")
    parser.add_argument("urdf", nargs="+", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("generated/urdf"))
    args = parser.parse_args()
    for urdf in args.urdf:
        manifest = parse_urdf(urdf)
        output_dir = args.output_root / _safe_name(manifest["robot"])
        manifest_path, override_path = write_outputs(urdf, output_dir)
        print(f"{manifest['robot']}: {len(manifest['links'])} links, {len(manifest['joints'])} joints")
        print(f"  manifest: {manifest_path}")
        print(f"  overrides: {override_path}")
        ignored = manifest["validation"]["mujoco_incompatible_meshes"]
        if ignored:
            print(f"  ignored MuJoCo meshes: {len(ignored)}")
        if manifest["validation"]["missing_meshes"]:
            print(f"  missing meshes: {len(manifest['validation']['missing_meshes'])}")


if __name__ == "__main__":
    main()
