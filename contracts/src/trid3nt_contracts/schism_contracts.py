"""SCHISM cross-scale coastal-hydrodynamics engine contracts (engine #12 landing).

SCHISM (schism-dev) is the fidelity-ladder's refinement-grade cross-scale coastal
hydrodynamics core -- the semi-implicit unstructured-grid solver behind NOAA's
operational STOFS-3D. This landing ships ONE archetype: ``tidal_hydro`` -- a
BAROTROPIC tidal simulation forced ONLY by an analytical/constituent tidal
boundary, needing NO external forcing legs (no HYCOM/ESPC-D open-ocean fields, no
sflux atmosphere, no river discharge). Two mesh sources feed it:

  * ``bundled_quarterannulus`` -- SCHISM's own Test_QuarterAnnulus verification
    case (Lynch & Gray annular tidal channel; an IDEALIZED, non-georeferenced
    mesh with a bundled ANALYTICAL M2 solution). The deliverable is the analytical
    RMSE/amplitude VERIFICATION at the station point -- the spike's green gate
    re-proven through the product path (ADR 0115).
  * ``coastal_tin`` -- an oceanmesh ``coastal_tin`` TIN (ADR 0101) for a REAL US
    coastal AOI, bathymetry sampled from our terrain fetchers (fetch_topobathy /
    fetch_dem, NAVD88) onto the TIN nodes via the proven ``tin_to_hgrid`` bridge,
    with a constituent tidal boundary. The deliverable is a max water-surface
    elevation surface CLIPPED to the AOI + COG (the ADR 0116 output contract) plus
    the mesh preview + a station elevation-timeseries chart.

``SCHISMRunArgs`` is the typed run spec the composer assembles; the result is a
``SchismElevationLayerURI`` (extends ``LayerURI``) carrying the tidal scalars the
agent CITES rather than invents (invariant 1 / FR-AS-7). No raw continental netCDF
is ever a layer (ADR 0116): the 2D max-elevation field is clipped + COG-tiled.

Fidelity scope, stated honestly (demonstration-geometry doctrine where it applies):
this archetype answers a BAROTROPIC TIDAL circulation question -- surge, waves
(WWM-III), and compound coastal flooding are the coming candidates (they need the
forcing legs, ADR 0115 section 4a), NOT this wave. For fast arbitrary-AOI flood
SCREENING use ``sfincs_flood``; SCHISM is the refinement-grade cross-scale core.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import GraceModel, SyntheticInput
from .execution import LayerURI

__all__ = [
    "SCHISM_ELEV_STYLE_PRESET",
    "SCHISM_WAVE_STYLE_PRESET",
    "SCHISMRunArgs",
    "SchismElevationLayerURI",
    "SchismWaveLayerURI",
    "SchismTransportValidationResult",
    "SCHISM_ARCHETYPES",
    "SCHISM_MESH_SOURCES",
    "SCHISM_CONSTITUENTS",
    "SCHISM_TRANSPORT_SCHEMES",
    "SCHISM_ERROR_CODES",
    "SCHISM_SOLVE_FAILED",
    "SCHISM_MESH_INVALID",
    "SCHISM_INPUT_INVALID",
    "SCHISM_OUTPUT_EMPTY",
]

#: Style preset for the max water-surface ELEVATION raster. HONEST REUSE of the
#: continuous flood-depth family ramp (a tidal max-elevation envelope is an
#: all-positive high-water surface, exactly the shape that ramp renders); the
#: layer always carries a DATA-DRIVEN ``legend`` so the real elevation range +
#: label render regardless of the QML preset library's coverage.
SCHISM_ELEV_STYLE_PRESET: str = "continuous_flood_depth"

#: Style preset for the max significant wave HEIGHT (Hs) raster. HONEST REUSE of
#: the continuous flood-depth family ramp (an Hs field is an all-positive nearshore
#: envelope, exactly the shape that ramp renders); the layer always carries a
#: DATA-DRIVEN ``legend`` so the real Hs range + label render regardless of the QML
#: preset library's coverage. Shared with the elevation raster by construction.
SCHISM_WAVE_STYLE_PRESET: str = "continuous_flood_depth"

#: The registered archetypes for this engine. v1 shipped the barotropic
#: tidal-hydrodynamics archetype (``tidal_hydro``, no external forcing legs); the
#: SCHISM+WWM two-way wave-current coupling archetype (``coupled_waves``, the Duck
#: FRF validation, ADR 0126/0129) is the second. STOFS-class surge + CORIE estuary
#: remain queued sign-off candidates (each needing its own forcing legs).
SCHISM_ARCHETYPES: tuple[str, ...] = ("tidal_hydro", "coupled_waves")

#: The mesh sources the ``tidal_hydro`` archetype accepts.
SCHISM_MESH_SOURCES: tuple[str, ...] = ("bundled_quarterannulus", "coastal_tin")

#: The tidal constituents an analytical/constituent boundary may drive (the major
#: 8; v1 defaults to M2, the dominant semidiurnal). A per-constituent amplitude +
#: phase is authored into ``bctides.in`` at the open boundary nodes.
SCHISM_CONSTITUENTS: tuple[str, ...] = ("M2", "S2", "N2", "K2", "K1", "O1", "P1", "Q1")

#: The transport-scheme settings the transport-validation template contrasts, both
#: driven through the SAME ``itr_met=3`` code path on the hydro-core binary via the
#: per-element ``tvd.prop`` flag (1 -> TVD^2 limiter active; 0 -> first-order upwind
#: everywhere). The published Test_HeatConsv_TVD / Test_HeatConsv_Upwind pair.
SCHISM_TRANSPORT_SCHEMES: tuple[str, ...] = ("tvd", "upwind")

# --- typed error codes (open-set A.6 surface) ------------------------------- #
#: The SCHISM solve failed: the "Run completed successfully" sentinel was never
#: written (SCHISM exits 0 even on a mid-run abort -- the HEC-RAS lesson), an MPI
#: fault, or a timeout. The honest-failure code (never a silent empty success).
SCHISM_SOLVE_FAILED: str = "SCHISM_SOLVE_FAILED"
#: The mesh (hgrid.gr3) is invalid for SCHISM: a non-manifold boundary the
#: pinch-cleaner could not open, an incomplete boundary loop, or the ipre grid
#: preprocessor rejecting the topology. Raised by the TIN->gr3 bridge / grid check.
SCHISM_MESH_INVALID: str = "SCHISM_MESH_INVALID"
#: The run args were invalid before dispatch (bad archetype/mesh_source, a
#: non-finite/out-of-band tidal amplitude or sim window, an unknown constituent,
#: or a coastal_tin AOI that resolves to no bbox).
SCHISM_INPUT_INVALID: str = "SCHISM_INPUT_INVALID"
#: The solve completed but produced no out2d elevation field / no finite nodes
#: (an empty solve is a failure, not an empty success -- honesty floor).
SCHISM_OUTPUT_EMPTY: str = "SCHISM_OUTPUT_EMPTY"

SCHISM_ERROR_CODES: tuple[str, ...] = (
    SCHISM_SOLVE_FAILED,
    SCHISM_MESH_INVALID,
    SCHISM_INPUT_INVALID,
    SCHISM_OUTPUT_EMPTY,
)


class SCHISMRunArgs(GraceModel):
    """Forcing + scenario parameters for a SCHISM barotropic tidal run.

    Assembled by the SCHISM composer after agent-confirmed parameter extraction;
    serialized (in part) into the worker manifest. Confirmation-before-consequence
    (invariant 9 -- a solver run) is enforced by the input-review + mesh-preview
    gates around the template (ADR 0107 / 0099), not re-implemented here.

    Fields:
        schema_version: contract version pin (additive growth only).
        archetype: the barotropic tidal archetype. v1 ships exactly
            ``"tidal_hydro"``.
        mesh_source: ``"bundled_quarterannulus"`` (the verification mesh + its
            bundled analytical solution -- the RMSE gate) or ``"coastal_tin"`` (an
            oceanmesh TIN for a real US coastal AOI with sampled bathymetry).
        location_query: place name for the ``coastal_tin`` AOI (geocoded to a
            bbox). Ignored for the bundled mesh.
        bbox: explicit EPSG:4326 ``(min_lon, min_lat, max_lon, max_lat)`` AOI for
            the ``coastal_tin`` mesh (wins over ``location_query``). Ignored for
            the bundled mesh.
        constituents: the tidal constituents the analytical boundary drives
            (default ``["M2"]`` -- the dominant semidiurnal). Each must be in
            ``SCHISM_CONSTITUENTS``.
        tidal_amplitude_m: the open-boundary tidal ELEVATION amplitude (metres)
            for the primary constituent on a ``coastal_tin`` run (a plausible Gulf/
            Atlantic amplitude is ~0.15-0.7 m). Ignored for the bundled mesh (its
            bctides.in is baked). Clamped (0, 5].
        tidal_period_hr: the primary constituent period (hours). Default 12.42 (M2).
        sim_days: total run length (days). The bundled verification is 5 d (the
            spike gate); a coastal_tin run defaults to a few days past a 1-day
            ramp so the tidal signal spins up. Clamped [1, 15].
        min_edge_length_m / max_edge_length_m: the oceanmesh TIN edge bounds
            (metres) for a ``coastal_tin`` run.
        open_boundary_side: which exterior side of the TIN is the open (tidal)
            boundary (``"south"|"north"|"east"|"west"``). Default ``"south"``
            (seaward for a Gulf coast).
        input_mode: run-mode lever (ADR 0107). ``"user_gated"`` presents the
            resolved tidal forcing + mesh basis for review before the solve (and
            fires the mesh preview gate); ``"auto"`` (default) proceeds labeled.
    """

    schema_version: Literal["v1"] = "v1"
    archetype: Literal["tidal_hydro"] = "tidal_hydro"
    mesh_source: Literal["bundled_quarterannulus", "coastal_tin"] = (
        "bundled_quarterannulus"
    )
    location_query: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    constituents: list[str] = Field(default_factory=lambda: ["M2"])
    tidal_amplitude_m: float = Field(default=0.5, gt=0.0, le=5.0)
    tidal_period_hr: float = Field(default=12.42, gt=0.0, le=48.0)
    sim_days: float = Field(default=5.0, ge=1.0, le=15.0)
    min_edge_length_m: float = Field(default=200.0, gt=0.0)
    max_edge_length_m: float = Field(default=2000.0, gt=0.0)
    open_boundary_side: Literal["south", "north", "east", "west"] = "south"
    input_mode: Literal["auto", "user_gated"] | None = None

    @field_validator("tidal_amplitude_m", "tidal_period_hr", "sim_days")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v:  # NaN
            raise ValueError("must be finite")
        return v

    @field_validator("constituents")
    @classmethod
    def _known_constituents(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in SCHISM_CONSTITUENTS]
        if bad:
            raise ValueError(
                f"unknown tidal constituent(s) {bad!r}; allowed {SCHISM_CONSTITUENTS}"
            )
        return v or ["M2"]


class SchismElevationLayerURI(LayerURI):
    """A ``LayerURI`` for a SCHISM max water-surface ELEVATION raster + tidal scalars.

    Extends ``LayerURI`` field-for-field. The raster is the peak (max-over-time)
    free-surface ELEVATION at each mesh node, interpolated onto a regular grid and
    (for a georeferenced ``coastal_tin`` run) CLIPPED to the AOI + COG-tiled (ADR
    0116 -- never a raw continental netCDF layer). Adds the numbers the agent
    narrates rather than invents (invariant 1 / FR-AS-7):

        elev_max_m: peak water-surface elevation anywhere/anytime (metres, native
            datum) -- the headline high-water crest.
        elev_min_m: lowest water-surface elevation anywhere/anytime (metres) --
            the low-water trough (a tidal signal swings +/-).
        tidal_range_m: the basin-wide max minus min elevation (metres) -- the
            modeled tidal range (the plausibility signal: a few tenths of a metre
            to a couple metres on the US coast).
        n_nodes / n_elements: the SCHISM grid size (the modeled domain extent).
        sim_days: the run length (days).
        mesh_source: ``"bundled_quarterannulus"`` or ``"coastal_tin"``.
        constituents: the tidal constituents the boundary was forced with.
        station_elev_amplitude_m: the modeled tidal amplitude at the station point
            (metres) -- half the peak-to-trough station swing over the spun-up
            window.
        analytical_rmse_m: (VERIFICATION archetype only) RMSE of the modeled
            station elevation vs the bundled ANALYTICAL M2 solution over the
            spun-up window (metres). ``None`` for a coastal_tin run.
        analytical_amp_err_m: (VERIFICATION only) modeled-vs-analytical amplitude
            error (metres). ``None`` for a coastal_tin run.
        analytical_correlation: (VERIFICATION only) modeled-vs-analytical
            correlation. ``None`` for a coastal_tin run.

    ``layer_type`` is ``"raster"`` (the max-elevation COG); the SEPARATE mesh
    preview rides as a ``layer_type="mesh"`` LayerURI (ADR 0118). Uses the
    ``continuous_flood_depth`` style preset + a data-driven ``legend``. The
    ``fallback_note`` carries any demonstration-geometry / bathymetry-source
    honesty floor.
    """

    elev_max_m: float
    elev_min_m: float | None = None
    tidal_range_m: float | None = Field(default=None, ge=0.0)
    n_nodes: int | None = Field(default=None, ge=0)
    n_elements: int | None = Field(default=None, ge=0)
    sim_days: float | None = Field(default=None, ge=0.0)
    mesh_source: str | None = None
    constituents: list[str] = Field(default_factory=list)
    station_elev_amplitude_m: float | None = Field(default=None, ge=0.0)
    analytical_rmse_m: float | None = Field(default=None, ge=0.0)
    analytical_amp_err_m: float | None = Field(default=None, ge=0.0)
    analytical_correlation: float | None = None


class SchismWaveLayerURI(LayerURI):
    """A ``LayerURI`` for a SCHISM+WWM max significant-wave-HEIGHT raster + wave scalars.

    The ``coupled_waves`` archetype's typed result (ADR 0126/0129): the two-way
    wave-current coupled solve (SCHISM hydro core + WWM-III spectral waves + the
    GOTM k-epsilon turbulence closure, ``itur=3``) writes ``sigWaveHeight`` (Hs) and
    ``peakPeriod`` (Tp) per node per step into ``out2d``. The raster is the PEAK
    (max-over-time) significant wave height at each mesh node, interpolated onto a
    regular grid and (for the georeferenced Duck FRF mesh) CLIPPED to the mesh AOI +
    COG-tiled (ADR 0116 -- never a raw netCDF layer). The scalars the agent CITES
    rather than invents (invariant 1 / FR-AS-7):

        hs_max_m: peak significant wave height anywhere/anytime (metres) -- the
            headline nearshore wave crest.
        hs_mean_m: mean Hs over the wet nodes (metres) -- the field-average energy.
        tp_max_s: peak (discrete) wave period anywhere/anytime (seconds).
        tp_at_hs_max_s: the Tp at the Hs-max node (seconds) -- the dominant swell.
        offshore_hs_m: modeled Hs at the offshore (deepest) mesh boundary (metres)
            -- the forcing anchor a cross-shore transect shoals down from.
        n_nodes / n_elements: the SCHISM grid size (the modeled domain extent).
        sim_hours: the coupled-run length (hours; the Duck case is 4 h).

    Cross-shore VERIFICATION vs the bundled published gauge transect (the 8m-array /
    pressure-transducer Hm0/Tp; ``None`` when the V&V data is absent):

        vv_n_gauges: number of cross-shore gauges matched.
        vv_hs_rmse_m: RMSE of modeled vs measured Hs across the gauges (metres).
        vv_hs_bias_m: mean (modeled - measured) Hs bias across the gauges (metres).
        vv_hs_corr: modeled-vs-measured cross-shore Hs correlation.
        vv_offshore_hs_obs_m / vv_offshore_hs_mod_m: measured/modeled Hs at the
            offshore reference gauge (the boundary-forcing anchor).
        vv_tp_rmse_s: RMSE of modeled vs measured Tp across the gauges (seconds).

    ``layer_type`` is ``"raster"`` (the max-Hs COG); the SEPARATE mesh preview rides
    as a ``layer_type="mesh"`` LayerURI (ADR 0118). Uses the
    ``continuous_flood_depth`` style preset + a data-driven ``legend``. The
    ``fallback_note`` carries the coupled-wave fidelity / published-fixture honesty
    floor.
    """

    hs_max_m: float
    hs_mean_m: float | None = Field(default=None, ge=0.0)
    tp_max_s: float | None = Field(default=None, ge=0.0)
    tp_at_hs_max_s: float | None = Field(default=None, ge=0.0)
    offshore_hs_m: float | None = Field(default=None, ge=0.0)
    n_nodes: int | None = Field(default=None, ge=0)
    n_elements: int | None = Field(default=None, ge=0)
    sim_hours: float | None = Field(default=None, ge=0.0)
    # cross-shore V&V vs the bundled published gauge transect
    vv_n_gauges: int | None = Field(default=None, ge=0)
    vv_hs_rmse_m: float | None = Field(default=None, ge=0.0)
    vv_hs_bias_m: float | None = None
    vv_hs_corr: float | None = None
    vv_offshore_hs_obs_m: float | None = Field(default=None, ge=0.0)
    vv_offshore_hs_mod_m: float | None = Field(default=None, ge=0.0)
    vv_tp_rmse_s: float | None = Field(default=None, ge=0.0)


class SchismTransportValidationResult(GraceModel):
    """Typed result of a SCHISM transport-scheme numerical-mixing V&V (ADR 0156).

    NOT a ``LayerURI``: the case advects a temperature FRONT (a conservative scalar)
    across the idealized QuarterAnnulus tidal channel TWICE on the hydro-core binary
    -- once with the TVD^2 limiter active (per-element ``tvd.prop=1``) and once with
    first-order upwind everywhere (``tvd.prop=0``) -- through the identical flow, so
    the difference isolates the transport scheme's NUMERICAL MIXING. The mesh is
    schematic (planar, non-georeferenced), so the product is the scheme-contrast
    CHART + typed scalars, never a georeferenced map. Every number is plain
    arithmetic off the scribed ``temperature`` netCDF (invariant 1). Covers the two
    published Test_HeatConsv_* / Test_GEN_MassConsv verification questions in one
    run pair (the GEN module-specific path is full-monty-only -- documented, not
    run here).

    Fields:
        question: the one-line question answered.
        n_nodes / n_elements / n_layers: the SCHISM grid size (modeled domain).
        sim_days: the run length (days).
        front_t_hot_c / front_t_cold_c: the injected temperature-front end values
            (deg C) -- the conservative scalar's initial min/max.
        tvd_variance_retained_pct: fraction (percent) of the initial tracer spatial
            VARIANCE the TVD run still holds at run end -- higher = sharper front,
            less numerical mixing.
        upwind_variance_retained_pct: same for the first-order upwind run (lower --
            upwind is more numerically diffusive).
        excess_mixing_factor: how many times MORE variance the upwind scheme lost
            vs TVD (``(1 - upwind_frac) / (1 - tvd_frac)``) -- the headline mixing
            contrast (>1 means upwind mixes more).
        tvd_mass_drift_pct / upwind_mass_drift_pct: domain-integrated tracer-mass
            drift over the run (percent of the initial mass) -- the mass-conservation
            sanity gate (a conservative scalar with only open-boundary exchange
            should drift only slightly; near-zero is the numerical-scheme sanity
            check the Test_GEN_MassConsv question asks).
        tvd_overshoot_c / upwind_overshoot_c: the max excursion ABOVE the initial
            hot value (deg C) -- upwind is monotone (~0); an unlimited scheme would
            overshoot.
        validated: True iff the acceptance held (upwind lost strictly more variance
            than TVD AND both mass drifts within the sanity bound).
        metrics: extra scalars (per-scheme variance/mass time series endpoints,
            tolerances) -- all real parsed netCDF outputs.
        chart_titles: titles of the emitted comparison chart(s).
        demonstration_note: the LOUD honesty label -- an idealized verification-mesh
            numerical-mixing benchmark, not a georeferenced site study.
        schematic_only: always True (no georeferenced layer).
        gen_module_note: the honest disposition of the GEN-module-specific path
            (USE_GEN is full-monty-only and demands every module namelist; the
            conservative temperature tracer demonstrates the same mass-conservation
            mechanism on the clean hydro-core binary).
        synthetic_inputs: structured provenance (the verification-mesh basis + the
            published Test_HeatConsv_* / Test_GEN_MassConsv references).
    """

    question: str = ""
    n_nodes: int | None = Field(default=None, ge=0)
    n_elements: int | None = Field(default=None, ge=0)
    n_layers: int | None = Field(default=None, ge=0)
    sim_days: float | None = Field(default=None, ge=0.0)
    front_t_hot_c: float | None = None
    front_t_cold_c: float | None = None
    tvd_variance_retained_pct: float | None = Field(default=None, ge=0.0)
    upwind_variance_retained_pct: float | None = Field(default=None, ge=0.0)
    excess_mixing_factor: float | None = Field(default=None, ge=0.0)
    tvd_mass_drift_pct: float | None = None
    upwind_mass_drift_pct: float | None = None
    tvd_overshoot_c: float | None = None
    upwind_overshoot_c: float | None = None
    validated: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)
    chart_titles: list[str] = Field(default_factory=list)
    demonstration_note: str = ""
    schematic_only: bool = True
    gen_module_note: str = ""
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)
