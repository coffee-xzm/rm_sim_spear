"""Offline tests for pipeline.gltf_skinned.

The exporter is the only thing standing between the URDF manifest and a
``USkeletalMesh``, and a wrong bind pose shows up as a silently misplaced robot
rather than an error.  The tests therefore re-parse the emitted ``.glb`` and
check the property that actually matters: for every joint, the inverse bind
matrix must be the inverse of that joint's accumulated bind transform, because
that is what makes the skinning equation collapse to the identity in the bind
pose.  Nothing here imports ``unreal``.
"""

import contextlib
import io
import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pipeline.gltf_skinned import (
    build_skinned_gltf,
    link_hierarchy,
    link_transforms,
    main,
    quaternion_from_matrix,
    ue_local_transform,
    weld_triangles,
)
from pipeline.urdf_visual_meshes import origin_matrix, write_binary_stl


def _triangle(offset=(0.0, 0.0, 0.0), size=1.0):
    """A unit right triangle in the z=0 plane, translated by ``offset``."""
    ox, oy, oz = offset
    return np.array(
        [
            [[ox, oy, oz], [ox + size, oy, oz], [ox, oy + size, oz]],
        ],
        dtype=np.float64,
    )


def _matrix_from_quaternion(q) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def _rotation_from_rpy(rpy) -> np.ndarray:
    return origin_matrix((0.0, 0.0, 0.0), rpy)[:3, :3]


def _read_glb(path: Path) -> tuple[dict, bytes]:
    """Parse a GLB container; independent of the writer under test."""
    raw = path.read_bytes()
    magic, version, length = struct.unpack_from("<III", raw, 0)
    assert magic == 0x46546C67, hex(magic)
    assert version == 2
    assert length == len(raw), (length, len(raw))
    offset = 12
    document = None
    blob = b""
    while offset < len(raw):
        chunk_length, chunk_type = struct.unpack_from("<II", raw, offset)
        offset += 8
        payload = raw[offset:offset + chunk_length]
        offset += chunk_length
        assert len(payload) == chunk_length
        assert chunk_length % 4 == 0, chunk_length
        if chunk_type == 0x4E4F534A:
            document = json.loads(payload.decode("utf-8"))
        elif chunk_type == 0x004E4942:
            blob = payload
    assert document is not None
    return document, blob


_COMPONENTS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_TYPE_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _accessor(document: dict, blob: bytes, index: int) -> np.ndarray:
    """Decode one accessor, checking the bufferView's alignment on the way."""
    accessor = document["accessors"][index]
    view = document["bufferViews"][accessor["bufferView"]]
    assert view["byteOffset"] % 4 == 0, view["byteOffset"]
    assert view["byteOffset"] + view["byteLength"] <= len(blob)
    code, size = _COMPONENTS[accessor["componentType"]]
    per = _TYPE_COUNTS[accessor["type"]]
    start = view["byteOffset"] + accessor.get("byteOffset", 0)
    end = start + accessor["count"] * per * size
    assert end <= len(blob), (end, len(blob))
    flat = struct.unpack_from("<" + code * accessor["count"] * per, blob, start)
    return np.array(flat, dtype=np.float64).reshape(accessor["count"], per)[:, :].squeeze()


def _node_globals(document: dict) -> dict[int, np.ndarray]:
    """Accumulate every joint node's bind transform the way a renderer would."""
    parents = {}
    for index, node in enumerate(document["nodes"]):
        for child in node.get("children", []):
            parents[child] = index

    def build(index: int) -> np.ndarray:
        node = document["nodes"][index]
        local = np.eye(4)
        local[:3, :3] = _matrix_from_quaternion(node.get("rotation", (0, 0, 0, 1)))
        local[:3, 3] = node.get("translation", (0.0, 0.0, 0.0))
        parent = parents.get(index)
        return local if parent is None else build(parent) @ local

    return {index: build(index) for index in range(len(document["nodes"]))}


def _write_manifest(root: Path, links, joints, *, root_link, robot="test_robot") -> dict:
    return {
        "schema_version": 1,
        "robot": robot,
        "units": "meters",
        "root_link": root_link,
        "links": links,
        "joints": joints,
    }


