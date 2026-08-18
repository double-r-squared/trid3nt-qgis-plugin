"""PySWMM quasi-2D urban-flood engine contracts (Path A, sprint-16 P1).

The SWMM analogue of ``modflow_contracts.py``. Two shapes back the urban
North-Star demo path (NATE's PCSWMM screenshot: animated depth around BUILDING
OBSTRUCTIONS + a SOUND BARRIER with RED walls / GREEN flap gates):

- ``SWMMRunArgs`` — the forcing/structure parameters the agent confirms with the
  user before submitting a quasi-2D SWMM run. Consumed by the engine adapter /
  worker (``workers/swmm/...``) that maps these onto a quasi-2D SWMM
  deck (one STORAGE node per active cell, 4-connectivity overland CONDUITS, one
  boundary OUTFALL, per-cell rainfall SUBCATCHMENTS fed by a single RAINGAGE +
  the Atlas-14 nested hyetograph TIMESERIES) per the P0 spike
  (``workers/swmm/spike_quasi2d.py``).
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
  ``trid3nt_server/workflows/swmm_hyetograph.py`` from
  ``total_rain_depth_mm`` + ``storm_duration_hr`` + ``rain_interval_min``. It is
  NOT flat and NOT SCS-Type-II — these args parameterize the nested builder.
- ``SWMMDepthLayerURI`` is a structured numeric carrier (invariant 1 / Decision
  H): the agent narrates ``max_depth_m``, ``flooded_area_km2`` and
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

from .common import BBox, EngineRunArgsMixin, GraceModel, SyntheticInput
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
    "PollutantSpec",
    "POLLUTANT_PRESETS",
    "resolve_pollutant_presets",
    "SWMMRunArgs",
    "SWMMDepthLayerURI",
    "SWMMPollutantLayerURI",
    "SWMMNetworkLayerURI",
    "SWMMDualDrainageLayerURI",
    "SWMMDeckRunResult",
    "SWMMComparisonVariant",
    "SWMMComparisonResult",
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
# NOTE (law 9, ADR 0285 P4): there is no demo overland Manning's n. When the user
# supplies none, the composer DERIVES it from NLCD land cover over the AOI (the
# area-weighted mean of the SFINCS manning_mapping table) or REFUSES - never an
# invented friction constant. ``manning_overland`` is therefore Optional (None ->
# derive-or-refuse), NOT a baked default.


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
        manning_overland: overland-flow Manning n (> 0). Default None -> the
            composer DERIVES it from NLCD land cover over the AOI (area-weighted
            mean) or REFUSES; never a baked friction constant (law 9).
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
    # law 9 (ADR 0285 P4): None -> the composer derives overland n from NLCD land
    # cover over the AOI, or REFUSES (no invented friction). A user value (> 0) is
    # used verbatim.
    manning_overland: float | None = Field(default=None, gt=0.0)

    # Universal emit-on-solve cadence lever (ADR 0282). The OUTPUT reporting
    # cadence -- how often SWMM writes a depth snapshot -> the animation frame
    # count (deck-side REPORT_STEP, interval-shaped so ``output_interval_min``
    # maps DIRECTLY). ``None`` (default) keeps the legacy 5-min REPORT_STEP
    # (byte-identical). Distinct from ``rain_interval_min`` (the hyetograph
    # forcing timestep). This is the SOLE frame-count control -- there is NO
    # post-hoc frame thinning (the seam publishes every reported step).
    output_interval_min: float | None = Field(default=None, gt=0.0)

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
    LLM cites typed fields, never invents them (invariant 1):

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


class SWMMNetworkLayerURI(LayerURI):
    """A ``LayerURI`` for an IMPORTED municipal storm-drain network + its
    hydraulic response to a design storm (the dual-drainage MINOR system).

    Extends ``LayerURI`` field-for-field (so it maps onto ``map-command
    load-layer`` with no translation, same as every other layer) and carries the
    typed network + response scalars the agent narrates (invariant 1 --
    the LLM cites these fields, never invents a pipe count or a peak flow). The
    layer itself is a VECTOR (nodes as points carrying max-HGL / flooded, conduits
    as lines carrying surcharge) - a piped network's natural render.

    Fields:
        n_junctions: imported junction (manhole/inlet) node count (>= 0).
        n_conduits: imported conduit (pipe) count (>= 0).
        n_outfalls: imported / inferred outfall node count (>= 1).
        total_pipe_length_m: summed conduit length, m (>= 0).
        peak_outfall_flow_cms: peak discharge summed across outfalls, m^3/s (>= 0).
        total_outfall_volume_m3: cumulative volume delivered to outfalls, m^3 (>= 0).
        n_flooded_nodes: nodes that surcharged above their rim (flooded), count.
        n_surcharged_conduits: conduits that ran full/surcharged, count.
        max_node_hgl_m: peak hydraulic-grade-line elevation across nodes, m.
        continuity_error_pct: the .rpt Flow Routing Continuity error (%), the
            mass-balance honesty readout.
        n_inverts_filled: nodes whose invert was gap-filled (DEM/slope-walk) -
            the labeled-degrade count (>= 0); a large value means a low-confidence
            network geometry.
        n_topology_snapped: conduit endpoints snapped to a nearest node because
            the GIS carried no explicit from/to topology (>= 0).
        network_source: a short human label for where the network came from
            (a user upload, an ArcGIS FeatureServer, or a synthesized fallback).
    """

    n_junctions: int = Field(ge=0)
    n_conduits: int = Field(ge=0)
    n_outfalls: int = Field(ge=0)
    total_pipe_length_m: float = Field(ge=0.0)
    peak_outfall_flow_cms: float = Field(ge=0.0)
    total_outfall_volume_m3: float = Field(ge=0.0)
    n_flooded_nodes: int = Field(ge=0)
    n_surcharged_conduits: int = Field(ge=0)
    max_node_hgl_m: float = 0.0
    continuity_error_pct: float = 0.0
    n_inverts_filled: int = Field(default=0, ge=0)
    n_topology_snapped: int = Field(default=0, ge=0)
    network_source: str = ""


class SWMMDualDrainageLayerURI(SWMMDepthLayerURI):
    """A ``LayerURI`` for a COUPLED dual-drainage run - the overland MAJOR system
    (surface depth raster, inherited) EXCHANGING flow with the imported piped
    MINOR system at inlets. The practice-verification's defining "both halves".

    The primary layer is the OVERLAND peak-depth raster (inherits
    ``max_depth_m`` / ``flooded_area_km2`` / ``n_buildings_affected``); a pipe
    network vector is emitted alongside as context. Adds the MINOR-system +
    coupling scalars the agent narrates (invariant 1):

        n_pipe_junctions / n_pipe_conduits / n_pipe_outfalls: the imported piped
            network's node/link/outfall counts (>= 0).
        n_inlets: surface<->sewer coupling links wired (catchbasins/inlets) - the
            count that makes the run dual drainage (>= 0).
        pipe_peak_outfall_flow_cms: peak discharge summed across PIPE outfalls,
            m^3/s - the minor system's captured, routed flow (>= 0).
        n_pipe_flooded_nodes: pipe junctions that surcharged above their rim,
            pushing water back to the surface (>= 0).
        n_pipe_surcharged_conduits: pipes that ran full/surcharged (>= 0).
        n_inverts_filled / n_topology_snapped: the imported network's
            labeled-degrade counts (>= 0).
    """

    n_pipe_junctions: int = Field(default=0, ge=0)
    n_pipe_conduits: int = Field(default=0, ge=0)
    n_pipe_outfalls: int = Field(default=0, ge=0)
    n_inlets: int = Field(default=0, ge=0)
    pipe_peak_outfall_flow_cms: float = Field(default=0.0, ge=0.0)
    n_pipe_flooded_nodes: int = Field(default=0, ge=0)
    n_pipe_surcharged_conduits: int = Field(default=0, ge=0)
    n_inverts_filled: int = Field(default=0, ge=0)
    n_topology_snapped: int = Field(default=0, ge=0)


class SWMMPollutantLayerURI(LayerURI):
    """A ``LayerURI`` for a SWMM per-cell peak washoff-CONCENTRATION raster, plus
    the typed water-quality narration scalars.

    Extends ``LayerURI`` field-for-field (so it maps onto ``map-command
    load-layer`` with no translation, same as every other layer) and carries the
    WQ numbers the agent narrates for one pollutant (invariant 1 — the
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


class SWMMDeckRunResult(GraceModel):
    """The typed result of running a CITED, PUBLISHED SWMM ``.inp`` deck.

    NOT a ``LayerURI``: the cited textbook decks carry SCHEMATIC coordinates (local
    model units, not lon/lat), so the runner emits CHARTS (hydrographs / stage
    recession / control tracking / pollutographs) as the primary product and does
    NOT fabricate a georeferenced map layer. This carrier holds the typed scalars
    the agent narrates (invariant 1 -- the LLM cites these fields, never
    invents a peak flow or a pond stage) plus the LOUD demonstration-honesty
    citation (the deck is the cited example's network, NOT a user AOI).

    Fields:
        deck_id: the internal cited-deck id.
        deck_title: the published example's title (verbatim citation).
        deck_author: the named author of the published deck.
        deck_source: the hosting collection label.
        deck_url: the pinned public source URL the deck was fetched from.
        capabilities: the published capabilities this deck demonstrates (LID,
            storage-routing, PID/RTC) - why it is a distinct template.
        forcing: what drove the run - "rainfall" / "initial_storage" /
            "dry_weather_flow".
        flow_units: the deck's FLOW_UNITS ("CFS" / "CMS" / ...) - the unit the
            headline scalars are reported in (so a CFS deck is never mislabeled CMS).
        continuity_error_pct: the .rpt Flow-Routing Continuity error (%), the
            mass-balance honesty readout.
        n_nodes / n_links / n_subcatchments: the deck's object counts (>= 0).
        peak_outfall_flow: peak discharge summed across outfalls, in flow_units
            (>= 0).
        max_node_depth: peak node depth over the run, deck depth units (>= 0).
        n_flooded_nodes / n_surcharged_conduits: surcharge tallies (>= 0).
        headline: a small dict of the deck-specific headline numbers the chart
            visualizes (e.g. the LID with/without runoff pair, the PID target vs
            achieved wet-well depth) - all real parsed outputs.
        chart_titles: the titles of the charts emitted for this run (the agent
            references these instead of describing an absent map layer).
        demonstration_note: the LOUD honesty label - this is the cited example's
            network, schematic (not georeferenced), a demonstration not a site study.
        schematic_only: always True for a published-deck run (no georeferenced
            layer) - the client suppresses any zoom-to / layer-load expectation.
        synthetic_inputs: structured provenance (the published deck basis + every
            override labeled).
        rain_scale: the applied rainfall multiplier (1.0 = the published storm).
    """

    deck_id: str
    deck_title: str
    deck_author: str
    deck_source: str
    deck_url: str
    capabilities: list[str] = Field(default_factory=list)
    forcing: str
    flow_units: str = ""
    continuity_error_pct: float = 0.0
    n_nodes: int = Field(default=0, ge=0)
    n_links: int = Field(default=0, ge=0)
    n_subcatchments: int = Field(default=0, ge=0)
    peak_outfall_flow: float = Field(default=0.0, ge=0.0)
    max_node_depth: float = Field(default=0.0, ge=0.0)
    n_flooded_nodes: int = Field(default=0, ge=0)
    n_surcharged_conduits: int = Field(default=0, ge=0)
    headline: dict[str, Any] = Field(default_factory=dict)
    chart_titles: list[str] = Field(default_factory=list)
    demonstration_note: str = ""
    schematic_only: bool = True
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)
    rain_scale: float = 1.0


