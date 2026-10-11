"""Report the geometry facts of a UE Content folder without loading any asset.

Run with the editor commandlet (no PIE, no rendering):

    UnrealEditor-Cmd <project>.uproject -run=pythonscript \
        -script=tools/inspect_arena_assets.py -unattended -nop4 -nosplash

Why the AssetRegistry and not the assets: ``UStaticMesh::GetAssetRegistryTags``
(``Engine/Source/Runtime/Engine/Private/StaticMesh.cpp``) already publishes
everything this needs -- ``Triangles``, ``Vertices``, ``LODs``, ``Materials``,
``CollisionPrims``, ``CollisionComplexity``, ``NaniteEnabled``, ``NaniteTriangles``,
``EstTotalCompressedSize``, ``EstNaniteCompressedSize``, ``DistanceFieldSize``.
So the whole survey runs off tag strings, loads zero StaticMeshes and cannot
mutate anything.  Answering "is the arena Nanite?" this way is the point: the
project's ``r.Nanite=0`` rests on *"this scene's meshes are not Nanite"*, which
``docs/render_budget.md`` itself records as inferred from pixel equality and
never checked against an asset.

Arguments come from ``RMSIM_ARENA_*`` environment variables as well as the
command line, because ``-script=`` accepts only one token (see
``docs/urdf_workflow.md``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import unreal  # noqa: E402 - must follow the repo-root sys.path insert


DEFAULT_PATH = "/Game/RMUC2026/Geometries"
DEFAULT_REPORT = "/tmp/rm_sim_spear_arena_assets.json"

# Tag -> the numeric field it fills on each record.  ``Triangles`` et al. are
# ``FAssetRegistryTag::TT_Numerical`` strings; a missing tag means "not a
# StaticMesh" or "asset failed to compile", hence the ``None`` default.
NUMERIC_TAGS = {
    "Triangles": "triangles",
    "Vertices": "vertices",
    "LODs": "lods",
    "Materials": "materials",
    "CollisionPrims": "collision_prims",
    "NaniteTriangles": "nanite_triangles",
    "NaniteVertices": "nanite_vertices",
    "EstTotalCompressedSize": "est_compressed_bytes",
    "EstNaniteCompressedSize": "est_nanite_compressed_bytes",
    "DistanceFieldSize": "distance_field_bytes",
}
TEXT_TAGS = {
    "NaniteEnabled": "nanite_enabled",
    "CollisionComplexity": "collision_complexity",
    "DefaultCollision": "default_collision",
}


def _tag(asset_data, name: str):
    """``AssetData.get_tag_value`` or ``None``; never raises on a missing tag."""
    try:
        value = asset_data.get_tag_value(name)
    except Exception:  # noqa: BLE001 - the binding raises on unknown tags
        return None
    if value is None or value == "":
        return None
    return value


def _record(asset_data, package: str) -> dict:
    record: dict = {"package": package, "class": str(asset_data.asset_class_path.asset_name)}
    for tag, field in NUMERIC_TAGS.items():
        raw = _tag(asset_data, tag)
        try:
            record[field] = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            record[field] = None
    for tag, field in TEXT_TAGS.items():
        record[field] = _tag(asset_data, tag)
    return record


def _summarise(records: list[dict]) -> dict:
    """Aggregate counts and totals; ``None`` values are excluded, not zeroed."""
    def total(field: str) -> int:
        return sum(r[field] for r in records if r.get(field) is not None)

    def present(field: str) -> int:
        return sum(1 for r in records if r.get(field) is not None)

    nanite_true = sum(1 for r in records if r.get("nanite_enabled") == "True")
    mesh_records = [r for r in records if r.get("triangles") is not None]
    return {
        "assets": len(records),
        "static_meshes": len(mesh_records),
        "assets_without_triangle_tag": len(records) - len(mesh_records),
        "total_triangles": total("triangles"),
        "total_vertices": total("vertices"),
        "total_est_compressed_bytes": total("est_compressed_bytes"),
        "nanite_enabled_true": nanite_true,
        "nanite_enabled_false": sum(1 for r in records if r.get("nanite_enabled") == "False"),
        "nanite_enabled_unknown": len(records) - nanite_true
        - sum(1 for r in records if r.get("nanite_enabled") == "False"),
        "total_nanite_triangles": total("nanite_triangles"),
        "lod_histogram": dict(sorted(Counter(
            r["lods"] for r in records if r.get("lods") is not None).items())),
        "collision_complexity_histogram": dict(Counter(
            str(r.get("collision_complexity")) for r in records).most_common()),
        "most_common_default_collision": dict(Counter(
            str(r.get("default_collision")) for r in records).most_common(5)),
        # A record with the tag is a mesh that compiled; the denominator is the
        # meshes, not the assets, so a folder of non-meshes cannot skew this.
        "mean_triangles_per_mesh": (total("triangles") // len(mesh_records)
                                    if mesh_records else 0),
        "reported_vertices": present("vertices"),
    }


def _top(records: list[dict], field: str, count: int) -> list[dict]:
    usable = [r for r in records if r.get(field) is not None]
    usable.sort(key=lambda r: r[field], reverse=True)
    return [{"package": r["package"], field: r[field],
             "nanite_enabled": r.get("nanite_enabled"),
             "triangles": r.get("triangles")} for r in usable[:count]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=os.environ.get("RMSIM_ARENA_PATH", DEFAULT_PATH),
                        help="Content folder to survey, e.g. /Game/RMUC2026/Geometries")
    parser.add_argument("--report", type=Path,
                        default=Path(os.environ.get("RMSIM_ARENA_REPORT", DEFAULT_REPORT)),
                        help="where to write the full per-asset JSON report")
    parser.add_argument("--top", type=int,
                        default=int(os.environ.get("RMSIM_ARENA_TOP", "20")),
                        help="how many heaviest assets to list by triangles and by bytes")
    parser.add_argument("--limit", type=int,
                        default=int(os.environ.get("RMSIM_ARENA_LIMIT", "0")),
                        help="survey at most this many assets (0 = all); for a smoke test")
    args = parser.parse_args()

    # ``list_assets`` is a registry read: it returns package paths as strings and
    # does not load anything.
    packages = sorted(unreal.EditorAssetLibrary.list_assets(args.path, recursive=True))
    if args.limit > 0:
        packages = packages[:args.limit]
    if not packages:
        unreal.log_error(f"rm_sim_spear: no assets under {args.path}")
        print(json.dumps({"event": "arena_assets", "path": args.path, "assets": 0}))
        return 1

    records, unreadable = [], []
    for package in packages:
        data = unreal.EditorAssetLibrary.find_asset_data(package)
        if data is None:
            unreadable.append(package)
            continue
        records.append(_record(data, package))

    summary = _summarise(records)
    report = {
        "path": args.path,
        "summary": summary,
        "top_by_triangles": _top(records, "triangles", args.top),
        "top_by_est_compressed_bytes": _top(records, "est_compressed_bytes", args.top),
        "assets": records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    # stdout is the repo's JSON event stream; it must stay small, so the heavy
    # per-asset list only ever goes to --report.
    print(json.dumps({
        "event": "arena_assets",
        "path": args.path,
        "report": str(args.report),
        "unreadable": len(unreadable),
        **summary,
        "nanite_share": (round(summary["nanite_enabled_true"] / summary["static_meshes"], 4)
                         if summary["static_meshes"] else None),
    }, ensure_ascii=False))
    unreal.log(f"rm_sim_spear: surveyed {len(records)} assets under {args.path} "
               f"-> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
