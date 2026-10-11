"""Physical camera model: focal length, sensor format, aperture, focus.

The capture chain has no notion of a lens today.  SPEAR reads the *viewport*
projection (``GameViewportService.get_current_viewport_desc`` -> ``fov_degrees``
-> ``align_camera_with_viewport``) and writes the aspect-adjusted value into the
capture component's ``FOVAngle``, and nothing in this project sets a depth of
field.  In standalone ``-game`` that is UE's 90 degree default camera: a debug
wide angle lens, not the ~40-50 degree small-sensor lens the robot carries.

Field of view is what turns a fixed pixel budget into angular resolution.  At
1440 px wide, 90 degrees is 0.063 deg/px, while an 8 mm lens on a 1/2.3" sensor
(42 degrees) is 0.029 deg/px - the sampling density that 2x supersampling buys,
measured at +852 MiB and +36.5 ms/frame (docs/render_budget.md section 7.4).

Depth of field falls out of the same three numbers, which is what makes this a
*camera* rather than two unrelated knobs: UE's circle-of-DoF derives the focal
length from the view's FOV and ``DepthOfFieldSensorWidth``, so setting both from
the same lens reproduces the real blur instead of an arbitrary one.

Everything here is pure arithmetic, so it is unit tested without Unreal.  The
numbers and the acceptance protocol live in docs/rendering.md, section
"镜头模型与角采样密度".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "ROBOT_CAMERA_LENS",
    "SENSOR_FORMATS_MM",
    "Lens",
    "horizontal_fov_deg",
    "implied_focal_length_mm",
    "rescaled_fov_deg",
]


#
# Named sensor formats, (width_mm, height_mm) of the active area.  These are the
# datasheet sizes of the small CMOS sensors these robot cameras usually use;
# the exact numbers must come from the camera at hand before they are used for
# acceptance, because the sensor width is what sets both FOV and CoC.
#
SENSOR_FORMATS_MM: dict[str, tuple[float, float]] = {
    "1/2.5": (5.76, 4.29),
    "1/2.3": (6.17, 4.55),
    "1/2.8": (5.37, 4.04),
    "1/1.8": (7.18, 5.32),
    "2/3": (8.80, 6.60),
}


def horizontal_fov_deg(focal_length_mm: float, sensor_width_mm: float) -> float:
    """Horizontal field of view of a rectilinear lens, in degrees."""
    return 2.0*math.degrees(math.atan(sensor_width_mm/(2.0*focal_length_mm)))


def implied_focal_length_mm(fov_deg: float, sensor_width_mm: float) -> float:
    """Focal length a rectilinear lens needs to cover ``fov_deg``.

    This is the relation UE uses inside its depth-of-field pass (focal length
    from the view FOV and the configured sensor width), so it doubles as the
    consistency check between the FOV we set and the blur we ask for.
    """
    return sensor_width_mm/(2.0*math.tan(math.radians(fov_deg)/2.0))


def rescaled_fov_deg(fov_deg: float, from_aspect: float, to_aspect: float) -> float:
    """Rebase a horizontal FOV onto another aspect ratio, keeping the vertical FOV.

    Mirrors what the capture path already does with the viewport projection:
    ``spear/services/viewport_service.py`` and
    ``tools/test_urdf_views.py::_configure_capture_projection`` both keep the
    vertical coverage and widen or narrow horizontally, so a 90 degree 16:9
    viewport becomes 73.7 degrees on a 4:3 render target.
    """
    half_angle = math.radians(fov_deg)/2.0
    return 2.0*math.degrees(math.atan(math.tan(half_angle)*to_aspect/from_aspect))


@dataclass(frozen=True)
class Lens:
    """A real camera: focal length, sensor, aperture and focus distance."""

    focal_length_mm: float
    sensor_width_mm: float
    sensor_height_mm: float
    fstop: float = 2.0
    focus_distance_cm: float = 150.0
    name: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "focal_length_mm",
            "sensor_width_mm",
            "sensor_height_mm",
            "fstop",
            "focus_distance_cm",
        ):
            value = float(getattr(self, field_name))
            if not value > 0.0:
                raise ValueError(f"{field_name} must be positive, got {value!r}")

    @property
    def sensor_aspect(self) -> float:
        return self.sensor_width_mm/self.sensor_height_mm

    @property
    def horizontal_fov_deg(self) -> float:
        return horizontal_fov_deg(self.focal_length_mm, self.sensor_width_mm)

    @property
    def vertical_fov_deg(self) -> float:
        return 2.0*math.degrees(math.atan(self.sensor_height_mm/(2.0*self.focal_length_mm)))

    @property
    def diagonal_fov_deg(self) -> float:
        diagonal_mm = math.hypot(self.sensor_width_mm, self.sensor_height_mm)
        return 2.0*math.degrees(math.atan(diagonal_mm/(2.0*self.focal_length_mm)))

    def fov_angle_deg(self, aspect: float) -> float:
        """The capture component's ``FOVAngle`` for a render target of ``aspect``.

        The sensor rectangle is fitted so that one dimension is used in full and
        the other is cropped, which is what a real camera does when the output
        aspect does not match the sensor.
        """
        if aspect >= self.sensor_aspect:
            return self.horizontal_fov_deg
        else:
            half_angle = math.radians(self.vertical_fov_deg)/2.0
            return 2.0*math.degrees(math.atan(math.tan(half_angle)*aspect))

    def average_pixel_angle_deg(self, width_px: int, aspect: float) -> float:
        """Horizontal FOV divided by pixel count: the naive sampling figure."""
        return self.fov_angle_deg(aspect)/float(width_px)

    def center_pixel_angle_deg(self, width_px: int, aspect: float) -> float:
        """Angular width of the pixel on the optical axis.

        For a rectilinear (gnomonic) projection this is the *widest* pixel in
        the frame: ``tan`` maps linearly to pixels, so equal pixel steps cover
        less and less angle towards the edge, i.e. the periphery is magnified.
        It is the number to quote as "degrees per pixel at the axis"; compare it
        with :meth:`average_pixel_angle_deg` to see how non-uniform the sampling
        is (1.27x at 90 degrees, 1.05x on an 8 mm lens).
        """
        half_angle = math.radians(self.fov_angle_deg(aspect))/2.0
        return 2.0*math.degrees(math.atan(math.tan(half_angle)/float(width_px)))

    def angular_gain(self, reference_fov_deg: float, aspect: float) -> float:
        """How many times denser this lens samples than a ``reference_fov_deg`` view.

        Both FOVs must already be the render target's, i.e. run the reference
        through :func:`rescaled_fov_deg` first.  The ratio of tangents is the
        honest figure because ``tan`` is what maps linearly to pixels; the gain
        in samples per unit solid angle is its square.
        """
        mine = math.tan(math.radians(self.fov_angle_deg(aspect))/2.0)
        reference = math.tan(math.radians(reference_fov_deg)/2.0)
        return reference/mine

    def circle_of_confusion_mm(self, coc_pixels: float, width_px: int) -> float:
        """Blur diameter on the sensor that lands on ``coc_pixels`` output pixels."""
        return coc_pixels*self.sensor_width_mm/float(width_px)

    def hyperfocal_m(self, coc_pixels: float, width_px: int) -> float:
        """Focus distance at which everything from half of it to infinity is sharp."""
        focal_m = self.focal_length_mm/1000.0
        coc_m = self.circle_of_confusion_mm(coc_pixels, width_px)/1000.0
        return (focal_m*focal_m)/(self.fstop*coc_m) + focal_m

    def depth_of_field_m(self, coc_pixels: float, width_px: int) -> tuple[float, float]:
        """Near and far limits of acceptable focus, in metres.

        The far limit is ``inf`` once the lens is focused at or past the
        hyperfocal distance, which is the normal working point of a short lens.
        """
        focal_m = self.focal_length_mm/1000.0
        focus_m = self.focus_distance_cm/100.0
        hyperfocal = self.hyperfocal_m(coc_pixels, width_px)
        near = hyperfocal*focus_m/(hyperfocal + (focus_m - focal_m))
        if focus_m >= hyperfocal:
            return (near, math.inf)
        else:
            far = hyperfocal*focus_m/(hyperfocal - (focus_m - focal_m))
            return (near, far)

    def ue_post_process_overrides(self) -> dict[str, object]:
        """``PostProcessSettings`` entries that reproduce this lens in UE's circle DoF.

        Three engine facts drive the list (all UE 5.8):

        * ``DepthOfFieldFocalDistance`` defaults to 0, which the renderer treats
          as "depth of field disabled" (``Scene.cpp:606``), so the entry point
          has to be set explicitly;
        * ``DepthOfFieldSensorWidth`` defaults to 24.576 mm (APS-C,
          ``Scene.cpp:611``) and the engine derives the focal length from it and
          the view's FOV (``DiaphragmDOFUtils.cpp:28-42``) - leaving the default
          would invent a 27.7 mm lens behind our 47.8 degree capture and blur
          everything;
        * ``DepthOfFieldEnabled`` must be forced on (``DiaphragmDOFUtils.cpp:53``
          gates the whole effect on it), and every value carries its
          ``bOverride_*`` bit because UE ignores a field whose bit is clear.

        ``DepthOfFieldMethod`` is deliberately absent: ``EDepthOfFieldMethod`` is
        deprecated in 5.8 and no renderer code reads it, so setting it would look
        meaningful while doing nothing.
        """
        return {
            "bOverride_DepthOfFieldEnabled": True,
            "DepthOfFieldEnabled": True,
            "bOverride_DepthOfFieldFocalDistance": True,
            "DepthOfFieldFocalDistance": float(self.focus_distance_cm),
            "bOverride_DepthOfFieldFstop": True,
            "DepthOfFieldFstop": float(self.fstop),
            "bOverride_DepthOfFieldMinFstop": True,
            "DepthOfFieldMinFstop": float(self.fstop),
            "bOverride_DepthOfFieldSensorWidth": True,
            "DepthOfFieldSensorWidth": float(self.sensor_width_mm),
            "bOverride_DepthOfFieldSqueezeFactor": True,
            "DepthOfFieldSqueezeFactor": 1.0,
        }

    def describe(self) -> str:
        return (
            f"{self.name or 'lens'} {self.focal_length_mm:g}mm f/{self.fstop:g} "
            f"on {self.sensor_width_mm:g}x{self.sensor_height_mm:g}mm "
            f"(hfov {self.horizontal_fov_deg:.1f} deg, focus {self.focus_distance_cm:g}cm)"
        )


#
# The camera on the robot (URDF link ``cam_link_8``): an 8 mm M12 lens at f/2.4
# on a 1/1.8" sensor.  Focal length, sensor format and f-number came from the
# hardware; the focus distance is ASSUMED (a focus in the middle of the 1-3 m
# working range) and must be replaced by the real assembly before the depth of
# field is trusted - override with ``--lens-focus-cm``.  At 1440x1080 these
# numbers put the sharp band at 1.15-7.77 m, so the working range stays inside
# it; focusing at the hyperfocal distance (2.68 m) instead would give
# 1.42 m-infinity.
#
ROBOT_CAMERA_LENS = Lens(
    focal_length_mm=8.0,
    sensor_width_mm=SENSOR_FORMATS_MM["1/1.8"][0],
    sensor_height_mm=SENSOR_FORMATS_MM["1/1.8"][1],
    fstop=2.4,
    focus_distance_cm=200.0,
    name="robot camera 8mm",
)
