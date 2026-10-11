"""Offline tests for pipeline.urdf_visual_meshes.

Every fixture is built inside the test's own temp directory so the suite never
depends on the generated 总装 assets (which are large and not always present).
"""

import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pipeline.urdf_visual_meshes import (
    build_visual_meshes,
    main,
    merge_link_visuals,
    origin_matrix,
    read_stl,
    write_binary_stl,
    write_obj,
)


def _raw_binary_stl(triangles, header=b"swURDF high-performance binary STL") -> bytes:
    """Build binary STL bytes without the module's writer.

    Keeping this independent of ``write_binary_stl`` means the round-trip test
    cannot pass because both sides share the same bug.
    """
    tris = np.asarray(triangles, dtype="<f4").reshape(-1, 3, 3)
    out = bytearray(header.ljust(80, b"\0"))
    out += struct.pack("<I", len(tris))
    for triangle in tris:
        out += struct.pack("<3f", 0.0, 0.0, 0.0)
        for vertex in triangle:
            out += struct.pack("<3f", float(vertex[0]), float(vertex[1]), float(vertex[2]))
        out += struct.pack("<H", 0)
    return bytes(out)


def _triangle(offset=(0.0, 0.0, 0.0)):
    base = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float64
    )
    return base + np.asarray(offset, dtype=np.float64)


def _visual(path, *, name="visual_000", xyz=(0.0, 0.0, 0.0), rpy=(0.0, 0.0, 0.0), exists=True):
    return {
        "name": name,
        "origin": {"xyz": list(xyz), "rpy": list(rpy)},
        "geometry": {"type": "mesh", "filename": Path(path).name if path else None},
        "mesh_path": str(path) if path else None,
        "mesh_exists": exists,
    }


