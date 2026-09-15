"""``derive_true_color``: an ABI layer's visible bands -> the daytime RGB.

The instrument has no green band, so green is synthesised from the two visible
bands and the near-infrared one, and the air between the ground and the instrument
is taken back off each band before they are mixed; the mixture, the correction and
the display gamma are stated at the lines that apply them. Nothing here fetches -
the bands arrive on the layer.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._abi_layer import read_bands
from trid3nt_server.tools.derive._hydrology_common import write_cog

__all__ = ["TrueColorError", "TrueColorLayerURI", "derive_true_color"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_true_color.derive_true_color")


class TrueColorError(RuntimeError):
    """A typed refusal: ``TRUE_COLOR_LAYER_UNREADABLE`` (no layer, no uri, or the
    visible bands are absent), ``TRUE_COLOR_GEOMETRY_MISSING`` (the layer does not
    state when and from where it was scanned, so the air between cannot be taken
    off), ``TRUE_COLOR_NO_DAYLIGHT`` (the crop carries no visible reflectance),
    ``TRUE_COLOR_WRITE_FAILED``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TrueColorLayerURI(LayerURI):
    """The daytime composite, with the bands it was mixed from and the share of
    the crop that carried any daylight at all."""

    source_bands: list[int] = []
    daylight_fraction: float = 0.0


#: The three visible-and-near-infrared bands the composite mixes: the red one, the
#: blue one, and the near-infrared band that stands in for the missing green.
_RED_BAND = 2
_BLUE_BAND = 1
_VEGGIE_BAND = 3

#: The synthetic green: ``green = 0.45*red + 0.10*veggie + 0.45*blue``. The
#: instrument carries no green channel, and this mixture of the red, near-infrared
#: and blue reflectances is the one that puts vegetation and bare ground back at the
#: colours an eye expects; a mixture without the near-infrared term reads grey-blue.
_GREEN_MIX = (0.45, 0.10, 0.45)

#: Display gamma for the reflectance channels. Visible reflectances over land sit
#: low in the range, so the midtones are raised by ``value ** (1/2.2)`` - the
#: standard display transfer - or the scene reads near-black.
_GAMMA = 1.0 / 2.2

#: Each mixed band's Rayleigh optical depth at sea level, from the standard formula
#: ``tau = 0.008569 * lam**-4 * (1 + 0.0113 * lam**-2 + 0.00013 * lam**-4)`` with the
#: wavelength in micrometres, evaluated at the three band centres 0.47, 0.64 and
#: 0.86 um. Air scatters the blue roughly four times as hard as the red, which is why
#: an uncorrected composite reads blue-grey over ground that is tan.
_RAYLEIGH_TAU = {1: 0.1850, 2: 0.0525, 3: 0.0159}

#: The geostationary orbit radius and the earth's radius in kilometres, which fix the
#: view zenith angle at a cell from its arc to the satellite's subpoint.
_ORBIT_RADIUS_KM = 42164.0
_EARTH_RADIUS_KM = 6378.137

#: The floor on the cosine of the solar zenith angle used in the slant path, about 87
#: degrees. The correction divides by it, so without a floor the last lit cells before
#: the terminator would be amplified without limit.
_MIN_SOLAR_COS = 0.05

#: An RGB composite paints its own colours; the format set renders the three bands
#: through with no ramp of its own.
_STYLE = {"kind": "continuous"}

