import json
import unittest
from pathlib import Path

from pipeline.urdf_manifest import parse_urdf, write_outputs


ROOT = Path(__file__).resolve().parents[1]


class UrdfManifestTests(unittest.TestCase):
    def test_repository_urdfs_have_stable_robot_structure(self):
        expected = {
            "总装": (13, 12, "base_link"),
            "新烧饼底盘2装电器设备": (9, 8, "base_link"),
        }
        for path in sorted((ROOT / "assets").rglob("*.urdf")):
            manifest = parse_urdf(path)
            self.assertEqual(
                (len(manifest["links"]), len(manifest["joints"]), manifest["root_link"]),
                (*expected[manifest["robot"]],),
            )
            self.assertEqual(manifest["validation"]["missing_meshes"], [])
            self.assertEqual(manifest["validation"]["unsupported_joint_types"], [])


    def test_outputs_include_override_template(self):
        import tempfile

        urdf = next((ROOT / "assets").rglob("*.urdf"))
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, override_path = write_outputs(urdf, Path(directory) / "robot")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            overrides = json.loads(override_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(set(overrides["links"]), {link["name"] for link in manifest["links"]})
        self.assertEqual(overrides["default_visual_source"], "urdf")
