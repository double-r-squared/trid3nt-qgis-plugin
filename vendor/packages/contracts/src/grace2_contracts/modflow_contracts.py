"""MODFLOW 6 groundwater-engine contracts (sprint-13 Stage 1, §2.3 MODFLOW
integration, OQ-9 mf6-gwt solute transport).

Two shapes back the Case 2 groundwater-contamination demo path
(news article -> parameter extraction -> MODFLOW run -> plume layer):

- ``MODFLOWRunArgs``  - the forcing parameters the agent confirms with the user
  before submitting a MODFLOW run. Consumed by the engine adapter
  (``services/workers/modflow/gwt_adapter.py``, job-0221) that maps these to
  MF6-GWT input files via ``flopy``, and by the agent-side
  ``run_modflow_job`` tool (job-0227).
- ``PlumeLayerURI`` - the postprocess output layer. Extends ``LayerURI``
  field-for-field (so it still maps onto ``map-command load-layer`` with no
  translation, like every other layer) and adds the two plume scalars the
  agent narrates: peak concentration and plume footprint.

Design notes
------------
- ``spill_location_latlon`` is ordered ``(lat, lon)`` - this is a single point,
  NOT a ``bbox``. The project ``BBox`` convention is ``(min_lon, min_lat, ...)``
  (lon-first, EPSG:4326); a *point* spill location reads more naturally as
  ``(lat, lon)`` and is documented as such here so the engine adapter and the
  agent tool both honor the same order. Each component is range-validated
  (lat in [-90, 90], lon in [-180, 180]).
- Defaults for ``aquifer_k_ms`` (hydraulic conductivity) and ``porosity`` are
  TENTATIVE demo parameterization per sprint-13 manifest OQ-3: K=1e-4 m/s,
  porosity=0.3 (saturated sandy coastal plain). The composer (job-0228) must
  narrate to the user that these are demo defaults, not site-specific
  hydrogeology. See report Open Questions.
- ``PlumeLayerURI`` is a structured numeric carrier (invariant 1 / Decision H /
  FR-AS-7): the agent narrates ``max_concentration_mgl`` and ``plume_area_km2``
  from these typed fields rather than inventing them from free text.
- ``contaminant`` is a free ``str`` (open by design - the contaminant name is an
  open vocabulary, e.g. "benzene", "TCE", "PFOA"; the engine maps it to MF6-GWT
  transport parameters). It is non-numeric, so it stays a scalar.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from .common import EngineRunArgsMixin, GraceModel
from .execution import LayerURI

# Streambed defaults for the RIV head-dependent river<->aquifer flux package
# (sprint-17 J9 river-seepage). Per-cell RIV conductance C = K_bed * L * W / M
# (bed hydraulic conductivity * reach length-in-cell * reach width / bed
# thickness). When a per-cell conductance is not supplied the adapter derives
# it from these defaults (demo streambed, narrated as a demo value just like
# the OQ-3 aquifer K / porosity). A typical silty streambed K ~= 0.1 m/day with
# a 1 m bed thickness and a ~5 m channel across a 50 m cell gives an O(10-100)
# m^2/day conductance; the spike used a flat 100 m^2/day and produced
# 835 m^3/day of leakage, so 50 m^2/day is a conservative default per-cell.
DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY: float = 50.0  # per RIV reach cell
DEFAULT_STREAMBED_THICKNESS_M: float = 1.0  # streambed (M) for K-derived conductance

__all__ = [
    "MODFLOWRunArgs",
    "SpeciesSpec",
    "MultiSpeciesPlumeResult",
    "PlumeLayerURI",
    "SeepageLayerURI",
    "DrawdownLayerURI",
    "DewaterLayerURI",
    "BudgetPartitionLayerURI",
    "MoundingLayerURI",
    "ASRLayerURI",
    "HydroperiodLayerURI",
    "CaptureZoneLayerURI",
    "SaltwaterWedgeLayerURI",
    "StreamReachLayerURI",
    "SubsidenceLayerURI",
    "DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY",
    "DEFAULT_STREAMBED_THICKNESS_M",
    "DEFAULT_AQUIFER_SY",
    "DEFAULT_AQUIFER_SS",
    "DEFAULT_WETLAND_SY",
]


# TENTATIVE demo defaults (sprint-13 manifest OQ-3). Narrated as demo values,
# not site-specific hydrogeology, by the Case 2 composer.
DEFAULT_AQUIFER_K_MS: float = 1e-4  # hydraulic conductivity, m/s (sandy coastal plain)
DEFAULT_POROSITY: float = 0.3  # effective porosity, dimensionless

# Transient-storage defaults for the three new sprint-18 archetypes
# (sustainable_yield / mine_dewatering / regional_water_budget). These feed the
# GwfSto package (specific yield + specific storage) the transient stress period
# needs; the existing spill/seepage archetype path does NOT read them (it stays
# steady + the byte-identical conservative-tracer deck). Demo values, narrated.
DEFAULT_AQUIFER_SY: float = 0.2  # specific yield (drainable porosity), dimensionless
DEFAULT_AQUIFER_SS: float = 1e-5  # specific storage (1/m), confined-aquifer demo value

# Wetland-hydroperiod specific-yield default (sprint-18 Wave-2 wetland_hydroperiod
# archetype). The seasonal water-table response under a recharging wetland is
# governed by the unconfined specific yield; a shallow-water-table wetland soil
# drains/fills with a Sy ~= 0.2 (same family as DEFAULT_AQUIFER_SY but named
# separately so the wetland archetype reads its own demo value). Narrated demo.
DEFAULT_WETLAND_SY: float = 0.2  # wetland-soil specific yield, dimensionless


class SpeciesSpec(GraceModel):
    """One solute species in a multi_species MODFLOW 6 GWT run (Wave-3 - ADDITIVE).

    Carries the per-species transport forcing the adapter maps to ONE
    ``ModflowGwt`` (DIS/IC/ADV/DSP/MST/SRC/SSM/OC) plus a ``ModflowGwfgwt``
    flow<->transport exchange on the shared GWF flow field. The MST physics
    (sorption Kd / first-order decay) is per-species and each species transports
    INDEPENDENTLY: its own SRC is its only source and its own decay only REMOVES
    its mass.

    NOTE on decay chains: parent->daughter mass INGROWTH (the daughter being
    produced by the parent's decay, e.g. TCE -> cis-DCE -> VC) is NOT modeled.
    MF6's ``GWT6-GWT6`` exchange couples two GWT models across a SPATIAL grid
    interface (domain decomposition), not chemical ingrowth on a shared grid, so
    the adapter does not wire one. The ``parent`` field is RECORDED on the run
    manifest for provenance only (``decay_chain_coupled`` stays False); a pure
    daughter with ``release_rate_kg_s=0.0`` will simply stay at zero
    concentration. True chain ingrowth is future work.

    Use this when:
        Building one of the N entries of ``MODFLOWRunArgs.species`` for an
        ``archetype="multi_species"`` run (N independent or chained plumes from a
        single shared GWF flow field).

    Do NOT use this for:
        The single-contaminant spill path (leave ``MODFLOWRunArgs.species`` None
        and use the top-level ``contaminant`` / ``release_rate_kg_s``).

    Fields:
        name: species name (open vocabulary, e.g. "TCE", "cis-DCE", "VC"). Must
            be unique within a ``species`` list (the adapter keys GWT models on
            it). Non-empty.
        release_rate_kg_s: this species' mass release rate at the spill cell,
            kg/s (>= 0). Since ingrowth is not modeled, a species with 0.0 here
            has NO source and stays at zero concentration (set a real rate for
            every species you want a plume from).
        sorption_kd: OPTIONAL per-species linear sorption distribution coefficient
            Kd (m^3/kg) applied at the ``GwtMst`` package. None => no sorption
            (conservative-tracer transport for this species).
        decay_per_day: OPTIONAL per-species first-order decay rate (1/day, >= 0)
            applied at ``GwtMst``. None => no decay. This only REMOVES this
            species' mass; the lost mass is NOT loaded onto any daughter (no
            ingrowth coupling -- see the class note).
        parent: OPTIONAL name of the conceptual PARENT species, RECORDED on the
            manifest for provenance ONLY. It does NOT wire any species-to-species
            coupling (ingrowth is not modeled -- see the class note); each species
            still transports independently. When set, must match the ``name`` of
            another species in the same ``species`` list.
    """

    name: str = Field(min_length=1)
    release_rate_kg_s: float = Field(ge=0.0)
    sorption_kd: float | None = Field(default=None, ge=0.0)
    decay_per_day: float | None = Field(default=None, ge=0.0)
    parent: str | None = None


class MODFLOWRunArgs(EngineRunArgsMixin):
    """Forcing parameters for a MODFLOW 6 + MF6-GWT groundwater run.

    Adopts ``EngineRunArgsMixin`` (levers STEP 3): ``temporal_mode`` (default
    ``"steady"``, no-op for the demo deck), ``output_frames`` (default 24), and
    ``advanced_physics`` (default ``None``). ``advanced_physics`` keys are
    validated against ``physics_registry.PHYSICS_REGISTRY["modflow"]`` (sorption
    Kd / bulk density / first-order decay / longitudinal+transverse dispersivity)
    and applied at the ``GwtMst`` / ``GwtDsp`` deck seam; ``None`` =>
    byte-identical conservative-tracer deck.

    Returned/assembled by the Case 2 composer after agent-confirmed parameter
    extraction; consumed by ``run_modflow_job`` (agent) and the ``flopy``
    GWT adapter (engine). The agent confirms these with the user before
    submission (confirmation-before-consequence, invariant 9).

    Use this when:
        Building the input to a groundwater-contamination MODFLOW run from a
        spill event (location + contaminant + release schedule + aquifer
        properties).

    Do NOT use this for:
        Surface-water / flood forcing (that is SFINCS ``ModelSetup``), or for
        carrying solver output (that is ``PlumeLayerURI``).

    Fields:
        schema_version: contract version pin (additive growth only).
        spill_location_latlon: point spill location as ``(lat, lon)`` in
            EPSG:4326. NOTE the order is lat-first (a point, not a bbox).
        contaminant: contaminant name (open vocabulary, e.g. "benzene", "TCE").
        release_rate_kg_s: contaminant mass release rate, kg/s (> 0).
        duration_days: release duration, days (> 0).
        aquifer_k_ms: aquifer hydraulic conductivity, m/s (> 0). Defaults to a
            TENTATIVE demo value (OQ-3); narrate as a demo default.
        porosity: aquifer effective porosity, dimensionless in (0, 1].
            Defaults to a TENTATIVE demo value (OQ-3); narrate as a demo default.

    River-coupling fields (sprint-17 J9 - ADDITIVE, all optional; the pure-spill
    deck is byte-identical when ``river_geometry_uri`` is None):
        river_geometry_uri: a FlatGeobuf / GeoJSON URI of the river polyline(s)
            (from ``fetch_river_geometry`` / NLDI) to drape onto the structured
            grid as RIV head-dependent boundary cells. When None, no RIV/along-
            river SRC is added and the deck is the original spill-only deck.
        river_stage_m: explicit river stage (water-surface elevation, m, local
            datum) for every RIV reach cell. When None the adapter samples stage
            from the DEM at each reach cell (``river_dem_uri``) or, absent a DEM,
            falls back to ``AQUIFER_TOP_M`` + a small head so the reach is a
            gaining/losing boundary, not a no-op.
        river_stage_depth_m: water depth (m) above the streambed bottom used to
            derive stage from the sampled streambed elevation (stage = rbot +
            depth). Demo default applied by the adapter when None.
        streambed_conductance_m2_day: per-reach-cell RIV conductance (m^2/day).
            When None the adapter derives it from
            ``DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY``.
        river_dem_uri: optional DEM COG URI used to sample streambed-bottom
            elevation (rbot) and stage along the reach. When None the adapter
            uses flat demo streambed values relative to ``AQUIFER_TOP_M``.
        along_river_source: when True the contaminant SRC mass-loading is placed
            at the RIV reach cells (the seepage source enters where the river
            leaks into the aquifer) INSTEAD of the single spill cell. When False
            (default) the SRC stays at the spill cell exactly as the original
            groundwater-contamination deck.

    Archetype selector + per-archetype fields (sprint-18 Wave-1 - ADDITIVE, all
    optional/defaulted; ``archetype is None`` is the EXISTING spill/seepage path
    and the deck is byte-identical):
        archetype: which new MODFLOW question this run answers. ``None`` (the
            default) keeps the existing spill/seepage deck. The three new values
            are ``"sustainable_yield"`` (well-pumping drawdown), ``"mine_dewatering"``
            (DRN-package pit dewatering), and ``"regional_water_budget"`` (zonal
            flow-budget partition). The adapter branches on this; an unknown value
            is rejected by the literal.

        --- sustainable_yield (pumping-well drawdown) ---
        well_location_latlon: pumping-well point as ``(lat, lon)`` EPSG:4326
            (lat-first, same convention as ``spill_location_latlon``).
        pumping_rate_m3_day: sustained extraction rate as a POSITIVE magnitude,
            m^3/day (sustainable_yield is always an extraction question). The
            adapter applies the MF6 WEL sign internally (a negative discharge
            removes water from the cell), so the user passes a positive number.
        aquifer_sy: specific yield (drainable porosity) for the GwfSto transient
            storage term, dimensionless. Demo default ``DEFAULT_AQUIFER_SY`` (0.2).
        aquifer_ss: specific storage (1/m) for the GwfSto transient term. Demo
            default ``DEFAULT_AQUIFER_SS`` (1e-5).
        sim_years: transient simulation length, years (> 0). When None the
            adapter uses ``n_periods`` (or its own demo default).
        n_periods: number of transient stress periods (>= 1). An alternative to
            ``sim_years`` for explicit period control.

        --- mine_dewatering (DRN-package pit dewatering) ---
        pit_footprint_lonlat: the pit footprint as an ordered list of
            ``(lon, lat)`` vertices (lon-first, the BBox/polygon convention) draped
            onto the grid as DRN drain cells. A point or single-cell pit is a
            one-element list.
        drain_elevation_m: DRN drain elevation (the target dewatered head, m local
            datum) applied to every pit cell. Water above this elevation drains out.
        drain_conductance_m2_day: per-cell DRN conductance (m^2/day) controlling the
            head-dependent drain flux Q = C*(h - drain_elev) for h > drain_elev.
        well_pumping_rate_m3_day: OPTIONAL supplemental sump-WEL extraction as a
            POSITIVE magnitude (m^3/day; the adapter applies the negative MF6 sign)
            combined with the drains (a pit can be dewatered by drains plus pumping
            wells). None = drains only.

        --- regional_water_budget (flow-budget partition) ---
        zone_partition: RESERVED (Wave-2). regional_water_budget currently returns
            the WHOLE-DOMAIN volumetric budget partition (per-term IN/OUT from the
            real CBC: WEL/RCH/RCHA/CHD/STO/DRN, FLOW-JA-FACE excluded from the
            headline). Per-zone (e.g. upgradient/downgradient) partitioning is not
            yet wired agent-side; this field is accepted but does not change the
            output until then. Leave None.

    Archetype fields (sprint-18 Wave-2 - ADDITIVE, all optional/defaulted; a
    run-args with none of them is byte-identical to the Wave-1 / spill path):

        --- MAR (managed aquifer recharge -> RCH mounding) ---
        basin_footprint_lonlat: the infiltration-basin footprint as an ordered
            list of ``(lon, lat)`` vertices (lon-first, the BBox/polygon
            convention) draped onto the grid as RCH recharge cells.
        infiltration_rate_m_day: applied recharge rate over the basin, m/day
            (> 0). The RCH package raises the water table (mounding) under it.
        recharge_months: number of months the basin floods (>= 1). Drives the
            transient stress-period count alongside ``n_periods``.
        n_periods: REUSED (sustainable_yield) - explicit transient period count
            override.

        --- ASR (aquifer storage & recovery -> seasonal WEL inject/recover) ---
        well_location_latlon: REUSED (sustainable_yield) - the ASR well point as
            ``(lat, lon)`` EPSG:4326 (lat-first).
        injection_rate_m3_day: ASR injection rate as a POSITIVE magnitude,
            m^3/day (> 0). The adapter applies the MF6 WEL sign (inject = +).
        recovery_rate_m3_day: ASR recovery (extraction) rate as a POSITIVE
            magnitude, m^3/day (> 0). The adapter applies the WEL sign (recover
            = -).
        injection_months: months of the injection half-cycle (>= 1).
        recovery_months: months of the recovery half-cycle (>= 1).
        n_cycles: number of inject/recover cycles (>= 1).

        --- wetland_hydroperiod (seasonal water-table range under a wetland) ---
        wetland_footprint_lonlat: the wetland footprint as an ordered list of
            ``(lon, lat)`` vertices (lon-first) draped onto the grid as the
            recharge + EVT cells.
        recharge_schedule_m_day: per-transient-period recharge rate schedule
            (one m/day value per period) applied over the wetland footprint.
        et_surface_m: EVT surface elevation (m, local datum) - the elevation at
            which evapotranspiration is at its max rate.
        et_max_rate_m_day: maximum ET rate at the surface, m/day (> 0).
        et_extinction_depth_m: ET extinction depth (m, > 0) below the surface
            past which ET is zero (the EVT linear-decline depth).
        specific_yield: wetland-soil specific yield for the unconfined seasonal
            response, dimensionless in (0, 1]. Demo default ``DEFAULT_WETLAND_SY``
            (0.2).

    Archetype fields (Wave-5 - ADDITIVE, all optional/defaulted; a run-args with
    none of them is byte-identical to all prior paths):

        --- saltwater_intrusion (Henry-style variable-density GWF+GWT wedge) ---
        A nrow=1 vertical cross-section with ModflowGwfbuy coupling solute
        concentration to fluid density; GHB+AUX supplies salt at the seaward
        column and WEL+AUX injects freshwater at the inland column. The Henry
        (1964) analytic benchmark is the canonical demo target. LOCAL-ONLY (the
        Henry demo grid is small + fast; no Batch submit). The composer MUST
        raise an InputError (Invariant 9 honesty gate) when
        ``coastal_transect_latlon`` is None -- a coastline can NEVER be fabricated.
        coastal_transect_latlon: two ``(lat, lon)`` endpoints (A=seaward, B=inland)
            defining the cross-section axis, EPSG:4326 (lat-first). REQUIRED for
            this archetype -- None triggers an InputError. When None and the
            archetype is NOT saltwater_intrusion this field is ignored (additive).
        seawater_salinity_ppt: salinity at the seaward GHB+AUX boundary, ppt. Demo
            default 35.0 (open ocean); estuarine problems may use lower values. > 0.
        n_vertical_layers: number of vertical layers (nlay) in the cross-section
            grid. More layers resolve the density interface more sharply. Default 20;
            bounds [4, 80].
        freshwater_inflow_m3_day: freshwater inflow at the inland WEL+AUX boundary,
            m^3/day (POSITIVE magnitude). When None the adapter auto-derives from
            the transect geometry + aquifer K (Henry-representative flux). > 0.
    """

    schema_version: Literal["v1", "v2"] = "v2"

    # Point spill location: (lat, lon), EPSG:4326. Lat-first by design (a point,
    # not the lon-first BBox convention). Each component range-validated below.
    spill_location_latlon: tuple[float, float]

    contaminant: str = Field(min_length=1)

    release_rate_kg_s: float = Field(gt=0.0)
    duration_days: float = Field(gt=0.0)

    aquifer_k_ms: float = Field(default=DEFAULT_AQUIFER_K_MS, gt=0.0)
    porosity: float = Field(default=DEFAULT_POROSITY, gt=0.0, le=1.0)

    # --- River-coupling (sprint-17 J9; ADDITIVE, all optional) -------------- #
    river_geometry_uri: str | None = None
    river_stage_m: float | None = None
    river_stage_depth_m: float | None = Field(default=None, gt=0.0)
    streambed_conductance_m2_day: float | None = Field(default=None, gt=0.0)
    river_dem_uri: str | None = None
    along_river_source: bool = False

    # --- Archetype selector (sprint-18 Wave-1 + Wave-2 + Wave-4 + Wave-5; ADDITIVE, optional) -- #
    # None = the EXISTING spill/seepage path (deck byte-identical). Wave-4 adds
    # "capture_zone" (zone-of-contribution via MF6 PRT backward particle tracking)
    # and "wellhead_protection" (EPA-style fixed-travel-time tiers via the same PRT
    # mechanism). Both are LOCAL-ONLY runs (PRT is fast; no Batch submit).
    # Wave-5 adds "saltwater_intrusion" (Henry-style variable-density GWF+GWT
    # wedge via ModflowGwfbuy). LOCAL-ONLY (the Henry demo grid is small + fast).
    archetype: (
        Literal[
            "sustainable_yield",
            "mine_dewatering",
            "regional_water_budget",
            "MAR",
            "ASR",
            "wetland_hydroperiod",
            "multi_species",
            "capture_zone",
            "wellhead_protection",
            "saltwater_intrusion",
            "stream_depletion",
            "land_subsidence",
        ]
        | None
    ) = None

    # --- stream_depletion: SFR routed river<->aquifer exchange (module wave) - #
    # "How does pumping this well affect the river?" A routed MODFLOW-6 SFR6
    # stream network (per-reach stage + Manning discharge + the GWF exchange
    # term) draped from the fetched NHDPlus flowline, coupled to the EXISTING
    # sustainable_yield WEL well. Depletion = the baseline-vs-pumped delta in the
    # SFR<->GWF exchange summed over reaches (the streamflow captured by the
    # well). All four forcing fields below are OPTIONAL demo-defaulted: a real
    # fetcher supplies none of them today, so the adapter narrates them as demo
    # assumptions (never fake site precision). When ``archetype`` is NOT
    # "stream_depletion" they are ignored (additive; other decks byte-identical).
    river_inflow_m3_s: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Headwater streamflow inflow at the most-upstream SFR reach, m^3/s "
            "(the SFR perioddata INFLOW term, converted internally to m^3/day). "
            "When None the adapter applies a demo default (narrated as a demo "
            "assumption; a real run reads it from fetch_noaa_nwm_streamflow or "
            "fetch_usgs_nwis_gauges). Must be > 0."
        ),
    )
    river_width_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Channel width applied to every SFR reach (the packagedata rwid), m. "
            "When None the adapter applies a 5-15 m demo default. A real run reads "
            "it from NHDPlus VAA. Must be > 0."
        ),
    )
    streambed_k_m_day: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Streambed hydraulic conductivity applied to every SFR reach (the "
            "packagedata rhk), m/day - it controls the reach<->aquifer leakage. "
            "When None the adapter applies a demo default (the "
            "DEFAULT_RIVER_CONDUCTANCE lineage). Narrated as a demo assumption. "
            "Must be > 0."
        ),
    )
    manning_n: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Manning roughness coefficient applied to every SFR reach (the "
            "packagedata man), used by SFR to compute reach stage + discharge from "
            "the routed flow. When None the adapter applies a 0.035 demo default "
            "(a natural-channel value). Narrated as a demo assumption. Must be > 0."
        ),
    )

    # --- land_subsidence: CSUB aquifer-system compaction (module wave) ------- #
    # "How much will the ground sink if we keep pumping this well?" A MODFLOW-6
    # CSUB (Skeletal storage, Compaction, and SUBsidence) package layered onto the
    # EXISTING sustainable_yield transient WEL deck: the pumping drawdown pushes the
    # effective stress in a compressible fine-grained interbed past its
    # preconsolidation, producing PERMANENT (inelastic) aquifer-system compaction
    # -> a ground-subsidence bowl (cm) + a per-cell compaction time-series. v1 =
    # ONE no-delay HEAD_BASED interbed per pumped footprint cell (bypasses the
    # geostatic-stress geometry so a flat demo grid is honest); preconsolidation =
    # the initial head so any drawdown drives inelastic compaction. All four fields
    # below are OPTIONAL demo-defaulted (no site clay-fraction fetcher in v1 -- the
    # subsidence MAGNITUDE is set by these, so the adapter narrates them as demo
    # assumptions, never site precision). When ``archetype`` is NOT
    # "land_subsidence" they are ignored (additive; other decks byte-identical).
    #
    # STORAGE double-count (pinned by the local mf6 6.5.0 smoke): CSUB supplies the
    # coarse skeletal storage via ``csub_cg_ske_m``, so when land_subsidence is
    # active the STO package ``ss`` is dropped to 0 -- mf6 6.5.0 HARD-REQUIRES
    # STO ss == 0 in all active cells when CSUB is present, so the fix is
    # engine-enforced (not a silent correction) and the CSUB-run head decline
    # matches the plain sustainable_yield run.
    csub_ssv_inelastic_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Inelastic (virgin) specific storage Ssv of the compressible interbed, "
            "m^-1 -- the number that SETS the subsidence magnitude. When None the "
            "adapter applies a ~2e-3 m^-1 Corcoran-Clay-scale demo default "
            "(narrated as a demo assumption; a real run reads it from a Central "
            "Valley textural / clay-fraction source, v2). Must be > 0."
        ),
    )
    csub_sse_elastic_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Elastic (recompression) specific storage Sse of the interbed, m^-1 -- "
            "one-to-two orders BELOW Ssv (the elastic/inelastic contrast IS the "
            "subsidence physics). When None a ~5e-5 m^-1 demo default is applied "
            "(narrated as a demo assumption). Must be > 0."
        ),
    )
    csub_interbed_thick_frac: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description=(
            "Interbed thickness as a FRACTION of the model layer thickness (the "
            "CSUB packagedata thick with cell_fraction=True). When None a ~0.5 demo "
            "default is applied (interbed occupies ~half the ~30 m demo layer). The "
            "ultimate compaction scales with this via dz = Ssv * b * dh. In (0, 1]."
        ),
    )
    csub_cg_ske_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Coarse-grained ELASTIC skeletal specific storage Ss of the aquifer "
            "matrix, m^-1 (the CSUB cg_ske_cr) -- this REPLACES the STO ss the plain "
            "run used (mf6 requires STO ss==0 under CSUB), so it should match the "
            "aquifer's elastic Ss (~1e-5). When None a ~1e-5 m^-1 demo default is "
            "applied (narrated as a demo assumption). Must be > 0."
        ),
    )

    # --- multi_species: N-species solute transport (Wave-3) ----------------- #
    # An optional list of per-species transport specs. When None (the default)
    # the EXISTING single-contaminant path is used unchanged (the deck builds the
    # one ModflowGwt + ModflowGwfgwt exchange from ``contaminant`` /
    # ``release_rate_kg_s`` exactly as before). When supplied (with
    # ``archetype="multi_species"``) the adapter builds ONE ModflowGwt + ONE
    # ModflowGwfgwt per species on the shared GWF flow field; each species
    # transports INDEPENDENTLY. Parent->daughter ingrowth is NOT wired (a species'
    # ``parent`` is recorded for provenance only -- see SpeciesSpec). ADDITIVE:
    # ``species is None`` => byte-identical single-contaminant deck.
    species: list[SpeciesSpec] | None = None

    # --- sustainable_yield: pumping-well drawdown --------------------------- #
    well_location_latlon: tuple[float, float] | None = None
    pumping_rate_m3_day: float | None = None  # negative = extraction (WEL sign)
    aquifer_sy: float = Field(default=DEFAULT_AQUIFER_SY, gt=0.0, le=1.0)
    aquifer_ss: float = Field(default=DEFAULT_AQUIFER_SS, gt=0.0)
    sim_years: float | None = Field(default=None, gt=0.0)
    n_periods: int | None = Field(default=None, ge=1)

    # --- mine_dewatering: DRN-package pit dewatering ------------------------ #
    pit_footprint_lonlat: list[tuple[float, float]] | None = None
    drain_elevation_m: float | None = None
    drain_conductance_m2_day: float | None = Field(default=None, gt=0.0)
    well_pumping_rate_m3_day: float | None = None  # optional supplemental WEL

    # --- regional_water_budget: zonal flow-budget partition ---------------- #
    zone_partition: str | None = None

    # --- MAR: managed aquifer recharge (RCH mounding) ---------------------- #
    # An infiltration basin floods a footprint with a recharge rate over a number
    # of recharge months; the RCH/RCHA package raises the water table (mounding)
    # under the basin. All optional/defaulted -> additive.
    basin_footprint_lonlat: list[tuple[float, float]] | None = None
    infiltration_rate_m_day: float | None = Field(default=None, gt=0.0)
    recharge_months: int | None = Field(default=None, ge=1)

    # --- ASR: aquifer storage & recovery (seasonal WEL inject/recover) ------ #
    # A single ASR well (reuse ``well_location_latlon``) INJECTS at a positive
    # rate for ``injection_months`` then RECOVERS (extracts) at a positive rate
    # for ``recovery_months``, repeated for ``n_cycles``. Both rates are passed
    # as POSITIVE magnitudes; the adapter applies the MF6 WEL sign (inject = +,
    # recover = -). All optional/defaulted -> additive.
    injection_rate_m3_day: float | None = Field(default=None, gt=0.0)
    recovery_rate_m3_day: float | None = Field(default=None, gt=0.0)
    injection_months: int | None = Field(default=None, ge=1)
    recovery_months: int | None = Field(default=None, ge=1)
    n_cycles: int | None = Field(default=None, ge=1)

    # --- wetland_hydroperiod: seasonal water-table range under a wetland ---- #
    # A wetland footprint receives a per-period recharge schedule
    # (``recharge_schedule_m_day``, one rate per transient period) while EVT
    # removes water at the surface (EVT package: surface elevation, max rate,
    # extinction depth); the seasonal head range is the hydroperiod. All
    # optional/defaulted -> additive.
    wetland_footprint_lonlat: list[tuple[float, float]] | None = None
    recharge_schedule_m_day: list[float] | None = None
    et_surface_m: float | None = None
    et_max_rate_m_day: float | None = Field(default=None, gt=0.0)
    et_extinction_depth_m: float | None = Field(default=None, gt=0.0)
    specific_yield: float = Field(default=DEFAULT_WETLAND_SY, gt=0.0, le=1.0)

    # --- capture_zone / wellhead_protection: MF6 PRT backward particle tracking #
    # Both archetypes run the same two-simulation sequence: a GWF flow solve
    # (reusing ``well_location_latlon`` for the pumping well) followed by a PRT
    # backward-particle-tracking solve that releases particles around the well
    # screen and tracks them back to their capture origin. The difference is only
    # in framing and default travel-time tiers:
    #   capture_zone       - general zone-of-contribution (tiers: [1, 5, 10] years)
    #   wellhead_protection - EPA-style fixed-travel-time tiers (tiers: [2, 5, 10] years)
    # The adapter applies default tiers when ``capture_zone_travel_time_years`` is
    # None. Both archetypes are LOCAL-ONLY (PRT is fast; Batch is NOT used). All
    # optional/defaulted -> additive; ``None`` => byte-identical to other paths.
    capture_zone_travel_time_years: list[float] | None = Field(
        default=None,
        description=(
            "Travel-time isochrone tiers for the backward-particle-tracking capture "
            "zone, years. Each value defines one isochrone boundary: particles that "
            "reach the well within this time bound delineate the zone for that tier. "
            "When None the adapter uses archetype-specific defaults: [1, 5, 10] for "
            "capture_zone and [2, 5, 10] for wellhead_protection. Supplied values "
            "must be > 0; the adapter sorts them ascending before building the deck."
        ),
    )
    n_particles: int = Field(
        default=16,
        ge=4,
        le=256,
        description=(
            "Number of particles released around the pumping-well screen per "
            "backward-tracking solve. Particles are placed on a ring around the "
            "well cell at the start of the PRT simulation. More particles produce "
            "a denser pathline fan and a more representative capture-zone convex "
            "hull, at the cost of slightly longer PRT runtime. Default 16 is "
            "adequate for a demo; 32-64 improves shape fidelity for irregular "
            "flow fields. Bounds: [4, 256]."
        ),
    )
    prt_max_tracking_years: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Maximum total backward-tracking time for the PRT simulation, years. "
            "When None the adapter derives a safe limit from the longest requested "
            "travel-time tier (e.g. max(capture_zone_travel_time_years) * 1.5). "
            "Set explicitly to override the auto-derived limit (e.g. to cap runtime "
            "for a large domain). Must be > 0."
        ),
    )

    # --- saltwater_intrusion: Henry-style variable-density wedge (Wave-5) ----- #
    # A coastal vertical cross-section (nrow=1 slice) with a seaward GHB+AUX
    # supplying salt, a freshwater WEL+AUX on the inland boundary, and the
    # ModflowGwfbuy variable-density term coupling solute concentration to fluid
    # density. The Henry (1964) analytic benchmark is the canonical demo target.
    # ALL fields are optional/defaulted -> additive; ``None`` => byte-identical
    # to all prior paths. The user MUST supply coastal_transect_latlon; the
    # composer raises an InputError otherwise (Invariant 9 honesty gate -- a
    # coastline can NEVER be fabricated).
    coastal_transect_latlon: tuple[tuple[float, float], tuple[float, float]] | None = Field(
        default=None,
        description=(
            "Two ``(lat, lon)`` endpoints defining the coastal transect, EPSG:4326 "
            "(lat-first, same point convention as ``spill_location_latlon``). "
            "Endpoint A is the seaward (ocean) end of the transect; endpoint B is "
            "the inland end. The transect line A->B is the cross-section axis for "
            "the Henry-style variable-density GWT solve; the model grid runs from "
            "the inland boundary (WEL+AUX fresh) at column 0 to the seaward "
            "boundary (GHB+AUX salt) at the last column, and the intrusion length "
            "is measured from the seaward edge inland. "
            "REQUIRED for the saltwater_intrusion archetype -- the composer MUST "
            "raise an InputError (Invariant 9) if this is None when the archetype "
            "is 'saltwater_intrusion'. A coastline can NEVER be fabricated. "
            "When None (the default) the field is ignored and the deck is byte-"
            "identical to all prior paths."
        ),
    )
    seawater_salinity_ppt: float = Field(
        default=35.0,
        gt=0.0,
        description=(
            "Seawater salinity at the seaward (GHB+AUX) boundary, ppt (parts per "
            "thousand, g/kg). Applied as the initial concentration in the seaward "
            "column (IC) and as the AUX auxiliary variable on the GHB boundary "
            "package to supply salt into the domain. 35.0 ppt is the open-ocean "
            "demo default (narrated as a demo value). Estuarine or coastal lagoon "
            "problems may use lower values (e.g. 25 ppt). Must be > 0."
        ),
    )
    n_vertical_layers: int = Field(
        default=20,
        ge=4,
        le=80,
        description=(
            "Number of vertical model layers in the cross-section grid, nlay. The "
            "horizontal nrow is always 1 (a 2-D vertical slice); ncol is derived "
            "from the transect length and a target cell width. More layers resolve "
            "the density interface (saltwater toe) more sharply at the cost of "
            "longer GWT solve times. Demo default 20 is adequate for the Henry "
            "benchmark; increase to 40-80 for a sharper wedge interface in longer "
            "transects. Bounds: [4, 80]."
        ),
    )
    freshwater_inflow_m3_day: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Freshwater inflow rate applied at the inland WEL+AUX boundary, m^3/day "
            "(POSITIVE magnitude; the adapter applies the MF6 WEL sign). When None "
            "the adapter derives a Henry-benchmark-representative inflow from the "
            "transect geometry and the aquifer hydraulic conductivity (Q = K * i * A "
            "with a gentle demo gradient). Supplying an explicit value overrides the "
            "auto-derived flux, allowing the user to explore how terrestrial freshwater "
            "discharge pushes back the saltwater wedge toe. Must be > 0."
        ),
    )

    @field_validator("spill_location_latlon")
    @classmethod
    def _validate_latlon(cls, value: tuple[float, float]) -> tuple[float, float]:
        """Enforce ``(lat, lon)`` ranges: lat in [-90, 90], lon in [-180, 180]."""
        lat, lon = value
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(
                f"spill_location_latlon latitude out of range [-90, 90]: {lat!r} "
                f"(expected (lat, lon) order)"
            )
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(
                f"spill_location_latlon longitude out of range [-180, 180]: {lon!r} "
                f"(expected (lat, lon) order)"
            )
        return value

    @field_validator("well_location_latlon")
    @classmethod
    def _validate_well_latlon(
        cls, value: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        """Enforce ``(lat, lon)`` ranges on the pumping well, when supplied."""
        if value is None:
            return None
        lat, lon = value
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(
                f"well_location_latlon latitude out of range [-90, 90]: {lat!r} "
                f"(expected (lat, lon) order)"
            )
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(
                f"well_location_latlon longitude out of range [-180, 180]: {lon!r} "
                f"(expected (lat, lon) order)"
            )
        return value


class PlumeLayerURI(LayerURI):
    """A ``LayerURI`` for a MODFLOW plume layer, plus narration scalars.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the two structured numbers the agent narrates about the plume so the
    LLM cites typed fields, never invents them (invariant 1, FR-AS-7):

        max_concentration_mgl: peak contaminant concentration in the plume,
            mg/L (>= 0).
        plume_area_km2: areal footprint of the plume above the detection
            threshold, km^2 (>= 0).

    ``layer_type`` for a plume is typically ``"raster"`` (a concentration COG),
    but the base contract's vocabulary is inherited unchanged - no new format
    set is introduced (rasters COG; vectors FlatGeobuf/GeoParquet).
    """

    max_concentration_mgl: float = Field(ge=0.0)
    plume_area_km2: float = Field(ge=0.0)


class MultiSpeciesPlumeResult(GraceModel):
    """The output carrier for a multi_species MODFLOW run (Wave-3 - ADDITIVE).

    A multi_species run produces N plumes - one per ``SpeciesSpec``. Each plume
    REUSES ``PlumeLayerURI`` field-for-field (so each still maps onto
    ``map-command load-layer`` with no translation, and the agent narrates each
    species' ``max_concentration_mgl`` / ``plume_area_km2`` from typed fields).
    This is a thin typed carrier the composer returns so the ordered list of
    per-species plumes round-trips as one structured object; it does NOT
    introduce a new ``LayerURI`` - the per-species layer stays ``PlumeLayerURI``.

    The single-contaminant path is UNAFFECTED: it returns a single
    ``PlumeLayerURI`` as before. This carrier is used only by the
    ``archetype="multi_species"`` composer return.

    Fields:
        plumes: ordered list of one ``PlumeLayerURI`` per species (same order as
            ``MODFLOWRunArgs.species``). At least one plume.
    """

    plumes: list[PlumeLayerURI] = Field(min_length=1)


class SeepageLayerURI(LayerURI):
    """A ``LayerURI`` for the river-seepage (RIV leakage) layer + narration scalars.

    The companion of ``PlumeLayerURI`` for the sprint-17 river-coupled MODFLOW
    engine. The postprocess reads the GWF cell-by-cell budget RIV term - the
    per-reach-cell head-dependent exchange flux Q = C*(stage - h) - and renders
    a DIVERGING gaining/losing-stream COG (negative = the river GAINS water from
    the aquifer i.e. baseflow OUT of the aquifer; positive = the river LOSES
    water to the aquifer i.e. seepage INTO the aquifer, MF6 RIV sign convention:
    a positive budget ``q`` is flow FROM the boundary INTO the cell, so positive
    = aquifer-recharging losing reach).

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command
    load-layer`` with no translation (same as every other layer). Adds the
    structured numbers the agent narrates about the river<->aquifer exchange so
    the LLM cites typed fields, never invents them (invariant 1, FR-AS-7):

        total_leakage_m3_day: net signed RIV exchange summed over all reach
            cells, m^3/day (positive = net losing/recharging the aquifer).
        gaining_m3_day: total magnitude of the GAINING (river-gaining-from-
            aquifer, baseflow) flux over the reach, m^3/day (>= 0).
        losing_m3_day: total magnitude of the LOSING (river-losing-to-aquifer,
            seepage) flux over the reach, m^3/day (>= 0).
        river_cell_count: number of RIV reach cells draped onto the grid (>= 0).

    ``layer_type`` is ``"raster"`` (a diverging seepage COG); the base
    contract's format vocabulary is inherited unchanged.
    """

    total_leakage_m3_day: float
    gaining_m3_day: float = Field(ge=0.0)
    losing_m3_day: float = Field(ge=0.0)
    river_cell_count: int = Field(ge=0)


class DrawdownLayerURI(LayerURI):
    """A ``LayerURI`` for the sustainable-yield drawdown layer + narration scalars.

    The headline output of the ``"sustainable_yield"`` archetype: the postprocess
    reads the transient GWF head (.hds) and renders head-DECLINE (pre-pumping head
    minus pumped head) as a COG so the user sees the cone of depression a pumping
    well draws down around it.

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation (same as every other layer). Adds the structured numbers
    the agent narrates so the LLM cites typed fields, never invents them
    (invariant 1, FR-AS-7):

        max_drawdown_m: peak head decline anywhere in the domain, m (>= 0).
        head_decline_timeseries: OPTIONAL per-step head decline at the well (or a
            monitoring cell), m, one value per saved transient step. None when the
            run published a single steady/peak frame with no time series.

    ``layer_type`` is ``"raster"`` (a drawdown COG); the base contract's format
    vocabulary is inherited unchanged.
    """

    max_drawdown_m: float = Field(ge=0.0)
    head_decline_timeseries: list[float] | None = None


class DewaterLayerURI(LayerURI):
    """A ``LayerURI`` for the mine-dewatering DRN-flux layer + narration scalars.

    The headline output of the ``"mine_dewatering"`` archetype: the postprocess
    reads the GWF cell-by-cell budget DRN term (the per-cell head-dependent drain
    flux Q = C*(h - drain_elev)) over the pit footprint and renders the dewatering
    rate as a COG, narrating the total water the pit must pump to stay dewatered.

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation (same as every other layer). Adds the structured numbers
    the agent narrates so the LLM cites typed fields, never invents them
    (invariant 1, FR-AS-7):

        dewatering_rate_m3_day: total DRN outflow magnitude summed over the pit
            drain cells, m^3/day (>= 0) - the pumping rate the pit needs.
        drain_cell_count: number of DRN drain cells draped onto the grid (>= 0).

    ``layer_type`` is ``"raster"`` (a dewatering-rate COG); the base contract's
    format vocabulary is inherited unchanged.
    """

    dewatering_rate_m3_day: float = Field(ge=0.0)
    drain_cell_count: int = Field(ge=0)


class BudgetPartitionLayerURI(LayerURI):
    """A ``LayerURI`` for the regional-water-budget zonal partition + scalars.

    The headline output of the ``"regional_water_budget"`` archetype: the
    postprocess reads the GWF cell-by-cell budget and partitions the flow terms
    (CHD in/out, RIV, WEL, storage) by zone, narrating where the regional water
    goes. The ``layer_type`` may be ``"vector"`` (a per-zone polygon carrying the
    partitioned budget) or ``"raster"`` (a zone-id raster); the base contract's
    format vocabulary is inherited unchanged.

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation (same as every other layer). Adds the structured budget the
    agent narrates so the LLM cites typed fields, never invents them
    (invariant 1, FR-AS-7):

        budget_partition_m3_day: mapping of zone/term label -> signed flow rate,
            m^3/day (positive = into the aquifer/zone, MF6 budget sign convention).
            e.g. ``{"upgradient_chd_in": 1200.0, "downgradient_chd_out": -1180.0,
            "storage": -20.0}``.
    """

    budget_partition_m3_day: dict[str, float]


class MoundingLayerURI(LayerURI):
    """A ``LayerURI`` for the MAR groundwater-mounding layer + narration scalars.

    The headline output of the ``"MAR"`` (managed aquifer recharge) archetype: the
    postprocess reads the transient GWF head (.hds) and renders the mound (pumped/
    recharged head minus pre-recharge head) as a COG so the user sees how high the
    water table rises under the infiltration basin.

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation (same as every other layer). Adds the structured numbers
    the agent narrates so the LLM cites typed fields, never invents them
    (invariant 1, FR-AS-7):

        max_mounding_m: peak head RISE (mounding) anywhere in the domain, m (>= 0).
        recharged_volume_m3: OPTIONAL total volume of water recharged into the
            aquifer over the simulation, m^3 (>= 0). None when not computed.

    ``layer_type`` is ``"raster"`` (a mounding COG); the base contract's format
    vocabulary is inherited unchanged.
    """

    max_mounding_m: float = Field(ge=0.0)
    recharged_volume_m3: float | None = Field(default=None, ge=0.0)


class ASRLayerURI(LayerURI):
    """A ``LayerURI`` for the ASR (aquifer storage & recovery) layer + scalars.

    The headline output of the ``"ASR"`` archetype: the postprocess reads the
    transient GWF head (.hds) at the ASR well and renders a representative head
    surface as a COG, narrating the cyclic inject/recover storage behavior and the
    recovery efficiency (the fraction of injected water recovered).

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation (same as every other layer). Adds the structured numbers
    the agent narrates so the LLM cites typed fields, never invents them
    (invariant 1, FR-AS-7):

        recovery_efficiency: OPTIONAL fraction (dimensionless, 0..1) of injected
            water recovered over the ASR cycle(s). None when not computed.
        head_timeseries: OPTIONAL per-step head at the ASR well, m, one value per
            saved transient step (the inject-rise / recover-fall sawtooth). None
            when the run published a single frame with no time series.

    ``layer_type`` is ``"raster"`` (a head COG); the base contract's format
    vocabulary is inherited unchanged.
    """

    recovery_efficiency: float | None = Field(default=None, ge=0.0, le=1.0)
    head_timeseries: list[float] | None = None


class HydroperiodLayerURI(LayerURI):
    """A ``LayerURI`` for the wetland-hydroperiod layer + narration scalars.

    The headline output of the ``"wetland_hydroperiod"`` archetype: the
    postprocess reads the transient GWF head (.hds) under the wetland footprint
    and renders the seasonal head-range (max minus min water table over the
    transient periods) as a COG, narrating how much the wetland water table swings
    across the recharge/ET seasons.

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation (same as every other layer). Adds the structured numbers
    the agent narrates so the LLM cites typed fields, never invents them
    (invariant 1, FR-AS-7):

        seasonal_head_range_m: the seasonal water-table swing (max head minus min
            head over the wetland) m (>= 0).
        head_timeseries: OPTIONAL per-step head under the wetland, m, one value
            per saved transient step (the seasonal rise/fall). None when the run
            published a single frame with no time series.

    ``layer_type`` is ``"raster"`` (a seasonal-range COG); the base contract's
    format vocabulary is inherited unchanged.
    """

    seasonal_head_range_m: float = Field(ge=0.0)
    head_timeseries: list[float] | None = None


class CaptureZoneLayerURI(LayerURI):
    """A ``LayerURI`` for a MODFLOW PRT backward-particle-tracking capture zone
    (Wave-4 - ADDITIVE).

    The headline output of the ``"capture_zone"`` and ``"wellhead_protection"``
    archetypes. Both run a two-simulation sequence: a GWF groundwater-flow solve
    followed by an MF6 PRT backward-particle-tracking solve. PRT releases
    ``n_particles`` particles around the pumping-well screen and tracks them
    backward in time; the convex hull of all backtracked pathlines at each
    requested travel-time threshold is the capture zone isochrone for that tier.

    IMPORTANT PRECISION CAVEAT -- this is the FIRST vector MODFLOW ``LayerURI``
    (``layer_type='vector'``). The polygon is the CONVEX HULL of discrete
    backtracked pathlines on a structured rectilinear grid with demo aquifer
    parameters, NOT a calibrated regulatory wellhead protection area. Treat it as
    a qualitative planning envelope, not a legally defensible delineation. The
    agent must narrate this caveat when presenting the layer to the user
    (invariant 1, FR-AS-7).

    The difference between the two archetypes is framing and default travel-time
    tiers only; both produce the same carrier:
        capture_zone       - general zone-of-contribution framing.
        wellhead_protection - EPA-style fixed-travel-time framing (typical
            tiers: 2 / 5 / 10 years; EPA wellhead protection program under SDWA
            Section 1428, delineation per EPA 440/6-87-010).

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the structured numbers the agent narrates so the LLM cites typed fields,
    never invents them (invariant 1, FR-AS-7):

        capture_zone_area_km2: area of the outer isochrone envelope (the hull of
            all pathlines regardless of tier), km^2 (>= 0). This is the broadest
            extent of the capture zone - useful as a headline scalar.
        travel_time_years: the isochrone travel-time tiers actually computed, years
            (e.g. [1, 5, 10] for capture_zone or [2, 5, 10] for
            wellhead_protection). One tier may be dropped if no particles reached
            that distance within the tracking window; the agent narrates gaps.
        isochrone_areas_km2: per-tier nested area, km^2 (>= 0 for each value).
            Keys are the tier durations as strings (e.g. ``{"1": 0.05, "5": 0.32,
            "10": 1.4}``); the value is the area of the convex hull for particles
            captured within that travel-time threshold. Narrated tier-by-tier.
        particle_count: number of particles actually released in the PRT solve
            (matches ``MODFLOWRunArgs.n_particles`` unless the well cell was on
            the domain boundary and some release positions were clipped). >= 0.

    ``layer_type`` defaults to ``'vector'`` (a FlatGeobuf polygon carrying the
    isochrone tiers as feature attributes); the base contract's format vocabulary
    is inherited unchanged. This is the first vector MODFLOW layer -- all prior
    MODFLOW LayerURI subclasses are ``'raster'``.
    """

    layer_type: Literal["raster", "vector"] = "vector"

    capture_zone_area_km2: float = Field(
        ge=0.0,
        description=(
            "Area of the outer capture-zone isochrone envelope (convex hull of ALL "
            "backtracked pathlines at the longest requested travel-time tier), km^2. "
            "This is the broadest headline extent. The polygon is a planning-level "
            "envelope; see the class docstring precision caveat."
        ),
    )
    travel_time_years: list[float] = Field(
        min_length=1,
        description=(
            "Travel-time isochrone tiers actually computed, years (one float per "
            "tier). Matches ``MODFLOWRunArgs.capture_zone_travel_time_years`` after "
            "applying defaults; a tier may be absent if no particles reached that "
            "distance within the PRT tracking window."
        ),
    )
    isochrone_areas_km2: dict[str, float] = Field(
        description=(
            "Per-tier nested isochrone area, km^2. Keys are tier durations as "
            "strings (e.g. '1', '5', '10'); values are the convex-hull area of "
            "backtracked pathlines captured within that travel-time threshold. "
            "All values >= 0. The agent narrates each tier so the user understands "
            "the zone-of-contribution at each time scale."
        ),
    )
    particle_count: int = Field(
        ge=0,
        description=(
            "Number of particles actually released in the PRT backward-tracking "
            "solve. Normally equals ``MODFLOWRunArgs.n_particles``; may be slightly "
            "lower if the well cell was near the domain boundary and some release "
            "positions were clipped to valid grid cells."
        ),
    )


class SaltwaterWedgeLayerURI(LayerURI):
    """A ``LayerURI`` for a MODFLOW Henry-style variable-density saltwater intrusion
    cross-section (Wave-5 - ADDITIVE).

    The headline output of the ``"saltwater_intrusion"`` archetype. A GWF+GWT
    single-simulation sequence on a nrow=1 vertical cross-section (the Henry 1964
    benchmark geometry) with the ModflowGwfbuy variable-density term coupling the
    GWT salinity field to the GWF fluid density. The seaward boundary (GHB+AUX)
    supplies salt; the inland boundary (WEL+AUX) injects freshwater; the steady-
    state concentration field reveals the saltwater wedge and its toe penetration.

    IMPORTANT PRECISION CAVEAT -- this is a DEMO Henry-style variable-density
    cross-section on a structured rectilinear grid with demo aquifer parameters,
    NOT a site-calibrated saltwater intrusion model. The intrusion_length_m is
    the bottom-layer 50%-isochlor (50% of seawater_salinity_ppt) toe penetration
    measured from the seaward boundary; it is a qualitative planning metric, NOT
    a regulatory or engineering delineation. The agent must narrate this caveat
    when presenting the layer to the user (invariant 1, FR-AS-7).

    The PRIMARY product is a Vega-Lite cross-section heatmap chart (emitted via
    pipeline_emitter.emit_chart_payloads) showing salinity vs. distance inland
    and depth, with the 50% isochlor as an overlaid rule/line. This
    ``SaltwaterWedgeLayerURI`` carries the MAP element: a FlatGeobuf VECTOR in
    EPSG:4326 containing the coastal transect line (A->B from the manifest
    transect endpoints) and a toe POINT at the 50%-isochlor penetration along
    that line. The vector geo-contextualizes the cross-section on the map;
    the chart carries the physics. Neither the chart nor the vector should be
    over-interpreted as a calibrated result.

    ``layer_type`` defaults to ``'vector'`` (a FlatGeobuf transect line + toe
    point, NOT a raster COG). This is the second vector MODFLOW ``LayerURI``
    after ``CaptureZoneLayerURI``.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the structured numbers the agent narrates so the LLM cites typed fields,
    never invents them (invariant 1, FR-AS-7):

        intrusion_length_m: bottom-layer 50%-isochlor toe penetration measured
            inland from the seaward boundary, m (>= 0). This is the HEADLINE
            SCALAR: how far salt has pushed into the aquifer at the base of the
            domain where the wedge toe is deepest. Narrated as a demo metric;
            do NOT oversell calibration accuracy.
        toe_distance_m: distance from the seaward boundary to the 50%-isochlor
            toe in the BOTTOM model layer, m (>= 0). Alias for
            ``intrusion_length_m`` retained for downstream compatibility;
            both fields are populated from the same measurement.
        seaward_salinity_ppt: actual peak salinity in the domain at the seaward
            boundary (the GHB+AUX applied concentration), ppt. Recorded from the
            run manifest; matches ``MODFLOWRunArgs.seawater_salinity_ppt`` unless
            the adapter applied a cap. The 50%-isochlor threshold is derived from
            this value (threshold = 0.5 * seaward_salinity_ppt).
        transect_endpoints: the A->B coastal transect endpoints as two ``(lat,
            lon)`` pairs, EPSG:4326 (lat-first, same point convention as
            ``MODFLOWRunArgs.coastal_transect_latlon``). Endpoint A is seaward;
            endpoint B is inland. Recorded from the run manifest so the
            postprocessor can geolocate the cross-section and toe point without
            re-reading the run args.
    """

    layer_type: Literal["raster", "vector"] = "vector"

    intrusion_length_m: float = Field(
        ge=0.0,
        description=(
            "Bottom-layer 50%-isochlor toe penetration measured inland from the "
            "seaward boundary, m (>= 0). Headline scalar: how far salt has pushed "
            "into the aquifer at the deepest part of the wedge. Demo Henry-style "
            "result; narrate as a qualitative planning metric, not a calibrated "
            "delineation (see class docstring caveat)."
        ),
    )
    toe_distance_m: float = Field(
        ge=0.0,
        description=(
            "Distance from the seaward boundary to the 50%-isochlor toe in the "
            "BOTTOM model layer, m (>= 0). Alias for ``intrusion_length_m`` "
            "retained for downstream compatibility; both fields carry the same "
            "measurement. >= 0."
        ),
    )
    seaward_salinity_ppt: float = Field(
        description=(
            "Peak salinity applied at the seaward GHB+AUX boundary, ppt. Recorded "
            "from the run manifest; matches ``MODFLOWRunArgs.seawater_salinity_ppt`` "
            "unless the adapter applied a cap. The 50%-isochlor threshold is "
            "0.5 * seaward_salinity_ppt. Narrated alongside intrusion_length_m so "
            "the user knows which salinity level defines the wedge toe."
        ),
    )
    transect_endpoints: tuple[tuple[float, float], tuple[float, float]] = Field(
        description=(
            "A->B coastal transect endpoints as two ``(lat, lon)`` pairs, "
            "EPSG:4326 (lat-first). Endpoint A is the seaward (ocean) end; "
            "endpoint B is the inland end. Recorded from the run manifest so the "
            "postprocessor can geolocate the transect LINE and the toe POINT on "
            "the map without re-reading the run args. Must match "
            "``MODFLOWRunArgs.coastal_transect_latlon`` exactly."
        ),
    )


class StreamReachLayerURI(LayerURI):
    """A ``LayerURI`` for the SFR routed stream-depletion reach network (module wave).

    The headline output of the ``"stream_depletion"`` archetype: a MODFLOW-6 SFR6
    stream network is draped from the fetched NHDPlus flowline onto the model grid
    as path-ordered reaches, coupled to a pumping WEL well. The postprocess parses
    the per-reach SFR observation CSV (``<gwf>.sfr.obs.csv``: stage, downstream-
    flow, and the reach<->aquifer ``sfr`` exchange term) and renders a per-reach
    polyline FlatGeobuf carrying, per reach, the routed discharge, stage, the
    signed exchange, and the pumping-induced depletion (baseline-vs-pumped delta).

    SIGN CONVENTIONS (pinned by the local mf6 6.5.0 smoke fixture, per feature
    attribute): the SFR ``downstream-flow`` obs is reported NEGATIVE (an outflow
    magnitude); the feature ``flow_m3_day`` carries its ABSOLUTE value (the routed
    discharge). The SFR ``sfr`` exchange obs is REACH-RELATIVE from the stream's
    water balance: POSITIVE = the reach LOSES water to the aquifer (a losing
    reach, stream->aquifer leakage); NEGATIVE = the aquifer FEEDS the reach (a
    gaining reach, baseflow into the stream). Streamflow depletion for the run is
    ``sum(exchange at the pumped period) - sum(exchange at the baseline period)``,
    a POSITIVE number = the streamflow the well captured. Gaining/losing reach
    counts are classified from the pumped-period per-reach exchange sign.

    Like ``CaptureZoneLayerURI`` / ``SaltwaterWedgeLayerURI`` this is a VECTOR
    layer (``layer_type='vector'``) reaching the client through the inline-GeoJSON
    ``add_loaded_layer`` path, NOT the raster-only ``publish_layer``.

    IMPORTANT PRECISION CAVEAT -- the aquifer K/Sy, the channel width, the Manning
    roughness, and the streambed K are DEMO DEFAULTS with no site-specific fetcher.
    Treat the depletion fraction as a qualitative planning estimate (the streambed
    resistance keeps it below the Glover-Balmer analytic curve; an honest,
    explainable gap), NOT a calibrated water-rights determination. The agent must
    narrate this caveat (invariant 1, FR-AS-7).

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation. Adds the structured numbers the agent narrates so the LLM
    cites typed fields, never invents them (invariant 1, FR-AS-7):

        total_depletion_m3_day: net streamflow captured from the stream by the
            pumping (sum of the pumped-vs-baseline exchange delta over all
            reaches), m^3/day (>= 0). The headline scalar.
        depletion_fraction: total_depletion_m3_day / pumping_rate_m3_day,
            dimensionless in [0, ~1] (a well captures at most its own pumping from
            the stream at steady state; typically 0.4-0.8 for a near-stream well).
        n_reaches: number of SFR reaches draped onto the grid (>= 1).
        max_stage_decline_m: the largest per-reach stage drop from baseline to
            pumped, m (>= 0) - how much the pumping lowered any reach's water
            surface.
        gaining_reach_count: number of reaches GAINING from the aquifer at the
            pumped period (exchange < 0), >= 0.
        losing_reach_count: number of reaches LOSING to the aquifer at the pumped
            period (exchange > 0), >= 0.
    """

    layer_type: Literal["raster", "vector"] = "vector"

    total_depletion_m3_day: float = Field(
        ge=0.0,
        description=(
            "Net streamflow captured from the stream by the pumping well, m^3/day "
            "(>= 0). Computed as sum over reaches of (pumped-period SFR<->GWF "
            "exchange minus baseline-period exchange); positive = depletion "
            "(capture). The headline scalar the honesty floor checks."
        ),
    )
    depletion_fraction: float = Field(
        ge=0.0,
        description=(
            "total_depletion_m3_day divided by the well pumping rate, "
            "dimensionless. At steady state a well captures at most its own "
            "pumping from the stream (fraction ~<= 1); a near-stream well is "
            "typically 0.4-0.8. Streambed resistance keeps it below the "
            "Glover-Balmer analytic curve (an honest, explainable gap)."
        ),
    )
    n_reaches: int = Field(
        ge=1,
        description="Number of SFR reaches draped onto the model grid (>= 1).",
    )
    max_stage_decline_m: float = Field(
        ge=0.0,
        description=(
            "Largest per-reach stream-stage drop from the baseline period to the "
            "pumped period, m (>= 0) - how much the pumping lowered any reach's "
            "water surface."
        ),
    )
    gaining_reach_count: int = Field(
        ge=0,
        description=(
            "Number of reaches GAINING water from the aquifer at the pumped period "
            "(SFR exchange < 0, aquifer->stream baseflow). >= 0."
        ),
    )
    losing_reach_count: int = Field(
        ge=0,
        description=(
            "Number of reaches LOSING water to the aquifer at the pumped period "
            "(SFR exchange > 0, stream->aquifer leakage). >= 0."
        ),
    )


class SubsidenceLayerURI(LayerURI):
    """A ``LayerURI`` for the CSUB pumping-induced land-subsidence bowl (module wave).

    The headline output of the ``"land_subsidence"`` archetype: a MODFLOW-6 CSUB
    package layered onto the transient pumping (WEL) deck computes aquifer-system
    compaction as the pumping drawdown pushes the effective stress in a
    compressible interbed past its preconsolidation, and writes a per-cell
    z-displacement grid whose FINAL frame is the cumulative subsidence bowl. The
    postprocess reprojects that final frame to an EPSG:4326 COG (cm) -- so, UNLIKE
    the SFR ``StreamReachLayerURI`` VECTOR, this is a RASTER layer
    (``layer_type='raster'``) reaching the client through ``publish_layer``, the
    same raster path as ``DrawdownLayerURI``.

    SIGN CONVENTION (PINNED by the local mf6 6.5.0 smoke fixture,
    services/workers/modflow/fixtures/csub_smoke): downward subsidence/compaction
    is reported POSITIVE. The CSUB z-displacement grid (HeadFile text tag
    ``CSUB-ZDISPLACE`` -- truncated to 16 chars, NOT ``CSUB-ZDISPLACEMENT``) is
    positive-down at the pumped cell on the real binary; the postprocess owns this
    convention so the agent never narrates subsidence as uplift. Units are metres
    in the binary; ``max_subsidence_cm`` is metres * 100.

    STORAGE double-count guard (the #1 CSUB trap, pinned in the smoke): CSUB
    supplies the coarse skeletal storage, so the reused STO ``ss`` is dropped to 0
    when CSUB is present -- mf6 6.5.0 HARD-ERRORS ("Specific storage values in the
    storage (STO) package must be zero in all active cells when using the CSUB
    package") if it is not, so the fix is engine-enforced and the CSUB-run head
    decline matches the plain sustainable_yield run (proven within 0.00% in the
    smoke).

    IMPORTANT PRECISION CAVEAT -- the aquifer K/Ss/Sy, the interbed thickness, and
    the inelastic/elastic compaction indices are DEMO DEFAULTS with no
    site-specific fetcher (v1). The HEAD_BASED formulation bypasses geostatic
    stress (honest on a flat demo grid, cannot represent depth-dependent
    effective-stress evolution -- that is v2). Treat the subsidence magnitude as a
    qualitative planning estimate, NOT a calibrated Central Valley forecast. The
    agent must narrate this caveat (invariant 1, FR-AS-7).

    Extends ``LayerURI`` field-for-field so it maps onto ``map-command load-layer``
    with no translation. Adds the structured numbers the agent narrates so the LLM
    cites typed fields, never invents them (invariant 1, FR-AS-7):

        max_subsidence_cm: peak cumulative ground subsidence over the pumping
            horizon, cm (>= 0, positive-down). The headline scalar.
        subsidence_area_km2: area of cells whose subsidence exceeds a small floor,
            km^2 (>= 0) -- the footprint of the subsidence bowl.
        max_head_decline_m: the largest head drop from the baseline to the final
            period, m (>= 0) -- the drawdown that forced the compaction.
        inelastic_fraction: share of the total compaction that is INELASTIC
            (permanent, non-recoverable), dimensionless in [0, 1]. Near 1.0 when
            preconsolidation == the initial head (the correct signature that
            pumping past preconsolidation produces PERMANENT subsidence).
        interbed_count: number of CSUB interbeds over the pumped footprint (>= 1).
    """

    layer_type: Literal["raster", "vector"] = "raster"

    max_subsidence_cm: float = Field(
        ge=0.0,
        description=(
            "Peak cumulative ground subsidence over the pumping horizon, cm "
            "(>= 0, positive-down per the pinned CSUB sign convention). The final "
            "z-displacement grid's maximum. The headline scalar the honesty floor "
            "checks."
        ),
    )
    subsidence_area_km2: float = Field(
        ge=0.0,
        description=(
            "Area of grid cells whose final subsidence exceeds a small floor, "
            "km^2 (>= 0) -- the footprint of the subsidence bowl."
        ),
    )
    max_head_decline_m: float = Field(
        ge=0.0,
        description=(
            "Largest head decline from the baseline period to the final pumped "
            "period, m (>= 0) -- the pumping drawdown that forced the compaction."
        ),
    )
    inelastic_fraction: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Share of the total compaction that is INELASTIC (permanent, "
            "non-recoverable), dimensionless in [0, 1]. Near 1.0 when the "
            "preconsolidation head == the initial head (the physically-correct "
            "signature that pumping past preconsolidation produces PERMANENT "
            "subsidence, not recoverable elastic rebound)."
        ),
    )
    interbed_count: int = Field(
        ge=1,
        description=(
            "Number of CSUB interbeds draped over the pumped footprint (>= 1)."
        ),
    )