def _chain_manifest(root: Path):
    """base -> arm -> tool, two visuals each, meshes written next to ``root``."""
    meshes = {}
    for name, offset in (("a", (0.0, 0.0, 0.0)), ("b", (1.0, 0.0, 0.0))):
        path = root / f"{name}.stl"
        write_binary_stl(path, _triangle(offset))
        meshes[name] = str(path)

    def link(name, visual_names):
        return {
            "name": name,
            "visuals": [
                {
                    "name": visual,
                    "origin": {"xyz": [0.1 * index, 0.2 * index, 0.3 * index], "rpy": [0.0, 0.0, 0.0]},
                    "geometry": {"type": "mesh", "filename": f"{visual}.stl"},
                    "mesh_path": meshes[visual],
                    "mesh_exists": True,
                    "material": {"name": None, "rgba": [0.7, 0.7, 0.7, 1.0]},
                }
                for index, visual in enumerate(visual_names)
            ],
        }

    links = [link("base", ["a"]), link("arm", ["a", "b"]), link("tool", ["b"])]
    joints = [
        {
            "name": "arm_joint",
            "type": "revolute",
            "parent": "base",
            "child": "arm",
            "origin": {"xyz": [0.5, -0.25, 0.75], "rpy": [0.3, -0.2, 1.1]},
            "axis": [0.0, 0.0, 1.0],
        },
        {
            "name": "tool_joint",
            "type": "fixed",
            "parent": "arm",
            "child": "tool",
            "origin": {"xyz": [-0.1, 0.4, 0.05], "rpy": [math.pi, 0.0, 0.0]},
            "axis": [1.0, 0.0, 0.0],
        },
    ]
    return _write_manifest(root, links, joints, root_link="base")


class QuaternionTest(unittest.TestCase):
    def test_identity(self):
        quaternion = quaternion_from_matrix(np.eye(3))
        self.assertAlmostEqual(quaternion[3], 1.0)
        for value in quaternion[:3]:
            self.assertAlmostEqual(value, 0.0)

    def test_every_shepperd_branch_round_trips(self):
        # One rotation per branch of the trace test, chosen so each branch is
        # the one taken: identity, 180 about X, 180 about Y, 180 about Z.
        cases = [
            np.eye(3),
            np.diag([1.0, -1.0, -1.0]),
            np.diag([-1.0, 1.0, -1.0]),
            np.diag([-1.0, -1.0, 1.0]),
            _rotation_from_rpy([0.3, -0.2, 1.1]),
            _rotation_from_rpy([math.pi, 0.0, 0.0]),
        ]
        for matrix in cases:
            with self.subTest(matrix=matrix.tolist()):
                recovered = _matrix_from_quaternion(quaternion_from_matrix(matrix))
                np.testing.assert_allclose(recovered, matrix, atol=1e-12)

    def test_result_is_normalised(self):
        quaternion = quaternion_from_matrix(_rotation_from_rpy([2.0, 1.0, -0.5]))
        self.assertAlmostEqual(float(np.linalg.norm(quaternion)), 1.0)


class LocalTransformTest(unittest.TestCase):
    def test_translation_is_the_mirrored_vector(self):
        rotation, translation = ue_local_transform([1.0, 2.0, 3.0], [0.0, 0.0, 0.0])
        np.testing.assert_allclose(translation, [1.0, -2.0, 3.0])
        np.testing.assert_allclose(rotation, np.eye(3), atol=1e-12)

    def test_rotation_is_conjugated_by_the_mirror(self):
        rpy = [0.3, -0.2, 1.1]
        flip = np.diag([1.0, -1.0, 1.0])
        rotation, _ = ue_local_transform([0.0, 0.0, 0.0], rpy)
        np.testing.assert_allclose(rotation, flip @ _rotation_from_rpy(rpy) @ flip, atol=1e-12)


