"""Merge the visual meshes of each manifest link into one mesh per link.

A SolidWorks robot arrives as one STL per visual element (955 visuals over the
13 links of ``总装``).  Unreal's importer wants one StaticMesh per link node, so
this module bakes every visual origin into its own mesh and concatenates the
per-link result.  The input is the manifest produced by
:mod:`pipeline.urdf_manifest` rather than the URDF itself, because the manifest
has already resolved each mesh path and recorded ``mesh_exists``; using it keeps
"what the importer will see" in exactly one place.

Units are unambiguous here: URDF/MuJoCo positions are metres, UE positions are
centimetres with the Y axis mirrored (``p_ue_cm = 100 * (x, -y, z)``).  Mirroring
the finished vertex is equivalent to the rotation form documented in
``docs/urdf_workflow.md`` because ``D = diag(1, -1, 1)`` is its own inverse:
``D @ R @ v == (D @ R @ D) @ (D @ v)``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
from pathlib import Path
from typing import Any, Iterable

import numpy as np

#: Marker written by the ``swURDF high-performance binary STL`` exporter.  We
#: reuse it so a freshly merged file is recognisable to the same tooling.
_BINARY_HEADER = b"swURDF high-performance binary STL"

#: 80-byte header + uint32 count + one packed record per triangle.
_TRIANGLE_DTYPE = np.dtype(
    [
        ("normal", "<f4", (3,)),
        ("vertices", "<f4", (3, 3)),
        ("attribute", "<u2"),
    ]
)

_FORMATS = ("stl", "obj")


def _as_triangles(triangles: Any) -> np.ndarray:
    """Return ``triangles`` as a ``(N, 3, 3)`` float64 array.

    A bare ``(3, 3)`` triangle is accepted as a one-triangle mesh so that
    callers writing a single facet do not need a wrapping dimension.
    """
    array = np.asarray(triangles, dtype=np.float64)
    if array.size == 0:
        return np.zeros((0, 3, 3), dtype=np.float64)
    if array.shape[-2:] != (3, 3):
        raise ValueError(f"expected a (N, 3, 3) triangle array, got {array.shape}")
    return array.reshape(-1, 3, 3)


def _is_binary_stl(raw: bytes) -> bool:
    """Return True when ``raw`` is a binary STL by its size self-check.

    A binary STL states its triangle count at byte 80, so the file length has to
    agree with it.  That arithmetic is the reliable discriminator: plenty of
    exporters leave an ASCII-looking ``solid`` keyword in the 80-byte header.
    """
    if len(raw) < 84:
        return False
    count = struct.unpack_from("<I", raw, 80)[0]
    return 84 + 50 * count == len(raw)


def _parse_ascii_stl(raw: bytes) -> np.ndarray:
    text = raw.decode("utf-8", errors="replace")
    vertices: list[list[float]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].lower() == "vertex":
            vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
    if len(vertices) % 3:
        raise ValueError(f"ASCII STL has {len(vertices)} vertices, not a multiple of 3")
    if not vertices:
        return np.zeros((0, 3, 3), dtype=np.float64)
    return np.asarray(vertices, dtype=np.float64).reshape(-1, 3, 3)


def read_stl(path: str | Path) -> np.ndarray:
    """Read a binary or ASCII STL into ``(N, 3, 3)`` float64 triangle vertices."""
    path = Path(path)
    raw = path.read_bytes()
    if _is_binary_stl(raw):
        count = struct.unpack_from("<I", raw, 80)[0]
        if count == 0:
            return np.zeros((0, 3, 3), dtype=np.float64)
        records = np.frombuffer(raw, dtype=_TRIANGLE_DTYPE, count=count, offset=84)
        return records["vertices"].astype(np.float64)
    if raw[:5].lower() == b"solid":
        return _parse_ascii_stl(raw)
    raise ValueError(f"unrecognised STL file (neither binary nor ASCII): {path}")


def write_binary_stl(path: str | Path, triangles: Any) -> int:
    """Write ``triangles`` (``(N, 3, 3)``, metres or centimetres) as binary STL.

    Returns the triangle count.  Face normals are filled in rather than left
    zeroed: some importers shade with the stored normal even though it is
    redundant with the winding.
    """
    path = Path(path)
    mesh = _as_triangles(triangles)
    count = int(mesh.shape[0])
    records = np.zeros(count, dtype=_TRIANGLE_DTYPE)
    if count:
        edge_a = mesh[:, 1] - mesh[:, 0]
        edge_b = mesh[:, 2] - mesh[:, 0]
        cross = np.cross(edge_a, edge_b)
        length = np.linalg.norm(cross, axis=1, keepdims=True)
        normals = np.divide(cross, length, out=np.zeros_like(cross), where=length > 0.0)
        records["normal"] = normals.astype(np.float32)
        records["vertices"] = mesh.astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(_BINARY_HEADER.ljust(80, b"\0"))
        handle.write(struct.pack("<I", count))
        handle.write(records.tobytes())
    return count


def write_obj(path: str | Path, triangles: Any) -> int:
    """Write ``triangles`` as a material-free Wavefront OBJ; return the count.

    Vertices are emitted per corner (no deduplication) so that callers can map
    face indices straight back to triangle corners, which is what the UE
    importer needs when it rebuilds per-visual sections.
    """
    path = Path(path)
    mesh = _as_triangles(triangles)
    count = int(mesh.shape[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for triangle in mesh:
            for vertex in triangle:
                handle.write(f"v {vertex[0]:.9g} {vertex[1]:.9g} {vertex[2]:.9g}\n")
        for index in range(count):
            base = 3 * index
            handle.write(f"f {base + 1} {base + 2} {base + 3}\n")
    return count


def origin_matrix(xyz: Iterable[float], rpy: Iterable[float]) -> np.ndarray:
    """Return the 4x4 URDF origin transform for ``xyz`` (metres) and ``rpy``.

    URDF fixes the rotation order as extrinsic X-Y-Z, i.e.
    ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)``.  Getting this wrong silently
    misplaces every visual, so the order is spelled out here on purpose.
    """
    x, y, z = (float(value) for value in xyz)
    roll, pitch, yaw = (float(value) for value in rpy)
    cx, sx = math.cos(roll), math.sin(roll)
    cy, sy = math.cos(pitch), math.sin(pitch)
    cz, sz = math.cos(yaw), math.sin(yaw)
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = np.array(
        [
            [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx],
        ],
        dtype=np.float64,
    )
    matrix[:3, 3] = (x, y, z)
    return matrix


def _visual_mesh_path(visual: dict[str, Any], base_dir: Path | None) -> Path | None:
    """Resolve one visual's mesh file, or None when it has no mesh geometry."""
    geometry = visual.get("geometry") or {}
    if geometry.get("type") not in (None, "mesh"):
        return None
    candidate = visual.get("mesh_path")
    if candidate:
        path = Path(candidate)
        return path if path.is_absolute() or base_dir is None else base_dir / path
    filename = geometry.get("filename")
    if not filename:
        return None
    if filename.startswith("package://"):
        # package://pkg/relative/path: the manifest already resolved the common
        # case, so this is only a fallback for hand-written manifests.
        filename = filename.removeprefix("package://").split("/", 1)[-1]
    else:
        filename = filename.removeprefix("file://")
    path = Path(filename)
    if path.is_absolute():
        return path
    return (base_dir / path) if base_dir is not None else path


