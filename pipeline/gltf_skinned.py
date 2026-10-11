"""Export a manifest's link visuals as ONE skinned glTF mesh plus a skeleton.

Why this exists
---------------
``pipeline.urdf_visual_meshes`` can already merge each link's visuals into one
mesh per link, which the UE importer can only consume as one *actor* (and one
primitive) per link.  Measured on the ``总装`` robot, the game thread's pose cost
tracks the number of scene components that move, so going from 967 actors to 13
is a large win but not the whole win: 13 primitives still cost roughly 2 ms per
frame in ``View Visibility`` re-culling (see ``docs/render_budget.md`` §18).

A *skeletal* mesh replaces all of that with a single primitive whose vertices
move because 13 bone transforms changed.  Bone transforms are not primitive
transforms, so the hypothesis under test is that posing them costs nothing in
``FScene::UpdatePrimitiveTransform`` and therefore nothing in the visibility pass.

Format choice
-------------
glTF, not FBX.  glTF is JSON plus a binary blob in one ``.glb`` container, so it
can be generated from pure Python with no Autodesk SDK and no Blender detour.
UE 5.8 imports it through Interchange, and ``InterchangeGltfMesh.cpp:104-218``
reads ``skins``/``JOINTS_0``/``WEIGHTS_0`` into a real ``USkeletalMesh``, naming
the bones after the joint nodes.

Two facts about that importer shape the output (both verified in the 5.8 source):

* ``InterchangeGltfTranslator.cpp:617`` calls ``ScaleNodeTranslations`` with
  ``GltfUnitConversionMultiplier == 100`` and nothing else.  Interchange's glTF
  path does **not** rotate Y-up into Z-up, so this file is written directly in
  UE axes and in metres; UE ends up in centimetres with the axes already right.
* The bind pose is derived from ``inverseBindMatrices``
  (``GLTFReader.cpp:1380-1502``) and from the node transforms, so the mesh has to
  be authored in the *skeleton root's* space: with ``jointMatrix = global(joint)
  * inverseBindMatrix`` reducing to identity in the bind pose, a vertex stays
  exactly where it was written.  Vertices are therefore baked through each
  link's global bind transform, not left in the link frame.

Conventions, all inherited from :mod:`pipeline.urdf_visual_meshes`:

* ``p_ue = D @ p_urdf`` with ``D = diag(1, -1, 1)`` (URDF metres -> UE axes,
  still metres here; UE multiplies by 100 on import).
* A URDF transform ``(R, t)`` maps to ``(D @ R @ D, D @ t)``, and that mapping
  commutes with composition, so link-global transforms can be accumulated as
  ordinary 4x4 products in the UE frame.

Rigid skinning
--------------
Every visual of a link is rigid with respect to that link, so each vertex gets
weight 1.0 on exactly one bone.  No influence blending, no joints to solve.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.urdf_visual_meshes import (
    _merge_visuals,
    _visual_mesh_path,
    origin_matrix,
    read_stl,
)

#: ``D = diag(1, -1, 1)``: URDF -> UE axes, the same mirror the rest of the
#: project uses.  Applied to finished vertices it equals conjugating every
#: rotation (see the ``urdf_visual_meshes`` module docstring for the identity).
_AXIS_FLIP = np.array([1.0, -1.0, 1.0], dtype=np.float64)

#: Rounding used when welding a triangle soup.  STL stores three vertices per
#: triangle with no sharing at all, so an unwelded 16.8M-triangle robot would
#: need 50M vertices; welding on the position alone cuts that to 8.4M.  The
#: quantum is 0.1 micrometre, far below any CAD tolerance.
_WELD_DECIMALS = 6

_JSON_CHUNK = 0x4E4F534A  # 'JSON'
_BIN_CHUNK = 0x004E4942  # 'BIN\0'


# --------------------------------------------------------------------------- #
# small rotation helpers (numpy only; no scipy in this environment)
# --------------------------------------------------------------------------- #


def ue_local_transform(xyz_m: Any, rpy_rad: Any) -> tuple[np.ndarray, np.ndarray]:
    """URDF metres + RPY radians -> ``(rotation, translation)`` in the UE frame.

    ``translation`` is ``D @ xyz`` in metres and ``rotation`` is the 3x3 matrix
    ``D @ R @ D``.  Mirrors :func:`tools.import_urdf_visuals.ue_transform`, which
    does the same thing with ``unreal`` types.  The pair is ordered
    rotation-first so it composes directly with :func:`_compose`.
    """
    translation = _AXIS_FLIP * np.asarray([float(v) for v in xyz_m], dtype=np.float64)
    rotation = origin_matrix((0.0, 0.0, 0.0), rpy_rad)[:3, :3]
    flip = np.diag(_AXIS_FLIP)
    return flip @ rotation @ flip, translation


def quaternion_from_matrix(matrix: np.ndarray) -> list[float]:
    """Return the glTF ``[x, y, z, w]`` quaternion of a 3x3 rotation matrix.

    Shepperd's method: pick the largest of the four candidate denominators so the
    result never divides by a near-zero trace.  A URDF joint origin can be a
    180-degree rotation (the wheels are), where the naive trace branch is
    exactly the unstable case.
    """
    m = np.asarray(matrix, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (m[2, 1] - m[1, 2]) / scale
        y = (m[0, 2] - m[2, 0]) / scale
        z = (m[1, 0] - m[0, 1]) / scale
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        scale = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / scale
        x = 0.25 * scale
        y = (m[0, 1] + m[1, 0]) / scale
        z = (m[0, 2] + m[2, 0]) / scale
    elif m[1, 1] > m[2, 2]:
        scale = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / scale
        x = (m[0, 1] + m[1, 0]) / scale
        y = 0.25 * scale
        z = (m[1, 2] + m[2, 1]) / scale
    else:
        scale = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / scale
        x = (m[0, 2] + m[2, 0]) / scale
        y = (m[1, 2] + m[2, 1]) / scale
        z = 0.25 * scale
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm == 0.0:
        return [0.0, 0.0, 0.0, 1.0]
    return [x / norm, y / norm, z / norm, w / norm]


def _compose(parent: tuple[np.ndarray, np.ndarray], local: tuple[np.ndarray, np.ndarray]):
    """Return ``parent * local`` for ``(rotation, translation)`` pairs."""
    parent_rotation, parent_translation = parent
    local_rotation, local_translation = local
    return (
        parent_rotation @ local_rotation,
        parent_rotation @ local_translation + parent_translation,
    )


def _transform_matrix(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = translation
    return matrix


# --------------------------------------------------------------------------- #
# manifest -> skeleton
# --------------------------------------------------------------------------- #


def link_hierarchy(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return the links in parent-before-child order plus a name -> index map.

    Ordering matters twice: ``skin.joints`` fixes the joint index a vertex
    references, and accumulating global bind transforms needs the parent done
    first.  The URDF is a tree, so a breadth-first walk from ``root_link``
    always suffices; a link orphaned by a malformed manifest is appended at the
    end rather than dropped, so the exporter never silently loses geometry.
    """
    links = {link["name"]: link for link in manifest.get("links") or []}
    parents: dict[str, str | None] = {}
    for joint in manifest.get("joints") or []:
        child = joint.get("child")
        if child in links:
            parents[child] = joint.get("parent")

    root = manifest.get("root_link")
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(name: str) -> None:
        if name in seen or name not in links:
            return
        seen.add(name)
        ordered.append(links[name])
        for candidate, parent in parents.items():
            if parent == name:
                walk(candidate)

    if root:
        walk(root)
    for name in links:
        walk(name)
    return ordered, {link["name"]: index for index, link in enumerate(ordered)}