class HierarchyTest(unittest.TestCase):
    def test_parents_come_before_children(self):
        manifest = _chain_manifest(Path("."))
        ordered, index = link_hierarchy(manifest)
        self.assertEqual([link["name"] for link in ordered], ["base", "arm", "tool"])
        self.assertLess(index["base"], index["arm"])
        self.assertLess(index["arm"], index["tool"])

    def test_orphan_link_is_appended_not_dropped(self):
        manifest = _chain_manifest(Path("."))
        manifest["links"].append({"name": "floating", "visuals": []})
        ordered, index = link_hierarchy(manifest)
        self.assertIn("floating", index)
        # The chain still has to be contiguous and ordered.
        self.assertLess(index["arm"], index["tool"])


class LinkTransformTest(unittest.TestCase):
    def test_root_is_identity_and_chain_composes(self):
        manifest = _chain_manifest(Path("."))
        globals_, locals_ = link_transforms(manifest)
        np.testing.assert_allclose(globals_[0], np.eye(4), atol=1e-12)

        expected = np.eye(4)
        for index in range(3):
            rotation, translation = locals_[index]
            local = np.eye(4)
            local[:3, :3] = rotation
            local[:3, 3] = translation
            expected = expected @ local
            np.testing.assert_allclose(globals_[index], expected, atol=1e-12)


