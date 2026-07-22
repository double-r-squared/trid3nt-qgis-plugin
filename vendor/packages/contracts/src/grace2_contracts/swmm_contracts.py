"""PySWMM quasi-2D urban-flood engine contracts (Path A, sprint-16 P1).

The SWMM analogue of ``modflow_contracts.py``. Two shapes back the urban
North-Star demo path (NATE's PCSWMM screenshot: animated depth around BUILDING
OBSTRUCTIONS + a SOUND BARRIER with RED walls / GREEN flap gates):

- ``SWMMRunArgs`` — the forcing/structure parameters the agent confirms with the
  user before submitting a quasi-2D SWMM run. Consumed by the engine adapter /
  worker (``services/workers/swmm/...``) that maps these onto a quasi-2D SWMM
  deck (one STORAGE node per active cell, 4-connectivity overland CONDUITS, one
  boundary OUTFALL, per-cell rainfall SUBCATCHMENTS fed by a single RAINGAGE +
  the Atlas-14 nested hyetograph TIMESERIES) per the P0 spike
  (``services/workers/swmm/spike_quasi2d.py``).
- ``SWMMDepthLayerURI`` — the postprocess output layer. Extends ``LayerURI``
  field-for-field (so it still maps onto ``map-command load-layer`` with no
  translation, like every other layer) and adds the three depth scalars the
  agent narrates plus the tagged barrier-line geometry it draws.

Design notes
------------
- ``bbox`` is the project ``BBox`` convention: ``(min_lon, min_lat, max_lon,
  max_lat)`` in EPSG:4326 (lon-first), range-validated by the shared ``BBox``
  type. The SWMM AOI is an *area*, not a point (contrast with MODFLOW's
  ``spill_location_latlon`` point), so it is a bbox.
- ``building_representation`` is an EXPLICIT PARAMETER, never silently
  hardcoded (cross-check improvement from the flood-pipeline reference). Default
  ``"drop"`` matches the screenshot (a building = a hole/void cell removed from
  the overland mesh so water routes around it); ``"raise"`` lifts the cell
  invert to dam flow; ``"roughness"`` keeps the cell but bumps Manning n.
- ``infiltration_method`` selects SCS-CN vs Green-Ampt on the PERVIOUS fraction
  (cross-check improvement). ``"none"`` is the fully-impervious spike default.
- The Atlas-14 NESTED (alternating-block) hyetograph is built by
  ``services/agent/src/grace2_agent/workflows/swmm_hyetograph.py`` from
  ``total_rain_depth_mm`` + ``storm_duration_hr`` + ``rain_interval_min``. It is
  NOT flat and NOT SCS-Type-II — these args parameterize the nested builder.
- ``SWMMDepthLayerURI`` is a structured numeric carrier (invariant 1 / Decision
  H / FR-AS-7): the agent narrates ``max_depth_m``, ``flooded_area_km2`` and
  ``n_buildings_affected`` from these typed fields rather than inventing them.
- ``barriers`` is a GeoJSON ``FeatureCollection`` of tagged ``LineString``
  segments (each feature's ``properties.barrier_type`` ∈ {"wall", "flap_gate"})
  so the client draws RED walls / GREEN flap gates over the depth raster. It is
  carried as a plain ``dict`` (the GeoJSON wire form) with a structural
  validator — contracts must not take a geometry-library dependency.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import BBox, EngineRunArgsMixin, GraceModel
from .execution import LayerURI

__all__ = [
    "BuildingRepresentation",
    "InfiltrationMethod",
    "BarrierType",
    "WashoffModel",
    "DEFAULT_RETURN_PERIOD_YR",
    "DEFAULT_STORM_DURATION_HR",
    "DEFAULT_RAIN_INTERVAL_MIN",
    "DEFAULT_TARGET_RESOLUTION_M",
    "DEFAULT_MANNING_OVERLAND",
    "PollutantSpec",
    "POLLUTANT_PRESETS",
    "resolve_pollutant_presets",
    "SWMMRunArgs",
    "SWMMDepthLayerURI",
    "SWMMPollutantLayerURI",
]


# How a building footprint is represented in the quasi-2D overland mesh.
#   "drop"      — remove the cell from the mesh (a hole/void); water routes
#                 AROUND the obstruction (the buildings-as-obstacles behavior).
#                 Matches NATE's PCSWMM screenshot. DEFAULT.
#   "raise"     — keep the cell but lift its invert above grade so it dams flow.
#   "roughness" — keep the cell but bump its Manning n (a soft obstruction).
# Open ``Literal`` so the engine may add representations without a wire break.
BuildingRepresentation = Literal["drop", "raise", "roughness"]

# LLM-friendly aliases for ``building_representation``. The docs describe the
# concept as "BUILDING OBSTRUCTIONS", so the LLM frequently invents synonyms
# ("obstacles", "block", "friction", ...) that fail the bare ``Literal`` and
# trigger a visible self-correcting retry loop. We normalize these synonyms to
# the canonical value on the FIRST attempt; an unknown string passes through
# unchanged so a genuinely-invalid value still raises the honest Literal error.
_BUILDING_REPRESENTATION_ALIASES: dict[str, str] = {
    # "drop" — building cells become holes; water routes around the obstruction.
    "obstacle": "drop",
    "obstacles": "drop",
    "obstruction": "drop",
    "obstructions": "drop",
    "hole": "drop",
    "holes": "drop",
    "remove": "drop",
    # "raise" — building cells dam flow.
    "block": "raise",
    "dam": "raise",
    "wall": "raise",
    # "roughness" — building cells bump Manning n.
    "friction": "roughness",
    "manning": "roughness",
}

# Infiltration on the PERVIOUS fraction of each cell's subcatchment.
#   "none"       — fully impervious (the spike default; all rain runs off).
#   "scs_cn"     — SCS Curve Number loss (fetch_gcn250_curve_numbers).
#   "green_ampt" — Green-Ampt loss (fetch_statsgo_soils -> Ks/suction/IMD).
InfiltrationMethod = Literal["none", "scs_cn", "green_ampt"]

# Tag on each barrier LineString feature.
#   "wall"      — RED wall: an OMITTED overland conduit between two cells.
#   "flap_gate" — GREEN flap gate: a one-way SWMM ORIFICE (has_flap_gate=True).
BarrierType = Literal["wall", "flap_gate"]


# TENTATIVE urban-demo defaults (sprint-16; narrated as demo values, not
# site-calibrated parameters, by the composer).
DEFAULT_RETURN_PERIOD_YR: int = 100  # design-storm return period, years
DEFAULT_STORM_DURATION_HR: float = 6.0  # storm duration, hours (spike used 6 h)
DEFAULT_RAIN_INTERVAL_MIN: int = 5  # hyetograph timestep, minutes
DEFAULT_TARGET_RESOLUTION_M: float = 10.0  # target cell size, m (spike used 10 m)
DEFAULT_MANNING_OVERLAND: float = 0.03  # overland Manning n (spike value)


# --------------------------------------------------------------------------- #
# Water-quality (buildup/washoff) — the urban engine's SECOND output family
# (sprint-WQ). Rides SWMMRunArgs.pollutants as OPTIONAL fields; when unset the
# deck is BYTE-IDENTICAL to the hydraulics-only depth deck (zero regression).
# --------------------------------------------------------------------------- #
# Washoff mode.
#   "exp" — EXP washoff W = C1 * q^C2 * B (buildup-driven first-flush; the
#           headline mode the demo asks for).
#   "emc" — a fixed event-mean concentration (bypasses buildup; a flat-conc
#           "conservative dilution" CONTROL run with NO first flush).
WashoffModel = Literal["exp", "emc"]


class PollutantSpec(GraceModel):
    """One pollutant's SWMM buildup/washoff parameterization (a DEMO preset).

    Every coefficient here is an EPA-literature DEMO DEFAULT, narrated as such by
    the composer — NOT a site calibration (we have no per-site buildup/washoff
    fetcher, exactly like the depth path's demo Manning n / infiltration
    defaults). The composer resolves a keyword ("tss" / "e_coli" / "tn") to one of
    these; an advanced caller may pass a fully-specified spec to override.

    SWMM semantics PINNED by the Phase-1 in-image smoke (units + POW arg order):
      - buildup POW: ``B = min(buildup_max, buildup_rate * t^buildup_power)`` per
        unit AREA. In a CMS (SI) deck the mass unit is metric: ``buildup_max`` /
        ``buildup_rate`` are in (pollutant-mass-unit) per HECTARE (kg/ha for a
        MG/L pollutant; count/ha for a ``#/L`` count pollutant). ``buildup_power``
        is the TIME EXPONENT (keep it ~0.5-2.0; a large exponent overflows
        ``t^power`` and SWMM rejects the deck — the swmm-api ``BuildUp(C1,C2,C3)``
        arg order IS SWMM's column order max/rate/EXPONENT).
      - washoff EXP: ``W = washoff_coef * q^washoff_exp * B`` (runoff-driven).
      - ``decay_per_day`` is a first-order routing sink (1/day; 0 = conservative
        TSS; ~1/day die-off for bacteria).

    Fields:
        name: SWMM pollutant name (deck ``[POLLUTANTS]`` id; also the
            ``out.pollutants`` key the postprocess maps to a concentration index).
        unit: SWMM concentration unit — ``"MG/L"`` (mass) or ``"#/L"`` (count).
            The count unit propagates to the outfall LOAD as a COUNT reported by
            SWMM in LOG10 form (the ``.rpt`` "LogN" column), which the postprocess
            carries through honestly (never mislabels counts as kg).
        buildup_max: POW max buildup (mass/ha or count/ha), > 0.
        buildup_rate: POW rate constant, >= 0.
        buildup_power: POW time exponent (dimensionless), > 0, kept small.
        washoff_coef: EXP washoff coefficient C1, >= 0.
        washoff_exp: EXP washoff runoff exponent C2, >= 0.
        decay_per_day: first-order routing decay (1/day), >= 0.
        emc_concentration: fixed event-mean concentration (in ``unit``) used ONLY
            when the run's ``washoff_model="emc"`` (the flat-conc control).
    """

    name: str
    unit: Literal["MG/L", "#/L"] = "MG/L"
    buildup_max: float = Field(gt=0.0)
    buildup_rate: float = Field(default=1.0, ge=0.0)
    buildup_power: float = Field(default=1.0, gt=0.0)
    washoff_coef: float = Field(default=5.0, ge=0.0)
    washoff_exp: float = Field(default=1.8, ge=0.0)
    decay_per_day: float = Field(default=0.0, ge=0.0)
    emc_concentration: float = Field(default=100.0, ge=0.0)


# Keyword -> demo PollutantSpec. EPA-typical residential-runoff anchors (narrated
# as demo values by the composer, never site precision):
#   TSS  — EPA SWMM Applications-Manual Example 5 residential suspended solids:
#          ~50 lb/ac cap (56 kg/ha) @ ~1 lb/ac/day (1.12 kg/ha/day); EMC 100 mg/L.
#   E_coli — count pollutant (#/L); demo buildup cap ~1e11 count/ha, ~1/day
#            freshwater daylight die-off.
#   TN / TP — nutrient demo anchors (lower buildup than TSS).
POLLUTANT_PRESETS: dict[str, PollutantSpec] = {
    "tss": PollutantSpec(
        name="TSS", unit="MG/L", buildup_max=56.0, buildup_rate=1.12,
        buildup_power=1.0, washoff_coef=5.0, washoff_exp=1.8, decay_per_day=0.0,
        emc_concentration=100.0,
    ),
    "e_coli": PollutantSpec(
        name="E_coli", unit="#/L", buildup_max=1.0e11, buildup_rate=1.0e10,
        buildup_power=1.0, washoff_coef=5.0, washoff_exp=1.8, decay_per_day=1.0,
        emc_concentration=1.0e4,
    ),
    "tn": PollutantSpec(
        name="TN", unit="MG/L", buildup_max=5.0, buildup_rate=0.1,
        buildup_power=1.0, washoff_coef=2.0, washoff_exp=1.5, decay_per_day=0.0,
        emc_concentration=2.0,
    ),
    "tp": PollutantSpec(
        name="TP", unit="MG/L", buildup_max=1.0, buildup_rate=0.02,
        buildup_power=1.0, washoff_coef=2.0, washoff_exp=1.5, decay_per_day=0.0,
        emc_concentration=0.3,
    ),
}

# Common LLM aliases -> canonical preset keyword.
_POLLUTANT_ALIASES: dict[str, str] = {
    "tss": "tss", "sediment": "tss", "suspended solids": "tss",
    "total suspended solids": "tss", "turbidity": "tss",
    "e_coli": "e_coli", "e-coli": "e_coli", "ecoli": "e_coli",
    "bacteria": "e_coli", "coliform": "e_coli", "fecal": "e_coli",
    "fecal coliform": "e_coli", "pathogen": "e_coli", "pathogens": "e_coli",
    "tn": "tn", "nitrogen": "tn", "total nitrogen": "tn", "nutrient": "tn",
    "nutrients": "tn", "nitrate": "tn",
    "tp": "tp", "phosphorus": "tp", "total phosphorus": "tp", "phosphate": "tp",
}


def resolve_pollutant_presets(pollutants: list[str] | None) -> list[PollutantSpec]:
    """Map a list of pollutant keywords to their demo ``PollutantSpec`` presets.

    Case/space-insensitive, alias-aware ("bacteria" -> e_coli, "sediment" ->
    tss). Duplicates and unknown keywords are dropped (an unknown keyword never
    fabricates a spec — the composer simply models the ones it recognizes).
    Returns ``[]`` for ``None`` / empty (=> no WQ sections => byte-identical
    hydraulics-only deck). Order follows the caller's list (first occurrence).
    """
    if not pollutants:
        return []
    specs: list[PollutantSpec] = []
    seen: set[str] = set()
    for raw in pollutants:
        if not isinstance(raw, str):
            continue
        key = _POLLUTANT_ALIASES.get(raw.strip().lower())
        if key is None or key in seen:
            continue
        seen.add(key)
        specs.append(POLLUTANT_PRESETS[key])
    return specs


class SWMMRunArgs(EngineRunArgsMixin):
    """Forcing + structure parameters for a quasi-2D PySWMM urban-flood run.

    Adopts ``EngineRunArgsMixin`` (levers STEP 3): ``advanced_physics`` keys are
    validated against ``physics_registry.PHYSICS_REGISTRY["swmm"]``
    (routing_method / routing_step_s / variable_step / threads) and merged into
    the SWMM ``[OPTIONS]`` block at deck write; ``None`` => byte-identical
    DYNWAVE deck. ``temporal_mode`` / ``output_frames`` are inert for SWMM today
    (the depth animation already emits frames from the .out).

    Returned/assembled by the urban composer after agent-confirmed parameter
    extraction; consumed by the SWMM worker/adapter. The agent confirms these
    with the user before submission (confirmation-before-consequence,
    invariant 9).

    Use this when:
        Building the input to a quasi-2D urban-flood SWMM run over an AOI
        (design storm + building representation + infiltration + optional
        structural barriers/flap gates).

    Do NOT use this for:
        Surface-water riverine/coastal flooding (that is SFINCS ``ModelSetup``)
        or groundwater contamination (that is ``MODFLOWRunArgs``), nor for
        carrying solver output (that is ``SWMMDepthLayerURI``).

    Fields:
        schema_version: contract version pin (additive growth only).
        bbox: AOI as ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. The
            engine fetches DEM + buildings within it and builds the overland
            mesh. The adaptive-grid/element-cap budget (lifted from
            ``sfincs_builder.py``) may COARSEN ``target_resolution_m`` for a
            large AOI; this is the requested resolution, not a guarantee.
        return_period_yr: design-storm return period, years (for the Atlas-14
            depth lookup). Demo default 100-yr.
        total_rain_depth_mm: OPTIONAL explicit total storm depth, mm (> 0). When
            set, it OVERRIDES the Atlas-14 return-period lookup (the user gave a
            depth directly). When ``None``, the engine looks up the depth from
            ``return_period_yr`` + ``storm_duration_hr`` (Atlas-14, with the
            Atlas-2 fallback per the data-source fallback norm).
        storm_duration_hr: design-storm duration, hours (> 0). Feeds both the
            Atlas-14 depth lookup AND the nested-hyetograph builder.
        rain_interval_min: hyetograph timestep, minutes (> 0). The nested
            hyetograph emits one intensity per interval over the duration.
        building_representation: how building footprints enter the mesh, EXACTLY
            one of {"drop", "raise", "roughness"} (EXPLICIT parameter, never
            hardcoded). ``"drop"`` (DEFAULT, recommended) = building cells become
            holes so water routes AROUND them (the buildings-as-obstacles
            behavior; matches the screenshot); ``"raise"`` = cells dam flow;
            ``"roughness"`` = bump Manning n. Leave UNSET to get ``"drop"``.
        infiltration_method: loss model on the pervious fraction. Default
            ``"none"`` (fully impervious, the spike default).
        target_resolution_m: requested overland cell size, m (> 0). Subject to
            the adaptive-grid budget for large AOIs.
        manning_overland: overland-flow Manning n (> 0). Default 0.03 (spike).
        mass_balance_tolerance_pct: the honesty gate. If the SWMM .rpt Flow
            Routing Continuity error EXCEEDS this (%), the worker raises a typed
            ``SWMM_MASS_BALANCE_EXCEEDED`` error instead of publishing a
            silently-wrong layer. In (0, 100]; default 5%.
        barriers: OPTIONAL GeoJSON ``FeatureCollection`` of tagged ``LineString``
            segments defining structural walls / flap gates. Each feature's
            ``properties.barrier_type`` ∈ {"wall", "flap_gate"}. ``None`` for a
            plain (no-structure) run. Same shape echoed back on
            ``SWMMDepthLayerURI.barriers`` for rendering.
    """

    schema_version: Literal["v1"] = "v1"

    bbox: BBox

    return_period_yr: int = Field(default=DEFAULT_RETURN_PERIOD_YR, gt=0)
    total_rain_depth_mm: float | None = Field(default=None, gt=0.0)
    storm_duration_hr: float = Field(default=DEFAULT_STORM_DURATION_HR, gt=0.0)
    rain_interval_min: int = Field(default=DEFAULT_RAIN_INTERVAL_MIN, gt=0)

    building_representation: BuildingRepresentation = "drop"
    infiltration_method: InfiltrationMethod = "none"

    target_resolution_m: float = Field(default=DEFAULT_TARGET_RESOLUTION_M, gt=0.0)
    manning_overland: float = Field(default=DEFAULT_MANNING_OVERLAND, gt=0.0)

    # Mass-balance honesty gate (cross-check improvement). Continuity error
    # above this fraction -> typed SWMM_MASS_BALANCE_EXCEEDED, not a wrong layer.
    mass_balance_tolerance_pct: float = Field(default=5.0, gt=0.0, le=100.0)

    barriers: dict[str, Any] | None = None

    # --- Water-quality (buildup/washoff) — OPTIONAL second output family ----
    # ``pollutants`` is a list of keywords the composer maps to demo presets
    # ("tss", "e_coli"/"bacteria", "tn", "tp"). ``None`` / [] => NO WQ sections
    # => a BYTE-IDENTICAL hydraulics-only deck (zero depth-path regression). An
    # advanced caller may pass fully-specified ``pollutant_specs`` to override the
    # presets. ``dry_buildup_days`` sets OPTIONS DRY_DAYS so buildup accumulates
    # over N antecedent dry days before the storm; ``washoff_model`` selects the
    # EXP first-flush headline vs the EMC flat-conc control.
    pollutants: list[str] | None = None
    pollutant_specs: list[PollutantSpec] | None = None
    dry_buildup_days: int = Field(default=0, ge=0)
    washoff_model: WashoffModel = "exp"

    @field_validator("building_representation", mode="before")
    @classmethod
    def _normalize_building_representation(cls, value: Any) -> Any:
        """Map common LLM synonyms onto the canonical representation BEFORE the
        ``Literal`` check, so the FIRST attempt succeeds (no self-correcting
        retry loop). The docs call the concept "BUILDING OBSTRUCTIONS", so the
        LLM invents synonyms like "obstacles" -> these normalize to ``"drop"``.

        Lowercase/strip, then alias-map ({obstacles,...} -> "drop";
        {block,dam,wall} -> "raise"; {friction,manning} -> "roughness"). An
        unknown string passes through UNCHANGED so a genuinely-invalid value
        still raises the honest ``Literal`` error.
        """
        if not isinstance(value, str):
            return value
        key = value.strip().lower()
        return _BUILDING_REPRESENTATION_ALIASES.get(key, key)

    @field_validator("barriers")
    @classmethod
    def _validate_barriers(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Structurally validate the barrier GeoJSON FeatureCollection.

        Enforces: a ``FeatureCollection`` whose every feature is a ``LineString``
        tagged with ``properties.barrier_type`` ∈ {"wall", "flap_gate"}. We
        validate STRUCTURE only (no geometry-library dependency in contracts).
        """
        if value is None:
            return None
        return _validate_barrier_feature_collection(value)


def _validate_barrier_feature_collection(fc: dict[str, Any]) -> dict[str, Any]:
    """Shared structural validator for a tagged-LineString FeatureCollection."""
    if fc.get("type") != "FeatureCollection":
        raise ValueError(
            f"barriers must be a GeoJSON FeatureCollection, got type={fc.get('type')!r}"
        )
    features = fc.get("features")
    if not isinstance(features, list):
        raise ValueError("barriers.features must be a list")
    valid_tags = {"wall", "flap_gate"}
    for idx, feat in enumerate(features):
        if not isinstance(feat, dict) or feat.get("type") != "Feature":
            raise ValueError(f"barriers.features[{idx}] must be a GeoJSON Feature")
        geom = feat.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") != "LineString":
            raise ValueError(
                f"barriers.features[{idx}].geometry must be a LineString "
                f"(got {geom.get('type') if isinstance(geom, dict) else geom!r})"
            )
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            raise ValueError(
                f"barriers.features[{idx}].geometry.coordinates must be a "
                f"LineString with >= 2 positions"
            )
        props = feat.get("properties") or {}
        tag = props.get("barrier_type")
        if tag not in valid_tags:
            raise ValueError(
                f"barriers.features[{idx}].properties.barrier_type must be one "
                f"of {sorted(valid_tags)}, got {tag!r}"
            )
    return fc


class SWMMDepthLayerURI(LayerURI):
    """A ``LayerURI`` for a SWMM overland-depth layer, plus narration scalars
    and the tagged barrier geometry.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the structured numbers the agent narrates about the inundation so the
    LLM cites typed fields, never invents them (invariant 1, FR-AS-7):

        max_depth_m: peak overland water depth across the AOI, m (>= 0).
        flooded_area_km2: areal footprint above the wet threshold, km^2 (>= 0).
        n_buildings_affected: count of building footprints touched by water at
            or above the wet threshold (>= 0).

    And the structural-overlay geometry the client renders:

        barriers: OPTIONAL GeoJSON ``FeatureCollection`` of tagged ``LineString``
            segments — RED walls (``barrier_type="wall"``) / GREEN flap gates
            (``barrier_type="flap_gate"``) — drawn over the depth raster. Echoes
            the run's barriers back so the result is self-describing.

    ``layer_type`` for a depth layer is typically ``"raster"`` (a depth COG, or
    a time-varying COG sequence for the animation); the base contract's
    vocabulary is inherited unchanged (rasters COG; vectors FlatGeobuf/
    GeoParquet). For the time-stepped animation the inherited ``temporal`` field
    carries the WMS-T config.
    """

    max_depth_m: float = Field(ge=0.0)
    flooded_area_km2: float = Field(ge=0.0)
    n_buildings_affected: int = Field(ge=0)

    barriers: dict[str, Any] | None = None

    @field_validator("barriers")
    @classmethod
    def _validate_barriers(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Structurally validate the barrier GeoJSON FeatureCollection (same
        rule as ``SWMMRunArgs.barriers``)."""
        if value is None:
            return None
        return _validate_barrier_feature_collection(value)


class SWMMPollutantLayerURI(LayerURI):
    """A ``LayerURI`` for a SWMM per-cell peak washoff-CONCENTRATION raster, plus
    the typed water-quality narration scalars.

    Extends ``LayerURI`` field-for-field (so it maps onto ``map-command
    load-layer`` with no translation, same as every other layer) and carries the
    WQ numbers the agent narrates for one pollutant (invariant 1 / FR-AS-7 — the
    LLM cites these typed fields, never invents a load or a concentration). It is
    ADDITIVE CONTEXT beside the depth ``SWMMDepthLayerURI`` primary: a WQ failure
    never sinks the flood headline.

    Fields:
        pollutant_name: the SWMM pollutant this layer describes (``[POLLUTANTS]``
            id / ``out.pollutants`` key).
        pollutant_units: the concentration unit — ``"mg/L"`` or ``"#/L"``.
        outfall_load: cumulative mass (or count) delivered to the outfall over the
            storm, parsed from the ``.rpt`` Outfall Loading Summary (>= 0). For a
            count pollutant SWMM reports this in LOG10 form ("LogN"); the
            postprocess converts it to a raw count and labels it honestly.
        outfall_load_units: the load unit string — ``"kg"`` for a mass pollutant,
            ``"counts"`` for a count pollutant (converted from the ``.rpt`` LogN),
            so a count load is NEVER mislabeled as mass.
        peak_outfall_conc: peak outfall concentration over the storm (in
            ``pollutant_units``), >= 0 — the pollutograph crest (first flush).
        washoff_mass_fraction: washed load / total built-up mass, in [0, 1] — the
            supply-limited check (washed <= built). ``None`` when the built mass
            could not be read.
        wq_continuity_error_pct: the ``.rpt`` Quality Routing Continuity error (%)
            for this pollutant — the WQ mass-balance honesty readout. ``None`` when
            the block was absent/unreadable.
    """

    pollutant_name: str
    pollutant_units: str
    outfall_load: float = Field(ge=0.0)
    outfall_load_units: str
    peak_outfall_conc: float = Field(ge=0.0)
    washoff_mass_fraction: float | None = None
    wq_continuity_error_pct: float | None = None
