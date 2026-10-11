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
        self.assertEqual(plan["robot_visual"]["link"], "base_link")
        self.assertEqual(plan["robot_visual"]["actor_path"], "Scene/body")
        self.assertEqual(plan["warnings"], [])

    def test_plan_links_describe_parent_joint_and_ue_origin(self):
        # A hand-written manifest keeps the expectation exact: the list must
        # follow manifest link order (not joint order) and convert metres to
        # Unreal's left-handed centimetres.
        manifest = {
            "robot": "mini",
            "source_urdf": "mini.urdf",
            "links": [{"name": "root"}, {"name": "leaf"}, {"name": "mid"}],
            "joints": [
                {
                    "name": "root_to_mid",
                    "type": "fixed",
                    "parent": "root",
                    "child": "mid",
                    "origin": {"xyz": [0.25, -0.5, 0.75], "rpy": [0.5, -0.25, 0.0]},
                },
                {
                    "name": "mid_to_leaf",
                    "type": "revolute",
                    "parent": "mid",
                    "child": "leaf",
                    "origin": {"xyz": [1.0, 2.0, 3.0], "rpy": [0.0, 0.0, 1.5]},
                },
            ],
        }
        plan = expand_plan(manifest, {"scene_bindings": {"robot": {
            "link": "root",
            "mode": "link_hierarchy",
            "link_actor_tag": "MuJoCoLink={link}",
        }}})

        self.assertEqual([entry["name"] for entry in plan["links"]], ["root", "leaf", "mid"])
        self.assertEqual(plan["links"][0], {
            "name": "root",
            "parent": None,
            "joint": None,
            "origin_ue_cm": [0.0, 0.0, 0.0],
            "origin_rpy_rad": [0.0, 0.0, 0.0],
        })
        self.assertEqual(plan["links"][1], {
            "name": "leaf",
            "parent": "mid",
            "joint": "mid_to_leaf",
            "origin_ue_cm": [100.0, -200.0, 300.0],
            "origin_rpy_rad": [0.0, 0.0, 1.5],
        })
        self.assertEqual(plan["links"][2]["parent"], "root")
        self.assertEqual(plan["links"][2]["joint"], "root_to_mid")
        self.assertEqual(plan["links"][2]["origin_ue_cm"], [25.0, 50.0, 75.0])
        self.assertEqual(plan["links"][2]["origin_rpy_rad"], [0.5, -0.25, 0.0])
        # The robot visual carries the configured importer tag template (the
        # binder formats it per link) and the optional visual-source mode.
        self.assertEqual(plan["robot_visual"]["link_actor_tag"], "MuJoCoLink={link}")
        self.assertEqual(plan["robot_visual"]["mode"], "link_hierarchy")

    def test_real_manifest_links_keep_manifest_order_and_root_first(self):
        urdf = ROOT / "assets/总装_urdf/总装.urdf"
        manifest = parse_urdf(urdf)
        plan = expand_plan(manifest, make_override_template(manifest))

        self.assertEqual(
            [entry["name"] for entry in plan["links"]],
            [link["name"] for link in manifest["links"]],
        )
        self.assertEqual(plan["links"][0]["name"], manifest["root_link"])
        self.assertIsNone(plan["links"][0]["parent"])
        by_name = {entry["name"]: entry for entry in plan["links"]}
        self.assertEqual(by_name["link_6"]["parent"], "link_5")
        self.assertEqual(by_name["link_6"]["joint"], "link_6_joint")
        # An override file without an explicit tag keeps the importer default.
        self.assertEqual(plan["robot_visual"]["link_actor_tag"], "MuJoCoBody={link}")
        self.assertNotIn("mode", plan["robot_visual"])

    def test_second_model_reports_missing_camera_mount(self):
        urdf = ROOT / "assets/新烧饼底盘2装电器设备_urdf/新烧饼底盘2装电器设备.urdf"
        manifest = parse_urdf(urdf)
        plan = expand_plan(manifest, make_override_template(manifest))
        self.assertEqual(len(plan["armor"]), 4)
        self.assertIsNone(plan["camera"])
        self.assertTrue(any("no links matched camera" in item for item in plan["warnings"]))

    def test_scene_actor_path_and_camera_mode_are_preserved(self):
        urdf = ROOT / "assets/总装_urdf/总装.urdf"
        manifest = parse_urdf(urdf)
        config = make_override_template(manifest)
        config["scene_bindings"]["armor"]["link_regex"] = "^armor_link_1$"
        config["scene_bindings"]["armor"]["actor_path"] = "Scene/armor"
        config["scene_bindings"]["camera"]["mode"] = "cinecamera"
        config["scene_bindings"]["camera"]["actor_path"] = "CineCameraActor"
        plan = expand_plan(manifest, config)
        self.assertEqual(plan["armor"], [{
            "link": "armor_link_1",
            "actor_name": "armor_1",
            "actor_path": "Scene/armor",
            "material_slot": 0,
            "material_asset": None,
        }])
        self.assertEqual(plan["camera"]["mode"], "cinecamera")
        self.assertEqual(plan["camera"]["actor_path"], "CineCameraActor")
        self.assertEqual(plan["robot_visual"]["actor_name"], "body")

    def test_aggregate_armor_template_clones_once_per_link(self):
        urdf = ROOT / "assets/总装_urdf/总装.urdf"
        manifest = parse_urdf(urdf)
        config = make_override_template(manifest)
        config["scene_bindings"]["armor"].update({
            "actor_path": "Scene/armor",
            "clone_per_link": True,
            "clone_name_template": "RMSim/armor_{index}",
        })
        plan = expand_plan(manifest, config)
        self.assertEqual(len(plan["armor"]), 4)
        self.assertEqual({entry["actor_path"] for entry in plan["armor"]}, {"Scene/armor"})
        self.assertEqual(
            [entry["clone_name"] for entry in plan["armor"]],
            ["RMSim/armor_1", "RMSim/armor_2", "RMSim/armor_3", "RMSim/armor_4"],
        )
        self.assertTrue(all(entry["hide_template"] for entry in plan["armor"]))

    def test_armor_section_can_be_switched_off(self):
        """A robot with ``ue_asset`` armor needs no scene-template clone.

        The imported visual already sits under the link anchor, so binding a
        scene template as well would duplicate it and drag the template's own
        group along.  A deliberate switch-off must stay silent: the binder
        turns any plan warning into a fatal error.
        """
        manifest = parse_urdf(ROOT / "assets/总装_urdf/总装.urdf")
        config = make_override_template(manifest)
        config["scene_bindings"]["armor"]["enabled"] = False
        plan = expand_plan(manifest, config)

        self.assertEqual(plan["armor"], [])
        self.assertEqual(plan["warnings"], [])
        # The armor links keep their own anchors, so the pose loop still drives
        # them; only the scene-template binding disappears.
        self.assertIn("armor_link_1", [entry["name"] for entry in plan["links"]])

    def test_null_armor_section_is_disabled(self):
        manifest = parse_urdf(ROOT / "assets/总装_urdf/总装.urdf")
        config = make_override_template(manifest)
        config["scene_bindings"]["armor"] = None
        plan = expand_plan(manifest, config)

        self.assertEqual(plan["armor"], [])
        self.assertEqual(plan["warnings"], [])

    def test_missing_armor_section_keeps_legacy_default(self):
        manifest = parse_urdf(ROOT / "assets/总装_urdf/总装.urdf")
        config = make_override_template(manifest)
        del config["scene_bindings"]["armor"]
        plan = expand_plan(manifest, config)

        self.assertEqual(
            [entry["link"] for entry in plan["armor"]],
            ["armor_link_1", "armor_link_2", "armor_link_3", "armor_link_4"],
        )

    def test_shipped_override_binds_no_template_armor(self):
        """Guard the shipped data file: armor visuals come from overrides."""
        manifest = json.loads(
            (ROOT / "generated/urdf/总装/manifest.json").read_text(encoding="utf-8"))
        config = json.loads(
            (ROOT / "generated/urdf/总装/ue_overrides.json").read_text(encoding="utf-8"))
        plan = expand_plan(manifest, config)

        self.assertEqual(plan["armor"], [])
        self.assertEqual(plan["warnings"], [])
        link_names = [entry["name"] for entry in plan["links"]]
        for link in ("armor_link_1", "armor_link_2", "armor_link_3", "armor_link_4"):
            self.assertIn(link, link_names)
        # Every armor link's visual must come from a UE asset, not from the URDF
        # STL: that is what makes the scene-template binding redundant.
        for link in ("armor_link_1", "armor_link_2", "armor_link_3", "armor_link_4"):
            self.assertEqual(config["links"][link]["visual_source"], "ue_asset")
            self.assertTrue(config["links"][link]["ue_asset"])