class WeldTest(unittest.TestCase):
    def test_shared_corner_is_welded_once(self):
        # Two triangles sharing the edge x=0: 4 distinct corners, not 6.
        triangles = np.array(
            [
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]],
            ]
        )
        positions, indices, normals = weld_triangles(triangles)
        self.assertEqual(positions.shape[0], 4)
        self.assertEqual(indices.shape[0], 6)
        self.assertTrue(indices.max() < positions.shape[0])

    def test_opposed_faces_fall_back_to_a_real_normal(self):
        # Two exactly opposed triangles cancel to a zero vector; a zero normal
        # renders as garbage, so the weld has to substitute a real one.
        triangles = np.array(
            [
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
            ]
        )
        positions, indices, normals = weld_triangles(triangles)
        lengths = np.linalg.norm(normals, axis=1)
        np.testing.assert_allclose(lengths, np.ones_like(lengths), atol=1e-6)
        np.testing.assert_allclose(np.abs(normals[:, 2]), np.ones(normals.shape[0]), atol=1e-6)

    def test_negative_zero_does_not_split_a_vertex(self):
        triangles = np.array(
            [
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                [[-0.0, -0.0, -0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
            ]
        )
        positions, _, _ = weld_triangles(triangles)
        self.assertEqual(positions.shape[0], 3)

    def test_empty_input(self):
        positions, indices, normals = weld_triangles(np.zeros((0, 3, 3)))
        self.assertEqual(positions.shape, (0, 3))
        self.assertEqual(indices.shape, (0,))
        self.assertEqual(normals.shape, (0, 3))


class BuildSkinnedGltfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manifest = _chain_manifest(self.root)
        self.output = self.root / "robot.glb"
        self.report = build_skinned_gltf(self.manifest, self.output, base_dir=self.root)
        self.document, self.blob = _read_glb(self.output)

    def tearDown(self):
        self._tmp.cleanup()

    def test_report_counts_match_the_document(self):
        mesh = self.document["meshes"][0]
        primitive = mesh["primitives"][0]
        attributes = primitive["attributes"]
        count = self.document["accessors"][attributes["POSITION"]]["count"]
        self.assertEqual(count, self.report["vertices"])
        self.assertEqual(self.document["accessors"][primitive["indices"]]["count"] // 3,
                         self.report["triangles"])

    def test_every_accessor_count_agrees(self):
        primitive = self.document["meshes"][0]["primitives"][0]
        attributes = primitive["attributes"]
        counts = {name: self.document["accessors"][index]["count"]
                  for name, index in attributes.items()}
        self.assertEqual(len(set(counts.values())), 1, counts)

    def test_joint_count_and_names(self):
        self.assertEqual(self.document["skins"][0]["joints"], [0, 1, 2])
        self.assertEqual(self.report["joints"], ["base", "arm", "tool"])
        # Joint nodes occupy the first ``joint_count`` node slots, in the same
        # parent-before-child order as skin.joints.
        self.assertEqual(self.document["nodes"][0]["name"], "base")
        self.assertEqual(self.document["nodes"][1]["name"], "arm")
        self.assertEqual(self.document["nodes"][2]["name"], "tool")

    def test_inverse_bind_matrices_invert_the_node_hierarchy(self):
        # This is the whole ball game: jointMatrix = global(joint) * IBM must be
        # the identity in the bind pose, or the robot imports displaced.
        globals_ = _node_globals(self.document)
        accessor = self.document["skins"][0]["inverseBindMatrices"]
        raw = np.empty((3, 16), dtype=np.float64)
        for index in range(3):
            raw[index] = _accessor(self.document, self.blob, accessor)[index]
        for joint in range(3):
            # glTF stores matrices column-major.
            ibm = raw[joint].reshape(4, 4).T
            np.testing.assert_allclose(globals_[joint] @ ibm, np.eye(4), atol=1e-5)

    def test_weights_are_rigid_and_use_the_right_bone(self):
        primitive = self.document["meshes"][0]["primitives"][0]
        attributes = primitive["attributes"]
        weights = _accessor(self.document, self.blob, attributes["WEIGHTS_0"]).reshape(-1, 4)
        joints = _accessor(self.document, self.blob, attributes["JOINTS_0"]).reshape(-1, 4)
        np.testing.assert_allclose(weights.sum(axis=1), np.ones(weights.shape[0]), atol=1e-6)
        # Exactly one influence per vertex, and only the first column is used.
        self.assertEqual(int((weights > 0).sum()), weights.shape[0])
        np.testing.assert_allclose(joints[:, 1:], np.zeros_like(joints[:, 1:]))

        # Every joint must own some geometry, and the joint used must match the
        # link the vertex position actually came from.
        used = set(int(v) for v in np.unique(joints[:, 0]))
        self.assertEqual(used, {0, 1, 2})

        positions = _accessor(self.document, self.blob, attributes["POSITION"]).reshape(-1, 3)
        globals_ = _node_globals(self.document)
        for joint in sorted(used):
            subset = positions[joints[:, 0] == joint]
            # The mesh is baked into root space, so pulling the joint's bind
            # transform back out must land the vertices near their link origin.
            local = (subset - globals_[joint][:3, 3]) @ globals_[joint][:3, :3]
            self.assertLess(local[:, 2].max(), 0.5, f"joint {joint} kept z in root space")

    def test_indices_are_in_range_and_triangular(self):
        primitive = self.document["meshes"][0]["primitives"][0]
        indices = _accessor(self.document, self.blob, primitive["indices"])
        count = self.document["accessors"][primitive["attributes"]["POSITION"]]["count"]
        self.assertEqual(indices.size % 3, 0)
        self.assertLess(int(indices.max()), count)
        self.assertGreaterEqual(int(indices.min()), 0)

    def test_scene_references_the_mesh_node_and_the_root_joint(self):
        nodes = self.document["scenes"][0]["nodes"]
        self.assertIn(0, nodes)
        self.assertIn(self.document["skins"][0]["skeleton"], nodes)

    def test_max_visuals_per_link_shrinks_the_mesh(self):
        small = self.root / "small.glb"
        report = build_skinned_gltf(self.manifest, small, base_dir=self.root,
                                    max_visuals_per_link=1)
        self.assertLess(report["triangles"], self.report["triangles"])
        self.assertEqual(report["joint_count"], 3)

    def test_empty_manifest_is_rejected(self):
        manifest = _write_manifest(self.root, [], [], root_link="base")
        with self.assertRaises(ValueError):
            build_skinned_gltf(manifest, self.root / "nope.glb", base_dir=self.root)

    def test_cli_writes_a_report(self):
        report_path = self.root / "report.json"
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        output = self.root / "cli.glb"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main([str(manifest_path), "--output", str(output),
                         "--report", str(report_path), "--max-visuals-per-link", "1"])
        self.assertEqual(code, 0)
        self.assertTrue(output.exists())
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["joint_count"], 3)


if __name__ == "__main__":
    unittest.main()
