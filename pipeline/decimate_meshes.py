"""Shrink a manifest's source meshes to what the camera can actually resolve.

Why this exists
---------------
The ``总装`` robot's skinned asset is 16,786,420 triangles.  That number is not a
modelling decision, it is a *CAD* decision: the STLs were exported for
mechanical fidelity, so a 3x8x4 mm bearing is 79,680 triangles and the robot
places that bearing 160 times.  One mesh is 76% of the whole robot.

Two things make decimation the right lever rather than scene surgery:

* The exporter merges each link's visuals into one mesh, so a source mesh used
  ``n`` times contributes ``n`` copies.  Decimating 210 distinct source meshes
  is amplified 7.78x on the way out -- a cheap edit with a large effect.
* Every visual is rigid to its link, and each link's visuals share one
  material with no UVs.  There is no texture bake and no skinning weight
  gradient to preserve, so mesh simplification cannot corrupt anything except
  the silhouette itself.

Budget policy
-------------
The budget is expressed in the units that matter: **projected pixels**.  A part
whose bounding-box diagonal spans ``q`` pixels on screen needs roughly ``q``
triangles for its silhouette to stay inside a pixel (a chord-error argument:
``r(1 - cos(pi/n))`` against a one-pixel sagitta gives ``n ~ 1.6*sqrt(q)`` and
``2n^2 ~ 5q`` triangles for a doubly-curved part).  ``--tris-per-pixel`` is the
constant in front of ``q``; ``--pixel-size-m`` is the world size of one pixel at
the reference render, which is the only place the camera enters this file.

Guarded, not asserted
---------------------
QEM on CAD triangle soup has a floor.  ``新电池`` cannot be reduced below about
22k triangles however small a target is asked for, because its open shell
boundaries are expensive to collapse; ``dm_j4310`` floors near 1k.  The tool
therefore *asks* for the budget and *reports* what it got, and then checks the
result: points are sampled on both surfaces and the two-way distance is
measured with ray casting.  If the worst deviation exceeds
``--max-deviation-m`` the target is raised and the decimation retried, so the
output is bounded by a measured error rather than by a hope.

Visual geometry only
--------------------
This rewrites ``links[].visuals``.  That is deliberate: ``links[].collisions``
feeds MuJoCo, and the default ``simple_collision`` path derives the wheel
cylinder from the AABB of a wheel body's mesh geoms.  The bearings sit well
inside that AABB, so they carry no contact information -- but the meshes that
*do* set it must not be touched.  See ``docs/urdf_workflow.md``.

Needs open3d (``pip install open3d``), which is optional: nothing else in the
pipeline imports it.  ``main`` fails with that instruction rather than an
ImportError when it is missing.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from pipeline.urdf_visual_meshes import _visual_mesh_path, read_stl, write_binary_stl

#: One pixel at the reference render.  The perf runs draw a roughly 0.6 m robot
#: across 512 px, so a pixel is ~1.2 mm on the robot's surface.  Only used to
#: turn a world-space bounding box into a pixel count.
DEFAULT_PIXEL_SIZE_M = 0.0012

#: Triangles per projected pixel.  5.0 is the chord-error estimate for a doubly
#: curved surface; the default is deliberately below it because the error check
#: below is what actually bounds the result, and a lower start is cheaper.
DEFAULT_TRIS_PER_PIXEL = 4.0

#: Never reduce a source mesh below this, whatever the projection says: below a
#: dozen triangles a part is not a shape any more and reads as missing.
MIN_TRIANGLES = 12

#: Deviation the result is allowed to reach, as a multiple of ``pixel_size_m``.
#: At 1.0 the whole output is sub-pixel at the reference render.
DEFAULT_MAX_DEVIATION_PIXELS = 1.0


def _require_open3d():
    """Import open3d, or explain how to get it."""
    try:
        # Imported lazily: everything else in the pipeline runs on numpy alone.
        import open3d
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "pipeline.decimate_meshes needs open3d (pip install open3d); "
            "everything else in the pipeline runs on numpy alone"
        ) from exc
    return open3d


def pixel_budget(
    diagonal_m: float,
    *,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
    tris_per_pixel: float = DEFAULT_TRIS_PER_PIXEL,
    minimum: int = MIN_TRIANGLES,
) -> int:
    """Triangles a part of size ``diagonal_m`` may keep at ``pixel_size_m``.

    The part's projected diameter is ``diagonal_m/pixel_size_m`` pixels; the
    budget is ``tris_per_pixel`` triangles per projected pixel, floored at
    ``minimum``.  Degenerate (zero-size) parts get ``minimum``.
    """
    if pixel_size_m <= 0.0:
        raise ValueError("pixel_size_m must be positive")
    if diagonal_m <= 0.0 or not math.isfinite(diagonal_m):
        return minimum
    projected = diagonal_m / pixel_size_m
    return max(minimum, int(round(tris_per_pixel * projected)))


def _mesh_from_triangles(open3d: Any, triangles: np.ndarray) -> Any:
    """Build a shared-vertex open3d mesh from an ``(N, 3, 3)`` STL soup.

    STL stores each triangle's three vertices independently, so the welding and
    the degenerate-face cleanup are prerequisites for QEM to see a surface at
    all rather than 3N unrelated points.
    """
    mesh = open3d.geometry.TriangleMesh()
    mesh.vertices = open3d.utility.Vector3dVector(np.asarray(triangles, dtype=np.float64).reshape(-1, 3))
    mesh.triangles = open3d.utility.Vector3iVector(np.arange(triangles.shape[0]*3).reshape(-1, 3))
    mesh.remove_duplicated_vertices()
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.compute_vertex_normals()
    return mesh


def _triangles_from_mesh(mesh: Any) -> np.ndarray:
    """Inverse of :func:`_mesh_from_triangles`: an ``(N, 3, 3)`` array."""
    points = np.asarray(mesh.vertices, dtype=np.float64)
    index = np.asarray(mesh.triangles, dtype=np.int64)
    return points[index]


def _sample_surface(triangles: np.ndarray, count: int, *, seed: int = 0) -> np.ndarray:
    """Area-weighted uniform points on a triangle soup.

    Hand-rolled rather than ``open3d``'s sampler because that one takes no seed,
    and a measurement report that changes between runs is not a measurement.
    """
    if triangles.shape[0] == 0 or count <= 0:
        return np.zeros((0, 3), dtype=np.float64)
    edge_a = triangles[:, 1] - triangles[:, 0]
    edge_b = triangles[:, 2] - triangles[:, 0]
    area = 0.5*np.linalg.norm(np.cross(edge_a, edge_b), axis=1)
    total = float(area.sum())
    if total <= 0.0:
        return np.zeros((0, 3), dtype=np.float64)
    rng = np.random.default_rng(seed)
    index = rng.choice(triangles.shape[0], size=count, p=area/total)
    # Fold the unit square's upper triangle onto the lower one so the barycentric
    # pair stays uniform on the triangle instead of doubling the density there.
    u = rng.random(count)
    v = rng.random(count)
    flip = u + v > 1.0
    u = np.where(flip, 1.0 - u, u)
    v = np.where(flip, 1.0 - v, v)
    return triangles[index, 0] + edge_a[index]*u[:, None] + edge_b[index]*v[:, None]


def deviation_mm(original: Any, simplified: Any, *, samples: int = 50_000, seed: int = 0) -> dict[str, float]:
    """Two-way surface distance in millimetres between an original and its LOD.

    Both directions matter and they catch different failures: originals -> LOD
    finds detail that was smoothed away, LOD -> original finds vertices that
    drifted off the surface or bridged a gap.  ``max`` is what the retry loop
    acts on; the percentiles say whether the worst case is a spike or systemic.
    """
    open3d = _require_open3d()
    results: dict[str, float] = {}
    for label, source, target in (("orig_to_lod", original, simplified), ("lod_to_orig", simplified, original)):
        # A fixed seed keeps a report reproducible between runs; the sample count
        # is clamped so a 30-triangle mesh is not oversampled into the same
        # point repeatedly.
        count = max(1, min(samples, max(1, len(target.triangles))*4))
        points = _sample_surface(_triangles_from_mesh(source), count, seed=seed)
        if points.shape[0] == 0:
            results[f"{label}_max_mm"] = 0.0
            results[f"{label}_p99_mm"] = 0.0
            results[f"{label}_mean_mm"] = 0.0
            continue
        scene = open3d.t.geometry.RaycastingScene()
        scene.add_triangles(open3d.t.geometry.TriangleMesh.from_legacy(target))
        query = open3d.core.Tensor(points.astype(np.float32))
        distance = np.asarray(scene.compute_distance(query).numpy(), dtype=np.float64)
        results[f"{label}_max_mm"] = float(distance.max()*1000.0)
        results[f"{label}_p99_mm"] = float(np.percentile(distance, 99.0)*1000.0)
        results[f"{label}_mean_mm"] = float(distance.mean()*1000.0)
    results["max_mm"] = max(results["orig_to_lod_max_mm"], results["lod_to_orig_max_mm"])
    return results


def decimate_triangles(
    triangles: np.ndarray,
    target: int,
    *,
    boundary_weight: float = 1.0,
) -> np.ndarray:
    """QEM-decimate an ``(N, 3, 3)`` triangle soup towards ``target`` triangles.

    Returns the *reachable* result, which can be well above ``target``: CAD
    shells exported as STL have open boundaries that resist collapse.  Callers
    compare the returned count against ``target`` themselves.
    """
    open3d = _require_open3d()
    mesh = _mesh_from_triangles(open3d, triangles)
    if target >= len(mesh.triangles):
        return _triangles_from_mesh(mesh)
    simplified = mesh.simplify_quadric_decimation(
        target_number_of_triangles=int(target),
        boundary_weight=boundary_weight,
    )
    simplified.remove_degenerate_triangles()
    simplified.remove_duplicated_triangles()
    return _triangles_from_mesh(simplified)


def _diagonal_m(triangles: np.ndarray) -> float:
    """Bounding-box diagonal of a triangle soup, in the mesh's own units."""
    if triangles.shape[0] == 0:
        return 0.0
    low = triangles.reshape(-1, 3).min(axis=0)
    high = triangles.reshape(-1, 3).max(axis=0)
    return float(np.linalg.norm(high - low))


