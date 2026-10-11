import tempfile
import unittest
from pathlib import Path

from pipeline.asset_catalog import AssetCatalog


class AssetCatalogTests(unittest.TestCase):
    def test_catalog_is_deterministic_and_checks_required_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "RMUC2026").mkdir()
            (root / "Materials").mkdir()
            (root / "README.txt").write_text("ignored", encoding="utf-8")
            catalog = AssetCatalog.from_root(root)
            self.assertEqual(catalog.packages(), ("Materials", "RMUC2026"))
            catalog.require_packages("RMUC2026")
            with self.assertRaises(FileNotFoundError):
                catalog.require_packages("Missing")

    def test_catalog_rejects_missing_root(self):
        with self.assertRaises(FileNotFoundError):
            AssetCatalog.from_root("/tmp/does-not-exist-rm-sim-spear")


if __name__ == "__main__":
    unittest.main()
