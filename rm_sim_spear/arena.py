"""Where the arena lives and how a run is placed on it.

There is one arena: the MJCF world exported from the Unreal level by the Blender
``blender_mujoco_terrain`` addon.  This project used to build its own
(``pipeline/arena_scene.py`` -> ``arena.json``) and the two disagreed about the
sign of y, which is why the built one is gone -- see
``docs/rmuc26_asset_review.md``.

What is left for both run tools to agree on is small: which file the export is,
and that ``--world-offset-cm`` is *required* alongside it.  ``arena.json`` was
already in level coordinates and had to refuse a non-zero offset; the addon
export is the opposite -- it carries its own origin, so the offset is the only
thing that puts the MuJoCo world and the Unreal level in the same place.
"""

from __future__ import annotations

from pathlib import Path

#: What ``--world-offset-cm`` defaults to when there is no arena at all.
DEFAULT_WORLD_OFFSET_CM = (0.0, 0.0, -140.0)


def resolve_world_offset_cm(requested,
                            arena_scene: Path | None = None
                            ) -> tuple[float, float, float]:
    """Pick the MuJoCo-origin offset.

    With no arena the value is the invented flat plane's height.  With an addon
    export there is no sensible default to invent: measured on ``assets/rmuc26``
    the export's origin sits at level + ``(16.505, 1.6243, 0)`` m, so an
    unasked-for ``(0, 0, 0)`` puts the robot 16.5 m from where Unreal draws it.
    The caller is expected to have got the real value from
    ``tools/check_arena_scene.py`` and passed it in.
    """
    if arena_scene is None:
        if requested is None:
            return DEFAULT_WORLD_OFFSET_CM
        return tuple(float(value) for value in requested)
    return tuple(float(value) for value in (requested or (0.0, 0.0, 0.0)))


def resolve_arena_scene(explicit: Path | None) -> Path | None:
    """The addon-exported MJCF world this run should use, if any.

    Never inferred -- there is no default path for somebody else's export -- so
    it is opt-in by path.
    """
    if explicit is None:
        return None
    if not explicit.is_file():
        raise SystemExit(f"--arena-scene is not a file: {explicit}")
    if explicit.suffix.lower() not in (".xml",):
        raise SystemExit(f"--arena-scene wants an MJCF file (.xml): {explicit}")
    return explicit
