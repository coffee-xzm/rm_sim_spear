"""Offline tests for the physical camera (lens) model.

The point of these tests is that the numbers we hand to Unreal are derived from
a real lens instead of copied from the debug viewport: FOV from focal length and
sensor width, depth of field from f-stop and focus distance.  A silent error in
any of them distorts every captured frame, so the arithmetic is pinned here.
"""

import math
import unittest

from rm_sim_spear.lens import (
    ROBOT_CAMERA_LENS,
    SENSOR_FORMATS_MM,
    Lens,
    horizontal_fov_deg,
    implied_focal_length_mm,
    rescaled_fov_deg,
)

# An arbitrary bench lens for the math tests (8 mm on a 1/2.3" sensor); the
# hardware the project actually captures with is ROBOT_CAMERA_LENS (1/1.8").
BENCH_LENS = Lens(
    focal_length_mm=8.0,
    sensor_width_mm=6.17,
    sensor_height_mm=4.55,
    fstop=2.0,
    focus_distance_cm=150.0,
    name="bench 8mm",
)


class FovMathTest(unittest.TestCase):
    def test_horizontal_fov_matches_the_thin_lens_formula(self) -> None:
        self.assertAlmostEqual(horizontal_fov_deg(8.0, 6.17), 42.18, delta=0.02)
        self.assertAlmostEqual(horizontal_fov_deg(12.0, 6.17), 28.84, delta=0.02)
        self.assertAlmostEqual(BENCH_LENS.horizontal_fov_deg, 42.18, delta=0.02)

    def test_implied_focal_length_inverts_the_fov(self) -> None:
        for focal_mm, sensor_mm in ((8.0, 6.17), (4.0, 7.18), (12.0, 8.8)):
            fov = horizontal_fov_deg(focal_mm, sensor_mm)
            self.assertAlmostEqual(implied_focal_length_mm(fov, sensor_mm), focal_mm, places=6)

    def test_vertical_fov_follows_the_sensor_height(self) -> None:
        self.assertAlmostEqual(BENCH_LENS.vertical_fov_deg, 2.0*math.degrees(
            math.atan(4.55/(2.0*8.0))), places=9)

    def test_rescaled_fov_keeps_vertical_coverage(self) -> None:
        # A 90 degree 16:9 viewport becomes 73.7 degrees on a 4:3 render target,
        # and both show the same vertical extent.
        rescaled = rescaled_fov_deg(90.0, 16.0/9.0, 4.0/3.0)
        self.assertAlmostEqual(rescaled, 73.74, places=2)
        source_vertical = 2.0*math.degrees(math.atan(math.tan(math.radians(90.0)/2.0)/(16.0/9.0)))
        target_vertical = 2.0*math.degrees(math.atan(math.tan(math.radians(rescaled)/2.0)/(4.0/3.0)))
        self.assertAlmostEqual(source_vertical, target_vertical, places=9)


class LensProjectionTest(unittest.TestCase):
    def test_fov_angle_uses_the_full_sensor_dimension_and_crops_the_other(self) -> None:
        sensor_aspect = BENCH_LENS.sensor_aspect
        # Same aspect as the sensor: the full horizontal field of view.
        self.assertAlmostEqual(BENCH_LENS.fov_angle_deg(sensor_aspect), BENCH_LENS.horizontal_fov_deg,
                               places=9)
        # Wider than the sensor: still full width, the extra pixels are vertical.
        self.assertAlmostEqual(BENCH_LENS.fov_angle_deg(16.0/9.0), BENCH_LENS.horizontal_fov_deg,
                               places=9)
        # Narrower: horizontal is cropped, the vertical field of view survives.
        square = BENCH_LENS.fov_angle_deg(1.0)
        self.assertLess(square, BENCH_LENS.horizontal_fov_deg)
        vertical_kept = 2.0*math.degrees(math.atan(math.tan(math.radians(square)/2.0)/1.0))
        self.assertAlmostEqual(vertical_kept, BENCH_LENS.vertical_fov_deg, places=9)

    def test_center_pixel_is_wider_than_the_average_pixel(self) -> None:
        # A rectilinear projection magnifies the periphery, so the pixel on the
        # axis covers the most angle and the edge the least.
        average = BENCH_LENS.average_pixel_angle_deg(1440, 4.0/3.0)
        center = BENCH_LENS.center_pixel_angle_deg(1440, 4.0/3.0)
        self.assertGreater(center, average)
        self.assertLess(center/average, 1.1)

    def test_the_lens_samples_denser_than_the_90_degree_debug_camera(self) -> None:
        aspect = 4.0/3.0
        # The viewport default UE gives us: a 90 degree horizontal FOV.
        gain = BENCH_LENS.angular_gain(90.0, aspect)
        # ~2.6x per axis, ~6.7x per unit solid angle; 2x supersampling - which
        # costs +852 MiB and +36.5 ms/frame - buys 2x per axis.
        self.assertGreater(gain, 2.4)
        self.assertLess(gain, 2.7)
        # If the render target aspect differs from the window's, SPEAR rescales
        # the viewport FOV first; that is the other honest reference.
        rescaled = rescaled_fov_deg(90.0, 16.0/9.0, aspect)
        self.assertAlmostEqual(BENCH_LENS.angular_gain(rescaled, aspect), 1.95, delta=0.05)

    def test_every_named_sensor_format_stays_in_the_robot_camera_band(self) -> None:
        # An 8 mm M12 lens on these sensors is a 37-48 degree lens.  A typo in
        # the sensor table (say 61.7 mm) would silently become a 150 degree
        # fisheye, so pin the plausible band.
        for name, (width_mm, height_mm) in SENSOR_FORMATS_MM.items():
            lens = Lens(focal_length_mm=8.0, sensor_width_mm=width_mm, sensor_height_mm=height_mm)
            with self.subTest(sensor=name):
                self.assertGreater(lens.horizontal_fov_deg, 35.0)
                self.assertLess(lens.horizontal_fov_deg, 60.0)
                self.assertAlmostEqual(lens.sensor_aspect, width_mm/height_mm, places=9)


