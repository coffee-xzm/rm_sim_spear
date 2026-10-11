import json
import tempfile
import unittest
from pathlib import Path

from pipeline.visual_override import (
    SOURCE_PLACEHOLDER,
    SOURCE_UE_ASSET,
    SOURCE_URDF,
    asset_placements,
    describe_sources,
    first_visual_origin,
    load_overrides,
    visual_source,
)


class VisualSourceTests(unittest.TestCase):
    def test_missing_entry_defaults_to_the_urdf_mesh(self):
        self.assertEqual(visual_source(None), SOURCE_URDF)
        self.assertEqual(visual_source({}), SOURCE_URDF)

    def test_documented_sources_are_accepted(self):
        self.assertEqual(visual_source({"visual_source": SOURCE_UE_ASSET}), SOURCE_UE_ASSET)
        self.assertEqual(visual_source({"visual_source": "placeholder"}), SOURCE_PLACEHOLDER)

    def test_unknown_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown visual_source"):
            visual_source({"visual_source": "spawn_something"})


class AssetPlacementTests(unittest.TestCase):
    def test_single_path_uses_the_link_visual_origin_as_fallback(self):
        link = {"visuals": [{"origin": {"xyz": [0.1, 0.2, 0.3], "rpy": [0.0, 0.0, 1.5708]}}]}
        placements = asset_placements(
            {"ue_asset": "/Game/X.SM_X"}, first_visual_origin(link))
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].asset, "/Game/X.SM_X")
        self.assertEqual(placements[0].xyz_m, (0.1, 0.2, 0.3))
        self.assertAlmostEqual(placements[0].rpy_rad[2], 1.5708)

    def test_local_transform_wins_over_the_fallback(self):
        link = {"visuals": [{"origin": {"xyz": [9.0, 9.0, 9.0], "rpy": [0.0, 0.0, 0.0]}}]}
        placements = asset_placements(
            {"ue_asset": "/Game/X.SM_X",
             "local_transform": {"xyz_m": [1.0, 2.0, 3.0], "rpy_rad": [0.5, 0.0, 0.0]}},
            first_visual_origin(link))
        self.assertEqual(placements[0].xyz_m, (1.0, 2.0, 3.0))
        self.assertEqual(placements[0].rpy_rad, (0.5, 0.0, 0.0))

    def test_list_form_places_several_assets_with_per_entry_transforms(self):
        placements = asset_placements({
            "ue_asset": [
                {"asset": "/Game/A.SM_A", "xyz_m": [0.0, 0.0, 0.0]},
                {"asset": "/Game/B.SM_B", "xyz_m": [0.0, 0.1, 0.0], "label": "bar"},
            ],
            "local_transform": {"xyz_m": [1.0, 1.0, 1.0], "rpy_rad": [0.0, 0.0, 0.0]},
        })
        self.assertEqual([p.asset for p in placements], ["/Game/A.SM_A", "/Game/B.SM_B"])
        # The first entry keeps its own zero offset, not the shared fallback.
        self.assertEqual(placements[0].xyz_m, (0.0, 0.0, 0.0))
        self.assertEqual(placements[1].xyz_m, (0.0, 0.1, 0.0))
        self.assertEqual(placements[1].label, "bar")

    def test_entries_without_a_transform_share_local_transform(self):
        placements = asset_placements({
            "ue_asset": ["/Game/A.SM_A", "/Game/B.SM_B"],
            "local_transform": {"xyz_m": [0.0, 0.0, -1.4], "rpy_rad": [0.0, 0.0, 0.0]},
        })
        self.assertEqual([p.xyz_m for p in placements],
                         [(0.0, 0.0, -1.4), (0.0, 0.0, -1.4)])

    def test_missing_asset_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "needs a 'ue_asset' path"):
            asset_placements({"visual_source": SOURCE_UE_ASSET})

    def test_bad_transform_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "three numbers"):
            asset_placements({"ue_asset": "/Game/A.SM_A",
                              "local_transform": {"xyz_m": [1.0, 2.0]}})


class LoadOverridesTests(unittest.TestCase):
    def test_missing_file_means_no_overrides(self):
        self.assertEqual(load_overrides(Path("/tmp/does-not-exist-rmsim.json")), {})
        self.assertEqual(load_overrides(None), {})

    def test_file_round_trips_and_reports_sources(self):
        payload = {"links": {
            "armor_link_1": {"visual_source": "ue_asset", "ue_asset": "/Game/A.SM_A"},
            "link_5": {"visual_source": "placeholder"},
        }}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ue_overrides.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            overrides = load_overrides(path)
        self.assertEqual(set(overrides), {"armor_link_1", "link_5"})
        self.assertEqual(describe_sources(overrides, ["armor_link_1", "link_5", "base_link"]),
                         {"armor_link_1": "ue_asset", "link_5": "placeholder",
                          "base_link": "urdf"})


if __name__ == "__main__":
    unittest.main()