def link_transforms(
    manifest: dict[str, Any],
) -> tuple[list[np.ndarray], list[tuple[np.ndarray, np.ndarray]]]:
    """Return ``(globals, locals)`` per link, both in parent-before-child order.

    ``locals`` are the ``(rotation, translation)`` pairs a glTF node stores as
    TRS; ``globals`` are the accumulated 4x4 bind transforms a vertex has to be
    baked through and an ``inverseBindMatrix`` has to invert.
    """
    ordered, _ = link_hierarchy(manifest)
    joints = {joint.get("child"): joint for joint in manifest.get("joints") or []}
    root = manifest.get("root_link")

    globals_: list[np.ndarray] = []
    locals_: list[tuple[np.ndarray, np.ndarray]] = []
    by_name: dict[str, int] = {}
    for index, link in enumerate(ordered):
        by_name[link["name"]] = index

    for link in ordered:
        joint = joints.get(link["name"])
        if link["name"] == root or joint is None:
            local = (np.eye(3), np.zeros(3))
        else:
            origin = joint.get("origin") or {}
            local = ue_local_transform(
                origin.get("xyz", (0.0, 0.0, 0.0)), origin.get("rpy", (0.0, 0.0, 0.0))
            )
        parent = joints.get(link["name"], {}).get("parent") if joint is not None else None
        parent_index = by_name.get(parent) if parent else None
        if parent_index is None or parent_index >= index:
            accumulated = local
        else:
            accumulated = _compose((globals_[parent_index][:3, :3], globals_[parent_index][:3, 3]), local)
        globals_.append(_transform_matrix(*accumulated))
        locals_.append(local)
    return globals_, locals_


