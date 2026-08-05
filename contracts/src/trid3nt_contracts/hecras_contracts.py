"""HEC-RAS 6.x 1D/2D riverine-flood engine contracts (engine #11 landing).

HEC-RAS is the fidelity-ladder's refinement-grade riverine 1D/2D solver (the
FEMA/USACE-canonical US engine). The landing is TEMPLATE-FIRST (ADR 0100 / the
ras-commander feasibility spike): headless 2D mesh authoring is the real frontier
(RASMapper's terrain subgrid tables need Windows DLLs), so the first archetype
reparameterizes HEC's own shipped Muncie project (White River, Muncie IN) rather
than building geometry for an arbitrary AOI. The GEOMETRY is FROZEN (the RASMapper
subgrid tables are prebuilt); what varies is the unsteady FLOW forcing (the inflow
hydrograph, scaled by a plain multiplier or to a target peak discharge).

``HECRASRunArgs`` is the typed run spec the composer assembles (archetype selector
+ flow-scaling knobs + the ADR 0107 input_mode lever); ``HecrasDepthLayerURI``
extends ``LayerURI`` field-for-field (so it maps onto ``map-command load-layer``
with no translation) and adds the riverine-flood scalars the agent cites rather
than invents (invariant 1 / FR-AS-7). The headline deliverable is the SAME shape
as every other flood engine: a peak overland-DEPTH raster (max water surface minus
the per-cell bed elevation over the 2D flow area).

The demonstration-geometry scope is LOUD (NATE no-hand-wave doctrine): this
archetype answers "what-if flow on the Muncie White River demonstration model",
NOT "flood any user AOI" -- real-AOI HEC-RAS awaits headless geometry authoring.
Off-scope arbitrary-AOI riverine/coastal flooding -> ``sfincs_flood``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from .common import GraceModel
from .execution import LayerURI

__all__ = [
    "HECRAS_DEPTH_STYLE_PRESET",
    "HECRASRunArgs",
    "HecrasDepthLayerURI",
    "HECRAS_ARCHETYPES",
    "HECRAS_ERROR_CODES",
    "HECRAS_SOLVE_FAILED",
    "HECRAS_INPUT_INVALID",
    "HECRAS_FINISHED_SENTINEL_MISSING",
    "HECRAS_OUTPUT_EMPTY",
]

#: Style preset for the peak-depth raster. HONEST REUSE of the flood-depth family
#: (``sfincs_flood`` / ``swmm_urban_flood`` / ``geoclaw_inundation`` all render
#: overland depth on this ramp) -- a HEC-RAS 2D depth grid IS an overland flood
#: depth, so it shares the same continuous ramp + data-driven legend. No new
#: render infra; the layer always carries a data-driven ``legend`` so the real
#: depth range renders regardless of the QML preset library's coverage.
HECRAS_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"

#: The registered archetypes for this engine. Both reparameterize HEC's own shipped
#: Muncie White River (Muncie IN) demonstration project (frozen 1D/2D geometry):
#:   - ``muncie_riverine_flood`` -- what-if UNSTEADY FLOW forcing (ADR 0109).
#:   - ``muncie_levee_breach`` -- what the LEVEED protected 2D floodplain looks like
#:     when the lateral-structure levee FAILS vs HOLDS (ADR 0125); the deck's
#:     Breach Data block is toggled. Bald Eagle Creek multi-2D levee + rain-on-grid
#:     stay the queued next archetypes (ADR 0125 ledger), each needing its own
#:     shipped-geometry fixture (the Bald Eagle model awaits the Windows-Phase-1
#:     unblock).
HECRAS_ARCHETYPES: tuple[str, ...] = ("muncie_riverine_flood", "muncie_levee_breach")

# --- typed error codes (open-set A.6 surface) ------------------------------- #
#: The unsteady solve failed: a non-zero engine exit, or a mass-balance / result
#: failure. The Finished-sentinel gate (below) is the honest-failure specialization.
HECRAS_SOLVE_FAILED: str = "HECRAS_SOLVE_FAILED"
#: The run args were invalid before dispatch (bad archetype, non-finite scale, a
#: flow scale outside the modelable band).
HECRAS_INPUT_INVALID: str = "HECRAS_INPUT_INVALID"
#: The engine exited 0 but printed no "Finished" sentinel (HEC's engines can exit
#: 0 after printing an error). The M3 honesty gate: no sentinel == a failed run.
HECRAS_FINISHED_SENTINEL_MISSING: str = "HECRAS_FINISHED_SENTINEL_MISSING"
#: The solve produced no Results group / no wet cells (an empty solve is a
#: failure, not an empty success -- honesty floor).
HECRAS_OUTPUT_EMPTY: str = "HECRAS_OUTPUT_EMPTY"

HECRAS_ERROR_CODES: tuple[str, ...] = (
    HECRAS_SOLVE_FAILED,
    HECRAS_INPUT_INVALID,
    HECRAS_FINISHED_SENTINEL_MISSING,
    HECRAS_OUTPUT_EMPTY,
)


class HECRASRunArgs(GraceModel):
    """Forcing + scenario parameters for a HEC-RAS 6.x unsteady riverine run.

    Assembled by the HEC-RAS composer after agent-confirmed parameter extraction;
    serialized into the worker manifest. Confirmation-before-consequence
    (invariant 9 -- a solver run) is enforced by the input-review gate around the
    template (ADR 0107), not re-implemented here.

    TEMPLATE-FIRST scope: the ``archetype`` selects a shipped-geometry project
    baked in the worker image; the geometry/terrain/mesh are FROZEN (RASMapper's
    subgrid tables cannot be rebuilt headless -- ADR 0100). Only the unsteady FLOW
    forcing is reparameterized.

    Fields:
        schema_version: contract version pin (additive growth only).
        archetype: the shipped-geometry archetype (both on the frozen Muncie White
            River geometry). ``"muncie_riverine_flood"`` (what-if flow) or
            ``"muncie_levee_breach"`` (levee fails vs holds).
        breach_enabled: for ``muncie_levee_breach`` -- ``True`` (default) runs the
            levee FAILURE (the lateral-structure breaches active, the protected 2D
            floodplain floods); ``False`` runs the levee HOLDING (breaches disabled,
            the protected side stays dry). Ignored by ``muncie_riverine_flood``
            (its shipped breaches are left as-is).
        flow_scale: multiply the archetype's baseline inflow hydrograph by this
            factor (the plain-multiplier user/default path). ``1.0`` runs the
            published baseline; ``> 1`` a higher-flow "what-if" (deeper + wider
            inundation), ``< 1`` a lower-flow scenario. Clamped [0.25, 4.0] -- the
            frozen demonstration geometry is only faithful within a modelable band.
        target_peak_cfs: OPTIONAL alternative to ``flow_scale`` -- a target PEAK
            inflow discharge (cfs). When set, the worker derives the multiplier
            from the baseline peak (``target_peak_cfs / baseline_peak_cfs``), so a
            user/fetcher can pin the forcing to a real gauge/NWM peak (the seam-1
            fetcher / ADR 0102 pattern, basis="fetched"). Overrides ``flow_scale``.
            Clamped so the derived multiplier stays in the [0.25, 4.0] band.
        input_mode: run-mode lever (ADR 0107). ``"user_gated"`` presents the
            resolved flow forcing + the frozen-geometry note for review before the
            solve; ``"auto"`` (default) proceeds with them labeled.
    """

    schema_version: Literal["v1"] = "v1"
    archetype: Literal["muncie_riverine_flood", "muncie_levee_breach"] = "muncie_riverine_flood"
    breach_enabled: bool = True
    flow_scale: float = Field(default=1.0, ge=0.25, le=4.0)
    target_peak_cfs: float | None = Field(default=None, gt=0.0)
    input_mode: Literal["auto", "user_gated"] | None = None

    @field_validator("flow_scale")
    @classmethod
    def _finite_scale(cls, v: float) -> float:
        if v != v:  # NaN
            raise ValueError("flow_scale must be finite")
        return v


class HecrasDepthLayerURI(LayerURI):
    """A ``LayerURI`` for a HEC-RAS 2D peak overland-DEPTH raster + flood scalars.

    Extends ``LayerURI`` field-for-field (same as every other flood engine's
    result). The raster is the peak (max-over-time) WATER DEPTH at each 2D flow-area
    cell -- the max water-surface elevation minus the cell's terrain bed elevation,
    masked to cells that were ever wet (dry terrain is never painted as water).
    Adds the numbers the agent narrates rather than invents (invariant 1 /
    FR-AS-7):

        depth_max_ft: peak water DEPTH anywhere/anytime in the 2D domain, feet
            (>= 0) -- the headline crest depth (the model is US Customary).
        depth_mean_ft: mean depth over the wet cells, feet (>= 0) -- the typical
            inundation depth.
        wet_cell_count: number of 2D cells wet at their peak (>= 0) -- the
            inundation EXTENT signal (rises with flow: the scaled-flow sanity
            check the agent cites).
        wse_max_ft: peak free-surface ELEVATION anywhere, feet (the model's
            vertical datum) -- carried for completeness; ``ge`` unconstrained
            since an elevation may be below datum.
        flow_scale: the inflow-hydrograph multiplier the solve actually used
            (> 0) -- the reparameterization lever, surfaced so a "what-if" is
            visible/narratable, never hidden.
        peak_inflow_cfs: the PEAK inflow discharge (cfs) the scaled hydrograph
            forced the run with (>= 0) -- the physical forcing the agent cites.
        volume_error_pct: the unsteady mass-balance closure error percent from the
            engine's own Volume Accounting (the M3 volume-accounting gate) --
            surfaced so a solve's numerical health is visible; a value outside a
            small band means an unreliable result.
        n_cells: total 2D flow-area cell count (>= 0) -- the modeled domain size.
        breach_enabled: for the levee-breach archetype -- whether the levee FAILED
            (``True``, breaches active) or HELD (``False``). A HELD run is a VALID
            DRY SUCCESS: ``wet_cell_count == 0`` / ``depth_max_ft == 0`` means the
            protected side stayed dry (the levee held), NOT an empty/failed solve.
            ``None`` for the riverine-flood archetype.

    ``layer_type`` is ``"raster"`` (the peak-depth COG); it uses the
    ``continuous_flood_depth`` style preset + a data-driven ``legend``. The
    ``fallback_note`` carries the LOUD demonstration-geometry honesty floor.
    """

    depth_max_ft: float = Field(ge=0.0)
    depth_mean_ft: float | None = Field(default=None, ge=0.0)
    wet_cell_count: int | None = Field(default=None, ge=0)
    wse_max_ft: float | None = Field(default=None)
    flow_scale: float = Field(default=1.0, gt=0.0)
    peak_inflow_cfs: float | None = Field(default=None, ge=0.0)
    volume_error_pct: float | None = Field(default=None)
    n_cells: int | None = Field(default=None, ge=0)
    breach_enabled: bool | None = Field(default=None)
