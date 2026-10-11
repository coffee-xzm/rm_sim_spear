from dataclasses import dataclass
from pathlib import Path


def default_asset_root() -> Path:
    """Return local UE Content when available, with the rune tree as fallback."""
    repository_root = Path(__file__).resolve().parents[1]
    local_root = repository_root / "ue" / "Content"
    if local_root.is_dir():
        return local_root
    return Path.home() / "rm_sim_rune" / "SimUnreal" / "Content"


@dataclass(frozen=True)
class AssetCatalog:
    """Read-only view of this project's Unreal Content tree."""

    root: Path

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> "AssetCatalog":
        path = Path(root).expanduser() if root is not None else default_asset_root()
        path = path.resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Unreal Content directory does not exist: {path}")
        return cls(path)

    def packages(self) -> tuple[str, ...]:
        """List top-level Unreal packages in stable order.

        The catalog intentionally does not inspect or copy binary ``.uasset``
        files. UE remains responsible for loading assets and resolving
        redirectors; this layer only provides a deterministic source boundary.
        """
        return tuple(sorted(path.name for path in self.root.iterdir() if path.is_dir()))

    def require_packages(self, *names: str) -> None:
        available = set(self.packages())
        missing = sorted(name for name in names if name not in available)
        if missing:
            raise FileNotFoundError(
                "required Unreal asset packages are missing: " + ", ".join(missing)
            )