class StlIo(unittest.TestCase):
    def test_binary_round_trip_preserves_count_and_coordinates(self):
        triangles = np.array(
            [
                [[0.0, 0.0, 0.0], [1.0, 0.25, -0.5], [0.125, 2.0, 3.0]],
                [[-1.5, 2.5, -3.5], [4.0, 0.0, 0.5], [0.0, -0.125, 7.0]],
            ],
            dtype=np.float64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tiny.stl"
            written = write_binary_stl(path, triangles)
            loaded = read_stl(path)
        self.assertEqual(written, 2)
        self.assertEqual(loaded.shape, (2, 3, 3))
        self.assertEqual(loaded.dtype, np.float64)
        np.testing.assert_allclose(loaded, triangles, atol=1e-5, rtol=0)

    def test_reads_binary_stl_written_by_an_independent_writer(self):
        triangles = np.array(
            [[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]], dtype=np.float64
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.stl"
            path.write_bytes(_raw_binary_stl(triangles))
            loaded = read_stl(path)
        np.testing.assert_allclose(loaded, triangles, atol=1e-6, rtol=0)

    def test_binary_win_even_when_header_starts_with_solid(self):
        # Real exporters do this; the size self-check must win over the keyword.
        triangles = _triangle()[None, ...]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solid_header.stl"
            path.write_bytes(_raw_binary_stl(triangles, header=b"solid exported by swURDF"))
            loaded = read_stl(path)
        self.assertEqual(loaded.shape, (1, 3, 3))
        np.testing.assert_allclose(loaded, triangles, atol=1e-6, rtol=0)

    def test_ascii_stl_is_parsed(self):
        ascii_stl = """solid sample
  facet normal 0.0 0.0 1.0
    outer loop
      vertex 0.0 0.0 0.0
      vertex 1.0 0.0 0.0
      vertex 0.0 1.0 0.0
    endloop
  endfacet
  facet normal 0.0 0.0 1.0
    outer loop
      vertex 5.0 5.0 5.0
      vertex 6.0 5.0 5.0
      vertex 5.0 6.0 5.5
    endloop
  endfacet
endsolid sample
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ascii.stl"
            path.write_text(ascii_stl, encoding="utf-8")
            loaded = read_stl(path)
        self.assertEqual(loaded.shape, (2, 3, 3))
        np.testing.assert_allclose(loaded[0, 1], [1.0, 0.0, 0.0])
        np.testing.assert_allclose(loaded[1, 2], [5.0, 6.0, 5.5])

    def test_unknown_and_missing_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            garbage = Path(directory) / "garbage.stl"
            garbage.write_bytes(b"not an stl at all")
            with self.assertRaises(ValueError):
                read_stl(garbage)
            with self.assertRaises(FileNotFoundError):
                read_stl(Path(directory) / "absent.stl")


class OriginMatrix(unittest.TestCase):
    def test_identity_and_translation(self):
        np.testing.assert_allclose(origin_matrix([0, 0, 0], [0, 0, 0]), np.eye(4))
        matrix = origin_matrix([1.0, -2.0, 3.5], [0.0, 0.0, 0.0])
        np.testing.assert_allclose(matrix[:3, 3], [1.0, -2.0, 3.5])
        np.testing.assert_allclose(matrix[3], [0.0, 0.0, 0.0, 1.0])

    def test_single_axis_rotations_match_hand_computed_values(self):
        roll = origin_matrix([0, 0, 0], [math.pi / 2, 0, 0])
        np.testing.assert_allclose(
            roll[:3, :3], [[1, 0, 0], [0, 0, -1], [0, 1, 0]], atol=1e-12
        )
        pitch = origin_matrix([0, 0, 0], [0, math.pi / 2, 0])
        np.testing.assert_allclose(
            pitch[:3, :3], [[0, 0, 1], [0, 1, 0], [-1, 0, 0]], atol=1e-12
        )
        yaw = origin_matrix([0, 0, 0], [0, 0, math.pi / 2])
        np.testing.assert_allclose(
            yaw[:3, :3], [[0, -1, 0], [1, 0, 0], [0, 0, 1]], atol=1e-12
        )

    def test_combined_rpy_is_rz_ry_rx(self):
        roll, pitch, yaw = 0.3, -0.4, 0.5
        cx, sx = math.cos(roll), math.sin(roll)
        cy, sy = math.cos(pitch), math.sin(pitch)
        cz, sz = math.cos(yaw), math.sin(yaw)
        # Written out longhand from R = Rz @ Ry @ Rx rather than reusing the
        # module's helpers, so a swapped order or a transposed convention fails.
        expected = np.array(
            [
                [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
                [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
                [-sy, cy * sx, cy * cx],
            ]
        )
        np.testing.assert_allclose(
            origin_matrix([0, 0, 0], [roll, pitch, yaw])[:3, :3], expected, atol=1e-12
        )


class MergeLinkVisuals(unittest.TestCase):
    def test_visual_origins_are_applied_in_manifest_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.stl"
            second = root / "b.stl"
            write_binary_stl(first, _triangle())
            write_binary_stl(second, _triangle())
            link = {
                "name": "link_1",
                "visuals": [
                    _visual(first, name="visual_000", xyz=(0.0, 0.0, 0.0)),
                    _visual(second, name="visual_001", xyz=(1.0, 2.0, 3.0)),
                ],
            }
            merged = merge_link_visuals(link, scale=1.0, ue_axes=False)
        self.assertEqual(merged.shape, (2, 3, 3))
        np.testing.assert_allclose(merged[0], _triangle(), atol=1e-6)
        np.testing.assert_allclose(merged[1], _triangle((1.0, 2.0, 3.0)), atol=1e-6)
        self.assertGreater(np.linalg.norm(merged[0] - merged[1]), 1.0)

    def test_visual_rotation_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.stl"
            write_binary_stl(path, _triangle())
            link = {
                "name": "link_1",
                "visuals": [_visual(path, rpy=(0.0, 0.0, math.pi / 2))],
            }
            merged = merge_link_visuals(link, scale=1.0, ue_axes=False)
        # +X under a +90 degree yaw lands on +Y.
        np.testing.assert_allclose(merged[0, 1], [0.0, 1.0, 0.0], atol=1e-6)

    def test_ue_axes_and_scale_convert_metres_to_centimetres(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.stl"
            write_binary_stl(path, [[[1.0, 2.0, 3.0], [0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]])
            link = {"name": "link_1", "visuals": [_visual(path)]}
            ue = merge_link_visuals(link, scale=100.0, ue_axes=True)
            raw = merge_link_visuals(link, scale=100.0, ue_axes=False)
        np.testing.assert_allclose(ue[0, 0], [100.0, -200.0, 300.0], atol=1e-4)
        np.testing.assert_allclose(raw[0, 0], [100.0, 200.0, 300.0], atol=1e-4)

    def test_missing_and_absent_meshes_never_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.stl"
            write_binary_stl(path, _triangle())
            link = {
                "name": "link_1",
                "visuals": [
                    _visual(path, name="kept"),
                    _visual(path, name="flagged_missing", exists=False),
                    _visual(Path(directory) / "gone.stl", name="unreadable"),
                    {"name": "empty", "geometry": {"type": "mesh", "filename": None}},
                ],
            }
            merged = merge_link_visuals(link, scale=1.0, ue_axes=False)
        self.assertEqual(merged.shape, (1, 3, 3))

    def test_link_without_visuals_returns_empty_array(self):
        merged = merge_link_visuals({"name": "link_1", "visuals": []})
        self.assertEqual(merged.shape, (0, 3, 3))
        self.assertEqual(merged.dtype, np.float64)


class BuildVisualMeshes(unittest.TestCase):
    def _manifest(self, root: Path) -> dict:
        """Three links: root, a child with two visuals, and a fully skipped one."""
        mesh_a = root / "a.stl"
        mesh_b = root / "b.stl"
        write_binary_stl(mesh_a, _triangle())
        write_binary_stl(mesh_b, _triangle())
        missing = root / "missing.stl"
        return {
            "schema_version": 1,
            "robot": "mini",
            "source_urdf": str(root / "mini.urdf"),
            "units": {"urdf_length": "m", "ue_length": "cm"},
            "root_link": "base_link",
            "links": [
                {"name": "base_link", "visuals": [_visual(mesh_a, name="visual_000")]},
                {
                    "name": "link_1",
                    "visuals": [
                        _visual(mesh_b, name="visual_000", xyz=(0.0, 0.0, 0.0)),
                        _visual(mesh_b, name="visual_001", xyz=(0.1, 0.0, 0.0)),
                    ],
                },
                {"name": "link_2", "visuals": [_visual(missing, name="visual_000", exists=False)]},
            ],
            "joints": [
                {
                    "name": "base_link_link_1_joint",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "link_1",
                    "origin": {"xyz": [0.1, 0.2, 0.3], "rpy": [0.0, 0.0, 0.0]},
                    "axis": [0.0, 0.0, 1.0],
                }
            ],
        }

    def test_writes_one_file_per_link_with_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            output = root / "visual_meshes"
            report = build_visual_meshes(manifest, output)
            written = sorted(path.name for path in output.iterdir())
            link_2_written = (output / "link_2.stl").exists()
            loaded = read_stl(output / "link_1.stl")
        self.assertEqual(written, ["base_link.stl", "link_1.stl", "link_2.stl"])
        self.assertEqual(report["robot"], "mini")
        self.assertEqual(report["units"]["ue_length"], "cm")
        self.assertEqual(report["root_link"], "base_link")
        self.assertEqual(report["fmt"], "stl")
        self.assertEqual(report["total_triangles"], 3)

        links = {entry["name"]: entry for entry in report["links"]}
        self.assertEqual(links["base_link"]["parent"], None)
        self.assertEqual(links["base_link"]["joint"], None)
        self.assertEqual(links["base_link"]["origin_ue_cm"], [0.0, 0.0, 0.0])
        self.assertEqual(links["link_1"]["parent"], "base_link")
        self.assertEqual(links["link_1"]["joint"], "base_link_link_1_joint")
        self.assertEqual(links["link_1"]["origin_ue_cm"], [10.0, -20.0, 30.0])
        self.assertEqual(links["link_1"]["triangles"], 2)
        self.assertEqual(links["link_1"]["visual_count"], 2)
        self.assertEqual([v["triangles"] for v in links["link_1"]["visuals"]], [1, 1])
        self.assertEqual(
            links["link_1"]["visuals"][1]["visual_origin_xyz"], [0.1, 0.0, 0.0]
        )
        self.assertEqual(links["link_2"]["triangles"], 0)
        self.assertEqual(links["link_2"]["visual_count"], 0)
        self.assertEqual(links["link_2"]["parent"], None)
        self.assertTrue(link_2_written)
        self.assertEqual(loaded.shape, (2, 3, 3))

        self.assertEqual(len(report["skipped_visuals"]), 1)
        skipped = report["skipped_visuals"][0]
        self.assertEqual(skipped["link"], "link_2")
        self.assertEqual(skipped["visual"], "visual_000")
        self.assertEqual(skipped["reason"], "mesh_exists=false")
        self.assertEqual(Path(skipped["mesh_path"]).name, "missing.stl")
        # The report has to survive a JSON round trip for the UE importer.
        json.loads(json.dumps(report, ensure_ascii=False))

    def test_mesh_paths_are_absolute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            report = build_visual_meshes(manifest, root / "out")
        for entry in report["links"]:
            self.assertEqual(
                entry["mesh_path"], str(root / "out" / f"{entry['name']}.stl")
            )

    def test_relative_filenames_resolve_against_source_urdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meshes = root / "meshes"
            meshes.mkdir()
            write_binary_stl(meshes / "part.stl", _triangle())
            manifest = {
                "robot": "relative",
                "source_urdf": str(root / "robot.urdf"),
                "root_link": "base_link",
                "links": [
                    {
                        "name": "base_link",
                        "visuals": [
                            {
                                "name": "visual_000",
                                "origin": {"xyz": [0, 0, 0], "rpy": [0, 0, 0]},
                                "geometry": {"type": "mesh", "filename": "meshes/part.stl"},
                                "mesh_exists": True,
                            }
                        ],
                    }
                ],
                "joints": [],
            }
            report = build_visual_meshes(manifest, root / "out", fmt="obj")
            obj_path = Path(report["links"][0]["mesh_path"])
            obj_text = obj_path.read_text(encoding="utf-8")
        self.assertEqual(report["links"][0]["triangles"], 1)
        self.assertEqual(report["links"][0]["visual_count"], 1)
        self.assertIn("v ", obj_text)

    def test_obj_output_has_three_vertices_and_one_face_per_triangle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            report = build_visual_meshes(manifest, root / "out", fmt="obj")
            obj_path = Path(next(e for e in report["links"] if e["name"] == "link_1")["mesh_path"])
            lines = [line.split() for line in obj_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(obj_path.suffix, ".obj")
        self.assertEqual(report["fmt"], "obj")
        self.assertTrue(all(Path(e["mesh_path"]).suffix == ".obj" for e in report["links"]))
        vertices = [line for line in lines if line[0] == "v"]
        faces = [line for line in lines if line[0] == "f"]
        self.assertEqual(len(vertices), 3 * 2)
        self.assertEqual(len(faces), 2)
        for index, face in enumerate(faces):
            self.assertEqual(face[1:], [str(3 * index + 1), str(3 * index + 2), str(3 * index + 3)])
        for line in vertices:
            self.assertEqual(len(line), 4)

    def test_write_obj_returns_triangle_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "two.obj"
            count = write_obj(path, np.stack([_triangle(), _triangle((1, 1, 1))]))
            text = path.read_text(encoding="utf-8")
        self.assertEqual(count, 2)
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("v ")), 6)
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("f ")), 2)

    def test_unreadable_mesh_is_reported_and_does_not_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.stl"
            write_binary_stl(good, _triangle())
            manifest = {
                "robot": "broken",
                "root_link": "base_link",
                "links": [
                    {
                        "name": "base_link",
                        "visuals": [
                            _visual(good, name="visual_000"),
                            _visual(root / "gone.stl", name="visual_001", exists=True),
                        ],
                    }
                ],
                "joints": [],
            }
            report = build_visual_meshes(manifest, root / "out")
        self.assertEqual(report["total_triangles"], 1)
        self.assertEqual(report["links"][0]["visual_count"], 1)
        self.assertEqual([entry["reason"] for entry in report["skipped_visuals"]], ["mesh_unreadable"])

    def test_unknown_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = {"robot": "r", "root_link": "base_link", "links": [], "joints": []}
            with self.assertRaises(ValueError):
                build_visual_meshes(manifest, Path(directory) / "out", fmt="fbx")


class CommandLine(unittest.TestCase):
    def test_main_writes_report_and_prints_one_json_line(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = BuildVisualMeshes()._manifest(root)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main([str(manifest_path), "--output-root", str(root / "out")])
            report = json.loads((root / "out" / "visual_meshes.json").read_text(encoding="utf-8"))
        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        summary = json.loads(lines[0])
        self.assertEqual(summary["links"], 3)
        self.assertEqual(summary["triangles"], 3)
        self.assertEqual(summary["skipped_visuals"], 1)
        self.assertEqual(report["total_triangles"], 3)

    def test_module_entry_point_honours_report_and_no_ue_axes(self):
        import os
        import subprocess
        import sys

        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(BuildVisualMeshes()._manifest(root)), encoding="utf-8"
            )
            report_path = root / "custom_report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pipeline.urdf_visual_meshes",
                    str(manifest_path),
                    "--output-root",
                    str(root / "out"),
                    "--fmt",
                    "obj",
                    "--no-ue-axes",
                    "--report",
                    str(report_path),
                ],
                cwd=repository_root,
                env=dict(os.environ, PYTHONPATH=str(repository_root)),
                capture_output=True,
                text=True,
                check=True,
            )
            stdout_lines = completed.stdout.strip().splitlines()
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stdout_lines), 1)
        self.assertEqual(json.loads(stdout_lines[0])["fmt"], "obj")
        self.assertEqual(report["fmt"], "obj")
        self.assertEqual(report["total_triangles"], 3)
        self.assertTrue(all(entry["mesh_path"].endswith(".obj") for entry in report["links"]))


if __name__ == "__main__":
    unittest.main()