class DepthOfFieldTest(unittest.TestCase):
    def test_circle_of_confusion_scales_with_the_sensor(self) -> None:
        # 2 px at 1440 px wide on a 6.17 mm sensor is 8.6 um on the sensor.
        self.assertAlmostEqual(BENCH_LENS.circle_of_confusion_mm(2.0, 1440), 0.0085694, places=7)

    def test_hyperfocal_distance_matches_the_formula(self) -> None:
        focal_m = 0.008
        coc_m = BENCH_LENS.circle_of_confusion_mm(2.0, 1440)/1000.0
        expected = (focal_m*focal_m)/(2.0*coc_m) + focal_m
        self.assertAlmostEqual(BENCH_LENS.hyperfocal_m(2.0, 1440), expected, places=9)
        self.assertAlmostEqual(BENCH_LENS.hyperfocal_m(2.0, 1440), 3.74, places=2)

    def test_focusing_at_one_and_a_half_metres_gives_a_one_metre_deep_band(self) -> None:
        near, far = BENCH_LENS.depth_of_field_m(2.0, 1440)
        self.assertAlmostEqual(near, 1.07, places=2)
        self.assertAlmostEqual(far, 2.49, places=2)

    def test_a_wider_aperture_shortens_the_band(self) -> None:
        wide = Lens(focal_length_mm=8.0, sensor_width_mm=6.17, sensor_height_mm=4.55, fstop=1.8)
        narrow = Lens(focal_length_mm=8.0, sensor_width_mm=6.17, sensor_height_mm=4.55, fstop=2.8)
        wide_near, wide_far = wide.depth_of_field_m(2.0, 1440)
        narrow_near, narrow_far = narrow.depth_of_field_m(2.0, 1440)
        self.assertGreater(wide_near, narrow_near)
        self.assertLess(wide_far, narrow_far)

    def test_focusing_past_hyperfocal_reaches_infinity(self) -> None:
        lens = Lens(focal_length_mm=8.0, sensor_width_mm=6.17, sensor_height_mm=4.55,
                    focus_distance_cm=500.0)
        near, far = lens.depth_of_field_m(2.0, 1440)
        hyperfocal = lens.hyperfocal_m(2.0, 1440)
        self.assertEqual(far, math.inf)
        # "From half the hyperfocal to infinity" is the thin-lens approximation;
        # at 8 mm focal length the exact near limit sits a little further out.
        self.assertGreater(near, hyperfocal/2.0)
        self.assertLess(near, hyperfocal)

    def test_ue_overrides_carry_the_real_sensor_and_focus(self) -> None:
        overrides = BENCH_LENS.ue_post_process_overrides()
        self.assertEqual(overrides["DepthOfFieldSensorWidth"], 6.17)
        self.assertEqual(overrides["DepthOfFieldFocalDistance"], 150.0)
        self.assertEqual(overrides["DepthOfFieldFstop"], 2.0)
        # DepthOfFieldFocalDistance defaults to 0, which the renderer reads as
        # "depth of field off" (Scene.cpp:606), and the effect is additionally
        # gated on DepthOfFieldEnabled (DiaphragmDOFUtils.cpp:53): without these
        # two the whole lens would render perfectly sharp and say nothing.
        self.assertTrue(overrides["bOverride_DepthOfFieldEnabled"])
        self.assertTrue(overrides["DepthOfFieldEnabled"])
        # DepthOfFieldMethod is deprecated in 5.8 and read by nothing in the
        # renderer, so asking for it would be a no-op dressed as a setting.
        self.assertNotIn("DepthOfFieldMethod", overrides)
        # UE derives the focal length from the sensor width and the view FOV;
        # with our FOV that must come back as 8 mm, otherwise the blur is wrong.
        fov = BENCH_LENS.fov_angle_deg(BENCH_LENS.sensor_aspect)
        implied = implied_focal_length_mm(fov, float(overrides["DepthOfFieldSensorWidth"]))
        self.assertAlmostEqual(implied, 8.0, places=9)

    def test_every_depth_of_field_value_has_its_override_flag(self) -> None:
        # UE ignores a PostProcessSettings value whose bOverride_* bit is clear,
        # so a missing flag does not produce a wrong blur, it turns the depth of
        # field off - the failure would be silent in every captured frame.
        overrides = BENCH_LENS.ue_post_process_overrides()
        values = [name for name in overrides if not name.startswith("bOverride_")]
        self.assertGreaterEqual(len(values), 6)
        for name in values:
            with self.subTest(field=name):
                self.assertTrue(overrides.get(f"bOverride_{name}"), name)


