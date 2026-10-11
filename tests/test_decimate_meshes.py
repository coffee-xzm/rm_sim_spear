"""Offline tests for pipeline.decimate_meshes.

The decimator is where a rendering-only edit could accidentally reach the
physics model, so the tests pin two things: that it only ever rewrites
``links[].visuals`` (``links[].collisions`` is what MuJoCo reads), and that a
mesh which is already inside its budget is passed through untouched rather than
being re-encoded through the simplifier.

The geometry tests need open3d, which is an optional dependency -- nothing else
in the pipeline imports it.  They skip rather than fail when it is absent, so
the main suite keeps running on a numpy-only install.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pipeline.decimate_meshes import (
    DEFAULT_PIXEL_SIZE_M,
    MIN_TRIANGLES,
    _rewrite_paths,
    _sample_surface,
    decimate_manifest,
    main,
    pixel_budget,
)
from pipeline.urdf_visual_meshes import read_stl, write_binary_stl


def _has_open3d() -> bool:
    """Whether the optional geometry dependency is importable at all."""
    return importlib.util.find_spec("open3d") is not None


def _box(size=1.0, div=6):
    """A subdivided box as an ``(N, 3, 3)`` triangle soup.

    A plain 12-triangle box has nothing to decimate, so the grid is subdivided;
    each face becomes ``div*div`` quads and the shared grid vertices are
    repeated per triangle exactly as an STL would store them.
    """
    half = size/2.0
    edges = np.linspace(-half, half, div + 1)
    triangles = []
    for axis in range(3):
        for sign in (-1.0, 1.0):
            for i in range(div):
                for j in range(div):
                    corners = []
                    for du, dv in ((0, 0), (1, 0), (1, 1), (0, 1)):
                        point = [0.0, 0.0, 0.0]
                        point[axis] = sign*half
                        others = [k for k in range(3) if k != axis]
                        point[others[0]] = edges[i + du]
                        point[others[1]] = edges[j + dv]
                        corners.append(point)
                    triangles.append([corners[0], corners[1], corners[2]])
                    triangles.append([corners[0], corners[2], corners[3]])
    return np.array(triangles, dtype=np.float64)


class PixelBudgetTests(unittest.TestCase):
    def test_budget_scales_with_projected_size(self):
        small = pixel_budget(0.010, pixel_size_m=0.001, tris_per_pixel=4.0)
        large = pixel_budget(0.100, pixel_size_m=0.001, tris_per_pixel=4.0)
        self.assertEqual(small, 40)
        self.assertEqual(large, 400)

    def test_floor_applies_to_tiny_parts(self):
        # A part far under one pixel still has to be a shape.
        self.assertEqual(pixel_budget(1e-6, pixel_size_m=0.001), MIN_TRIANGLES)

    def test_degenerate_and_invalid_inputs(self):
        self.assertEqual(pixel_budget(0.0), MIN_TRIANGLES)
        self.assertEqual(pixel_budget(float("nan")), MIN_TRIANGLES)
        with self.assertRaises(ValueError):
            pixel_budget(0.01, pixel_size_m=0.0)

    def test_default_matches_documented_pixel_size(self):
        # 12 mm - the bearing that dominates the robot - is ten pixels across.
        self.assertEqual(DEFAULT_PIXEL_SIZE_M, 0.0012)


class SamplingTests(unittest.TestCase):
    def test_samples_land_on_the_surface(self):
        triangles = _box(size=2.0, div=2)
        points = _sample_surface(triangles, 500, seed=1)
        self.assertEqual(points.shape, (500, 3))
        # Every point of a unit box shell has max|coordinate| == 1.
        self.assertLess(np.abs(np.abs(points).max(axis=1) - 1.0).max(), 1e-9)

    def test_sampling_is_seeded(self):
        triangles = _box()
        self.assertTrue(np.array_equal(
            _sample_surface(triangles, 100, seed=7),
            _sample_surface(triangles, 100, seed=7),
        ))

    def test_empty_soup_yields_no_points(self):
        self.assertEqual(_sample_surface(np.zeros((0, 3, 3)), 10).shape, (0, 3))


class RewriteTests(unittest.TestCase):
    """The visual-only guarantee: collisions must survive a rewrite untouched."""

    def _manifest(self, root: Path):
        original = root/"part.stl"
        write_binary_stl(original, _box())
        visual = {"name": "v", "mesh_path": str(original), "geometry": {"type": "mesh", "filename": str(original)}}
        collision = {"name": "c", "mesh_path": str(original), "geometry": {"type": "mesh", "filename": str(original)}}
        return {"links": [{"name": "base_link", "visuals": [visual], "collisions": [collision]}]}, original

    def test_rewrite_touches_visuals_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, original = self._manifest(root)
            replacement = root/"part_lod.stl"
            changed = _rewrite_paths(manifest, {str(original): replacement}, base_dir=None)
            self.assertEqual(changed, 1)
            visual = manifest["links"][0]["visuals"][0]
            collision = manifest["links"][0]["collisions"][0]
            self.assertEqual(visual["mesh_path"], str(replacement))
            self.assertEqual(visual["geometry"]["filename"], str(replacement))
            self.assertEqual(collision["mesh_path"], str(original))
            self.assertEqual(collision["geometry"]["filename"], str(original))


class ManifestTests(unittest.TestCase):
    def _write_manifest(self, root: Path, triangles: np.ndarray) -> Path:
        mesh = root/"part.stl"
        write_binary_stl(mesh, triangles)
        manifest = {
            "robot": "test",
            "links": [{
                "name": "base_link",
                "visuals": [{"name": "v", "mesh_path": str(mesh), "geometry": {"type": "mesh", "filename": str(mesh)}}],
                "collisions": [{"name": "c", "mesh_path": str(mesh), "geometry": {"type": "mesh", "filename": str(mesh)}}],
            }],
        }
        path = root/"manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_mesh_inside_budget_is_copied_over_untouched(self):
        # A 12-triangle box at 1 mm is far under any budget, so this path needs
        # no open3d and must not rewrite the path at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write_manifest(root, _box(size=0.001, div=1))
            manifest = json.loads(path.read_text())
            rewritten, report = decimate_manifest(
                manifest, output_dir=root/"out", base_dir=root, pixel_size_m=0.001,
            )
            self.assertEqual(report["decimated_count"], 0)
            self.assertEqual(report["visuals_rewritten"], 0)
            self.assertEqual(
                rewritten["links"][0]["visuals"][0]["mesh_path"],
                manifest["links"][0]["visuals"][0]["mesh_path"],
            )

    @unittest.skipUnless(_has_open3d(), "open3d not installed")
    def test_decimation_reduces_triangles_within_allowance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write_manifest(root, _box(size=0.05, div=24))
            manifest = json.loads(path.read_text())
            rewritten, report = decimate_manifest(
                manifest, output_dir=root/"out", base_dir=root,
                pixel_size_m=0.001, tris_per_pixel=4.0, max_deviation_pixels=1.0,
            )
            self.assertEqual(report["decimated_count"], 1)
            entry = report["meshes"][0]
            self.assertLess(entry["triangles_after"], entry["triangles_before"])
            self.assertTrue(entry["within_allowance"], entry["deviation"])
            new_path = Path(rewritten["links"][0]["visuals"][0]["mesh_path"])
            self.assertTrue(new_path.exists())
            self.assertEqual(new_path.parent, root/"out")
            self.assertEqual(read_stl(new_path).shape[0], entry["triangles_after"])

    @unittest.skipUnless(_has_open3d(), "open3d not installed")
    def test_collision_mesh_is_never_decimated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write_manifest(root, _box(size=0.05, div=24))
            manifest = json.loads(path.read_text())
            rewritten, _ = decimate_manifest(
                manifest, output_dir=root/"out", base_dir=root, pixel_size_m=0.001,
            )
            collisions = rewritten["links"][0]["collisions"][0]
            self.assertEqual(collisions["mesh_path"], manifest["links"][0]["collisions"][0]["mesh_path"])
            # And no decimated STL was written for it beyond the visual's copy.
            self.assertEqual(sorted(p.name for p in (root/"out").glob("*.stl")), ["part.stl"])


class MainTests(unittest.TestCase):
    def test_main_writes_manifest_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mesh = root/"part.stl"
            write_binary_stl(mesh, _box(size=0.001, div=1))
            manifest_path = root/"manifest.json"
            manifest_path.write_text(json.dumps({
                "links": [{"name": "base_link", "visuals": [
                    {"name": "v", "mesh_path": str(mesh), "geometry": {"type": "mesh", "filename": str(mesh)}},
                ]}],
            }), encoding="utf-8")
            out = root/"out"
            rc = main([str(manifest_path), "--output-dir", str(out)])
            self.assertEqual(rc, 0)
            self.assertTrue((out/"manifest.json").exists())
            report = json.loads((out/"decimate_report.json").read_text())
            self.assertEqual(report["mesh_count"], 1)


if __name__ == "__main__":
    unittest.main()