def _distinct_mesh_paths(manifest: dict[str, Any], *, base_dir: Path | None) -> list[Path]:
    """Every source mesh the manifest's visuals reference, in first-seen order."""
    seen: dict[str, Path] = {}
    for link in manifest.get("links") or []:
        for visual in link.get("visuals") or []:
            path = _visual_mesh_path(visual, base_dir)
            if path is not None:
                seen.setdefault(str(path), path)
    return list(seen.values())


def _rewrite_paths(manifest: dict[str, Any], mapping: dict[str, Path], *, base_dir: Path | None) -> int:
    """Point every visual's mesh at ``mapping``'s replacement.  Returns the count."""
    changed = 0
    for link in manifest.get("links") or []:
        for visual in link.get("visuals") or []:
            path = _visual_mesh_path(visual, base_dir)
            if path is None:
                continue
            replacement = mapping.get(str(path))
            if replacement is None:
                continue
            visual["mesh_path"] = str(replacement)
            geometry = visual.get("geometry")
            if isinstance(geometry, dict) and geometry.get("filename"):
                geometry["filename"] = str(replacement)
            changed += 1
    return changed


def decimate_manifest(
    manifest: dict[str, Any],
    *,
    output_dir: str | Path,
    base_dir: str | Path | None = None,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
    tris_per_pixel: float = DEFAULT_TRIS_PER_PIXEL,
    max_deviation_pixels: float = DEFAULT_MAX_DEVIATION_PIXELS,
    verify_samples: int = 50_000,
    boundary_weight: float = 1.0,
    max_retries: int = 3,
    progress: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decimate every source mesh and return ``(new_manifest, report)``.

    The manifest is returned as a copy that points at STLs written under
    ``output_dir``; the originals are never modified.  ``base_dir`` resolves
    relative manifest paths and defaults to the manifest's own directory in
    :func:`main`.
    """
    base = Path(base_dir) if base_dir is not None else None
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    allowance_mm = max_deviation_pixels*pixel_size_m*1000.0

    mesh_entries: list[dict[str, Any]] = []
    mapping: dict[str, Path] = {}
    for path in _distinct_mesh_paths(manifest, base_dir=base):
        triangles = read_stl(path)
        original_count = int(triangles.shape[0])
        diagonal = _diagonal_m(triangles)
        budget = pixel_budget(diagonal, pixel_size_m=pixel_size_m, tris_per_pixel=tris_per_pixel)
        entry: dict[str, Any] = {
            "mesh": path.name,
            "source": str(path),
            "triangles_before": original_count,
            "bbox_diagonal_mm": diagonal*1000.0,
            "projected_px": diagonal/pixel_size_m if pixel_size_m > 0.0 else 0.0,
            "budget": budget,
        }
        if original_count <= budget:
            entry["status"] = "kept"
            entry["triangles_after"] = original_count
            mesh_entries.append(entry)
            if progress is not None:
                progress(entry)
            continue

        open3d = _require_open3d()
        original = _mesh_from_triangles(open3d, triangles)
        target = budget
        attempts: list[dict[str, Any]] = []
        simplified = original
        measured: dict[str, float] = {}
        for attempt in range(max_retries + 1):
            simplified = _mesh_from_triangles(
                open3d, decimate_triangles(triangles, target, boundary_weight=boundary_weight)
            )
            measured = deviation_mm(original, simplified, samples=verify_samples)
            attempts.append({"target": target, "triangles": len(simplified.triangles), **measured})
            if measured["max_mm"] <= allowance_mm or target >= original_count:
                break
            # Not good enough: give the mesh four times the budget and try again.
            # Four is coarse on purpose -- the point is to land under the
            # allowance, not to bisect it, and each retry costs a full QEM pass.
            target = min(original_count, target*4)
            if attempts[-1]["triangles"] <= 4 and target >= original_count:
                break

        destination = out/path.name
        write_binary_stl(destination, _triangles_from_mesh(simplified))
        mapping[str(path)] = destination
        entry["status"] = "decimated" if len(simplified.triangles) < original_count else "kept"
        entry["triangles_after"] = len(simplified.triangles)
        entry["ratio"] = original_count/max(1, len(simplified.triangles))
        entry["attempts"] = attempts
        entry["deviation"] = measured
        entry["within_allowance"] = measured["max_mm"] <= allowance_mm
        mesh_entries.append(entry)
        if progress is not None:
            progress(entry)

    rewritten = json.loads(json.dumps(manifest))
    changed = _rewrite_paths(rewritten, mapping, base_dir=base)
    report = {
        "pixel_size_m": pixel_size_m,
        "tris_per_pixel": tris_per_pixel,
        "allowance_mm": allowance_mm,
        "mesh_count": len(mesh_entries),
        "decimated_count": sum(1 for entry in mesh_entries if entry["status"] == "decimated"),
        "over_allowance": [entry["mesh"] for entry in mesh_entries if entry.get("within_allowance") is False],
        "visuals_rewritten": changed,
        "meshes": sorted(mesh_entries, key=lambda entry: -(entry["triangles_before"] - entry["triangles_after"])),
    }
    return rewritten, report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path, help="manifest.json whose visuals should be decimated")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for the decimated STLs")
    parser.add_argument("--manifest-out", type=Path, default=None, help="where to write the rewritten manifest")
    parser.add_argument("--report", type=Path, default=None, help="where to write the JSON report")
    parser.add_argument("--pixel-size-m", type=float, default=DEFAULT_PIXEL_SIZE_M, help="world size of one pixel at the reference render")
    parser.add_argument("--tris-per-pixel", type=float, default=DEFAULT_TRIS_PER_PIXEL, help="triangle budget per projected pixel")
    parser.add_argument("--max-deviation-pixels", type=float, default=DEFAULT_MAX_DEVIATION_PIXELS, help="deviation allowance in pixels")
    parser.add_argument("--verify-samples", type=int, default=50_000, help="surface samples per deviation measurement")
    parser.add_argument("--boundary-weight", type=float, default=1.0, help="QEM boundary preservation weight (0 frees open shells)")
    parser.add_argument("--max-retries", type=int, default=3, help="times to raise the budget after a failed error check")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_out = args.manifest_out or args.output_dir/"manifest.json"
    report_path = args.report or args.output_dir/"decimate_report.json"

    def progress(entry: dict[str, Any]) -> None:
        print(json.dumps({
            "event": "mesh",
            "mesh": entry["mesh"],
            "before": entry["triangles_before"],
            "after": entry["triangles_after"],
            "status": entry["status"],
            "budget": entry["budget"],
            "max_mm": entry.get("deviation", {}).get("max_mm"),
        }, ensure_ascii=False), flush=True)

    rewritten, report = decimate_manifest(
        manifest,
        output_dir=args.output_dir,
        base_dir=manifest_path.parent,
        pixel_size_m=args.pixel_size_m,
        tris_per_pixel=args.tris_per_pixel,
        max_deviation_pixels=args.max_deviation_pixels,
        verify_samples=args.verify_samples,
        boundary_weight=args.boundary_weight,
        max_retries=args.max_retries,
        progress=progress,
    )
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(rewritten, ensure_ascii=False), encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "event": "summary",
        "meshes": report["mesh_count"],
        "decimated": report["decimated_count"],
        "over_allowance": report["over_allowance"],
        "visuals_rewritten": report["visuals_rewritten"],
        "manifest_out": str(manifest_out),
        "report": str(report_path),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
