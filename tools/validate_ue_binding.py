"""Validate a generated scene binding plan against a loaded UE level."""

from __future__ import annotations

import json
import os
from pathlib import Path

import unreal


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = Path(os.environ.get(
    "RMSIM_BINDING_PLAN",
    str(REPO_ROOT / "generated/urdf/总装/scene_binding_plan.json"),
))
LEVEL = os.environ.get("RMSIM_LEVEL", "/Game/creo_stp")
OUTPUT = Path(os.environ.get("RMSIM_BINDING_REPORT", "/tmp/rm_sim_spear_binding_report.json"))

plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
if not unreal.EditorLoadingAndSavingUtils.load_map(LEVEL):
    raise RuntimeError(f"failed to load level: {LEVEL}")

subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = subsystem.get_all_level_actors()
by_label: dict[str, list] = {}
by_path: dict[str, list] = {}


def actor_path(actor):
    parts = []
    current = actor
    while current:
        parts.append(current.get_actor_label())
        current = current.get_attach_parent_actor()
    return "/".join(reversed(parts))


for actor in actors:
    by_label.setdefault(actor.get_actor_label(), []).append(actor)
    by_path.setdefault(actor_path(actor), []).append(actor)

required_names = []
required_paths = []


def append_unique(items, value):
    if value and value not in items:
        items.append(value)


camera = plan.get("camera")
for entry in plan.get("armor", []):
    if entry.get("actor_path"):
        append_unique(required_paths, entry["actor_path"])
    else:
        append_unique(required_names, entry["actor_name"])

# A SPEAR-created camera sensor is intentionally absent from the map.  A
# CineCameraActor, in contrast, must be present because it has no spawn fallback.
camera_mode = camera.get("mode", "spear_sensor") if camera else None
if camera is not None and camera_mode == "cinecamera":
    if camera.get("actor_path"):
        append_unique(required_paths, camera["actor_path"])
    else:
        append_unique(required_names, camera["actor_name"])
elif camera is not None and camera_mode not in {"spear_sensor", "cinecamera"}:
    raise RuntimeError(f"unsupported camera binding mode: {camera_mode!r}")

missing_names = sorted(name for name in required_names if name not in by_label)
missing_paths = sorted(path for path in required_paths if path not in by_path)
duplicates_names = sorted(name for name in required_names if len(by_label.get(name, [])) > 1)
duplicates_paths = sorted(path for path in required_paths if len(by_path.get(path, [])) > 1)
missing = sorted([*missing_names, *missing_paths])
duplicates = sorted([*duplicates_names, *duplicates_paths])
required = [*required_names, *required_paths]
camera_class_errors = []
if camera is not None and camera_mode == "cinecamera":
    camera_identifier = camera.get("actor_path") or camera.get("actor_name")
    camera_matches = (by_path if camera.get("actor_path") else by_label).get(camera_identifier, [])
    if camera_matches and not any(
        actor.get_class().get_path_name().endswith("CineCameraActor")
        for actor in camera_matches
    ):
        camera_class_errors.append(camera_identifier)
found = {
    identifier: {
        "count": len(matches),
        "classes": sorted({actor.get_class().get_path_name() for actor in matches}),
        "mobility": sorted({str(actor.get_editor_property("root_component").get_editor_property("mobility"))
                             for actor in matches if actor.get_editor_property("root_component")}),
    }
    for identifier, matches in [
        *[(name, by_label[name]) for name in required_names if name in by_label],
        *[(path, by_path[path]) for path in required_paths if path in by_path],
    ]
}

report = {
    "level": LEVEL,
    "plan": str(PLAN_PATH),
    "robot": plan.get("robot"),
    "actor_count": len(actors),
    "required_actor_names": required_names,
    "required_actor_paths": required_paths,
    "camera_mode": camera_mode,
    "actor_paths": sorted(by_path),
    "found": found,
    "missing": missing,
    "duplicates": duplicates,
    "camera_class_errors": camera_class_errors,
    "ready": not missing and not duplicates and not camera_class_errors,
}
OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
unreal.log(f"rm_sim_spear binding validation ready={report['ready']} missing={len(missing)} duplicates={len(duplicates)}")
if not report["ready"]:
    raise RuntimeError(
        "scene binding is not ready; "
        f"missing={missing}, duplicates={duplicates}, "
        f"camera_class_errors={camera_class_errors}; report={OUTPUT}"
    )