class SWMMComparisonVariant(GraceModel):
    """One knob-variant's parsed scalars in a SWMM mechanism-COMPARISON run.

    Every field is a real parsed solver output (invariant 1) - the agent narrates
    these, it never invents a peak or a load.

    Fields:
        label: the knob value this variant realizes (e.g. "Horton",
            "post-development", "duty/standby staged", "green roof").
        continuity_error_pct: this variant's Flow-Routing Continuity error (%).
        peak_value: peak of the variant's primary charted series (runoff /
            depth / flow / concentration), in ``SWMMComparisonResult.series_units``.
        peak_time_min: minutes-from-start at which ``peak_value`` occurs (>= 0).
        total_value: an integrated / summary quantity when meaningful (e.g. total
            runoff volume, total washoff load); 0.0 when not computed.
        max_node_depth: peak node depth over the run (>= 0).
        n_flooded_nodes / n_surcharged_conduits: surcharge tallies (>= 0).
        extra: family-specific scalars (e.g. per-pump run-fraction, per-outlet
            split share) - all real parsed outputs.
    """

    label: str
    continuity_error_pct: float = 0.0
    peak_value: float = 0.0
    peak_time_min: float = Field(default=0.0, ge=0.0)
    total_value: float = 0.0
    max_node_depth: float = Field(default=0.0, ge=0.0)
    n_flooded_nodes: int = Field(default=0, ge=0)
    n_surcharged_conduits: int = Field(default=0, ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)


