"""Launch-target helpers for the UE side of the simulation.

Kept separate from ``tools/run_physics_sim.py`` because that module imports
``spear`` (and therefore only runs inside the conda environment), while these
rules are pure and belong under the offline test suite.
"""

from __future__ import annotations

from pathlib import Path


def resolve_game_executable(path) -> Path:
    """Validate ``--game-executable``: the ``.sh`` archived for a packaged build.

    SPEAR launches a shipped build when ``SPEAR.LAUNCH_MODE`` is ``"game"`` and
    ``SPEAR.INSTANCE.GAME_EXECUTABLE`` points at that script
    (``~/spear/python/spear/instance.py:656-760``).  That is the shape
    ``~/rm_sim_rune`` ships, and what ``tools/package_linux.sh`` produces for
    this project; see ``docs/rendering.md`` section 12 for the measured
    difference between a cooked standalone and ``UnrealEditor -game``.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(f"--game-executable is not a file: {resolved}")
    return resolved