def _visual_scale(visual: dict[str, Any]) -> np.ndarray:
    scale = (visual.get("geometry") or {}).get("scale")
    if not scale:
        return np.ones(3, dtype=np.float64)
    return np.asarray([float(value) for value in scale], dtype=np.float64)


def _merge_visuals(
    link: dict[str, Any],
    *,
    scale: float,
    ue_axes: bool,
    base_dir: Path | None,
    cache: dict[str, np.ndarray] | None,
    skipped: list[dict[str, Any]] | None = None,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Shared implementation of the merge; returns the mesh and used visuals.

    Missing or unreadable meshes are skipped without raising: the manifest is
    generated data and a single unreadable STL must not block the other twelve
    link assets.  When ``skipped`` is given, one entry per dropped visual is
    appended so :func:`build_visual_meshes` can explain the shortfall.
    """
    merged: list[np.ndarray] = []
    used: list[dict[str, Any]] = []

    def drop(visual: dict[str, Any], path: Path | None, reason: str) -> None:
        if skipped is not None:
            skipped.append(
                {
                    "visual": visual.get("name"),
                    "mesh_path": str(path) if path is not None else None,
                    "reason": reason,
                }
            )

    for visual in link.get("visuals") or []:
        path = _visual_mesh_path(visual, base_dir)
        if not visual.get("mesh_exists", True):
            drop(visual, path, "mesh_exists=false")
            continue
        if path is None:
            drop(visual, None, "no_mesh_geometry")
            continue
        key = str(path)
        try:
            if cache is not None and key in cache:
                mesh = cache[key]
            else:
                mesh = read_stl(path)
                if cache is not None:
                    cache[key] = mesh
        except (OSError, ValueError):
            drop(visual, path, "mesh_unreadable")
            continue
        if mesh.size == 0:
            drop(visual, path, "empty_mesh")
            continue
        origin = visual.get("origin") or {}
        matrix = origin_matrix(origin.get("xyz", (0.0, 0.0, 0.0)), origin.get("rpy", (0.0, 0.0, 0.0)))
        local = mesh * _visual_scale(visual)
        link_frame = local @ matrix[:3, :3].T + matrix[:3, 3]
        if ue_axes:
            # p_ue_cm = 100 * (x, -y, z); see the module docstring for why
            # mirroring here matches R_ue = D @ R @ D.
            link_frame = link_frame * np.array([1.0, -1.0, 1.0]) * float(scale)
        else:
            link_frame = link_frame * float(scale)
        merged.append(link_frame)
        used.append(
            {
                "mesh_path": key,
                "visual_origin_xyz": [float(value) for value in origin.get("xyz", (0.0, 0.0, 0.0))],
                "triangles": int(mesh.shape[0]),
            }
        )
    if not merged:
        return np.zeros((0, 3, 3), dtype=np.float64), used
    return np.concatenate(merged, axis=0), used


def merge_link_visuals(link: dict[str, Any], *, scale: float = 100.0, ue_axes: bool = True) -> np.ndarray:
    """Concatenate every available visual mesh of ``link`` into the link frame.

    Visuals are baked in manifest order so the output is reproducible, and
    ``ue_axes`` selects the UE frame (``100 * (x, -y, z)``) or a raw
    ``scale``-multiplied URDF frame.  Unreadable or absent meshes are skipped
    silently, returning a ``(0, 3, 3)`` array when nothing is left.
    """
    mesh, _ = _merge_visuals(link, scale=scale, ue_axes=ue_axes, base_dir=None, cache=None)
    return mesh


def _link_filename(name: str) -> str:
    """Map a link name to a single path component.

    Names are kept verbatim (they may be non-ASCII) and only path separators are
    neutralised, so the asset name the importer sees still matches the link.
    """
    safe = str(name).strip() or "link"
    return re.sub(r"[\\/]+", "_", safe)


def _joint_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index joints by child link name; a link has at most one parent joint."""
    index: dict[str, dict[str, Any]] = {}
    for joint in manifest.get("joints") or []:
        child = joint.get("child")
        if child is not None and child not in index:
            index[child] = joint
    return index


def _write_mesh(path: Path, triangles: np.ndarray, fmt: str) -> int:
    if fmt == "stl":
        return write_binary_stl(path, triangles)
    if fmt == "obj":
        return write_obj(path, triangles)
    raise ValueError(f"unsupported fmt {fmt!r}, expected one of {_FORMATS}")


def build_visual_meshes(
    manifest: dict[str, Any],
    output_dir: str | Path,
    *,
    ue_axes: bool = True,
    fmt: str = "stl",
) -> dict[str, Any]:
    """Write one merged mesh per manifest link and return a JSON report.

    The report is the contract with the UE importer: ``parent``/``joint`` let it
    rebuild the node hierarchy, and ``origin_ue_cm`` carries the joint origin in
    UE centimetres (rotation deliberately left in the URDF frame -- the caller
    applies ``diag(1, -1, 1) @ R @ diag(1, -1, 1)`` where it needs it).
    """
    if fmt not in _FORMATS:
        raise ValueError(f"unsupported fmt {fmt!r}, expected one of {_FORMATS}")
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    base_dir = None
    source_urdf = manifest.get("source_urdf")
    if source_urdf:
        base_dir = Path(source_urdf).expanduser().resolve().parent

    joints = _joint_index(manifest)
    root_link = manifest.get("root_link")
    links_report: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total = 0
    cache: dict[str, np.ndarray] = {}
    for link in manifest.get("links") or []:
        name = link.get("name")
        link_skipped: list[dict[str, Any]] = []
        mesh, used = _merge_visuals(
            link,
            scale=100.0,
            ue_axes=ue_axes,
            base_dir=base_dir,
            cache=cache,
            skipped=link_skipped,
        )
        skipped.extend({"link": name, **entry} for entry in link_skipped)
        joint = None if name == root_link else joints.get(name)
        if joint is None:
            # The root has no parent joint, and an orphan link is better
            # reported as unattached than silently given a bogus transform.
            parent = None
            joint_name = None
            origin_ue_cm = [0.0, 0.0, 0.0]
        else:
            origin = joint.get("origin") or {}
            xyz = [float(value) for value in origin.get("xyz", (0.0, 0.0, 0.0))]
            parent = joint.get("parent")
            joint_name = joint.get("name")
            origin_ue_cm = [100.0 * xyz[0], -100.0 * xyz[1], 100.0 * xyz[2]]
        mesh_path = output_root / f"{_link_filename(name)}.{fmt}"
        triangles = _write_mesh(mesh_path, mesh, fmt)
        total += triangles
        links_report.append(
            {
                "name": name,
                "parent": parent,
                "joint": joint_name,
                "origin_ue_cm": origin_ue_cm,
                "mesh_path": str(mesh_path),
                "triangles": triangles,
                "visual_count": len(used),
                "visuals": used,
            }
        )
    return {
        "robot": manifest.get("robot"),
        "units": manifest.get("units"),
        "root_link": root_link,
        "links": links_report,
        "skipped_visuals": skipped,
        "total_triangles": total,
        "fmt": fmt,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge URDF link visuals into one mesh per link")
    parser.add_argument("manifest", type=Path, help="path to generated/urdf/<robot>/manifest.json")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--fmt", choices=list(_FORMATS), default="stl")
    parser.add_argument("--ue-axes", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--report", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    output_root = args.output_root or (args.manifest.parent / "visual_meshes")
    report_path = args.report or (output_root / "visual_meshes.json")
    report = build_visual_meshes(manifest, output_root, ue_axes=args.ue_axes, fmt=args.fmt)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "robot": report["robot"],
        "fmt": report["fmt"],
        "links": len(report["links"]),
        "triangles": report["total_triangles"],
        "skipped_visuals": len(report["skipped_visuals"]),
        "output_root": str(output_root),
        "report": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
