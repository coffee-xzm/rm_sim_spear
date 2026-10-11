"""Import a skinned ``.glb`` as a ``USkeletalMesh`` (+ ``USkeleton``) asset.

Run as an editor commandlet, exactly like ``import_urdf_visuals.py``::

    env DISPLAY=:1 .../UnrealEditor-Cmd <uproject> -run=pythonscript \\
      -script="/abs/path/tools/import_skinned_robot.py --glb /abs/path/robot.glb \\
        --destination /Game/RMSim --name 总装_skinned" -unattended -nop4 -nosplash

The whole script path **and** its arguments must be one quoted token: the
``-script=`` parser stops at the first space, so an unquoted ``--glb`` silently
becomes an Unreal command-line switch and this script runs with its defaults.

Why a separate script from ``import_urdf_visuals.py``: that one places one actor
per visual and deliberately shares StaticMesh assets between links, which is the
opposite of what a skeletal mesh wants.  This one only produces the asset; the
actor that owns it is spawned at runtime by
:mod:`rm_sim_spear.skeletal_robot`, so nothing about the level has to change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_DESTINATION = "/Game/RMSim"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a skinned glTF as a SkeletalMesh")
    parser.add_argument("--glb", required=True, type=Path, help="the .glb to import")
    parser.add_argument("--destination", default=DEFAULT_DESTINATION,
                        help=f"content path for the new assets (default {DEFAULT_DESTINATION})")
    parser.add_argument("--name", default=None,
                        help="asset name (default: the .glb file stem)")
    parser.add_argument("--report", type=Path, default=None,
                        help="write the JSON report here instead of only stdout")
    return parser.parse_args(argv)


def object_path(unreal, obj) -> str:
    """Return ``/Game/...`` for an imported object, falling back to its name."""
    try:
        return str(unreal.EditorAssetLibrary.get_path_name_for_loaded_asset(obj))
    except Exception:  # diagnostics only
        return str(obj.get_name())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    import unreal

    glb = args.glb.expanduser().resolve()
    if not glb.is_file():
        raise SystemExit(f"--glb does not exist: {glb}")
    name = args.name or glb.stem

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", str(glb))
    task.set_editor_property("destination_path", args.destination)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    tools.import_asset_tasks([task])

    imported = []
    skeletal_meshes = []
    skeletons = []
    for obj in task.get_objects():
        path = object_path(unreal, obj)
        class_name = obj.get_class().get_name()
        imported.append({"path": path, "class": class_name})
        if class_name == "SkeletalMesh":
            skeletal_meshes.append(path)
        elif class_name == "Skeleton":
            skeletons.append(path)

    for path in skeletal_meshes + skeletons:
        try:
            unreal.EditorAssetLibrary.save_asset(path)
        except Exception as exc:  # report, do not mask the import
            imported.append({"path": path, "save_error": f"{type(exc).__name__}: {exc}"})

    report = {
        "event": "skinned_import",
        "glb": str(glb),
        "glb_bytes": glb.stat().st_size,
        "destination": args.destination,
        "name": name,
        "imported": imported,
        "skeletal_meshes": skeletal_meshes,
        "skeletons": skeletons,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))

    if not skeletal_meshes:
        # Failing loudly matters: a glTF whose skins block was dropped imports
        # perfectly happily as a pile of StaticMeshes, and the run afterwards
        # would then "measure" a robot that silently never moves.
        print(json.dumps({
            "event": "error",
            "glb": str(glb),
            "message": "no SkeletalMesh was imported; check that the .glb has a skin",
        }, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
