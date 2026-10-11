import json
import tempfile
import unittest
from pathlib import Path

from pipeline.scene_binding_plan import expand_plan
from pipeline.urdf_manifest import make_override_template, parse_urdf


ROOT = Path(__file__).resolve().parents[1]


class SceneBindingPlanTests(unittest.TestCase):
    def test_total_assembly_binds_armors_and_camera(self):
        urdf = ROOT / "assets/总装_urdf/总装.urdf"
        manifest = parse_urdf(urdf)
        plan = expand_plan(manifest, make_override_template(manifest))
        self.assertEqual([item["link"] for item in plan["armor"]], ["armor_link_1", "armor_link_2", "armor_link_3", "armor_link_4"])
        self.assertEqual(plan["camera"]["link"], "cam_link_8")
        self.assertEqual(plan["imu"]["parent"], "cam_link_8")
        self.assertEqual(plan["warnings"], [])

    def test_second_model_reports_missing_camera_mount(self):
        urdf = ROOT / "assets/新烧饼底盘2装电器设备_urdf/新烧饼底盘2装电器设备.urdf"
        manifest = parse_urdf(urdf)
        plan = expand_plan(manifest, make_override_template(manifest))
        self.assertEqual(len(plan["armor"]), 4)
        self.assertIsNone(plan["camera"])
        self.assertTrue(any("no links matched camera" in item for item in plan["warnings"]))