_METADATA = AtomicToolMetadata(
    name="derive_true_color",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _observation_geometry(
    grid: dict[str, Any], on_error: Any
) -> tuple[Any, Any, Any]:
    """Per cell: the cosine of the solar zenith angle, the cosine of the view zenith
    angle, and the cosine of the angle light turns through between the two paths.

    The layer states only the instant it was scanned and the longitude it was scanned
    from; the sun's position follows the standard low-precision solar ephemeris and
    the view angles the geostationary geometry of that subpoint."""
    import numpy as np

    tags = grid.get("tags") or {}
    scanned_at, subpoint = tags.get("scan_time_utc"), tags.get("satellite_subpoint_lon")
    if not scanned_at or not subpoint:
        raise on_error(
            "the layer does not state when it was scanned or the longitude it was "
            "scanned from, so the air between the ground and the instrument cannot "
            "be taken off. Composite a layer a raw ABI band fetch returned.")
    when = datetime.fromisoformat(str(scanned_at).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    transform = grid["transform"]
    lon = transform.c + transform.a * (np.arange(grid["width"]) + 0.5)
    lat = transform.f + transform.e * (np.arange(grid["height"]) + 0.5)
    lon, lat = np.meshgrid(lon, lat)
    lat = np.radians(lat)

    days = (when - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds() / 86400.0
    anomaly = np.radians(357.528 + 0.9856003 * days)
    ecliptic = (
        np.radians(280.460 + 0.9856474 * days)
        + np.radians(1.915) * np.sin(anomaly)
        + np.radians(0.020) * np.sin(2.0 * anomaly))
    obliquity = np.radians(23.439 - 4e-7 * days)
    declination = np.arcsin(np.sin(obliquity) * np.sin(ecliptic))
    right_ascension = np.arctan2(
        np.cos(obliquity) * np.sin(ecliptic), np.cos(ecliptic))
    sidereal = (18.697374558 + 24.06570982441908 * days) % 24.0
    hour_angle = np.radians(sidereal * 15.0 + lon) - right_ascension
    solar_cos = (
        np.sin(lat) * np.sin(declination)
        + np.cos(lat) * np.cos(declination) * np.cos(hour_angle))
    solar_azimuth = np.arctan2(
        -np.sin(hour_angle),
        np.tan(declination) * np.cos(lat) - np.sin(lat) * np.cos(hour_angle))

    to_subpoint = np.radians(float(subpoint) - lon)
    arc = np.arccos(np.clip(np.cos(lat) * np.cos(to_subpoint), -1.0, 1.0))
    view_zenith = np.arctan2(
        _ORBIT_RADIUS_KM * np.sin(arc),
        _ORBIT_RADIUS_KM * np.cos(arc) - _EARTH_RADIUS_KM)
    view_azimuth = np.arctan2(
        np.sin(to_subpoint), -np.sin(lat) * np.cos(to_subpoint))

    turn_cos = (
        -solar_cos * np.cos(view_zenith)
        + np.sqrt(np.clip(1.0 - solar_cos ** 2, 0.0, 1.0))
        * np.sin(view_zenith) * np.cos(solar_azimuth - view_azimuth))
    return solar_cos, np.cos(view_zenith), turn_cos


@register_tool(
    _METADATA,
    # Reads only the layer it is handed and writes its own artifact.
    open_world_hint=False,
)
def derive_true_color(
    layer: Any,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> TrueColorLayerURI:
    """Composite the DAYTIME TRUE COLOUR picture from a raw ABI layer's visible bands.

    Use this on a layer of raw ABI bands that carries bands 1, 2 and 3 - the blue,
    red and near-infrared visible channels - to get the natural-looking daytime
    scene: land, water, cloud and smoke in the colours an eye would see. It is the
    base an active-fire, smoke or cloud overlay is read against.

    The air between the ground and the instrument is taken off each band first: clean
    air scatters the blue about four times as hard as the red, adding roughly a
    quarter to the blue a satellite sees over land against a few percent of the red,
    so an uncorrected composite renders blue-grey where bare ground is tan. What is
    taken off is the AIR only - haze, smoke and dust stay in the picture, which is
    what makes a plume visible. The instrument has no green band, so green is then
    synthesised as
    ``0.45*red + 0.10*near_infrared + 0.45*blue`` and the three channels are raised
    by the standard display gamma before they are scaled to 8 bits.

    Params:
        layer: the raw ABI layer to composite - the layer a raw ABI band fetch
            returned, or its uri. It must carry bands 1, 2 and 3; a layer without
            them is refused, naming the bands it does carry.

    Returns the composite as a three-band RGB raster, with the bands it mixed and
    the share of the crop that carried daylight. A crop with no visible reflectance
    at all - a night scene - is refused rather than returned black, and so is a layer
    that does not state when and from where it was scanned.
    """
    import numpy as np

    def _fail(message: str) -> TrueColorError:
        return TrueColorError("TRUE_COLOR_LAYER_UNREADABLE", message)

    def _no_geometry(message: str) -> TrueColorError:
        return TrueColorError("TRUE_COLOR_GEOMETRY_MISSING", message)

    bands, grid = read_bands(
        layer, (_BLUE_BAND, _RED_BAND, _VEGGIE_BAND), on_error=_fail)
    solar_cos, view_cos, turn_cos = _observation_geometry(grid, _no_geometry)

    # The single-scattering correction, in the reflectance FACTOR the instrument
    # reports: the path term is the air's own brightness along the view ray, and the
    # two-way transmittance is what the same air took out of the ground's. The solar
    # cosine cancels out of the path term because the reported factor carries it.
    phase = 0.75 * (1.0 + turn_cos ** 2)
    slant = 1.0 / np.maximum(solar_cos, _MIN_SOLAR_COS) + 1.0 / view_cos

    def _corrected(values: Any, band: int) -> Any:
        tau = _RAYLEIGH_TAU[band]
        surface = values - tau * phase / (4.0 * view_cos)
        return np.clip(surface / np.exp(-0.5 * tau * slant), 0.0, 1.0)

    red = _corrected(np.nan_to_num(bands[_RED_BAND], nan=0.0), _RED_BAND)
    blue = _corrected(np.nan_to_num(bands[_BLUE_BAND], nan=0.0), _BLUE_BAND)
    veggie = _corrected(np.nan_to_num(bands[_VEGGIE_BAND], nan=0.0), _VEGGIE_BAND)

    lit = float(np.mean(solar_cos > 0.0))
    if lit <= 0.0:
        raise TrueColorError(
            "TRUE_COLOR_NO_DAYLIGHT",
            "the sun is below the horizon everywhere in the crop at the instant this "
            "layer was scanned, so there is no daytime colour to composite - the "
            "scene is in darkness. Composite a frame from the sunlit part of the day.")

    a, b, c = _GREEN_MIX
    green = np.clip(a * red + b * veggie + c * blue, 0.0, 1.0)
    stretched = np.power(np.stack([red, green, blue], axis=0), np.float32(_GAMMA))
    rgb = np.clip(np.rint(stretched * 255.0), 0, 255).astype(np.uint8)

    seed = uuid.uuid4().hex[:8]
    uri = write_cog(rgb, crs=grid["crs"], transform=grid["transform"],
                    prefix="true_color", seed=seed, output_dir=_output_dir,
                    code="TRUE_COLOR_WRITE_FAILED", photometric="RGB")
    logger.info(
        "derive_true_color: %dx%d composite, %.1f%% of the crop lit",
        grid["width"], grid["height"], lit * 100.0)
    return TrueColorLayerURI(
        layer_id=f"true-color-{seed}",
        name="True colour",
        layer_type="raster",
        uri=uri,
        style=_STYLE,
        role="context",
        crs_authid=str(grid["crs"]) if grid["crs"] else "EPSG:4326",
        bbox=tuple(float(v) for v in grid["bounds"]),
        source_bands=[_BLUE_BAND, _RED_BAND, _VEGGIE_BAND],
        daylight_fraction=round(lit, 4))
