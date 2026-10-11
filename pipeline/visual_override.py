"""Interpret ``ue_overrides.json`` visual overrides (URDF vs UE asset).

``docs/urdf_workflow.md`` ("UE visual override") specifies that the *importer*
interprets this file: per link, ``visual_source`` selects where the UE visual
comes from -- the URDF mesh, an existing UE asset, or nothing at all
(``placeholder``, for camera/IMU mount points).  Until now only the generator
side existed (``pipeline.urdf_manifest.make_override_template`` wrote the keys),
so the documented override never took effect and the URDF mesh always won; this
module is the missing consumer, kept engine-free so it can be unit tested.

Schema per link (all keys optional except that ``ue_asset`` is required when the
source is ``ue_asset``)::

    {
      "visual_source": "urdf" | "ue_asset" | "placeholder",
      "ue_asset": "/Game/.../SM_Thing"            # one asset
                | [{"asset": "/Game/...", "xyz_m": [...], "rpy_rad": [...]}, ...],
      "local_transform": {"xyz_m": [...], "rpy_rad": [...]},
      "runtime_bindings": ["camera_mount"]        # not used by the importer
    }

An entry that omits its own transform inherits ``local_transform``; if that is
absent too, the link's first URDF visual origin is used, so "just swap the mesh"
needs no numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SOURCE_URDF = "urdf"
SOURCE_UE_ASSET = "ue_asset"
SOURCE_PLACEHOLDER = "placeholder"
SOURCES = (SOURCE_URDF, SOURCE_UE_ASSET, SOURCE_PLACEHOLDER)


@dataclass(frozen=True)
class AssetPlacement:
    """One UE asset to spawn in a link's frame."""

    asset: str
    xyz_m: tuple[float, float, float]
    rpy_rad: tuple[float, float, float]
    label: str | None = None


def _vec3(value: Any, *, name: str) -> tuple[float, float, float]:
    if value is None:
        return (0.0, 0.0, 0.0)
    try:
        numbers = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be three numbers, got {value!r}") from exc
    if len(numbers) != 3:
        raise ValueError(f"{name} must be three numbers, got {value!r}")
    return numbers


def visual_source(entry: Mapping[str, Any] | None) -> str:
    """Return a validated ``visual_source`` for one link override."""

    if not entry:
        return SOURCE_URDF
    source = str(entry.get("visual_source") or SOURCE_URDF)
    if source not in SOURCES:
        raise ValueError(
            f"unknown visual_source {source!r}; expected one of {', '.join(SOURCES)}")
    return source


def asset_placements(
    entry: Mapping[str, Any] | None,
    fallback_transform: Mapping[str, Any] | None = None,
) -> list[AssetPlacement]:
    """Resolve ``ue_asset`` entries to placements in the link's URDF frame.

    ``fallback_transform`` (usually the link's first URDF visual origin) supplies
    the transform for entries that do not carry their own.
    """

    if not entry:
        return []
    raw = entry.get("ue_asset")
    if raw is None:
        raise ValueError("visual_source 'ue_asset' needs a 'ue_asset' path")
    if isinstance(raw, str):
        items: Sequence[Any] = [{"asset": raw}]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        raise ValueError(f"'ue_asset' must be a path or a list, got {type(raw).__name__}")

    base = entry.get("local_transform") or fallback_transform or {}
    base_xyz = _vec3(base.get("xyz_m"), name="local_transform.xyz_m")
    base_rpy = _vec3(base.get("rpy_rad"), name="local_transform.rpy_rad")

    placements: list[AssetPlacement] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            item = {"asset": item}
        if not isinstance(item, Mapping):
            raise ValueError(f"'ue_asset[{index}]' must be a path or an object")
        asset = item.get("asset") or item.get("ue_asset")
        if not asset:
            raise ValueError(f"'ue_asset[{index}]' has no 'asset' path")
        placements.append(AssetPlacement(
            asset=str(asset),
            xyz_m=_vec3(item.get("xyz_m", base_xyz), name=f"ue_asset[{index}].xyz_m"),
            rpy_rad=_vec3(item.get("rpy_rad", base_rpy), name=f"ue_asset[{index}].rpy_rad"),
            label=str(item["label"]) if item.get("label") else None,
        ))
    return placements


def first_visual_origin(link: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The link's first URDF visual origin as an override-shaped transform."""

    visuals = (link or {}).get("visuals") or []
    if not visuals:
        return None
    origin = visuals[0].get("origin") or {}
    return {"xyz_m": origin.get("xyz"), "rpy_rad": origin.get("rpy")}


def load_overrides(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Read the per-link override map; a missing file means "no overrides"."""

    if path is None:
        return {}
    path = Path(path)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    overrides = payload.get("links") or {}
    if not isinstance(overrides, Mapping):
        raise ValueError(f"{path}: 'links' must be an object")
    return {str(name): dict(entry or {}) for name, entry in overrides.items()}


def describe_sources(overrides: Mapping[str, Mapping[str, Any]],
                     names: Iterable[str]) -> dict[str, str]:
    """``{link: visual_source}`` for reporting, e.g. in the import report."""

    return {str(name): visual_source(overrides.get(str(name))) for name in names}
