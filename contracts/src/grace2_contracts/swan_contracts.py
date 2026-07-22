"""SWAN (Simulating WAves Nearshore) spectral wave engine contracts (Phase 1).

The SWAN analogue of ``geoclaw_contracts.py`` / ``modflow_contracts.py``. SWAN
(TU Delft, GPL-3.0, Fortran-90, headless) is a third-generation spectral
nearshore wind-wave model. It is ADDED as an ADDITIVE comparison engine: a user
can run SWAN STANDALONE over a coastal AOI and get its OWN engineering-grade wave
field (significant wave height Hs, peak period Tp, mean direction Dir) so they can
compare SWAN against the existing SFINCS+SnapWave output on the SAME case.

This is NOT a pivot away from SFINCS and NOT a coupling job in v0.1: SWAN runs on
its own, produces Hs/Tp/Dir COG layers over the AOI, and the map paints the
incoming wave field directly. There is NO ``wave`` member added to the SFINCS
surge-forcing seam here (the engine spike marks SWAN->SFINCS wave-setup coupling a
LATER step); see the clearly-commented LATER-step seam in ``run_swan.py``.

Two shapes back the SWAN wave-field path:

- ``SwanRunArgs`` / ``SwanBuildSpec`` -- the grid + boundary-forcing parameters the
  agent confirms with the user before submitting a SWAN run. ``SwanRunArgs`` is the
  agent-facing run-args contract (the ``GeoClawRunArgs`` analogue); ``SwanBuildSpec``
  is the flat build_spec dict the deterministic ``.swn`` deck author consumes (the
  ``GeoClawBuildSpec`` analogue, authored agent-side by ``workflows/run_swan.py``).
- ``WaveFieldLayerURI`` -- the postprocess output layer. Extends ``LayerURI``
  field-for-field (so it still maps onto ``map-command load-layer`` with no
  translation, like every other layer) and adds the wave scalars the agent
  narrates (determinism boundary, Invariant 1 / FR-AS-7): the agent narrates
  ``max_hs_m`` / ``mean_tp_s`` / ``mean_dir_deg`` / ``wave_area_km2`` from these
  typed fields rather than inventing them.

Design notes
------------
- ``bbox`` is the project ``BBox`` convention: ``(min_lon, min_lat, max_lon,
  max_lat)`` in EPSG:4326 (lon-first), range-validated by the shared ``BBox``
  type. The SWAN AOI is an *area* (the computational domain), so it is a bbox.
- ``mode`` is an EXPLICIT PARAMETER, never silently hardcoded. ``"stationary"``
  (default) computes a storm-PEAK wave field (the fast standalone demo);
  ``"nonstationary"`` evolves a wave time-series (a hurricane wave evolution).
- The boundary forcing is PARAMETRIC for v0.1 (Hs / Tp / mean-direction / spread
  along the offshore side -- a ``BOUNDSPEC ... PAR`` block). True 2D nested spectra
  (``BOUNDNEST3`` from WAVEWATCH III) are a later, larger data dependency and are
  NOT modeled here.
- ``WaveFieldLayerURI`` REUSES the SHARED ``continuous_wave_height`` style preset
  (SWAN Hs is the same physical quantity SnapWave's ``hm0`` emits), so NO new
  publish_layer style key is required -- exactly how ``GeoClawDepthLayerURI``
  reuses ``continuous_flood_depth``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import BBox, GraceModel
from .execution import LayerURI

__all__ = [
    "SwanMode",
    "DEFAULT_SIM_DURATION_S",
    "DEFAULT_TIME_STEP_S",
    "DEFAULT_N_DIR",
    "DEFAULT_N_FREQ",
    "DEFAULT_FREQ_LOW_HZ",
    "DEFAULT_FREQ_HIGH_HZ",
    "DEFAULT_BOUNDARY_HS_M",
    "DEFAULT_BOUNDARY_TP_S",
    "DEFAULT_BOUNDARY_DIR_DEG",
    "DEFAULT_BOUNDARY_SPREAD_DEG",
    "DEFAULT_OUTPUT_FRAMES",
    "SWAN_OUTPUT_QUANTITIES",
    "SwanRunArgs",
    "SwanWaveBoundary",
    "WaveFieldLayerURI",
    "SWAN_WAVE_HEIGHT_STYLE_PRESET",
]

#: SWAN run mode. ``stationary`` solves a single storm-PEAK wave field (the fast
#: standalone demo path); ``nonstationary`` evolves a wave time-series. Open
#: ``Literal`` so the engine may add a mode without a wire break.
SwanMode = Literal["stationary", "nonstationary"]

#: LLM-friendly aliases for ``mode`` (the agent invents synonyms that would fail
#: the bare ``Literal`` and trigger a self-correcting retry). Normalize on the
#: FIRST attempt; an unknown string passes through unchanged so a genuinely-invalid
#: value still raises the honest Literal error. Mirrors GeoClaw's _SCENARIO_ALIASES.
_MODE_ALIASES: dict[str, str] = {
    "stat": "stationary",
    "steady": "stationary",
    "steady_state": "stationary",
    "static": "stationary",
    "peak": "stationary",
    "snapshot": "stationary",
    "nonstat": "nonstationary",
    "non_stationary": "nonstationary",
    "non-stationary": "nonstationary",
    "unsteady": "nonstationary",
    "transient": "nonstationary",
    "timeseries": "nonstationary",
    "time_series": "nonstationary",
    "evolution": "nonstationary",
}

# TENTATIVE SWAN demo defaults (Phase 1; narrated as demo values, not
# site-calibrated parameters, by the composer).
DEFAULT_SIM_DURATION_S: float = 10800.0  # nonstationary physical time, s (3 h)
DEFAULT_TIME_STEP_S: float = 600.0  # nonstationary compute step, s (10 min)

# Spectral grid (>=3 directional bins per quadrant -> >=12 dirs full circle;
# >=4 frequency bins). The defaults are a standard nearshore resolution.
DEFAULT_N_DIR: int = 36  # directional bins over the full 360 deg circle
DEFAULT_N_FREQ: int = 32  # frequency bins (logarithmic between flow and fhigh)
DEFAULT_FREQ_LOW_HZ: float = 0.04  # lowest relative frequency, Hz (25 s period)
DEFAULT_FREQ_HIGH_HZ: float = 1.0  # highest relative frequency, Hz (1 s period)

# Parametric offshore boundary defaults (a moderate storm sea-state). Hs / Tp /
# mean-direction / directional spread along the offshore side.
DEFAULT_BOUNDARY_HS_M: float = 3.0  # significant wave height at the boundary, m
DEFAULT_BOUNDARY_TP_S: float = 9.0  # peak wave period at the boundary, s
DEFAULT_BOUNDARY_DIR_DEG: float = 180.0  # mean wave direction (nautical), deg
DEFAULT_BOUNDARY_SPREAD_DEG: float = 25.0  # directional spread, deg

DEFAULT_OUTPUT_FRAMES: int = 24  # evenly-spaced nonstationary output frames

#: The gridded SWAN output quantities the postprocess rasterizes. HSIGN = Hs
#: (significant wave height, m); RTP = relative peak period (s); DIR = mean wave
#: direction (deg, nautical). These are the three the agent narrates + paints.
SWAN_OUTPUT_QUANTITIES: tuple[str, ...] = ("HSIGN", "RTP", "DIR")

#: Shared wave-height style preset (SWAN Hs == SnapWave hm0 physically), so NO
#: new publish_layer style key is added. Single source of truth here; resolves to
#: the cyan/blue ``continuous_wave_height`` ramp in ``publish_layer``.
SWAN_WAVE_HEIGHT_STYLE_PRESET: str = "continuous_wave_height"


class SwanWaveBoundary(GraceModel):
    """The PARAMETRIC offshore wave boundary forcing for a SWAN run.

    The Hs / Tp / mean-direction / spread quadruple SWAN imposes along the
    offshore boundary side via a ``BOUND SHAPE JONSWAP`` + ``BOUNDSPEC SIDE ...
    PAR`` block (the LLM-easy boundary path; true 2D nested spectra are a later
    fetcher, not modeled here). When ``None`` is passed at the run-args level the
    composer synthesizes a demo boundary from these defaults.

    Fields:
        hs_m: significant wave height at the boundary, m (> 0).
        tp_s: peak wave period at the boundary, s (> 0).
        dir_deg: mean wave direction at the boundary, degrees (nautical
            convention, direction FROM which waves travel), in [0, 360).
        spread_deg: directional spreading (one-sided width), degrees (> 0).
        side: the SWAN boundary side the forcing is imposed on -- one of
            {"N", "S", "E", "W"}. Defaults to "S" (the typical offshore-facing
            seaward side for a US Gulf/Atlantic AOI; the composer may override it
            from AOI geometry).
    """

    hs_m: float = Field(default=DEFAULT_BOUNDARY_HS_M, gt=0.0)
    tp_s: float = Field(default=DEFAULT_BOUNDARY_TP_S, gt=0.0)
    dir_deg: float = Field(default=DEFAULT_BOUNDARY_DIR_DEG, ge=0.0, lt=360.0)
    spread_deg: float = Field(default=DEFAULT_BOUNDARY_SPREAD_DEG, gt=0.0)
    side: Literal["N", "S", "E", "W"] = "S"


class SwanRunArgs(GraceModel):
    """Grid + boundary-forcing parameters for a standalone SWAN wave-field run.

    Returned/assembled by the SWAN composer after agent-confirmed parameter
    extraction; consumed by the SWAN worker/deck-author. The agent confirms these
    with the user before submission (confirmation-before-consequence,
    invariant 9).

    Use this when:
        Building the input to a STANDALONE SWAN nearshore wave-field run over an
        AOI -- the defensible engineering-grade wave climate (Hs / Tp / Dir) a
        user wants to COMPARE against SFINCS+SnapWave on the same case.

    Do NOT use this for:
        Compound-flood inundation depth (that is SFINCS ``ModelSetup`` /
        ``run_model_flood_scenario``, which already carries the FAST in-model
        SnapWave wave-setup path), shallow-water tsunami / dam-break run-up (that
        is GeoClaw ``GeoClawRunArgs``), or urban drainage (that is SWMM); nor for
        carrying solver output (that is ``WaveFieldLayerURI``). SWAN here is the
        higher-fidelity standalone wave FIELD, not a cheaper compound-flood solver.

    Fields:
        schema_version: contract version pin (additive growth only).
        bbox: the computational-domain AOI as ``(min_lon, min_lat, max_lon,
            max_lat)`` EPSG:4326. The engine fetches the topo/bathy DEM within it
            and builds the SWAN computational + bottom input grid.
        mode: the run mode, EXACTLY one of {"stationary", "nonstationary"}
            (EXPLICIT parameter, never hardcoded). ``"stationary"`` (DEFAULT)
            solves a storm-PEAK wave field; ``"nonstationary"`` evolves a wave
            time-series. Synonyms (e.g. "peak" -> stationary, "transient" ->
            nonstationary) are normalized.
        boundary: the PARAMETRIC offshore wave boundary (Hs / Tp / dir / spread /
            side). When ``None`` the composer synthesizes a demo boundary.
        wind_uri: OPTIONAL ``s3://`` URI of an ERA5 10 m wind input grid. When set
            the deck enables ``GEN3`` wind-sea growth (``INPGRID/READINP WIND``);
            when ``None`` the run is boundary-forced only (no wind generation).
        n_dir: spectral directional bins over the full circle (>= 12, i.e. >= 3
            per quadrant). Demo default 36.
        n_freq: spectral frequency bins (>= 4). Demo default 32.
        freq_low_hz: lowest relative frequency, Hz (> 0). Demo default 0.04.
        freq_high_hz: highest relative frequency, Hz (> freq_low_hz). Demo 1.0.
        sim_duration_s: nonstationary physical time, seconds (> 0). Ignored in
            stationary mode. Demo default 10800 (3 h).
        time_step_s: nonstationary compute time-step, seconds (> 0). Ignored in
            stationary mode. Demo default 600 (10 min).
        output_frames: number of evenly-spaced nonstationary output frames
            (>= 1). Drives the animation frame count (capped downstream). Demo 24.
        friction: enable JONSWAP bottom friction (depth-induced). Demo True.
        breaking: enable depth-induced breaking. Demo True.
        triads: enable triad (three-wave) nonlinear interactions (shallow water).
            Demo True.
        compute_class: FR-CE-3 compute class hint. Default ``"standard"``.
    """

    schema_version: Literal["v1"] = "v1"

    bbox: BBox

    mode: SwanMode = "stationary"

    boundary: SwanWaveBoundary | None = None

    wind_uri: str | None = None

    n_dir: int = Field(default=DEFAULT_N_DIR, ge=12, le=360)
    n_freq: int = Field(default=DEFAULT_N_FREQ, ge=4, le=128)
    freq_low_hz: float = Field(default=DEFAULT_FREQ_LOW_HZ, gt=0.0)
    freq_high_hz: float = Field(default=DEFAULT_FREQ_HIGH_HZ, gt=0.0)

    sim_duration_s: float = Field(default=DEFAULT_SIM_DURATION_S, gt=0.0)
    time_step_s: float = Field(default=DEFAULT_TIME_STEP_S, gt=0.0)
    output_frames: int = Field(default=DEFAULT_OUTPUT_FRAMES, ge=1)

    friction: bool = True
    breaking: bool = True
    triads: bool = True

    compute_class: str = "standard"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> Any:
        """Map common LLM synonyms onto the canonical mode BEFORE the ``Literal``
        check (so the FIRST attempt succeeds, no retry loop). An unknown string
        passes through UNCHANGED so a genuinely-invalid value still raises the
        honest ``Literal`` error."""
        if not isinstance(value, str):
            return value
        key = value.strip().lower()
        return _MODE_ALIASES.get(key, key)

    @field_validator("freq_high_hz")
    @classmethod
    def _validate_freq_band(cls, value: float, info: Any) -> float:
        """The high frequency must exceed the low frequency (a valid spectral
        band). ``freq_low_hz`` is validated first by field order."""
        low = info.data.get("freq_low_hz")
        if low is not None and value <= float(low):
            raise ValueError(
                f"freq_high_hz {value} must exceed freq_low_hz {low}"
            )
        return value


class WaveFieldLayerURI(LayerURI):
    """A ``LayerURI`` for a SWAN wave-field layer, plus narration scalars.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the structured numbers the agent narrates about the wave field so the
    LLM cites typed fields, never invents them (invariant 1, FR-AS-7):

        max_hs_m: peak significant wave height across the AOI, m (>= 0).
        mean_tp_s: mean peak period over the wet (wave-bearing) cells, s (>= 0).
        mean_dir_deg: mean wave direction over the wet cells, degrees nautical
            (the direction FROM which waves travel), in [0, 360).
        wave_area_km2: areal footprint with Hs above the wave threshold, km^2
            (>= 0).

    And the echoed run mode so the result is self-describing:

        mode: the SWAN run mode this layer came from
            ({"stationary", "nonstationary"}).

    ``layer_type`` for a wave-height layer is ``"raster"`` (an Hs COG, or a
    time-varying COG sequence for the nonstationary animation). The Hs raster uses
    the SHARED ``continuous_wave_height`` style preset.
    """

    max_hs_m: float = Field(ge=0.0)
    mean_tp_s: float = Field(ge=0.0)
    mean_dir_deg: float = Field(ge=0.0, lt=360.0)
    wave_area_km2: float = Field(ge=0.0)

    mode: SwanMode = "stationary"
