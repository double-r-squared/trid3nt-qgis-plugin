"""TELEMAC-2D river-dye surface-tracer engine contracts (river-dye North Star).

The TELEMAC analogue of ``geoclaw_contracts.py``. TELEMAC-2D solves the 2D
shallow-water equations with an advected TRACER over a real river reach; the
river-dye archetype releases a FINITE dye pulse at a mid-reach point source and
watches the plume travel downstream and dilute. The deliverable differs from the
flood engines in ONE deliberate way: the primary artifact is the engine's NATIVE
time-stepped mesh (a SELAFIN ``.slf`` MDAL reads directly, animating the dye
dataset group with zero new render infra), so the postprocess emits ONE
peak-concentration COG as the map anchor + narration carrier and lets the mesh
sibling carry the animation (see ``export_case_to_qgis`` + ``postprocess_telemac``).

``TelemacDyeLayerURI`` extends ``LayerURI`` field-for-field (so it still maps onto
``map-command load-layer`` with no translation) and adds the dye narration
scalars the agent cites rather than invents (invariant 1 / FR-AS-7).
"""

from __future__ import annotations

from pydantic import Field

from .execution import LayerURI

__all__ = [
    "TELEMAC_DYE_STYLE_PRESET",
    "TelemacDyeLayerURI",
]

#: Style preset for the dye-concentration raster. A DISTINCT key (not the flood
#: ``continuous_flood_depth`` nor the MODFLOW ``continuous_plume_concentration``)
#: so ``export_case_to_qgis._MESH_SIBLING_BY_STYLE_PRESET`` can map it to the
#: TELEMAC SELAFIN mesh sibling without colliding with another engine. The layer
#: always carries a data-driven ``legend`` so it renders regardless of the QML
#: preset library's coverage of this key (additive, legend-drives-render design).
TELEMAC_DYE_STYLE_PRESET: str = "continuous_dye_concentration"


class TelemacDyeLayerURI(LayerURI):
    """A ``LayerURI`` for a TELEMAC-2D peak dye-concentration layer + scalars.

    Extends ``LayerURI`` field-for-field (same as every other layer). Adds the
    structured numbers the agent narrates about the tracer plume so the LLM cites
    typed fields, never invents them (invariant 1, FR-AS-7):

        dye_cmax_mgl: peak dye concentration anywhere/anytime in the reach, mg/L
            (>= 0) -- the strength of the spill signal.
        dye_peak_time_s: OPTIONAL simulated time (s from t0) at which that peak
            concentration occurred (>= 0). ``None`` when unavailable.
        plume_reach_m: OPTIONAL along-reach distance (m, >= 0) the plume centroid
            travelled from the release point to its farthest downstream position
            -- how far the dye moved. ``None`` when unavailable.
        active_frames: OPTIONAL number of output frames in which the plume was
            present in-reach (>= 0) -- how long the dye lingered before it passed.
            ``None`` when unavailable.
        mesh_size_m: OPTIONAL target gmsh edge length (m, > 0) the mesh was built
            at -- the GRANULARITY the solve actually used (BK-3c). The agent cites
            this so mesh resolution is a visible, narratable lever, never hidden.
        mesh_node_estimate: OPTIONAL estimated node count for that resolution
            (>= 0) -- the size/cost signal the approve-mesh gate surfaces.
        mesh_resolution_label: OPTIONAL human label for how the resolution was
            chosen ("auto (medium)", "fine", "custom 8 m", ...). ``None`` when
            unavailable.

    ``layer_type`` is ``"raster"`` (the peak-concentration COG); the animation is
    played from the SELAFIN mesh sibling, not per-frame COGs. The raster uses the
    ``continuous_dye_concentration`` style preset + a data-driven ``legend``.
    """

    dye_cmax_mgl: float = Field(ge=0.0)
    dye_peak_time_s: float | None = Field(default=None, ge=0.0)
    plume_reach_m: float | None = Field(default=None, ge=0.0)
    active_frames: int | None = Field(default=None, ge=0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_node_estimate: int | None = Field(default=None, ge=0)
    mesh_resolution_label: str | None = Field(default=None)