class SWMMComparisonResult(GraceModel):
    """The typed result of a SWMM mechanism-COMPARISON template.

    NOT a ``LayerURI``: the comparison runs SMALL SYNTHETIC decks (a single
    subcatchment / a wet-well / a pond-outlet stub) whose coordinates are
    SCHEMATIC (local model units, not lon/lat). The product is the OVERLAY CHART
    that visually demonstrates the knob (method A vs B vs C in one figure) plus
    the typed per-variant scalars the agent cites. There is NO georeferenced map
    layer - the ``basis`` is an honestly-labeled synthetic mechanism demonstration.

    Fields:
        comparison_kind: the mechanism class compared (e.g.
            "infiltration_method", "outlet_structure", "pump_control",
            "lid_type", "wq_buildup_washoff").
        knob_name: the varied knob's name (what the LLM would change).
        knob_values: the ordered knob values compared (one per variant).
        flow_units: the decks' FLOW_UNITS ("CFS" / "CMS") - the unit context.
        series_units: the unit of ``variant.peak_value`` (e.g. "CFS", "ft",
            "mg/L") so a runoff peak is never mislabeled a depth.
        variants: the per-knob-value parsed scalars.
        headline: a small dict of the comparison's headline facts the chart
            visualizes (e.g. the peak-runoff spread across methods, the
            LID runoff reduction) - all real parsed outputs.
        chart_titles: the titles of the overlay chart(s) emitted.
        demonstration_note: the LOUD honesty label - a synthetic mechanism
            comparison on schematic decks, not a georeferenced site study.
        schematic_only: always True (no georeferenced layer).
        basis: "synthetic" - the decks are authored small mechanism networks.
        synthetic_inputs: structured provenance (the synthetic-deck basis + the
            published mechanism source each variant realizes).
    """

    comparison_kind: str
    knob_name: str
    knob_values: list[str] = Field(default_factory=list)
    flow_units: str = ""
    series_units: str = ""
    variants: list[SWMMComparisonVariant] = Field(default_factory=list)
    headline: dict[str, Any] = Field(default_factory=dict)
    chart_titles: list[str] = Field(default_factory=list)
    demonstration_note: str = ""
    schematic_only: bool = True
    basis: str = "synthetic"
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)