class RobotCameraLensTest(unittest.TestCase):
    """The project lens: 8 mm on the 1/1.8" sensor of the camera on cam_link_8."""

    def test_project_lens_is_the_hardware_that_was_specified(self) -> None:
        self.assertEqual(ROBOT_CAMERA_LENS.focal_length_mm, 8.0)
        self.assertEqual(ROBOT_CAMERA_LENS.sensor_width_mm, 7.18)
        self.almost(ROBOT_CAMERA_LENS.horizontal_fov_deg, 48.34)
        self.almost(ROBOT_CAMERA_LENS.vertical_fov_deg, 36.78)

    def test_project_lens_more_than_doubles_the_angular_density_of_the_default(self) -> None:
        # The capture path inherits UE's 90 degree viewport default; the robot
        # lens is what the perception stack actually sees (rendering.md 13.2).
        self.almost(ROBOT_CAMERA_LENS.angular_gain(90.0, 4.0/3.0), 2.26, places=2)

    def test_project_lens_crops_half_a_degree_on_a_4_by_3_capture(self) -> None:
        # sensor aspect 1.35 vs capture 1.33: the vertical field of view is kept
        # and the horizontal is cropped from 48.34 to 47.82 degrees.
        self.almost(ROBOT_CAMERA_LENS.fov_angle_deg(4.0/3.0), 47.82)

    def test_project_lens_keeps_the_working_range_in_focus(self) -> None:
        # The real lens: f/2.4 focused at an assumed 2 m.  The 1-3 m working
        # range has to stay inside the sharp band, otherwise every dataset frame
        # is soft for a reason nobody wrote down.
        near, far = ROBOT_CAMERA_LENS.depth_of_field_m(2.0, 1440)
        self.almost(near, 1.15)
        self.almost(far, 7.77)
        self.assertLess(near, 1.5)
        self.assertGreater(far, 3.0)

    def almost(self, value: float, expected: float, places: int = 2) -> None:
        self.assertAlmostEqual(value, expected, places=places)


class ValidationTest(unittest.TestCase):
    def test_rejects_non_positive_parameters(self) -> None:
        for field_name, value in (
            ("focal_length_mm", 0.0),
            ("sensor_width_mm", -1.0),
            ("sensor_height_mm", 0.0),
            ("fstop", 0.0),
            ("focus_distance_cm", -5.0),
        ):
            kwargs = {
                "focal_length_mm": 8.0,
                "sensor_width_mm": 6.17,
                "sensor_height_mm": 4.55,
                "fstop": 2.0,
                "focus_distance_cm": 150.0,
            }
            kwargs[field_name] = value
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    Lens(**kwargs)

    def test_describe_names_the_lens(self) -> None:
        self.assertIn("8mm", BENCH_LENS.describe())
        self.assertIn("42.2", BENCH_LENS.describe())


if __name__ == "__main__":
    unittest.main()
