import unittest

from rm_sim_spear.capture_resolution import (
    DEFAULT_SUPERSAMPLE,
    NO_PIXEL_BUDGET,
    InternalResolution,
    choose_internal_resolution,
)


class ChooseInternalResolutionTests(unittest.TestCase):
    def test_default_is_one_to_one_with_the_output(self) -> None:
        chosen = choose_internal_resolution(1440, 1080)
        self.assertEqual((chosen.width, chosen.height), (1440, 1080))
        self.assertEqual(chosen.scale, DEFAULT_SUPERSAMPLE)
        self.assertFalse(chosen.budget_limited)
        self.assertFalse(chosen.below_output)
        self.assertEqual(chosen.pixel_budget, NO_PIXEL_BUDGET)

    def test_supersample_without_budget_grows_the_target(self) -> None:
        chosen = choose_internal_resolution(1440, 1080, supersample=2.0)
        self.assertEqual((chosen.width, chosen.height), (2880, 2160))
        self.assertEqual(chosen.scale, 2.0)
        self.assertFalse(chosen.budget_limited)

    def test_default_512_output_is_untouched(self) -> None:
        chosen = choose_internal_resolution(512, 512)
        self.assertEqual((chosen.width, chosen.height), (512, 512))
        self.assertEqual(chosen.scale, 1.0)

    def test_budget_that_cannot_afford_one_sample_per_pixel_is_ignored(self) -> None:
        # Regression: this exact call rendered 632x474 and was then enlarged to
        # 1440x1080 with cv2.INTER_AREA, which broke distant thin lines.
        chosen = choose_internal_resolution(1440, 1080, supersample=2.0, max_pixels=300_000)
        self.assertEqual((chosen.width, chosen.height), (1440, 1080))
        self.assertEqual(chosen.scale, 1.0)
        self.assertFalse(chosen.below_output)
        self.assertTrue(chosen.budget_limited)

    def test_budget_equal_to_the_output_pixels_disables_supersampling(self) -> None:
        chosen = choose_internal_resolution(1440, 1080, supersample=2.0, max_pixels=1440 * 1080)
        self.assertEqual((chosen.width, chosen.height), (1440, 1080))
        self.assertEqual(chosen.scale, 1.0)
        self.assertTrue(chosen.budget_limited)

    def test_budget_above_the_output_keeps_requested_supersampling(self) -> None:
        chosen = choose_internal_resolution(1440, 1080, supersample=2.0, max_pixels=6_500_000)
        self.assertEqual((chosen.width, chosen.height), (2880, 2160))
        self.assertEqual(chosen.scale, 2.0)
        self.assertFalse(chosen.budget_limited)

    def test_budget_trims_supersampling_but_not_below_one(self) -> None:
        # 2000000 px at a 1440x1080 output is 1.13x per axis.
        chosen = choose_internal_resolution(1440, 1080, supersample=2.0, max_pixels=2_000_000)
        self.assertAlmostEqual(chosen.scale, (2_000_000 / (1440 * 1080)) ** 0.5)
        self.assertTrue(chosen.budget_limited)
        self.assertFalse(chosen.below_output)

    def test_describe_marks_a_budget_limited_target(self) -> None:
        limited = choose_internal_resolution(1440, 1080, supersample=2.0, max_pixels=1_000_000)
        self.assertIn("budget-limited from 2.00x", limited.describe())
        free = choose_internal_resolution(1440, 1080, supersample=2.0)
        self.assertNotIn("budget-limited", free.describe())

    def test_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            choose_internal_resolution(0, 1080)
        with self.assertRaises(ValueError):
            choose_internal_resolution(1440, -1)
        with self.assertRaises(ValueError):
            choose_internal_resolution(1440, 1080, supersample=0.5)
        with self.assertRaises(ValueError):
            choose_internal_resolution(1440, 1080, max_pixels=-1)

    def test_below_output_is_reachable_only_by_construction(self) -> None:
        crafted = InternalResolution(
            width=632, height=474, scale=0.44, supersample=2.0, pixel_budget=300_000)
        self.assertTrue(crafted.below_output)
        self.assertTrue(crafted.budget_limited)


if __name__ == "__main__":
    unittest.main()
