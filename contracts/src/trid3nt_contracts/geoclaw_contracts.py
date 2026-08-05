"""GeoClaw (Clawpack) shallow-water inundation engine contracts (sprint-17).

The GeoClaw analogue of ``swmm_contracts.py`` / ``modflow_contracts.py``.
GeoClaw (Clawpack, BSD, pip-installable) solves the 2D nonlinear shallow-water
equations over real topography with adaptive mesh refinement (AMR), so it covers
a hazard family SFINCS/SWMM do not: **tsunami inundation**, **dam-break /
embankment-failure overland flow**, and **shallow-water surge run-up**. The
deliverable is the SAME shape as every other flood engine: a peak overland-depth
COG + a per-timestep depth-frame animation group, narrated from typed scalars.

Two shapes back the GeoClaw demo path:

- ``GeoClawRunArgs`` — the forcing/scenario parameters the agent confirms with
  the user before submitting a GeoClaw run. Consumed by the GeoClaw worker
  (``services/workers/geoclaw/...``) which maps these onto a Clawpack
  ``setrun.py`` over the AOI + a topo COG + a driver SCENARIO (one of
  ``dam_break`` / ``tsunami`` / ``surge``), runs the headless Clawpack solver,
  and rasterizes ``fort.q`` frames -> depth.
- ``GeoClawDepthLayerURI`` — the postprocess output layer. Extends ``LayerURI``
  field-for-field (so it still maps onto ``map-command load-layer`` with no
  translation, like every other layer) and adds the three depth scalars the
  agent narrates plus the echoed driver descriptor.

Design notes
------------
- ``bbox`` is the project ``BBox`` convention: ``(min_lon, min_lat, max_lon,
  max_lat)`` in EPSG:4326 (lon-first), range-validated by the shared ``BBox``
  type. The GeoClaw AOI is an *area* (the computational domain), so it is a bbox.
- ``scenario`` is an EXPLICIT PARAMETER, never silently hardcoded. It selects the
  GeoClaw initial / boundary condition family:
    "dam_break"  — a raised water column (a reservoir / impoundment) released at
                   t=0 over dry topography; the canonical embankment-failure /
                   overland shallow-water test (qinit perturbation).
    "tsunami"    — a seafloor displacement source (GeoClaw ``dtopotools``
                   Okada-style or a prescribed dtopo) drives the wave; the
                   canonical GeoClaw use case (run-up + inundation).
    "surge"      — a prescribed sea-surface elevation forcing at the open ocean
                   boundary (a tide+surge hydrograph), the shallow-water surge
                   run-up family.
- ``GeoClawDepthLayerURI`` is a structured numeric carrier (invariant 1 /
  FR-AS-7): the agent narrates ``max_depth_m``, ``flooded_area_km2`` and
  ``max_inundation_m`` from these typed fields rather than inventing them.
- The depth raster reuses the SHARED ``continuous_flood_depth`` style preset
  (GeoClaw depth is the same physical quantity SFINCS/SWMM emit), so NO new
  publish_layer style key is required.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import BBox, GraceModel
from .execution import LayerURI

__all__ = [
    "GeoClawScenario",
    "DEFAULT_SIM_DURATION_S",
    "DEFAULT_DAM_BREAK_DEPTH_M",
    "DEFAULT_OUTPUT_FRAMES",
    "DEFAULT_AMR_LEVELS",
    "GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M",
    "AmrRegionWindow",
    "GeoClawRunArgs",
    "GeoClawDepthLayerURI",
    "GEOCLAW_DEPTH_STYLE_PRESET",
]


class AmrRegionWindow(GraceModel):
    """One explicit AMR refinement window (a GeoClaw ``regiondata`` region).

    GeoClaw's ``rundata.regiondata.regions`` list forces the adaptive mesh to at
    least ``min_level`` and at most ``max_level`` inside a lat/lon box over a
    time window ``[t_start_s, t_end_s]`` (overlapping regions combine by MAX of
    covering min/max levels). This is the explicit region-based flagging path -
    a deterministic alternative to leaving refinement to error estimation
    (``flag2refine`` / Richardson) alone.

    Fields:
        min_level: the minimum AMR level FORCED inside the window (>= 1). The
            mesh is refined to at least this level here for the whole window.
        max_level: the maximum AMR level ALLOWED inside the window (>=
            ``min_level``). Cap refinement here (e.g. keep a coarse offshore box
            cheap, or hold a subregion at one fixed level).
        t_start_s: window start time, seconds from t0 (>= 0).
        t_end_s: window end time, seconds from t0 (> ``t_start_s``).
        min_lon, max_lon: window longitude bounds, EPSG:4326 (min < max).
        min_lat, max_lat: window latitude bounds, EPSG:4326 (min < max).
    """

    min_level: int = Field(ge=1, le=10)
    max_level: int = Field(ge=1, le=10)
    t_start_s: float = Field(ge=0.0)
    t_end_s: float = Field(gt=0.0)
    min_lon: float = Field(ge=-180.0, le=180.0)
    max_lon: float = Field(ge=-180.0, le=180.0)
    min_lat: float = Field(ge=-90.0, le=90.0)
    max_lat: float = Field(ge=-90.0, le=90.0)

    @field_validator("max_level")
    @classmethod
    def _max_ge_min(cls, v: int, info: Any) -> int:
        lo = info.data.get("min_level")
        if lo is not None and v < lo:
            raise ValueError(f"max_level ({v}) must be >= min_level ({lo})")
        return v

    @field_validator("max_lon")
    @classmethod
    def _lon_ordered(cls, v: float, info: Any) -> float:
        lo = info.data.get("min_lon")
        if lo is not None and not (lo < v):
            raise ValueError(f"max_lon ({v}) must be > min_lon ({lo})")
        return v

    @field_validator("max_lat")
    @classmethod
    def _lat_ordered(cls, v: float, info: Any) -> float:
        lo = info.data.get("min_lat")
        if lo is not None and not (lo < v):
            raise ValueError(f"max_lat ({v}) must be > min_lat ({lo})")
        return v

    @field_validator("t_end_s")
    @classmethod
    def _t_ordered(cls, v: float, info: Any) -> float:
        lo = info.data.get("t_start_s")
        if lo is not None and not (lo < v):
            raise ValueError(f"t_end_s ({v}) must be > t_start_s ({lo})")
        return v

#: The driver-scenario families GeoClaw can run. Open ``Literal`` so the engine
#: may add scenarios (e.g. "landslide") without a wire break.
#:   "dam_break" — raised water column released over dry topo (qinit).
#:   "tsunami"   — seafloor-displacement source (dtopotools).
#:   "surge"     — prescribed sea-surface boundary forcing (surge hydrograph).
GeoClawScenario = Literal["dam_break", "tsunami", "surge"]

#: LLM-friendly aliases for ``scenario``. The agent frequently invents synonyms
#: ("flood", "wave", "breach", ...) that fail the bare ``Literal`` and trigger a
#: visible self-correcting retry loop. We normalize these to the canonical value
#: on the FIRST attempt; an unknown string passes through unchanged so a
#: genuinely-invalid value still raises the honest Literal error.
_SCENARIO_ALIASES: dict[str, str] = {
    # dam-break / overland release.
    "dam": "dam_break",
    "dambreak": "dam_break",
    "dam-break": "dam_break",
    "breach": "dam_break",
    "embankment": "dam_break",
    "embankment_failure": "dam_break",
    "levee_breach": "dam_break",
    "reservoir": "dam_break",
    "release": "dam_break",
    "overland": "dam_break",
    # tsunami.
    "wave": "tsunami",
    "seismic_wave": "tsunami",
    "earthquake_tsunami": "tsunami",
    "okada": "tsunami",
    "dtopo": "tsunami",
    # storm surge run-up.
    "storm_surge": "surge",
    "stormsurge": "surge",
    "coastal_surge": "surge",
}


# TENTATIVE GeoClaw demo defaults (sprint-17; narrated as demo values, not
# site-calibrated parameters, by the composer).
DEFAULT_SIM_DURATION_S: float = 3600.0  # simulated physical time, seconds (1 h)
DEFAULT_DAM_BREAK_DEPTH_M: float = 10.0  # raised-column height for dam_break, m
DEFAULT_OUTPUT_FRAMES: int = 24  # number of evenly-spaced fort.q output frames
DEFAULT_AMR_LEVELS: int = 2  # AMR refinement levels (1 = uniform grid)

#: Shared depth style preset (GeoClaw depth == SFINCS/SWMM depth physically), so
#: NO new publish_layer style key is added. Single source of truth here.
GEOCLAW_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"

#: Default fgmax wet-cell threshold (m) used to define wave-arrival-on-land. A
#: cell is considered "arrived" the first time its overland depth exceeds this
#: tolerance; the recorded time becomes ``arrival_time_s`` on the depth layer.
#: Demo default 0.01 m (1 cm) - a conservative, near-dry-front threshold.
GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M: float = 0.01


class GeoClawRunArgs(GraceModel):
    """Forcing + scenario parameters for a GeoClaw (Clawpack) shallow-water run.

    Returned/assembled by the GeoClaw composer after agent-confirmed parameter
    extraction; consumed by the GeoClaw worker/adapter. The agent confirms these
    with the user before submission (confirmation-before-consequence,
    invariant 9).

    Use this when:
        Building the input to a GeoClaw shallow-water inundation run over an AOI
        (tsunami run-up, dam-break / embankment-failure overland flow, or
        shallow-water surge run-up).

    Do NOT use this for:
        Pluvial urban drainage (that is ``SWMMRunArgs``), surface-water riverine/
        coastal rain-driven flooding (that is SFINCS ``ModelSetup``), or
        groundwater contamination (that is ``MODFLOWRunArgs``); nor for carrying
        solver output (that is ``GeoClawDepthLayerURI``).

    Fields:
        schema_version: contract version pin (additive growth only).
        bbox: the computational-domain AOI as ``(min_lon, min_lat, max_lon,
            max_lat)`` EPSG:4326. The engine fetches the topo/bathy DEM within it
            and builds the GeoClaw domain. GeoClaw's AMR refines dynamically; a
            larger domain at a coarse base grid is the canonical pattern.
        scenario: the driver family, EXACTLY one of {"dam_break", "tsunami",
            "surge"} (EXPLICIT parameter, never hardcoded). See module docstring.
        sim_duration_s: simulated physical time, seconds (> 0). The solve runs
            from t=0 to this; ``output_frames`` evenly-spaced fort.q dumps are
            written across it.
        dam_break_depth_m: for ``scenario="dam_break"`` ONLY — the height (m) of
            the raised water column / impoundment released at t=0 over dry topo
            (the qinit perturbation amplitude). Ignored for other scenarios.
        source_lonlat: OPTIONAL ``(lon, lat)`` of the driver source — the dam /
            reservoir centroid (dam_break), the tsunami epicentre (tsunami), or
            the surge-forcing reference point (surge). When ``None`` the engine
            uses the AOI centroid.
        tsunami_dtopo_uri: for ``scenario="tsunami"`` ONLY — OPTIONAL URI of a
            prescribed dtopo (seafloor-deformation) file. When ``None`` the
            engine synthesizes an Okada-style source at ``source_lonlat`` from
            ``source_magnitude``.
        source_magnitude: for ``scenario="tsunami"`` synthetic-source mode — the
            moment magnitude (Mw) used to scale the Okada slip. Demo default 8.0.
        surge_forcing_uri: for ``scenario="surge"`` ONLY — OPTIONAL URI of a
            sea-surface-elevation hydrograph CSV (time_series_csv shape, e.g. from
            fetch_gtsm_tide_surge / fetch_noaa_coops_tides) applied at the open
            ocean boundary. When ``None`` a synthetic single-pulse surge is used.
        output_frames: number of evenly-spaced fort.q output frames to write
            (>= 1). Drives the animation frame count (capped at MAX_FLOOD_FRAMES
            downstream). Demo default 24.
        amr_levels: AMR refinement levels (>= 1; 1 = uniform base grid). GeoClaw's
            adaptive mesh refines around the wet front. Demo default 2.
        manning_n: Manning roughness for the shallow-water friction term (> 0).
            GeoClaw default 0.025. Demo value 0.025.
        sea_level_m: the still-water datum (m) GeoClaw initializes the ocean to
            (the ``sea_level`` setrun parameter). Demo default 0.0 (MSL).
        fault_strike_deg: for ``scenario="tsunami"`` synthetic Okada source ONLY
            and USER-GATED - the fault strike angle (deg, [0, 360]). OPTIONAL;
            when ``None`` the engine uses its scenario default and MUST surface
            that it did so (NEVER silently fabricated downstream).
        fault_dip_deg: USER-GATED Okada fault dip angle (deg, (0, 90]). OPTIONAL;
            ``None`` -> engine default, surfaced not silently invented.
        fault_rake_deg: USER-GATED Okada fault rake / slip angle (deg, [-180,
            180]). OPTIONAL; ``None`` -> engine default, surfaced not invented.
        fault_depth_km: USER-GATED Okada fault top/centroid depth (km, > 0).
            OPTIONAL; ``None`` -> engine default, surfaced not invented.
        extra_topo_uris: OPTIONAL list of additional topo/bathy DEM URIs (fine
            coastal DEMs) appended AFTER the primary topo, in coarse->fine order
            (later entries refine earlier ones in GeoClaw's topo stack). Default
            ``[]`` (primary topo only). Additive: empty list preserves behaviour.
        fgmax_arrival_tol_m: the fgmax wet-cell threshold (m, > 0) defining
            wave-arrival-on-land for ``arrival_time_s``: a cell counts as arrived
            the first time its overland depth exceeds this tolerance. Default
            ``GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M`` (0.01 m / 1 cm).
        coastal_gauge_lonlat: OPTIONAL ``(lon, lat)`` of a single coastal gauge
            point at which GeoClaw records a water-level time series (a
            virtual tide gauge). When ``None`` no gauge is placed.
    """

    schema_version: Literal["v1"] = "v1"

    bbox: BBox

    scenario: GeoClawScenario = "dam_break"

    sim_duration_s: float = Field(default=DEFAULT_SIM_DURATION_S, gt=0.0)

    dam_break_depth_m: float = Field(default=DEFAULT_DAM_BREAK_DEPTH_M, gt=0.0)

    source_lonlat: tuple[float, float] | None = None

    tsunami_dtopo_uri: str | None = None
    source_magnitude: float = Field(default=8.0, gt=0.0, le=10.0)

    surge_forcing_uri: str | None = None

    output_frames: int = Field(default=DEFAULT_OUTPUT_FRAMES, ge=1)
    amr_levels: int = Field(default=DEFAULT_AMR_LEVELS, ge=1, le=6)

    manning_n: float = Field(default=0.025, gt=0.0)
    sea_level_m: float = Field(default=0.0)

    # User-gated tsunami fault geometry (Okada synthetic source). None-default;
    # the engine MUST surface any scenario-default substitution, NEVER fabricate
    # these silently downstream.
    fault_strike_deg: float | None = Field(default=None, ge=0.0, le=360.0)
    fault_dip_deg: float | None = Field(default=None, gt=0.0, le=90.0)
    fault_rake_deg: float | None = Field(default=None, ge=-180.0, le=180.0)
    fault_depth_km: float | None = Field(default=None, gt=0.0)

    # Fine coastal DEMs appended coarse->fine after the primary topo.
    extra_topo_uris: list[str] = Field(default_factory=list)
    # fgmax wet-cell threshold (m) defining wave-arrival-on-land.
    fgmax_arrival_tol_m: float = Field(
        default=GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M, gt=0.0
    )
    # Optional single coastal gauge (lon, lat) for a recorded water-level series.
    coastal_gauge_lonlat: tuple[float, float] | None = None

    # Explicit AMR refinement windows (region-based flagging). Each window forces
    # a lat/lon/time box to a min/max AMR level, appended AFTER the engine's
    # default region tiers. Empty -> refinement follows the default flag2refine
    # error estimation only. Additive: an empty list preserves prior behaviour.
    amr_regions: list[AmrRegionWindow] = Field(default_factory=list)

    # Spatially-varying Manning bottom-friction. When set, ``manning_coefficients``
    # is a list of n values for topography bands split by ``manning_break`` (a list
    # of elevation breakpoints, ascending, length = len(coefficients) - 1): a cell
    # with topography B below break[0] uses coefficients[0], between break[k-1] and
    # break[k] uses coefficients[k], and so on (e.g. an offshore n for B < 0 and an
    # onshore n for B >= 0 with a single break at 0.0). When ``None`` the single
    # global ``manning_n`` is used. Additive: None preserves prior behaviour.
    manning_coefficients: list[float] | None = None
    manning_break: list[float] = Field(default_factory=list)

    @field_validator("scenario", mode="before")
    @classmethod
    def _normalize_scenario(cls, value: Any) -> Any:
        """Map common LLM synonyms onto the canonical scenario BEFORE the
        ``Literal`` check (so the FIRST attempt succeeds, no retry loop). An
        unknown string passes through UNCHANGED so a genuinely-invalid value
        still raises the honest ``Literal`` error."""
        if not isinstance(value, str):
            return value
        key = value.strip().lower()
        return _SCENARIO_ALIASES.get(key, key)

    @field_validator("source_lonlat")
    @classmethod
    def _validate_source_lonlat(
        cls, value: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        """Range-check the optional ``(lon, lat)`` source point."""
        if value is None:
            return None
        lon, lat = float(value[0]), float(value[1])
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"source_lonlat lon out of range [-180, 180]: {lon}")
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"source_lonlat lat out of range [-90, 90]: {lat}")
        return (lon, lat)

    @field_validator("manning_coefficients")
    @classmethod
    def _validate_manning_coefficients(
        cls, value: list[float] | None
    ) -> list[float] | None:
        """Every Manning band coefficient must be strictly positive."""
        if value is None:
            return None
        if not value:
            raise ValueError("manning_coefficients, when set, must be non-empty")
        for n in value:
            if float(n) <= 0.0:
                raise ValueError(f"manning coefficient must be > 0, got {n}")
        return [float(n) for n in value]

    @field_validator("manning_break")
    @classmethod
    def _validate_manning_break(cls, value: list[float], info: Any) -> list[float]:
        """``manning_break`` must be ascending and exactly one shorter than
        ``manning_coefficients`` (N coefficients need N-1 elevation breakpoints)."""
        coeffs = info.data.get("manning_coefficients")
        if not value:
            if coeffs is not None and len(coeffs) > 1:
                raise ValueError(
                    f"manning_break must have {len(coeffs) - 1} breakpoint(s) for "
                    f"{len(coeffs)} coefficients, got none"
                )
            return list(value)
        brk = [float(b) for b in value]
        if any(brk[i] >= brk[i + 1] for i in range(len(brk) - 1)):
            raise ValueError(f"manning_break must be strictly ascending, got {brk}")
        if coeffs is None:
            raise ValueError(
                "manning_break requires manning_coefficients to also be set"
            )
        if len(brk) != len(coeffs) - 1:
            raise ValueError(
                f"manning_break length ({len(brk)}) must equal "
                f"len(manning_coefficients) - 1 ({len(coeffs) - 1})"
            )
        return brk


class GeoClawDepthLayerURI(LayerURI):
    """A ``LayerURI`` for a GeoClaw overland-depth layer, plus narration scalars
    and the echoed driver descriptor.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the structured numbers the agent narrates about the inundation so the
    LLM cites typed fields, never invents them (invariant 1, FR-AS-7):

        max_depth_m: peak overland water depth across the AOI, m (>= 0).
        flooded_area_km2: areal footprint above the wet threshold, km^2 (>= 0).
        max_inundation_m: peak overland depth observed on DRY-land cells (cells
            whose topography is above the still-water datum) - the run-up /
            inundation signal distinct from in-channel/ocean depth (>= 0).
        arrival_time_s: OPTIONAL wave-arrival-on-land time, seconds from t0 (>=
            0), derived from the fgmax grid (the first time overland depth at the
            arrival cell exceeds ``fgmax_arrival_tol_m``). ``None`` when fgmax was
            not run; the agent narrates an arrival time ONLY when this is present.

    And the echoed scenario descriptor so the result is self-describing:

        scenario: the GeoClaw driver family this layer came from
            ({"dam_break", "tsunami", "surge"}).

    ``layer_type`` for a depth layer is typically ``"raster"`` (a depth COG, or a
    time-varying COG sequence for the animation). The base contract's vocabulary
    is inherited unchanged. The depth raster uses the SHARED
    ``continuous_flood_depth`` style preset.
    """

    max_depth_m: float = Field(ge=0.0)
    flooded_area_km2: float = Field(ge=0.0)
    max_inundation_m: float = Field(ge=0.0)

    # Wave-arrival-on-land time (s from t0), from the fgmax grid. None when
    # fgmax was not run (the agent narrates an arrival time only when present).
    arrival_time_s: float | None = Field(default=None, ge=0.0)

    scenario: GeoClawScenario = "dam_break"

    # Coastal-gauge time-series scalars (the tsunami gauge-timeseries template).
    # Populated only when the run recorded + parsed a coastal gauge; None on the
    # plain inundation path. Additive: absence preserves prior behaviour.
    #   gauge_max_surface_elevation_m: peak water-surface elevation at the gauge, m.
    #   gauge_min_surface_elevation_m: trough water-surface elevation, m (leading
    #       depression / drawdown).
    #   gauge_max_amplitude_m: peak-to-trough surface amplitude, m (>= 0).
    #   gauge_coseismic_offset_m: initial (t0, post-quake) surface elevation at the
    #       gauge, m -- the co-seismic subsidence/uplift offset where present.
    #   gauge_max_depth_m: peak water depth at the gauge, m (>= 0).
    gauge_max_surface_elevation_m: float | None = Field(default=None)
    gauge_min_surface_elevation_m: float | None = Field(default=None)
    gauge_max_amplitude_m: float | None = Field(default=None, ge=0.0)
    gauge_coseismic_offset_m: float | None = Field(default=None)
    gauge_max_depth_m: float | None = Field(default=None, ge=0.0)

    # Provenance of the driver source (dam-break: real NID dam vs user-supplied
    # location/height). Set by the composer/tool so the agent narrates WHERE the
    # dam geometry came from instead of presenting an invented centroid/height as
    # site-specific. None on paths that do not resolve a named source (e.g. a
    # prescribed dtopo). Additive: absence preserves prior behaviour.
    source_note: str | None = Field(default=None)