# --------------------------------------------------------------------------- #
# triangle soup -> indexed, smooth-shaded mesh
# --------------------------------------------------------------------------- #


def weld_triangles(triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Weld a ``(N, 3, 3)`` soup into ``(positions, indices, normals)``.

    Vertices are welded on position alone and normals are area-weighted face
    normals averaged over the welded fan, which is cheap and gives a smooth
    result.  That is *not* what a CAD part wants everywhere -- a hard edge
    between two faces should stay hard -- but it is what makes the full 16.8M
    triangle robot fit in a 400 MB file instead of 2.4 GB.  Splitting vertices
    by normal angle is the follow-up; see ``docs/render_budget.md`` §19.

    The weld key is a ``(N, 3)`` float32 block viewed as one 12-byte record, so
    ``np.unique`` sorts a flat array of 12-byte keys rather than a 2-D float64
    one.  On the full robot that is the difference between sorting 50M rows of
    24 bytes and 50M rows of 12.
    """
    soup = np.asarray(triangles, dtype=np.float64)
    if soup.size == 0:
        empty = np.zeros(0, dtype=np.uint32)
        return np.zeros((0, 3), dtype=np.float32), empty, np.zeros((0, 3), dtype=np.float32)

    corners = soup.reshape(-1, 3)
    keys = np.round(corners, _WELD_DECIMALS).astype(np.float32)
    # ``-0.0`` and ``0.0`` compare equal numerically but not bytewise, and a
    # bytewise key would split every vertex sitting exactly on a mirror plane.
    keys[keys == 0.0] = 0.0
    keys = np.ascontiguousarray(keys).view(np.dtype((np.void, keys.dtype.itemsize * 3))).ravel()
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    positions = corners[first]
    indices = inverse.reshape(-1).astype(np.uint32).reshape(-1, 3)

    face = soup[:, 1] - soup[:, 0]
    other = soup[:, 2] - soup[:, 0]
    # Area weighting is implicit here: the cross product has magnitude
    # 2 * area, so accumulating the raw cross products averages the face
    # normals in proportion to the area they contribute.
    raw = np.cross(face, other)
    vertex_normals = np.zeros((positions.shape[0], 3), dtype=np.float64)
    for corner in range(3):
        np.add.at(vertex_normals, indices[:, corner], raw)
    norms = np.linalg.norm(vertex_normals, axis=1, keepdims=True)
    degenerate = norms.ravel() == 0.0
    if degenerate.any():
        # Exactly opposed faces (a zero-thickness shell, or a doubled surface)
        # cancel to a zero vector.  A zero normal renders as garbage, so fall
        # back to the first incident face's own normal instead.
        lengths = np.linalg.norm(raw, axis=1, keepdims=True)
        lengths[lengths == 0.0] = 1.0
        unit = raw / lengths
        owner = np.full(positions.shape[0], np.iinfo(np.int64).max, dtype=np.int64)
        for corner in range(3):
            np.minimum.at(owner, indices[:, corner], np.arange(soup.shape[0], dtype=np.int64))
        safe = owner < np.iinfo(np.int64).max
        vertex_normals[degenerate & safe] = unit[owner[degenerate & safe]]
        norms = np.linalg.norm(vertex_normals, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    vertex_normals = vertex_normals / norms

    return positions.astype(np.float32), indices.reshape(-1), vertex_normals.astype(np.float32)


# --------------------------------------------------------------------------- #
# glTF assembly
# --------------------------------------------------------------------------- #


class _BufferBuilder:
    """Accumulate aligned binary views and hand back ``(view, accessor)`` indices."""

    def __init__(self) -> None:
        self.blob = bytearray()
        self.views: list[dict[str, Any]] = []
        self.accessors: list[dict[str, Any]] = []

    def add(self, array: np.ndarray, *, component_type: int, type_name: str,
            normalized: bool = False, minmax: bool = False, target: int | None = None) -> int:
        payload = array.tobytes()
        while len(self.blob) % 4:
            self.blob.append(0)
        offset = len(self.blob)
        self.blob.extend(payload)

        view: dict[str, Any] = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(payload),
        }
        if target is not None:
            view["target"] = target
        self.views.append(view)

        accessor: dict[str, Any] = {
            "bufferView": len(self.views) - 1,
            "componentType": component_type,
            "count": int(array.shape[0]),
            "type": type_name,
        }
        if normalized:
            accessor["normalized"] = True
        if minmax:
            accessor["min"] = [float(v) for v in array.min(axis=0)]
            accessor["max"] = [float(v) for v in array.max(axis=0)]
        self.accessors.append(accessor)
        return len(self.accessors) - 1


def _visual_triangle_count(
    visual: dict[str, Any],
    base_dir: Path | None,
    cache: dict[str, np.ndarray],
) -> int:
    """Triangle count of one visual's source mesh, 0 when it cannot be read."""
    path = _visual_mesh_path(visual, base_dir)
    if path is None or not visual.get("mesh_exists", True):
        return 0
    key = str(path)
    if key not in cache:
        try:
            cache[key] = read_stl(path)
        except (OSError, ValueError):
            cache[key] = np.zeros((0, 3, 3), dtype=np.float64)
    return int(cache[key].shape[0])


def build_skinned_gltf(
    manifest: dict[str, Any],
    output_path: str | Path,
    *,
    max_visuals_per_link: int | None = None,
    max_triangles_per_visual: int | None = None,
    base_dir: Path | None = None,
    robot_name: str | None = None,
) -> dict[str, Any]:
    """Write ``output_path`` as a ``.glb`` holding one skinned mesh + a skeleton.

    ``max_visuals_per_link`` exists for the staged rollout: the deciding
    experiment only needs a *legitimately skinned* mesh to test whether bone
    writes are cheap, so it can run on a few hundred thousand triangles instead
    of paying the full 16.8M-triangle import first.
    """
    output = Path(output_path)
    ordered, joint_index = link_hierarchy(manifest)
    globals_, locals_ = link_transforms(manifest)
    names = [link["name"] for link in ordered]
    joint_count = len(names)
    if joint_count > 65535:
        raise ValueError(f"{joint_count} joints exceeds the JOINTS_0 uint16 range")

    joints = {joint.get("child"): joint for joint in manifest.get("joints") or []}
    cache: dict[str, np.ndarray] = {}

    position_blocks: list[np.ndarray] = []
    normal_blocks: list[np.ndarray] = []
    index_blocks: list[np.ndarray] = []
    joint_blocks: list[np.ndarray] = []
    offset = 0
    link_report: list[dict[str, Any]] = []
    total_triangles = 0

    for index, link in enumerate(ordered):
        visuals = list(link.get("visuals") or [])
        if max_visuals_per_link is not None:
            visuals = visuals[:max_visuals_per_link]
        if max_triangles_per_visual is not None:
            # Staged rollout, part two: a proxy skin that carries all 13 bones
            # but almost no geometry, so the fixed per-frame cost of a skinned
            # mesh can be separated from the cost of its triangles.
            visuals = [visual for visual in visuals
                       if _visual_triangle_count(visual, base_dir, cache) <= max_triangles_per_visual]
        sparse = dict(link, visuals=visuals)
        triangles, used = _merge_visuals(
            sparse, scale=1.0, ue_axes=True, base_dir=base_dir, cache=cache
        )
        if triangles.size == 0:
            link_report.append({"name": link["name"], "triangles": 0, "vertices": 0,
                                "visuals": 0, "skipped": True})
            continue
        # Bake through the link's global bind transform: the skinning equation
        # collapses to the identity in the bind pose only if the vertices are
        # already expressed in the skeleton root's space.
        matrix = globals_[index]
        baked = triangles @ matrix[:3, :3].T + matrix[:3, 3]
        positions, indices, normals = weld_triangles(baked)
        count = positions.shape[0]
        position_blocks.append(positions)
        normal_blocks.append(normals)
        index_blocks.append(indices + offset)
        joint_blocks.append(np.full((count, 4), (index, 0, 0, 0), dtype=np.uint16))
        offset += count
        total_triangles += indices.shape[0] // 3
        link_report.append({
            "name": link["name"],
            "triangles": int(indices.shape[0] // 3),
            "vertices": int(count),
            "visuals": len(used),
            "joint_index": index,
            "skipped": False,
        })

    if not position_blocks:
        raise ValueError("no link produced geometry; refusing to write an empty glTF")

    positions = np.concatenate(position_blocks, axis=0)
    normals = np.concatenate(normal_blocks, axis=0)
    indices = np.concatenate(index_blocks, axis=0).astype(np.uint32)
    joint_ids = np.concatenate(joint_blocks, axis=0)
    # Every vertex is rigidly bound to exactly one bone, so the weight vector is
    # constant.  It still has to be materialised: glTF has no "implicit weight".
    weights = np.zeros((positions.shape[0], 4), dtype=np.float32)
    weights[:, 0] = 1.0

    builder = _BufferBuilder()
    pos_accessor = builder.add(positions, component_type=5126, type_name="VEC3", minmax=True)
    nrm_accessor = builder.add(normals, component_type=5126, type_name="VEC3")
    jnt_accessor = builder.add(joint_ids, component_type=5123, type_name="VEC4")
    wgt_accessor = builder.add(weights, component_type=5126, type_name="VEC4")
    idx_accessor = builder.add(indices, component_type=5125, type_name="SCALAR")

    # inverseBindMatrix per joint, column-major as glTF requires.  UE derives the
    # bind pose from these (GLTFReader.cpp:1380-1502), so they must be the exact
    # inverse of the accumulated joint-origin chain.
    ibm = np.zeros((joint_count, 16), dtype=np.float32)
    for index in range(joint_count):
        matrix = np.linalg.inv(globals_[index])
        ibm[index] = matrix.T.reshape(16)
    ibm_accessor = builder.add(ibm, component_type=5126, type_name="MAT4")

    nodes: list[dict[str, Any]] = []
    children_of: dict[int, list[int]] = {index: [] for index in range(joint_count)}
    for index, link in enumerate(ordered):
        joint = joints.get(link["name"])
        parent = joint.get("parent") if joint is not None else None
        if parent in joint_index and joint_index[parent] < index:
            children_of[joint_index[parent]].append(index)

    for index, link in enumerate(ordered):
        rotation, translation = locals_[index]
        node: dict[str, Any] = {
            "name": names[index],
            "translation": [float(v) for v in translation],
            "rotation": quaternion_from_matrix(rotation),
        }
        if children_of[index]:
            node["children"] = children_of[index]
        nodes.append(node)

    mesh_node = len(nodes)
    nodes.append({
        "name": f"{robot_name or manifest.get('robot') or 'robot'}_skinned",
        "mesh": 0,
        "skin": 0,
    })

    root_index = joint_index.get(manifest.get("root_link"), 0)
    scene_nodes = [mesh_node] + [root_index]
    scene_nodes += [index for index in range(joint_count) if index != root_index and not any(
        index in children for children in children_of.values())]

    document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "rm_sim_spear pipeline.gltf_skinned"},
        "scene": 0,
        "scenes": [{"nodes": sorted(set(scene_nodes))}],
        "nodes": nodes,
        "skins": [{
            "name": "skeleton",
            "joints": list(range(joint_count)),
            "skeleton": root_index,
            "inverseBindMatrices": ibm_accessor,
        }],
        "meshes": [{
            "name": robot_name or manifest.get("robot") or "robot",
            "primitives": [{
                "attributes": {
                    "POSITION": pos_accessor,
                    "NORMAL": nrm_accessor,
                    "JOINTS_0": jnt_accessor,
                    "WEIGHTS_0": wgt_accessor,
                },
                "indices": idx_accessor,
                "material": 0,
                "mode": 4,
            }],
        }],
        "materials": [{
            "name": "default",
            "doubleSided": False,
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.7, 0.7, 0.7, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.6,
            },
        }],
        "bufferViews": builder.views,
        "accessors": builder.accessors,
        "buffers": [{"byteLength": len(builder.blob)}],
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    _write_glb(output, document, builder.blob)

    return {
        "glb": str(output),
        "size_bytes": output.stat().st_size,
        "robot": robot_name or manifest.get("robot"),
        "joints": names,
        "joint_count": joint_count,
        "vertices": int(positions.shape[0]),
        "triangles": int(indices.shape[0] // 3),
        "links": link_report,
        "max_visuals_per_link": max_visuals_per_link,
        "max_triangles_per_visual": max_triangles_per_visual,
    }


def _write_glb(path: Path, document: dict[str, Any], blob: bytearray) -> None:
    """Serialise ``document`` + ``blob`` as a single-file GLB container."""
    json_bytes = bytearray(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    while len(json_bytes) % 4:
        json_bytes.append(0x20)  # JSON chunk pads with spaces, per spec
    binary = bytearray(blob)
    while len(binary) % 4:
        binary.append(0x00)

    length = 12 + 8 + len(json_bytes) + 8 + len(binary)
    with path.open("wb") as handle:
        handle.write(struct.pack("<III", 0x46546C67, 2, length))
        handle.write(struct.pack("<II", len(json_bytes), _JSON_CHUNK))
        handle.write(json_bytes)
        handle.write(struct.pack("<II", len(binary), _BIN_CHUNK))
        handle.write(binary)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a URDF manifest's visuals as one skinned glTF (.glb)")
    parser.add_argument("manifest", type=Path, help="path to generated/urdf/<robot>/manifest.json")
    parser.add_argument("--output", type=Path, default=None,
                        help="output .glb (default: <manifest dir>/<robot>_skinned.glb)")
    parser.add_argument("--robot-name", default=None, help="override the mesh/asset name")
    parser.add_argument("--max-visuals-per-link", type=int, default=None, metavar="N",
                        help="staged rollout: keep only the first N visuals of each link")
    parser.add_argument("--max-triangles-per-visual", type=int, default=None, metavar="N",
                        help="staged rollout: drop visuals whose source mesh has more "
                             "than N triangles, to build a lightweight proxy skin")
    parser.add_argument("--report", type=Path, default=None,
                        help="write the JSON report here instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    robot = args.robot_name or manifest.get("robot") or args.manifest.parent.name
    output = args.output or (args.manifest.parent / f"{robot}_skinned.glb")
    report = build_skinned_gltf(
        manifest,
        output,
        max_visuals_per_link=args.max_visuals_per_link,
        max_triangles_per_visual=args.max_triangles_per_visual,
        base_dir=args.manifest.parent,
        robot_name=robot,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    summary = {key: report[key] for key in
               ("glb", "size_bytes", "joint_count", "vertices", "triangles")}
    summary["links_with_geometry"] = sum(1 for link in report["links"] if not link["skipped"])
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
