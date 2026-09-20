"""``derive_survey_surface`` - the registered name of the bed's survey grid.

The rule itself is the bed slot's own: only that slot needs a surface between
soundings, and it lives in ``inputs.bed.survey_surface``. This shim is here
while declared plan steps still name the tool.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.bed import (
    SurveySurfaceError,
    SurveySurfaceLayerURI,
    survey_surface,
)
from trid3nt_server.tools import register_tool

__all__ = ["SurveySurfaceError", "SurveySurfaceLayerURI", "derive_survey_surface"]

_METADATA = AtomicToolMetadata(
    name="derive_survey_surface",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    # Reads only the layer it is handed and writes its own artifact.
    open_world_hint=False,
)
def derive_survey_surface(
    points: Any,
    resolution_m: float,
    value_field: str | None = None,
    max_distance_m: float | None = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> SurveySurfaceLayerURI | None:
    """Interpolate a POINT layer of measurements onto a raster surface -> a continuous grid.

    ROUTING: "grid these soundings", "turn this point survey into a surface I can
    drape a mesh on", "make a bathymetry raster from these depth points", "IDW
    these measurements at 5 m". Use it wherever scattered measurements - a
    channel survey, a set of well readings, any point layer carrying one number -
    have to become the continuous field something else samples.

    The surface is inverse-distance weighted over the twelve nearest
    measurements with weights ``1/d^2``, computed in the points' own UTM zone so
    the cell size asked for is metres on the ground. It is valid only over the
    FOOTPRINT the soundings measured - what lies within the survey's own reach of
    one of them - and NODATA outside: the gap between two survey lines is
    interpolated, the bank a survey never crossed is not. That mask is what a
    merge below reads to say which cells a measurement painted.

    Do NOT use for: resampling a raster (that is a warp), or contouring.

    Params:
        points: the measurements - a point vector layer uri or inline GeoJSON.
            Non-point rows are ignored, so a survey artifact carrying its
            footprint beside its soundings enters as it is. ABSENT - a survey
            row whose source held nothing - there is no surface and the answer
            is nothing, which is what a caller composing over it reads.
        resolution_m: the cell size in metres.
        value_field: the property to interpolate. Optional only when the layer
            carries exactly one numeric field; with several, naming it is
            required rather than guessed.
        max_distance_m: THE REACH - how far a cell may be from the nearest
            measurement and still be inside the footprint. Default is three
            times the survey's own median point spacing, which is stated on the
            result; a coarse ``resolution_m`` never widens it, and a cell so
            coarse that no cell centre falls inside the footprint refuses.

    Returns the surface as a single-band float32 raster in the local UTM zone,
    with the field it interpolated, the vertical datum the measurements state on
    their own rows, any shift those rows publish onto a national frame (carried
    through UNAPPLIED - this surface is still counted from the survey's own
    zero), the method, the point count, the search radius, the share of cells
    filled and the value range. ``None`` where no soundings were handed over.
    """
    return survey_surface(points, resolution_m, value_field=value_field,
                          max_distance_m=max_distance_m,
                          _output_dir=_output_dir)
