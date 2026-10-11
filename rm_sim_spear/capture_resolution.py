"""Render-target sizing for the URDF view tests.

A UE scene capture renders exactly at its render-target size, so that size is
the sampling rate of the saved image.  ``r.ScreenPercentage`` does not apply to
a capture that does not inherit the main-view resolution: the engine disables
the screen percentage driver for those captures and uses a legacy driver with
``InResolutionFraction = 1.0`` (``SceneCaptureRendering.cpp``).  The target size
is therefore the only lever for thin distant lines.

Rule enforced here: the pixel budget may only trim ``--supersample``; it never
renders below the requested output resolution.  Rendering below ``1.0x`` and
enlarging on the CPU is what turned distant field lines into dashes in the
recorded 1440x1080 acceptance run (main target 632x474 = ``0.44x`` of
1440x1080, then ``cv2.INTER_AREA`` up to the output size).

Cost, for reference: 1440x1080 RGBA8 is 6.2 MB, a ``2.0x`` target is 24.9 MB,
while a run of this scene peaks around 6 GB of device-local memory.  The budget
is a readback/CPU-cost knob, not the VRAM lever: the startup peak must be
controlled before the map is loaded (``--low-vram``).
"""

from __future__ import annotations

from dataclasses import dataclass

#: 1.0 means "render the capture at exactly the requested output resolution",
#: which is what the Rune SDK camera does (1440x1080 target, 1440x1080 output).
DEFAULT_SUPERSAMPLE = 1.0

#: 0 means "no pixel budget": ``--supersample`` is then the only limit.
NO_PIXEL_BUDGET = 0


@dataclass(frozen=True)
class InternalResolution:
    """Chosen capture target size and the sampling rate it represents."""

    width: int
    height: int
    scale: float
    supersample: float
    pixel_budget: int

    @property
    def budget_limited(self) -> bool:
        """True when the pixel budget trimmed the requested supersampling."""
        return self.scale < self.supersample - 1e-9

    @property
    def below_output(self) -> bool:
        """True when the target is smaller than the requested output image.

        Only reachable if the caller bypasses :func:`choose_internal_resolution`,
        which clamps the scale at ``1.0``; kept so a regression is testable.
        """
        return self.scale < 1.0 - 1e-9

    def describe(self) -> str:
        text = f"{self.width}x{self.height} ({self.scale:.2f}x)"
        if self.budget_limited:
            text += f", budget-limited from {self.supersample:.2f}x"
        return text


def choose_internal_resolution(
    width: int,
    height: int,
    supersample: float = DEFAULT_SUPERSAMPLE,
    max_pixels: int = NO_PIXEL_BUDGET,
) -> InternalResolution:
    """Choose the capture target size for a requested output resolution.

    ``width``/``height`` are the output image size; ``supersample`` is the
    highest sampling rate to use; ``max_pixels`` is an optional ceiling on the
    target size (``0`` disables the ceiling).  The returned scale is never
    below ``1.0``: a budget that cannot afford even one sample per output pixel
    is ignored instead of silently degrading the image.
    """
    if width < 1 or height < 1:
        raise ValueError("width and height must be positive")
    if supersample < 1.0:
        raise ValueError("supersample must be >= 1")
    if max_pixels < 0:
        raise ValueError("max_pixels must be >= 0")

    requested_pixels = width * height
    scale = float(supersample)
    if max_pixels:
        scale = min(scale, (max_pixels / requested_pixels) ** 0.5)
    scale = max(1.0, scale)
    return InternalResolution(
        width=max(1, round(width * scale)),
        height=max(1, round(height * scale)),
        scale=scale,
        supersample=float(supersample),
        pixel_budget=int(max_pixels),
    )
