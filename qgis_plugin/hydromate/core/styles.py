"""Default symbology for the two things a hydraulic result is always read for.

Every other variable points the user at Layer Symbology, deliberately: guessing a ramp
for bed shear stress or turbulent kinetic energy would be worse than saying nothing,
because a plausible-looking wrong scale is harder to notice than an obviously default one.

**Water depth** - white to bright blue to dark blue, with everything below the case's own
minimum depth transparent. That threshold is not decoration. On a bed with a Nikuradse
roughness of 0.05-0.5 m, water 5 mm deep stands *inside* the grain roughness rather than
flowing over it, and rendering it as river makes a wetted extent look far larger than it
is. hydromate's own reporting uses the same filter, so the map and the report agree.

**Velocity** - arrows over an inverted *plasma* ramp, bright yellow at zero to dark purple
at the maximum. Capped at 5 m/s by default and **warned about** above it: a depth-averaged
river result above that is nearly always a wetting/drying artefact in a nearly-dry cell
rather than real flow, and letting one such node set the scale flattens the whole map into
a single colour.
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.core import (QgsColorRampShader, QgsGradientColorRamp, QgsGradientStop,
                       QgsMeshRendererScalarSettings, QgsMeshRendererVectorSettings)

from ..compat import color

#: Above this, a depth-averaged velocity is almost certainly a wetting/drying artefact.
VELOCITY_WARN_ABOVE = 5.0
DEFAULT_VELOCITY_MAX = 5.0
DEFAULT_MIN_DEPTH = 0.01

DEPTH_STOPS = ("#ffffff", "#cfe8ff", "#6bb6ff", "#1f78d1", "#08306b")
#: *plasma*, reversed - bright where the flow is slow, dark where it is fast, so the
#: arrows that matter read as dense marks rather than as glare.
PLASMA_REVERSED = ("#f0f921", "#fca636", "#e16462", "#b12a90", "#6a00a8", "#0d0887")


@dataclass
class DepthStyle:
    minimum: float = DEFAULT_MIN_DEPTH
    maximum: float = 1.0
    stops: tuple[str, ...] = DEPTH_STOPS


@dataclass
class VelocityStyle:
    maximum: float = DEFAULT_VELOCITY_MAX
    clamped_from: float | None = None       # the real maximum, when it was clamped
    stops: tuple[str, ...] = PLASMA_REVERSED

    @property
    def warning(self) -> str:
        if self.clamped_from is None:
            return ""
        return (f"The mesh reaches {self.clamped_from:.2f} m/s; the colour scale is "
                f"capped at {self.maximum:.1f} m/s. Velocities above ~5 m/s in a "
                "depth-averaged result are usually a wetting/drying artefact in a "
                "nearly-dry cell rather than real flow. Override the maximum if the "
                "reach genuinely is that fast.")


def clamp_velocity(observed_max: float | None,
                   cap: float = DEFAULT_VELOCITY_MAX) -> VelocityStyle:
    """Choose the velocity scale, remembering when the cap actually bit."""
    if not observed_max or observed_max <= 0:
        return VelocityStyle(maximum=cap)
    if observed_max > cap:
        return VelocityStyle(maximum=cap, clamped_from=float(observed_max))
    return VelocityStyle(maximum=float(observed_max))


def _ramp(stops: tuple[str, ...]) -> QgsGradientColorRamp:
    first, *middle, last = stops
    positions = [(i + 1) / (len(stops) - 1) for i in range(len(middle))]
    return QgsGradientColorRamp(
        color(first), color(last), False,
        [QgsGradientStop(pos, color(spec)) for pos, spec in zip(positions, middle)])


def _shader(minimum: float, maximum: float, stops: tuple[str, ...], *,
            transparent_below: float | None = None) -> QgsColorRampShader:
    shader = QgsColorRampShader(minimum, maximum, _ramp(stops))
    shader.setColorRampType(QgsColorRampShader.Interpolated)
    span = max(maximum - minimum, 1e-9)
    items = []
    if transparent_below is not None and transparent_below > minimum:
        # A hard transparent step at the threshold, not a fade: the point is that this
        # water is not flowing, and a faint blue haze would still read as river.
        items.append(QgsColorRampShader.ColorRampItem(
            transparent_below, color(stops[0], alpha=0), f"< {transparent_below:g}"))
        minimum = transparent_below
        span = max(maximum - minimum, 1e-9)
    for i, spec in enumerate(stops):
        value = minimum + span * i / (len(stops) - 1)
        items.append(QgsColorRampShader.ColorRampItem(value, color(spec), f"{value:.3g}"))
    shader.setColorRampItemList(items)
    return shader


def depth_settings(style: DepthStyle) -> QgsMeshRendererScalarSettings:
    """Scalar settings for water depth, with the sub-threshold band transparent."""
    settings = QgsMeshRendererScalarSettings()
    settings.setClassificationMinimumMaximum(0.0, max(style.maximum, style.minimum * 2))
    settings.setColorRampShader(_shader(0.0, max(style.maximum, style.minimum * 2),
                                        style.stops,
                                        transparent_below=style.minimum))
    return settings


def velocity_scalar_settings(style: VelocityStyle) -> QgsMeshRendererScalarSettings:
    settings = QgsMeshRendererScalarSettings()
    settings.setClassificationMinimumMaximum(0.0, style.maximum)
    settings.setColorRampShader(_shader(0.0, style.maximum, style.stops))
    return settings


def velocity_vector_settings(style: VelocityStyle) -> QgsMeshRendererVectorSettings:
    """Arrows, coloured by magnitude.

    The magnitude is the Euclidean norm of whatever components the dataset carries -
    which for a 2D result is (u, v) and for a depth-averaged 3D result is still the
    horizontal pair, because that is what a 2D calibration is compared against. QGIS
    computes it from the vector dataset itself, so nothing here has to read the file.
    """
    settings = QgsMeshRendererVectorSettings()
    settings.setColoringMethod(
        QgsMeshRendererVectorSettings.ColoringMethod.ColorRamp)
    shader = _shader(0.0, style.maximum, style.stops)
    settings.setColorRampShader(shader)
    settings.setLineWidth(0.6)
    return settings
