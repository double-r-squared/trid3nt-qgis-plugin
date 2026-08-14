"""ELMFIRE wildfire-spread engine contracts (FIRE-3).

The ELMFIRE analogue of ``geoclaw_contracts.py`` / ``swmm_contracts.py``.
ELMFIRE (Eulerian Level set Model of FIRE spread, Lautenberger 2013) is a
headless Fortran level-set fire-front solver that consumes the LANDFIRE 30 m
fuels stack + a projected same-grid input deck (FIRE-2 deck builder,
``services/workers/elmfire/deck_builder.py``) and emits per-run GeoTIFF/BIL
rasters: time of arrival, fireline intensity, spread rate, flame length. See
``reports/design/elmfire-engine-2026-07-07.md`` + the FIRE-1 container proof
(``reports/inflight/fire-1-container-proof.md``: image ``trid3nt/elmfire:dev``,
release 2025.0526).

Two shapes back the fire-spread path:

- ``ElmfireRunArgs`` — the scenario parameters the agent confirms with the
  user before dispatching a run (Invariant 9 confirmation-before-consequence
  via the server solver-confirm gate). Consumed by
  ``workflows/model_fire_spread_scenario.py`` which drives the FIRE-2 deck
  builder + the generic ``run_solver('elmfire')`` seam.
- ``FireSpreadLayerURI`` — the postprocess output layer. Extends ``LayerURI``
  field-for-field (so it maps onto ``map-command load-layer`` with no
  translation, like every other layer) and adds the typed narration scalars
  (Invariant 1: the agent narrates ``burned_area_km2`` etc. from these fields,
  never invents them).

FUEL-MOISTURE PRESET MAPPING (documented, the single source of truth)
=====================================================================
ELMFIRE takes dead-fuel moistures as three constant rasters (M1/M10/M100,
percent) plus live herbaceous/woody moisture contents (LH/LW, percent). The
v1 scenario surface exposes them as a THREE-VALUE PRESET dial instead of five
raw numbers (the design doc's "scenario-constant weather" v1 mode):

    preset       m1   m10  m100   lh    lw    reads as
    --------   ----  ----  ----  ----  ----   -----------------------------
    "dry"       3.0   4.0   5.0  30.0  60.0   critical fire weather — the
                                              canonical ELMFIRE tutorial
                                              01/03 scenario constants
                                              (design doc section 1.2)
    "moderate"  6.0   7.0   8.0  60.0  90.0   an average mid-season day
    "moist"    12.0  13.0  14.0  90.0 120.0   marginal burning conditions
                                              (recent rain / high RH); spread
                                              is slow-to-none in most fuels

The M1/M10/M100 ladder always ascends (larger fuels equilibrate slower, so a
constant-scenario snapshot carries m1 <= m10 <= m100), mirroring the NFDRS
convention and the ELMFIRE tutorial values. gridMET/HRRR-derived transient
moisture is the documented v2 path — never silently substituted here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import BBox, GraceModel
from .execution import LayerURI

__all__ = [
    "FuelMoisturePreset",
    "FUEL_MOISTURE_PRESETS",
    "DEFAULT_FIRE_DURATION_HOURS",
    "DEFAULT_FIRE_WIND_SPEED_MPH",
    "DEFAULT_FIRE_WIND_DIR_DEG",
    "DEFAULT_FIRE_CELLSIZE_M",
    "ELMFIRE_TOA_STYLE_PRESET",
    "ELMFIRE_FLAME_LEN_STYLE_PRESET",
    "ELMFIRE_SPREAD_RATE_STYLE_PRESET",
    "ElmfireRunArgs",
    "FireSpreadLayerURI",
    "ElmfireEllipseVerificationLayerURI",
    "ElmfireCrownRosVerificationLayerURI",
    "ElmfireSensitivityLayerURI",
]

#: The three-value dead/live fuel-moisture scenario dial (see module docstring
#: for the full mapping + rationale).
FuelMoisturePreset = Literal["dry", "moderate", "moist"]

#: preset -> {"m1_pct", "m10_pct", "m100_pct", "lh_pct", "lw_pct"}. The deck
#: builder consumes these EXACT keys (``weather`` block of the FIRE-2 deck
#: spec). "dry" == the ELMFIRE tutorial 01/03 canonical constants.
FUEL_MOISTURE_PRESETS: dict[str, dict[str, float]] = {
    "dry": {
        "m1_pct": 3.0, "m10_pct": 4.0, "m100_pct": 5.0,
        "lh_pct": 30.0, "lw_pct": 60.0,
    },
    "moderate": {
        "m1_pct": 6.0, "m10_pct": 7.0, "m100_pct": 8.0,
        "lh_pct": 60.0, "lw_pct": 90.0,
    },
    "moist": {
        "m1_pct": 12.0, "m10_pct": 13.0, "m100_pct": 14.0,
        "lh_pct": 90.0, "lw_pct": 120.0,
    },
}

#: LLM-friendly aliases for the preset (mirrors the GeoClaw scenario-alias
#: pattern: normalize BEFORE the Literal check so the first attempt succeeds;
#: an unknown string passes through unchanged and raises the honest error).
_PRESET_ALIASES: dict[str, str] = {
    "very_dry": "dry",
    "critical": "dry",
    "extreme": "dry",
    "low": "dry",
    "normal": "moderate",
    "average": "moderate",
    "medium": "moderate",
    "wet": "moist",
    "damp": "moist",
    "high": "moist",
}

# Scenario defaults (narrated as demo/scenario values, never site-calibrated).
DEFAULT_FIRE_DURATION_HOURS: float = 6.0  # ~ tutorial-01's 22,200 s burn
DEFAULT_FIRE_WIND_SPEED_MPH: float = 15.0  # tutorial-01 constant wind
DEFAULT_FIRE_WIND_DIR_DEG: float = 0.0  # direction wind blows FROM (met deg)
DEFAULT_FIRE_CELLSIZE_M: float = 30.0  # LANDFIRE native

#: publish_layer style presets for the three fire raster families. Registered
#: in ``publish_layer._TITILER_STYLE_REGISTRY`` (additive entries).
ELMFIRE_TOA_STYLE_PRESET: str = "continuous_fire_arrival_hr"
ELMFIRE_FLAME_LEN_STYLE_PRESET: str = "continuous_flame_length_m"
ELMFIRE_SPREAD_RATE_STYLE_PRESET: str = "continuous_fire_spread_rate"


#: Half-width, degrees, of the domain derived around a bare ignition point
#: when the model omits the bbox (~5 km at temperate latitudes).
DEFAULT_FIRE_DOMAIN_HALFWIDTH_DEG = 0.045


class ElmfireRunArgs(GraceModel):
    """Scenario parameters for one deterministic ELMFIRE fire-spread run.

    Assembled by the ``model_fire_spread`` tool after user confirmation
    (Invariant 9 — the server solver-confirm gate shows cell count + estimated
    runtime before dispatch); consumed by
    ``workflows/model_fire_spread_scenario.py``.

    Fields:
        schema_version: contract version pin (additive growth only).
        bbox: the computational-domain AOI ``(min_lon, min_lat, max_lon,
            max_lat)`` EPSG:4326. CONUS-only at v1 (LANDFIRE coverage — the
            fuels fetch fails typed outside CONUS, never hallucinated fuels).
        ignition_lonlat: REQUIRED ``(lon, lat)`` point ignition, EPSG:4326.
            Comes from the user (a map pick via ``request_spatial_input``
            ``mode="point"`` or a stated coordinate) — NEVER fabricated. Must
            fall inside ``bbox`` (deck-builder in-domain assert).
        wind_speed_mph: constant scenario wind speed, **mph at 20 ft**
            (ELMFIRE's native convention — the design doc units trap; a 10 m
            m/s value must be converted via
            ``deck_builder.wind_10m_ms_to_20ft_mph`` BEFORE landing here).
        wind_dir_deg: constant wind direction, meteorological degrees (the
            direction the wind blows FROM; 0 = northerly wind pushing the fire
            south, 270 = westerly pushing it east). Range [0, 360].
        fuel_moisture: the dead/live fuel-moisture preset dial — one of
            {"dry", "moderate", "moist"} (see the module-docstring mapping).
        duration_hours: simulated burn duration, hours (> 0, <= 48 at v1 —
            constant-weather realism degrades beyond ~2 days).
        cellsize_m: computational cell size, metres. 30 (LANDFIRE native) is
            the canon; coarser values are the cost lever for big AOIs.
    """

    schema_version: Literal["v1"] = "v1"

    bbox: BBox | None = None
    ignition_lonlat: tuple[float, float]

    wind_speed_mph: float = Field(
        default=DEFAULT_FIRE_WIND_SPEED_MPH, ge=0.0, le=120.0
    )
    wind_dir_deg: float = Field(default=DEFAULT_FIRE_WIND_DIR_DEG, ge=0.0, le=360.0)
    fuel_moisture: FuelMoisturePreset = "dry"
    duration_hours: float = Field(
        default=DEFAULT_FIRE_DURATION_HOURS, gt=0.0, le=48.0
    )
    cellsize_m: float = Field(default=DEFAULT_FIRE_CELLSIZE_M, ge=10.0, le=300.0)

    @field_validator("fuel_moisture", mode="before")
    @classmethod
    def _normalize_preset(cls, value: Any) -> Any:
        """Map common LLM synonyms onto the canonical preset BEFORE the
        Literal check. Unknown strings pass through unchanged so a genuinely
        invalid value still raises the honest Literal error."""
        if not isinstance(value, str):
            return value
        key = value.strip().lower()
        return _PRESET_ALIASES.get(key, key)

    @field_validator("ignition_lonlat", mode="before")
    @classmethod
    def _coerce_ignition_shape(cls, value: Any) -> Any:
        """Small local models pass "lon,lat" strings or {lon, lat} dicts for
        the ignition point (observed live 2026-07-08). Coerce the common
        shapes BEFORE the tuple check; anything unparseable passes through so
        the honest type error still fires."""
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",")]
            if len(parts) == 2:
                try:
                    return (float(parts[0]), float(parts[1]))
                except ValueError:
                    return value
        if isinstance(value, dict):
            lon = value.get("lon", value.get("longitude"))
            lat = value.get("lat", value.get("latitude"))
            if lon is not None and lat is not None:
                try:
                    return (float(lon), float(lat))
                except (TypeError, ValueError):
                    return value
        return value

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox_shape(cls, value: Any) -> Any:
        """Accept "a,b,c,d" strings; treat a 2-element point (the model
        conflating bbox with the ignition, observed live) or an empty value
        as ABSENT so the model_post_init default derives the domain from the
        ignition point instead of failing the run."""
        if isinstance(value, str):
            cleaned = value.replace(";", ",").strip().strip("[](){}")
            parts = [p.strip().strip("[](){}") for p in cleaned.split(",")]
            try:
                nums = [float(p) for p in parts if p]
            except ValueError:
                return value
            if len(nums) == 4:
                return cls._reorder_or_none(nums)
            if len(nums) <= 2:
                return None
        if isinstance(value, (list, tuple)):
            if len(value) <= 2:
                return None
            if len(value) == 4:
                try:
                    return cls._reorder_or_none([float(v) for v in value])
                except (TypeError, ValueError):
                    return value
        return value

    @staticmethod
    def _reorder_or_none(nums: list[float]) -> tuple[float, ...] | None:
        """Accept the canonical (min_lon, min_lat, max_lon, max_lat); repair
        the lon,lon,lat,lat ordering small models emit (observed live
        2026-07-08); anything else incoherent -> None so the domain derives
        from the ignition point (the gate card shows the final domain)."""

        def is_lat(v: float) -> bool:
            return -90.0 <= v <= 90.0

        def is_lon(v: float) -> bool:
            return -180.0 <= v <= 180.0

        a, b, c, d = nums
        if is_lon(a) and is_lat(b) and is_lon(c) and is_lat(d) and a < c and b < d:
            return (a, b, c, d)  # canonical
        if is_lon(a) and is_lon(b) and is_lat(c) and is_lat(d) and a < b and c < d:
            return (a, c, b, d)  # lon,lon,lat,lat -> reorder
        return None

    def model_post_init(self, __context: Any) -> None:
        """Default the computational domain to ~5 km around the ignition when
        the model omitted (or point-collapsed) the bbox - the sensible-default
        norm; the gate card still shows the derived domain for user sign-off."""
        if self.bbox is None:
            lon, lat = self.ignition_lonlat
            d = DEFAULT_FIRE_DOMAIN_HALFWIDTH_DEG
            object.__setattr__(
                self, "bbox", (lon - d, lat - d, lon + d, lat + d)
            )

    @field_validator("ignition_lonlat")
    @classmethod
    def _validate_ignition(
        cls, value: tuple[float, float]
    ) -> tuple[float, float]:
        lon, lat = float(value[0]), float(value[1])
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"ignition lon out of range [-180, 180]: {lon}")
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"ignition lat out of range [-90, 90]: {lat}")
        return (lon, lat)

    def fuel_moisture_values(self) -> dict[str, float]:
        """The concrete m1/m10/m100/lh/lw percentages for the chosen preset."""
        return dict(FUEL_MOISTURE_PRESETS[self.fuel_moisture])


class FireSpreadLayerURI(LayerURI):
    """A ``LayerURI`` for an ELMFIRE fire-spread raster, plus narration scalars.

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command
    load-layer`` unchanged. Adds the structured numbers the agent narrates
    (Invariant 1 / FR-AS-7 — typed fields, never free-generated):

        burned_area_km2: areal footprint the fire reached within the sim
            window, km^2 (>= 0). Computed by counting valid time-of-arrival
            cells on the SOURCE (projected, cellsize-known) grid.
        fire_arrival_max_hr: the latest time-of-arrival observed, hours from
            ignition (>= 0) — i.e. how long the front kept advancing.
        max_flame_length_m: peak flame length, metres (ELMFIRE emits feet;
            converted once in postprocess). ``None`` when the flame-length
            raster was not produced (narrate nothing, never a guess).
        max_spread_rate_m_min: peak spread rate, metres/minute (ELMFIRE emits
            ft/min; converted once in postprocess). ``None`` when absent.
        duration_hours: the simulated burn duration this layer covers.
        ignition_lonlat: the echoed ignition point so the result is
            self-describing.
    """

    burned_area_km2: float = Field(ge=0.0)
    fire_arrival_max_hr: float = Field(ge=0.0)
    max_flame_length_m: float | None = Field(default=None, ge=0.0)
    max_spread_rate_m_min: float | None = Field(default=None, ge=0.0)
    duration_hours: float = Field(gt=0.0)
    ignition_lonlat: tuple[float, float] | None = None


class ElmfireEllipseVerificationLayerURI(FireSpreadLayerURI):
    """The ELMFIRE constant-wind flat-terrain elliptical-VERIFICATION result.

    Extends ``FireSpreadLayerURI`` (the time-of-arrival raster is the primary
    layer) with the verification triple + Richards-ellipse geometry the agent
    narrates about the calibration check (Invariant 1 -- typed fields, never
    free-generated). Under constant fuel + uniform wind + flat terrain the fire
    perimeter from a point ignition is a closed-form ellipse; these fields report
    how well the numerical level-set perimeter matches it:

        rmse_m: RMSE of the numerical perimeter from the Richards ellipse, m.
        err_fraction: RMSE / ellipse semi-major axis, dimensionless (>= 0).
        correlation: perimeter-vs-ellipse radial correlation, in [-1, 1].
        corr_class: the graded quality ("excellent"/"good"/"fair"/"poor").
        length_to_width_ratio: the observed ellipse length:width ratio (>= 1).
        tolerance: the fractional pass tolerance err_fraction was gated on.
        passed: err_fraction <= tolerance AND the perimeter did not touch the
            domain edge.
    """

    rmse_m: float = Field(ge=0.0)
    err_fraction: float = Field(ge=0.0)
    correlation: float = Field(ge=-1.0, le=1.0)
    corr_class: str
    length_to_width_ratio: float = Field(ge=0.0)
    tolerance: float = Field(ge=0.0)
    passed: bool


class ElmfireCrownRosVerificationLayerURI(FireSpreadLayerURI):
    """The ELMFIRE ACTIVE-crown-fire rate-of-spread VERIFICATION result.

    Extends ``FireSpreadLayerURI`` (the time-of-arrival raster is the primary
    layer) with the Cruz-et-al.-(2005) closed-form crown-fire-ROS check
    (Invariant 1 -- typed fields, never free-generated). On an UNCAPPED,
    fully-active crown deck (constant fuel + uniform wind + flat terrain + a
    uniform canopy) the numerical level-set head fire spreads at the Cruz active
    crown-fire rate of spread; these fields report how closely the numerical head
    ROS reproduces that closed form:

        numerical_ros_m_min: the numerical HEAD rate of spread (head extent /
            duration), metres/minute (>= 0).
        cruz_ros_m_min: the Cruz (2005) active-crown closed-form ROS evaluated at
            the deck's own inputs, metres/minute (>= 0).
        rel_error: |numerical - cruz| / cruz, dimensionless (>= 0).
        tolerance: the fractional pass tolerance rel_error was gated on.
        passed: rel_error <= tolerance AND the perimeter did not touch the domain
            edge (an edge-touching front truncates the head extent -> invalid).
        wind_speed_mph / cbd_kg_m3 / effm_pct: the echoed closed-form inputs (the
            20-ft wind, canopy bulk density, and fine dead-fuel moisture) so the
            verification is self-describing.
    """

    numerical_ros_m_min: float = Field(ge=0.0)
    cruz_ros_m_min: float = Field(ge=0.0)
    rel_error: float = Field(ge=0.0)
    tolerance: float = Field(ge=0.0)
    passed: bool
    wind_speed_mph: float = Field(ge=0.0)
    cbd_kg_m3: float = Field(ge=0.0)
    effm_pct: float = Field(ge=0.0)


class ElmfireSensitivityLayerURI(FireSpreadLayerURI):
    """A one-parameter ELMFIRE sensitivity SWEEP result (constant flat deck).

    Extends ``FireSpreadLayerURI`` (the primary layer is the time-of-arrival
    raster of the sweep's representative -- most-elongated / most-spread -- run)
    with the swept response the agent narrates (Invariant 1 -- typed fields, never
    free-generated). One knob is swept across ``sweep`` points on an otherwise
    ALL-CONSTANT flat deck (no LANDFIRE/DEM fetch), isolating that knob's effect:

        swept_param: the knob name swept (e.g. "wind_speed_mph",
            "live_herbaceous_moisture_pct", "wind_fluctuation_member").
        swept_units: the swept knob's units (e.g. "mph", "percent", "member").
        response_metric: the measured response (e.g. "length_to_width_ratio",
            "burned_area_km2").
        response_units: the response units (e.g. "ratio", "km2", "m/min").
        sweep: the per-point ``[{"x": <knob value>, "y": <response>, ...}, ...]``
            list (ascending in ``x``) backing the sensitivity chart -- each point
            is one solved constant-deck run.
        summary: template-specific derived scalars (e.g. the binding wind of a
            length:width ceiling, the ensemble mean/spread of a wind-fluctuation
            sweep) -- each a plain float the agent may narrate.
    """

    swept_param: str
    swept_units: str
    response_metric: str
    response_units: str
    sweep: list[dict[str, float]] = Field(default_factory=list)
    summary: dict[str, float] = Field(default_factory=dict)
