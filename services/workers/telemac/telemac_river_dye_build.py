"""TELEMAC-2D river-dye pipeline (PHASE 1): REAL river reach -> dye transport.

Standalone, parameterized builder that takes a real river reach (fetched from
USGS NLDI NHDPlus flowlines) + a Copernicus GLO-30 DEM bed, meshes the
channel-following polygon with Gmsh (tagged inflow/outflow/wall physical
groups), authors a TELEMAC-2D TRACER (.cas) steering file, solves locally, and
renders the dye advecting down the REAL river curves.

This is the artifact the P2 worker image will call. Factored into:
  fetch_river_centerline() -> real NHDPlus geometry
  process_centerline()     -> project/resample/smooth to UTM meters
  fetch_dem_bed()          -> Copernicus GLO-30 DEM sample (USGS 3DEP fallback)
  build_channel_mesh()     -> Gmsh mesh (all P0 gotchas honored)
  assign_bed()             -> DEM onto nodes + gentle downstream slope
  write_slf() / write_cli()-> SELAFIN geometry + boundary conditions
  author_deck()            -> .cas with liquid-boundary mapping from the listing
  run_solver()             -> telemac2d.py (delete-empty-result gotcha)
  map_liquid_boundaries()  -> parse solver listing to map inflow/outflow BCs

HARD-WON P0 GOTCHAS honored (see build_gmsh_channel.py):
  (1) SELAFIN connectivity is 0-BASED
  (2) node array from triangle-referenced tags only (drop gmsh orphans)
  (3) IPOBO is rank-based (ring-walk order 1..nptfr)
  (4) liquid boundaries numbered by boundary-WALK order -> read the listing
  (5) tracer scheme = method-of-characteristics (scheme 1)
  (6) delete any empty pre-existing result .slf before running
  (7) meander bend radius > ~0.75*channel width (enforced via smoothing)

ASCII only. No product/agent code touched.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

LOG = logging.getLogger("trid3nt.worker.telemac.build")
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class ReachConfig:
    name: str = "snake_river_twin_falls"
    seed_lon: float = -114.307          # a point on the reach (NLDI snaps to COMID)
    seed_lat: float = 42.579
    nav_direction: str = "DM"           # downstream main
    distance_km: float = 6.0
    channel_width_m: float = 60.0       # Snake River near Twin Falls is broad
    mesh_size_m: float = 14.0
    resample_ds_m: float = 18.0         # centerline resample spacing
    smooth_window: int = 7              # centerline smoothing (odd)
    # hydraulics
    inflow_q_m3s: float = 250.0         # steady upstream discharge
    init_depth_m: float = 2.5           # initial constant water depth
    dye_conc_mgl: float = 100.0         # dye concentration of the spill pulse
    # TELEMAC-PHYS-1 constitutive-physics overrides (advanced / demo-default
    # levers). Each defaults to None -> author_deck emits the EXACT historical
    # literal string (byte-identical), so an unset run is unchanged. A set value
    # flows to the deck line. Ranges are clamped upstream (physics_registry /
    # the tool). See author_deck friction/diffusion block.
    #   friction_law:          LAW OF BOTTOM FRICTION (3=Strickler,4=Manning,2=Chezy)
    #   friction_coefficient:  FRICTION COEFFICIENT (Strickler Ks -> flow velocity)
    #   velocity_diffusivity:  VELOCITY DIFFUSIVITY (turbulent momentum nu_t, m2/s)
    #   tracer_diffusivity:    COEFFICIENT FOR DIFFUSION OF TRACERS (plume spread, m2/s)
    friction_law: int = None            # type: ignore[assignment]
    friction_coefficient: float = None  # type: ignore[assignment]
    velocity_diffusivity: float = None  # type: ignore[assignment]
    tracer_diffusivity: float = None    # type: ignore[assignment]
    # WIND-STRESS FORCING (TELEMAC-2D wind term). wind_speed_mps default 0.0
    # leaves the deck byte-identical: author_deck emits NO wind block and WIND
    # stays NO. A positive speed activates a spatially/temporally CONSTANT wind
    # (OPTION FOR WIND = 1): the deck sets WIND = YES + WIND VELOCITY ALONG X/Y
    # (resolved from speed + a meteorological FROM-direction into UTM x=east /
    # y=north components) + COEFFICIENT OF WIND INFLUENCE (gaia/t2d dico default
    # 1.55E-6 unless overridden) + THRESHOLD DEPTH FOR WIND = 1 m (wind not
    # applied on drying margins). Sustained wind sets up a free-surface slope /
    # drives circulation on a wide reach, embayment or lake. Keywords pinned
    # against telemac2d.dico v9.0 (WIND/OPTION FOR WIND/WIND VELOCITY ALONG X,Y/
    # COEFFICIENT OF WIND INFLUENCE/THRESHOLD DEPTH FOR WIND).
    wind_speed_mps: float = 0.0         # sustained wind speed (0 -> no wind)
    wind_dir_from_deg: float = 0.0      # meteorological: direction wind blows FROM
    wind_drag_coef: float = None        # type: ignore[assignment]  # None -> dico default
    # DISTRIBUTED ON-MESH RAINFALL / EVAPORATION forcing (TELEMAC-2D native
    # source term). Default None leaves the deck byte-identical: author_deck
    # emits NO rain block and RAIN OR EVAPORATION stays absent (= NO). A set
    # value activates a spatially-uniform, temporally-constant water flux applied
    # at EVERY wet mesh node (distinct from the inflow-boundary hydrograph): the
    # deck sets RAIN OR EVAPORATION = YES + RAIN OR EVAPORATION IN MM PER DAY =
    # <value>. TELEMAC's single signed keyword: POSITIVE = rainfall (adds water,
    # raises stage, wets tidal flats), NEGATIVE = evaporation (removes water).
    # The rate is a real gridMET storm total (mm/day) resolved by the composer,
    # or a user override. Keyword pinned against telemac2d.dico v9.0 (RAIN OR
    # EVAPORATION / RAIN OR EVAPORATION IN MM PER DAY).
    rain_or_evap_mm_per_day: float = None  # type: ignore[assignment]  # None -> no rain block
    # FINITE SPILL PULSE (realism default): a mid-reach point source injects dye
    # for a short window then stops, so the slug TRAVELS downstream and dilutes
    # rather than the old continuous upstream-inflow injection saturating the
    # whole reach. Clean flow (inflow->outflow) still drives it.
    spill_frac: float = 0.25            # along-channel position of the spill (0=up,1=down)
    # release-point picker: explicit spill location (EPSG:4326). When BOTH
    # are set they OVERRIDE spill_frac - the source snaps to the nearest
    # interior mesh node to this point (validated within 2 channel widths).
    release_lon: float = None           # type: ignore[assignment]
    release_lat: float = None           # type: ignore[assignment]
    # 2026-07-18 release-seeding: when True, a plausible release point ALSO
    # seeds the centerline/corridor resolution (nearest flowline to the
    # RELEASE, not the geocode center). Fix for bare release coords with no
    # river name meshing the water body nearest the CITY (a Longview prompt
    # meshed the Cowlitz instead of the Columbia, and the built mesh did not
    # even contain the requested release point). The composer arms it ONLY
    # for CALL-provided coords; a gate-picked map click moves the SOURCE,
    # never the reach (the approved solve must reproduce the previewed
    # mesh). See resolve_centerline_seed.
    seed_from_release: bool = False
    # 2026-07-18 decouple: when the approve-mesh gate click moved the
    # SOURCE (overwriting release_lon/release_lat), the manifest threads the
    # ORIGINAL call coords here so the reach seed still follows the pair the
    # preview meshed from - the click moves the source only, never the reach.
    # Absent (the common case) the release coords seed as before.
    seed_release_lon: float = None      # type: ignore[assignment]
    seed_release_lat: float = None      # type: ignore[assignment]
    # EXPLICIT bank source (NATE oceanmesh-wave leg 1 - no inexplicit mesh-source
    # fallbacks): "nhd_area" (default) samples USGS NHDArea river polygons for
    # per-station left/right bank offsets (mesh follows the REAL river);
    # "constant_ribbon" uses the assumed constant channel width. On the nhd_area
    # path with NO NHDArea coverage the worker raises BanksUnavailableError (a
    # typed gate) rather than silently ribboning - the DEM_FALLBACK_GATE pattern.
    # Legacy manifest spellings map: "auto" -> nhd_area, "constant" -> constant_ribbon.
    bank_source: str = "nhd_area"
    # wrong-watercourse fix: when the prompt NAMES the river, re-seed
    # onto the NAMED GNIS mainstem before the NLDI position-snap. A raw
    # geocode-point snap near a confluence (Longview = Columbia x Cowlitz)
    # routinely lands on the tributary or a slough; the named-flowline query
    # (proven manually on the Columbia, comid 24520442) disambiguates.
    river_name: str = ""
    # M3 substance classes: "tracer" = the existing dissolved-tracer path;
    # "oil" ALSO activates the TELEMAC oil-spill module (steering file presence
    # auto-activates in v9) - a floating particle slick rides on TOP of the
    # tracer solve (the module's soluble fraction feeds T1). oil_preset picks
    # the steering parameters from OIL_PRESETS.
    # WAQTEL v1a "decay" class: a first-order-decaying substance (sewage /
    # E. coli / effluent) rides the UNCHANGED dye tracer but the t2d cas couples
    # WAQTEL with WATER QUALITY PROCESS = 17, whose nametrac branch applies a
    # first-order decay SINK to every existing user tracer - so ZERO new tracers,
    # ZERO postprocess/contract change; only a sink term in the solve. author_deck
    # writes a tiny t2d_river.waqtel steering file from these two fields ONLY when
    # substance_class == "decay"; the defaults leave every non-decay run byte-
    # identical. decay_law: 1 = T90 bacterial die-off (coef = T90 hours), 2 =
    # first-order (coef = k in h^-1), 3 = first-order (coef = k in d^-1) - the
    # LAW OF TRACERS DEGRADATION values verified vs telemac2d.dico.
    substance_class: str = "tracer"
    oil_preset: str = "light_crude"
    decay_law: int = 1                  # WAQTEL LAW OF TRACERS DEGRADATION
    decay_coef: float = 2.0             # COEFFICIENT 1 FOR ... DEGRADATION (T90 h)
    # GAIA v1 sediment class (mutually exclusive with oil/decay): a SUSPENDED
    # sediment substance (sand / silt / mud) that settles + deposits. author_deck
    # couples GAIA (COUPLING WITH = 'GAIA' + GAIA STEERING FILE = gaia_river.cas)
    # and author_gaia_deck writes the ~18-line GAIA steering. In-image smoke
    # (2026-07-19, gaia.dico v9) PINNED the coupling wiring: GAIA appends the ONE
    # suspended class as a SECOND telemac2d TRACER, so the result r2d_river.slf
    # carries it as 'NCOH SEDIMENT1' in g/l (== kg/m3), while gaia_river.slf
    # carries 'CUMUL BED EVOL' in METRES (the deposition map, var mnemonic E).
    # v1 is SUPPLY-LIMITED: LAYERS INITIAL THICKNESS = 0 so nothing erodes - only
    # the injected pulse can deposit (erodible_bed = initial thickness > 0 +
    # bedload formulas is the v2 flag, OUT of v1). grain_size_um sets the NCO d50
    # (fine sand default); the source concentration reuses dye_conc_mgl (mg/L)
    # / 1000 -> kg/m3 (GAIA's SI unit, confirmed g/l in the smoke). The dye
    # tracer is KEPT as a REQUIRED hydraulic companion: a GAIA-only tracer (no
    # user tracer) trips DEBIMP "SUPERCRITICAL ENTRY WITH FREE DEPTH" on the
    # Q-prescribed inflow (proven in the smoke); the dye rides as a conservative
    # reference and the postprocess picks the SEDIMENT tracer, not the dye.
    grain_size_um: float = 200.0        # suspended d50 in microns (fine sand)
    sediment_density: float = 2650.0    # grain density kg/m3 (quartz)
    sediment_type: str = "sand"         # sand|silt|mud (narration + grain hint)
    # GAIA v2 ERODIBLE-BED MORPHODYNAMICS (bedload scour/deposition). erodible_bed
    # False (default) leaves the v1 SUPPLY-LIMITED suspended path byte-identical
    # (LAYERS INITIAL THICKNESS = 0, SUSPENSION on, BED LOAD off - only the pulse
    # deposits). erodible_bed True flips write_gaia_deck to the v2 recipe: a real
    # erodible bed stock (LAYERS INITIAL THICKNESS = bed_thickness_m), BED LOAD FOR
    # ALL SANDS = YES with a Shields-based bed-load transport formula
    # (bedload_formula, default 1 = Meyer-Peter-Mueller), SUSPENSION off, and a
    # MORPHOLOGICAL FACTOR (morphological_factor) that amplifies bed evolution per
    # hydraulic step. Under a flood hydrograph the bed then SCOURS (negative CUMUL
    # BED EVOL) below a contraction/steepening and re-deposits where the flow
    # slackens - the "where does the bed scour and where does it re-deposit"
    # question. On the T2D side the bedload path appends NO suspended tracer (the
    # dye stays the sole hydraulic-companion tracer), so the coupling adds only the
    # COUPLING WITH / GAIA STEERING FILE lines - no tracer widening. Keywords pinned
    # against gaia.dico v9.0 (LAYERS INITIAL THICKNESS / BED LOAD FOR ALL SANDS /
    # BED-LOAD TRANSPORT FORMULA FOR ALL SANDS / MORPHOLOGICAL FACTOR).
    erodible_bed: bool = False          # v2 flag - v1 forces supply-limited
    bed_thickness_m: float = 5.0        # v2 erodible bed stock depth (LAYERS INITIAL THICKNESS)
    bedload_formula: int = 1            # v2 BED-LOAD TRANSPORT FORMULA (1=Meyer-Peter-Mueller)
    morphological_factor: float = 10.0  # v2 MORPHOLOGICAL FACTOR (bed-evolution amplification)
    # GAIA v3 MULTI-CLASS GRADED SEDIMENT (mixed-grain sorting / segregation).
    # sediment_gradation is a list of (d50_um, initial_fraction) pairs. When it
    # carries >= 2 classes, write_gaia_deck emits a MULTI-CLASS non-cohesive
    # bedload deck: several CLASSES SEDIMENT DIAMETERS / CLASSES INITIAL FRACTION
    # with an Egiazaroff HIDING FACTOR FORMULA (=1) and an active layer (GAIA
    # auto-arms multilayer stratification with >1 class). Under a flood the coarse
    # and fine classes have DIFFERENT mobility (Meyer-Peter-Mueller transport
    # scales with grain size), so the bed SORTS: fines winnow out of scour zones
    # (the surface armors -> MEAN DIAMETER rises) and settle in deposition zones
    # (surface fines) - the "how does a mixture of grain sizes segregate vs a
    # single representative size" question. Rides the SAME erodible-bed coupling as
    # v2 (SUSPENSION off, no suspended tracer appended, dye stays sole companion);
    # the composer forces erodible_bed=True whenever a gradation is armed. Empty
    # tuple (default) leaves every single-class run byte-identical. Keywords pinned
    # in-image against gaia.dico v9.0 (CLASSES SEDIMENT DIAMETERS/INITIAL FRACTION/
    # TYPE OF SEDIMENT arrays, HIDING FACTOR FORMULA=1 Egiazaroff, D50 output var).
    sediment_gradation: tuple = ()      # v3 [(d50_um, fraction), ...] >=2 -> multi-class
    # NESTOR DREDGING (ADR 0254) -- a dig/dump rule layered ONTO the GAIA v2
    # erodible-bed morphodynamics base. dredging False (default) leaves every
    # sediment run byte-identical (no NESTOR keywords, no action/polygon files).
    # dredging True arms the in-image-precompiled NESTOR module (libnestor4*.so):
    # write_gaia_deck adds NESTOR : YES + NESTOR ACTION FILE + NESTOR POLYGON FILE
    # (+ NESTOR SURFACE REFERENCE FILE for the criterion mode) to the GAIA steering,
    # and the worker authors NESTOR's own-format action + polygon (+ surface-ref)
    # files. The action-file grammar is pinned against the in-image compiled fortran
    # (sources/nestor/readdigactions.f + readpolygons.f + isactioncompletelydefined.f):
    #   * blocks ACTION..ENDACTION, comment '/', terminator ENDFILE, top-level RESTART;
    #   * KeyWord = value lines (ParseSteerLine splits on '='), dates yyyy.mm.dd-hh:mm:ss;
    #   * field/polygon names carry a 3-digit numeral prefix ("001_channel").
    # NESTOR requires a real erodible bed stock (it digs ZF through the GAIA active
    # layer) and non-cohesive sand only (NSAND==NSICLA), so dredging FORCES the v2
    # erodible-bed path. Two modes:
    #   * "scheduled" (Dig_by_time): remove dredge_volume_m3 from the dredge zone over
    #     [start,end]; if dredge_disposal, place the spoil in the disposal zone
    #     (Dump_by_time). Minimal keywords -> the robust primary discriminator.
    #   * "criterion" (Dig_by_criterion): dig only where the silted bed rises above
    #     (design grade - dredge_crit_depth_m) down to (design grade - dredge_dig_depth_m),
    #     at dredge_rate_m_per_s; needs a NESTOR SURFACE REFERENCE FILE carrying the
    #     design navigation grade as cross-section profiles bracketing the zone.
    # Zone geometry + volumes/rates are un-fetchable engineering -> surfaced through
    # the input-review gate with labeled defaults; the worker builds channel-spanning
    # UTM boxes from the centerline when explicit polygons are absent.
    dredging: bool = False              # arm NESTOR dig/dump on the erodible-bed base
    dredge_mode: str = "scheduled"      # scheduled (Dig_by_time) | criterion (Dig_by_criterion)
    dredge_station_frac: float = 0.5    # along-channel position of the dredge box (0=up,1=down)
    dredge_zone_len_m: float = None     # type: ignore[assignment]  # box along-channel length (None -> 2x width)
    dredge_zone_utm: tuple = ()         # explicit dig-field polygon [(x,y),...] UTM (overrides the box)
    disposal_zone_utm: tuple = ()       # explicit dump-field polygon [(x,y),...] UTM
    dredge_disposal: bool = False       # scheduled: also place the spoil in a disposal zone
    dredge_disposal_station_frac: float = 0.85  # along-channel position of the disposal box
    dredge_volume_m3: float = 4000.0    # scheduled Dig_by_time target dredged volume (m3)
    dredge_start_frac: float = 0.15     # scheduled: dig-window start as fraction of the sim
    dredge_end_frac: float = 0.95       # scheduled: dig-window end as fraction of the sim
    dredge_crit_depth_m: float = 0.3    # criterion CritDepth: siltation tolerance above grade (m)
    dredge_dig_depth_m: float = 1.5     # criterion DigDepth: dig target below design grade (m)
    dredge_rate_m_per_s: float = 5.0e-4  # criterion DigRate: vertical dig rate (m per second)
    dredge_design_grade_m: float = None  # type: ignore[assignment]  # criterion design nav grade (m); None -> auto
    # WAQTEL O2 "do_sag" class (mutually exclusive with oil/decay/sediment): the
    # dissolved-oxygen SAG below a permitted discharge (US TMDL/permit question).
    # author_deck couples WAQTEL with WATER QUALITY PROCESS = 2 (the O2 module),
    # which appends THREE tracers after the dye: DISSOLVED O2, ORGANIC LOAD (=
    # ultimate CBOD as an O2 equivalent) and NH4 LOAD (nametrac_waqtel order,
    # PINNED by the 2026-08-07 in-image smoke). The reach is modeled STARTING at
    # the fully-mixed discharge: the mixed CBOD + DO ride in at the INFLOW
    # boundary (PRESCRIBED TRACERS VALUES, boundary-major per lb_order), decay
    # downstream (k1) consuming O2, and reaeration (k2) recovers it -- the classic
    # Streeter-Phelps profile (in-image V&V: 0.011 mg/L at the sag minimum vs the
    # 1925 closed form when P=R=BEN=k44=0, constant k2/Cs, T=20C). The dye tracer
    # stays as an inert conservative-dilution reference. write_waqtel_o2 writes
    # the O2 steering file; the defaults leave every non-do_sag run byte-identical.
    do_sag_bod_mgl: float = 20.0        # fully-mixed inflow CBOD (organic load) mg/L
    do_sag_upstream_do_mgl: float = 9.0 # DO carried in at the inflow mg/L
    do_sat_mgl: float = 9.0             # O2 SATURATION DENSITY CS mg/L (temp-dependent)
    do_water_temp_c: float = 20.0       # WATER TEMPERATURE for the O2 kinetics
    do_k1_per_day: float = 0.3          # K1 deoxygenation d^-1 (realistic default)
    do_k2_per_day: float = 0.9          # K2 reaeration d^-1 (constant-k2 default)
    do_k2_formula: int = 0              # FORMK2: 0=constant K2, 1=TVA .. 5=combined
    do_standard_mgl: float = 5.0        # WQ DO standard (chart reference line only)
    n_drogues: int = 100                # slick particle count (oil class)
    drogues_period_s: int = 60          # particle snapshot cadence, seconds
    # release AFTER the startup transient: constant-depth init drains shallow
    # margins for the first minutes and strands early-released floats (live:
    # 100 -> 8 particles by t=180s at LT=60; deep-water spike was unaffected)
    oil_release_step: int = 600
    pulse_window_s: float = 300.0       # dye-on window; source turns OFF after
    source_q_m3s: float = 8.0           # carrier discharge of the point source (small vs inflow)
    duration_s: float = 3600.0
    time_step_s: float = 1.0
    graphic_period: int = 200
    min_bed_slope: float = 3.0e-4       # enforced gentle downstream slope floor
    max_bed_slope: float = 6.0e-3
    # RAIN-ON-GRID (ADR 0196). mode="rain_on_grid" routes the worker to the RoG
    # pipeline (rog_build) instead of the channel-dye pipeline: a rain-fed
    # delineated-watershed TIN (staged by the agent-side mesh_acquisition step as
    # watershed_slf, UTM metres, BOTTOM = bed) solves with a distributed CN
    # infiltration + per-NLCD Manning + a free-exit outlet at the pour point. The
    # per-node CN2/Manning fields are staged as node_cn2_file/node_manning_file
    # (one value per line, mesh-node order); runoff_path ("native" constant-rain
    # SCS-CN vs "preprocessing" net-excess rain) is chosen by the agent-side
    # select_runoff_path and threaded here so the deck author writes the matching
    # branch. rain_intensity_mm_per_hr + rain_duration_s define the constant
    # design storm (native path); curve_number is the uniform-CN override (else
    # the NLCD-distributed field is used); amc_condition is the SCS antecedent
    # moisture class (1 dry / 2 normal / 3 wet); observed_gauge_id wires the
    # USGS-NWIS NSE/R2 overlay. Defaults leave every non-RoG (channel-dye) run
    # byte-identical: mode="river_dye" is the historical path.
    mode: str = "river_dye"
    watershed_slf: str = ""             # staged BOTTOM SELAFIN basename (RoG mesh)
    runoff_path: str = "native"         # native | preprocessing
    curve_number: float = None          # type: ignore[assignment]  # uniform CN2 override
    amc_condition: int = 2              # SCS antecedent moisture: 1 dry / 2 norm / 3 wet
    initial_abstraction_option: int = 1  # OPTION FOR INITIAL ABSTRACTION RATIO (1=0.2, 2=0.05)
    rain_intensity_mm_per_hr: float = 25.0   # constant design-storm intensity (native)
    rain_duration_s: float = None       # type: ignore[assignment]  # rain-on window (defaults to duration_s)
    rain_hyetograph_mm: list = None     # type: ignore[assignment]  # per-step net rain (preprocessing)
    # TIME-VARYING native hyetograph (ADR 0206): a list of [t_end_s, gross_mm]
    # blocks (gross rainfall per interval, t from 0). When set, the RoG worker
    # stages a per-case FORTRAN FILE flipping the engine's hardcoded RAINDEF=1
    # to RAINDEF=3 and writes the block file as FORMATTED DATA FILE 1, so the
    # native SCS-CN model runs per-timestep on the REAL hyetograph (the fix for
    # the constant-rain peak-timing lag). Empty/None -> the constant design-storm
    # native path (rain_intensity_mm_per_hr) is used, byte-identical to before.
    rain_hyetograph_blocks: list = None  # type: ignore[assignment]
    # CONTINUOUS SOIL-MOISTURE STORE (ADR 0213). When soil_store is True and a
    # rain_hyetograph_blocks GROSS series is set, the worker transforms the gross
    # hyetograph into a NET rainfall-excess hyetograph through a Michel et al.
    # (2005) continuous SCS-CN production store (level V, capacity S, recovery
    # timescale tau), then feeds that net series to the engine through a uniform
    # CN=100 pass-through (the engine adds no further abstraction). This replaces
    # the static per-event curve number with a dynamic antecedent STATE: the
    # store fills during rain and drains between storms over recovery_h, so a
    # single parameter set carries antecedent wetness a static CN cannot. S is the
    # calibration knob (soil_store_capacity_mm), recovery_h the drying-timescale
    # lever, init_mm the initial level V0 (from the antecedent-precipitation
    # spin-up the agent computes). None/False -> the static-CN native paths above
    # run unchanged.
    soil_store: bool = False
    soil_store_capacity_mm: float = None  # type: ignore[assignment]  # S (max retention)
    soil_store_recovery_h: float = None   # type: ignore[assignment]  # tau (drying timescale)
    soil_store_init_mm: float = None      # type: ignore[assignment]  # V0 (initial store level)
    node_cn2_file: str = ""             # staged per-node CN2 field basename
    node_manning_file: str = ""         # staged per-node Manning field basename
    outlet_lonlat: tuple = None         # type: ignore[assignment]  # pour-point (lon, lat)
    n_outlet_nodes: int = 6             # ring nodes marked as the free-exit outlet
    observed_gauge_id: str = ""         # USGS NWIS gauge for NSE/R2 (composer wiring)
    workdir: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))


_NLDI = "https://api.water.usgs.gov/nldi/linked-data"
_UA = "trid3nt-local-spike (agent@trid3nt.dev)"


# ---------------------------------------------------------------------------
# 1. REAL river geometry via USGS NLDI NHDPlus
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _snap_comid(lon: float, lat: float) -> int:
    url = f"{_NLDI}/comid/position?coords=POINT({lon}%20{lat})"
    fc = json.loads(_http_get(url))
    p = fc["features"][0]["properties"]
    return int(p.get("comid") or p.get("nhdplus_comid"))


_NHDPLUS_HR = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer"


def _named_flowline_seed(
    name: str, lon: float, lat: float, search_deg: float = 0.15
) -> tuple[float, float] | None:
    """Nearest vertex of the NAMED GNIS flowline to (lon, lat), or None.

    Queries NHDPlus_HR layer 3 (NetworkNHDFlowline) by gnis_name within a
    ~search_deg envelope around the raw seed. Fail-OPEN: any error / no match
    returns None and the caller keeps the raw position-snap (honest degrade).
    """
    safe = name.replace("'", "''").strip()
    if not safe:
        return None
    env = json.dumps({
        "xmin": lon - search_deg, "ymin": lat - search_deg,
        "xmax": lon + search_deg, "ymax": lat + search_deg,
        "spatialReference": {"wkid": 4326},
    })
    q = urllib.parse.urlencode({
        "f": "geojson",
        "where": f"UPPER(gnis_name)=UPPER('{safe}')",
        "geometry": env, "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "gnis_name", "returnGeometry": "true",
        "maxAllowableOffset": 0.0005, "resultRecordCount": 200,
    })
    try:
        fc = json.loads(_http_get(f"{_NHDPLUS_HR}/3/query?{q}", timeout=45.0))
    except Exception as exc:  # noqa: BLE001 -- network fail-open to raw seed
        LOG.warning("named-flowline seed query failed (%s) - raw seed kept", exc)
        return None
    best: tuple[float, float] | None = None
    best_d2 = float("inf")
    for feat in fc.get("features") or []:
        geom = feat.get("geometry") or {}
        lines = (
            [geom.get("coordinates")]
            if geom.get("type") == "LineString"
            else geom.get("coordinates") or []
        )
        for line in lines:
            for v in line or []:
                d2 = (v[0] - lon) ** 2 + (v[1] - lat) ** 2
                if d2 < best_d2:
                    best_d2, best = d2, (float(v[0]), float(v[1]))
    return best


def _mainstem_flowline_seed(
    lon: float,
    lat: float,
    search_deg: float = 0.05,
    max_reseed_km: float = 6.0,
) -> tuple[float, float] | None:
    """Re-seed a NAME-FREE reach onto the dominant nearby mainstem, or None.

    When no ``river_name`` disambiguates the reach, the bare position-snap
    (``_snap_comid``) lands on whatever NHDFlowline is geometrically nearest;
    at a confluence that is often a short low-order tributary stub (live:
    Longview = Columbia x Cowlitz snapped a 292 m order-3 stub). This queries
    NHDPlus_HR layer 3 within ``search_deg`` of the seed and prefers the
    highest ``streamorde`` channel, tie-broken by ``totdasqkm`` (total upstream
    drainage) then proximity -- but ONLY when that mainstem STRICTLY outranks
    the nearest flowline and its nearest vertex is within ``max_reseed_km``
    (bounded so a genuine small-creek study is never yanked onto a distant
    river). Fail-OPEN: any error / no improvement returns None and the caller
    keeps the raw position-snap (honest degrade).
    """
    env = json.dumps({
        "xmin": lon - search_deg, "ymin": lat - search_deg,
        "xmax": lon + search_deg, "ymax": lat + search_deg,
        "spatialReference": {"wkid": 4326},
    })
    q = urllib.parse.urlencode({
        "f": "geojson", "where": "1=1",
        "geometry": env, "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "gnis_name,streamorde,totdasqkm",
        "returnGeometry": "true",
        "maxAllowableOffset": 0.0005, "resultRecordCount": 500,
    })
    try:
        fc = json.loads(_http_get(f"{_NHDPLUS_HR}/3/query?{q}", timeout=45.0))
    except Exception as exc:  # noqa: BLE001 -- network fail-open to raw seed
        LOG.warning("mainstem-seed query failed (%s) - raw seed kept", exc)
        return None
    # (streamorde, totdasqkm, dist_deg, (vx, vy)) per flowline.
    cands: list[tuple[int, float, float, tuple[float, float]]] = []
    for feat in fc.get("features") or []:
        p = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        lines = (
            [geom.get("coordinates")]
            if geom.get("type") == "LineString"
            else geom.get("coordinates") or []
        )
        best_d2 = float("inf")
        best_v: tuple[float, float] | None = None
        for line in lines:
            for v in line or []:
                d2 = (v[0] - lon) ** 2 + (v[1] - lat) ** 2
                if d2 < best_d2:
                    best_d2, best_v = d2, (float(v[0]), float(v[1]))
        if best_v is None:
            continue
        order = int(p.get("streamorde") or 0)
        drainage = float(p.get("totdasqkm") or 0.0)
        cands.append((order, drainage, best_d2 ** 0.5, best_v))
    if not cands:
        return None
    nearest = min(cands, key=lambda c: c[2])
    # Mainstem = highest order, then most drainage, then nearest.
    mainstem = max(cands, key=lambda c: (c[0], c[1], -c[2]))
    reseed_km = mainstem[2] * 111.0
    if mainstem[0] <= nearest[0] or reseed_km > max_reseed_km:
        # The nearest flowline is already the (equal-)dominant channel, or the
        # only mainstem lies beyond the re-seed radius -- keep the raw seed.
        return None
    LOG.info(
        "mainstem re-seed: nearest order %d vs mainstem order %d "
        "(drainage %.0f km2) at %.2f km -> re-seeding",
        nearest[0], mainstem[0], mainstem[1], reseed_km,
    )
    return mainstem[3]


def _stitch_flowlines(features) -> list[tuple[float, float]]:
    """Order flowline segments head-to-tail into one upstream->downstream path."""
    import shapely.geometry as sg

    segs = [list(sg.shape(f["geometry"]).coords) for f in features]
    segs = [[(round(x, 7), round(y, 7)) for x, y in s] for s in segs]
    # index endpoints
    starts = {i: s[0] for i, s in enumerate(segs)}
    ends = {i: s[-1] for i, s in enumerate(segs)}
    end_set = set(ends.values())
    # head = a segment whose start is nobody's end
    heads = [i for i in starts if starts[i] not in end_set]
    start_i = heads[0] if heads else 0
    # chain by matching end->start
    used = set()
    chain = [start_i]
    used.add(start_i)
    cur = start_i
    start_lookup = defaultdict(list)
    for i, s in starts.items():
        start_lookup[s].append(i)
    while True:
        nxts = [j for j in start_lookup.get(ends[cur], []) if j not in used]
        if not nxts:
            break
        cur = nxts[0]
        used.add(cur)
        chain.append(cur)
    # concatenate, dropping the duplicate shared vertex between segments
    path: list[tuple[float, float]] = []
    for k, i in enumerate(chain):
        s = segs[i]
        if k > 0 and path and s and path[-1] == s[0]:
            s = s[1:]
        path.extend(s)
    return path


def resolve_centerline_seed(
    seed_lon: float,
    seed_lat: float,
    release_lon=None,
    release_lat=None,
    seed_from_release: bool = False,
    seed_release_lon=None,
    seed_release_lat=None,
):
    """The (lon, lat, kind) the centerline/corridor resolution centers on.

    Pure decision function (no network; offline-tested in
    tests/test_release_seed_preference.py). The release point wins over the
    geocode seed ONLY when the manifest armed ``seed_from_release`` AND the
    coords are plausible EPSG:4326 (numeric, lon in [-180, 180], lat in
    [-90, 90] - NaN/inf fail the range gate). Anything else keeps the seed
    byte-for-byte, so the proven location-seeded paths are unchanged.
    ``kind`` is ``"position"`` (geocode seed kept) or ``"release-position"``.

    2026-07-18 decouple: ``seed_release_lon``/``seed_release_lat`` are
    the ORIGINAL call coords the preview meshed from, threaded separately
    when an approve-mesh gate click overwrote ``release_lon``/``release_lat``
    (the click moves the SOURCE only). When armed they take precedence for
    the reach seed; an implausible pair degrades to the release coords, so
    the pre-existing manifests (keys absent) behave byte-identically.
    """
    base = (float(seed_lon), float(seed_lat), "position")
    if not seed_from_release:
        return base
    for lon_v, lat_v in (
        (seed_release_lon, seed_release_lat),
        (release_lon, release_lat),
    ):
        try:
            lon = float(lon_v)
            lat = float(lat_v)
        except (TypeError, ValueError):
            continue
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            continue
        return (lon, lat, "release-position")
    return base


def fetch_river_centerline(cfg: ReachConfig):
    """Return (lonlat centerline array, meta dict) from real NHDPlus flowlines."""
    seed_lon, seed_lat, seed_kind = resolve_centerline_seed(
        cfg.seed_lon, cfg.seed_lat,
        getattr(cfg, "release_lon", None), getattr(cfg, "release_lat", None),
        seed_from_release=bool(getattr(cfg, "seed_from_release", False)),
        seed_release_lon=getattr(cfg, "seed_release_lon", None),
        seed_release_lat=getattr(cfg, "seed_release_lat", None),
    )
    if seed_kind == "release-position":
        LOG.info(
            "release-seeded reach: corridor resolution centered on the release "
            "point (%.5f,%.5f), not the geocode seed (%.5f,%.5f)",
            seed_lon, seed_lat, cfg.seed_lon, cfg.seed_lat,
        )
    if cfg.river_name:
        named = _named_flowline_seed(cfg.river_name, seed_lon, seed_lat)
        if named is not None:
            named_kind = ("named-flowline" if seed_kind == "position"
                          else "release-named-flowline")
            LOG.info(
                "named-flowline re-seed %r: (%.5f,%.5f) -> (%.5f,%.5f)",
                cfg.river_name, seed_lon, seed_lat, named[0], named[1],
            )
            seed_lon, seed_lat, seed_kind = named[0], named[1], named_kind
        else:
            LOG.warning(
                "named-flowline re-seed %r found nothing - raw seed kept",
                cfg.river_name,
            )
    else:
        # No river_name to disambiguate: prefer the dominant nearby mainstem
        # over the bare nearest-flowline snap, so a seed near a confluence does
        # not land on a short low-order tributary stub (ADR 0104 Bug-1
        # reach-selection residual; ADR 0108). Fail-open to the raw seed.
        main = _mainstem_flowline_seed(seed_lon, seed_lat)
        if main is not None:
            LOG.info(
                "mainstem re-seed (no river_name): (%.5f,%.5f) -> (%.5f,%.5f)",
                seed_lon, seed_lat, main[0], main[1],
            )
            seed_lon, seed_lat = main
            seed_kind = f"{seed_kind}-mainstem"
    comid = _snap_comid(seed_lon, seed_lat)
    url = f"{_NLDI}/comid/{comid}/navigation/{cfg.nav_direction}/flowlines?distance={cfg.distance_km}"
    fc = json.loads(_http_get(url))
    feats = fc["features"]
    path = _stitch_flowlines(feats)
    ll = np.array(path, dtype=float)
    meta = dict(
        seed_comid=comid, n_flowlines=len(feats), n_raw_vertices=len(ll),
        seed_kind=seed_kind,
    )
    return ll, meta


# ---------------------------------------------------------------------------
# 2. Project / resample / smooth the real centerline
# ---------------------------------------------------------------------------
def _utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def process_centerline(ll: np.ndarray, cfg: ReachConfig):
    """lon/lat -> local UTM meters, resample uniform, light smoothing.

    Real centerlines are noisy (dense irregular vertices, small kinks). We
    (a) project to UTM, (b) resample to uniform arc-length spacing, (c) smooth
    with a moving average so offset banks do not self-intersect at bends.
    Flow direction = path order (NHDPlus flowlines are digitized downstream).
    """
    from pyproj import Transformer

    epsg = _utm_epsg(float(ll[:, 0].mean()), float(ll[:, 1].mean()))
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    xm, ym = tr.transform(ll[:, 0], ll[:, 1])
    xy = np.column_stack([xm, ym])

    # arc length
    d = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    s = np.concatenate([[0.0], np.cumsum(d)])
    total = float(s[-1])

    # uniform resample
    ns = max(int(total / cfg.resample_ds_m) + 1, 10)
    su = np.linspace(0, total, ns)
    xu = np.interp(su, s, xy[:, 0])
    yu = np.interp(su, s, xy[:, 1])

    # moving-average smooth (keep endpoints)
    w = cfg.smooth_window
    if w >= 3 and ns > w:
        k = np.ones(w) / w
        xs = np.convolve(xu, k, mode="same")
        ys = np.convolve(yu, k, mode="same")
        # preserve endpoints (convolve edge bias)
        m = w // 2
        xs[:m] = xu[:m]; xs[-m:] = xu[-m:]
        ys[:m] = yu[:m]; ys[-m:] = yu[-m:]
        xu, yu = xs, ys

    cl = np.column_stack([xu, yu])
    meta = dict(utm_epsg=epsg, centerline_length_m=round(total, 1),
                n_centerline_pts=ns, lonlat_transformer=tr)
    return cl, meta


# ---------------------------------------------------------------------------
# 2b. real river banks from USGS NHDArea polygons
# ---------------------------------------------------------------------------
class BanksUnavailableError(RuntimeError):
    """The nhd_area bank source could not produce real banks for this reach.

    NATE oceanmesh-wave leg 1 (no inexplicit mesh-source fallbacks): on the
    default ``bank_source="nhd_area"`` path, when NO NHDArea water polygon covers
    the reach (empty fetch / too little sampled water / fetch error) the worker
    does NOT silently substitute the constant-width ribbon. It raises THIS typed
    error so the server surfaces a ``TELEMAC_BANKS_UNAVAILABLE`` gate naming the
    explicit retry ``bank_source="constant_ribbon"`` + the assumed channel width -
    the DEM_FALLBACK_GATE pattern for a mesh-geometry source.
    """

    def __init__(self, assumed_channel_width_m: float) -> None:
        self.assumed_channel_width_m = float(assumed_channel_width_m)
        super().__init__(
            "no USGS NHDArea water polygon covers this reach on the nhd_area bank "
            "source, so real per-station banks could not be sampled. No bank "
            "geometry was substituted automatically. Retry with "
            f'bank_source="constant_ribbon" to mesh an assumed constant '
            f"{self.assumed_channel_width_m:g} m channel-width ribbon instead."
        )


class ReachDegenerateError(RuntimeError):
    """The reach geometry is degenerate: the channel is wider than the reach is
    long (or nearly so), so the offset bank curves fold and gmsh's mesh
    generator busy-loops (live: Longview WA snapped to a 292 m NHDFlowline stub
    with the 500 m default width -> generate(2) ran 32+ min in C).

    Gated BEFORE meshing (the 0091 gate pattern, never a hang, never a silent
    bad mesh): a typed, retryable error naming the corrective args - a longer
    ``reach_length_km``, an explicit ``river_name`` (re-seeds onto the named
    mainstem instead of a short tributary stub), or
    ``bank_source="constant_ribbon"`` with a smaller ``channel_width_m``.
    """

    def __init__(
        self,
        reach_length_m: float,
        channel_width_m: float,
    ) -> None:
        self.reach_length_m = float(reach_length_m)
        self.channel_width_m = float(channel_width_m)
        aspect = (self.reach_length_m / self.channel_width_m
                  if self.channel_width_m > 0 else 0.0)
        self.aspect_ratio = round(aspect, 3)
        super().__init__(
            f"reach geometry is degenerate: a {self.reach_length_m:.0f} m reach "
            f"with a {self.channel_width_m:.0f} m channel width (length/width "
            f"aspect {aspect:.2f} < {_MIN_REACH_ASPECT:g}) - the banks fold and "
            "the mesh generator cannot converge. Retry with a longer "
            "reach_length_km, an explicit river_name (re-seeds onto the named "
            'mainstem, not a short stub), or bank_source="constant_ribbon" with '
            "a smaller channel_width_m."
        )


#: Minimum reach length / channel width. Below this the offset banks fold and
#: gmsh busy-loops; gate it as ReachDegenerateError before meshing.
_MIN_REACH_ASPECT: float = 2.0

#: Hard wall-clock deadline (s) for the whole channel-mesh build in its killable
#: child process. ``TELEMAC_MESH_TIMEOUT_S`` (env) overrides. A C busy-loop in
#: gmsh cannot swallow the SIGKILL the parent delivers at this deadline (the old
#: in-process SIGALRM demonstrably could not preempt it).
_MESH_WALLCLOCK_TIMEOUT_S: float = 300.0


def _centerline_length_m(cl: "np.ndarray") -> float:
    """Arc length (metres) of the projected centerline polyline."""
    if cl is None or len(cl) < 2:
        return 0.0
    seg = np.diff(np.asarray(cl, dtype=float)[:, :2], axis=0)
    return float(np.hypot(seg[:, 0], seg[:, 1]).sum())


def _effective_channel_width_m(cfg: "ReachConfig") -> float:
    """The width the mesh will actually offset the banks by (metres).

    On the nhd_area path with sampled ``bank_offsets`` the effective width is the
    mean left+right offset; otherwise the assumed ``channel_width_m``."""
    offsets = getattr(cfg, "bank_offsets", None)
    if offsets is not None:
        try:
            left_off, right_off = offsets
            w = float(np.mean(left_off)) + float(np.mean(right_off))
            if w > 0:
                return w
        except Exception:  # noqa: BLE001 -- fall back to the assumed width
            pass
    return float(getattr(cfg, "channel_width_m", 0.0))


def validate_reach_geometry(cl: "np.ndarray", cfg: "ReachConfig") -> None:
    """Raise :class:`ReachDegenerateError` when the channel is wider than the
    reach is long (aspect < ``_MIN_REACH_ASPECT``) - the pre-mesh sanity gate
    that turns the live gmsh hang into a fast typed error."""
    reach_len = _centerline_length_m(cl)
    width = _effective_channel_width_m(cfg)
    if width > 0 and reach_len > 0 and (reach_len / width) < _MIN_REACH_ASPECT:
        raise ReachDegenerateError(reach_len, width)


_NHDAREA_URL = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/"
    "MapServer/8/query"
)


def fetch_bank_polygons(bbox4326, timeout=30.0):
    """NHDArea water polygons intersecting ``bbox4326`` (lonlat) as a list of
    (exterior_ring, [hole_rings]) lonlat arrays. None on ANY failure/empty -
    on the nhd_area path the caller raises BanksUnavailableError (no inexplicit
    ribbon fallback)."""
    import json as _json
    import os as _os
    import urllib.parse
    import urllib.request

    # Test seam (leg 1 forced-empty gate drive): force an empty NHDArea response
    # so the nhd_area banks gate can be exercised on a reach that does have
    # coverage. Env-gated only; the live path is untouched when unset.
    if _os.environ.get("TRID3NT_TELEMAC_FORCE_BANKS_EMPTY"):
        LOG.warning("fetch_bank_polygons: FORCED empty (TRID3NT_TELEMAC_FORCE_BANKS_EMPTY)")
        return None

    params = urllib.parse.urlencode({
        "geometry": ",".join(f"{v:.6f}" for v in bbox4326),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ftype", "f": "geojson",
        # big-river hardening (Columbia hang): server-side simplification
        # (~5 m at mid-latitudes) + a record cap - the reach bbox only needs
        # local bank detail, not the full mainstem polygon.
        "maxAllowableOffset": "0.00005",
        "resultRecordCount": "200",
    })
    try:
        with urllib.request.urlopen(f"{_NHDAREA_URL}?{params}", timeout=timeout) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- degrade, never dead-end
        LOG.warning("NHDArea fetch failed (%s); constant-width fallback", exc)
        return None
    polys = []
    for f in data.get("features") or []:
        g = f.get("geometry") or {}
        if g.get("type") == "Polygon":
            rings = g.get("coordinates") or []
            if rings:
                polys.append((np.asarray(rings[0], dtype=float),
                              [np.asarray(rr, dtype=float) for rr in rings[1:]]))
        elif g.get("type") == "MultiPolygon":
            for rings in g.get("coordinates") or []:
                if rings:
                    polys.append((np.asarray(rings[0], dtype=float),
                                  [np.asarray(rr, dtype=float) for rr in rings[1:]]))
    return polys or None


def estimate_bank_offsets(cl, polys_utm, max_half=800.0, step=4.0,
                          min_half=8.0, valid_frac_floor=0.3):
    """Per-station (left, right) bank distances from the water polygons.

    Casts a perpendicular transect at every centerline station, marks which
    samples are WATER (inside an exterior ring minus its holes), and takes the
    contiguous water run nearest the centerline. Stations with no water within
    ~30 m are interpolated from neighbours; if fewer than ``valid_frac_floor``
    of stations see water at all, returns None (constant-width fallback).
    Profiles are smoothed + gradient-limited so gmsh bank offsetting stays
    simple (no bowties from step changes)."""
    import shapely.geometry as sg
    try:
        import shapely
        _contains = lambda g, xs, ys: shapely.contains_xy(g, xs, ys)  # noqa: E731
    except Exception:  # noqa: BLE001 -- shapely<2 fallback
        from shapely.prepared import prep
        def _contains(g, xs, ys, _p={}):
            pg = _p.setdefault(id(g), prep(g))
            return np.array([pg.contains(sg.Point(x, y)) for x, y in zip(xs, ys)])

    water = [sg.Polygon(ext, holes=[h for h in holes if len(h) >= 4])
             for ext, holes in polys_utm if len(ext) >= 4]
    water = [w.buffer(0) for w in water if not w.is_empty]
    if not water:
        return None
    from shapely.ops import unary_union
    # CLIP to the transect envelope BEFORE the union (Columbia hang fix): the
    # fetched mainstem polygon can span far beyond the reach; unclipped it
    # makes every point-in-polygon test walk hundreds of thousands of
    # vertices. The clip box = centerline extent + max_half margin.
    clip = sg.box(cl[:, 0].min() - max_half, cl[:, 1].min() - max_half,
                  cl[:, 0].max() + max_half, cl[:, 1].max() + max_half)
    water = [w.intersection(clip) for w in water]
    water = [w for w in water if not w.is_empty]
    if not water:
        return None
    union = unary_union(water)
    try:
        nv = sum(len(g.exterior.coords) for g in getattr(union, "geoms", [union])
                 if g.geom_type == "Polygon")
        LOG.info("bank union: %d polys, ~%d verts after clip", len(water), nv)
    except Exception:  # noqa: BLE001
        pass

    x, y = cl[:, 0], cl[:, 1]
    dx = np.gradient(x); dy = np.gradient(y)
    seg = np.hypot(dx, dy); seg[seg == 0] = 1e-9
    nx = -dy / seg; ny = dx / seg
    ts = np.arange(-max_half, max_half + step, step)
    n = len(cl)
    left = np.full(n, np.nan); right = np.full(n, np.nan)
    for i in range(n):
        sx = x[i] + nx[i] * ts
        sy = y[i] + ny[i] * ts
        try:
            wet = np.asarray(_contains(union, sx, sy), dtype=bool)
        except Exception:  # noqa: BLE001
            return None
        if not wet.any():
            continue
        # contiguous wet runs; pick the one nearest t=0 (within 30 m)
        idx = np.flatnonzero(wet)
        splits = np.flatnonzero(np.diff(idx) > 1)
        runs = np.split(idx, splits + 1)
        zero = len(ts) // 2
        best, bestd = None, 1e18
        for run in runs:
            d = 0.0 if run[0] <= zero <= run[-1] else min(
                abs(ts[run[0]]), abs(ts[run[-1]]))
            if d < bestd:
                best, bestd = run, d
        if best is None or bestd > 30.0:
            continue
        # offsets relative to the centerline station (positive both sides)
        left[i] = max(min_half, ts[best[-1]])
        right[i] = max(min_half, -ts[best[0]])
    valid = np.isfinite(left) & np.isfinite(right)
    if valid.mean() < valid_frac_floor:
        return None
    # interpolate gaps from valid neighbours
    ii = np.arange(n)
    for arr in (left, right):
        good = np.isfinite(arr)
        arr[~good] = np.interp(ii[~good], ii[good], arr[good])
    # RECENTER + SMOOTH-FIRST (Columbia fold fix v2): the fold root causes were
    # (a) an off-center axis needing huge one-sided offsets and (b) building
    # the axis from RAW noisy per-station offsets (jagged axis -> oscillating
    # normals -> both banks fold). Correct order: smooth the shift/half-width
    # PROFILES first, then build the mid-water axis, then RESAMPLE it to
    # uniform spacing (kills kinks), then curvature-clamp.
    def _rsmooth(a, kk):
        pad = kk // 2
        ap = np.r_[a[pad:0:-1], a, a[-2:-pad - 2:-1]]
        return np.convolve(ap, np.ones(kk) / kk, mode="valid")

    shift = _rsmooth((left - right) / 2.0, 15)
    halfw = _rsmooth((left + right) / 2.0, 15)
    cl_mid = np.column_stack([x + nx * shift, y + ny * shift])
    cl_mid[:, 0] = _rsmooth(cl_mid[:, 0], 9)
    cl_mid[:, 1] = _rsmooth(cl_mid[:, 1], 9)

    # uniform arc-length resample of the axis (+ halfw onto the new stations)
    seg2 = np.hypot(*np.diff(cl_mid, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg2)])
    ds = float(np.median(seg))
    n_new = max(int(s[-1] / ds), 8)
    s_new = np.linspace(0.0, s[-1], n_new + 1)
    cl_mid = np.column_stack([np.interp(s_new, s, cl_mid[:, 0]),
                              np.interp(s_new, s, cl_mid[:, 1])])
    halfw = np.interp(s_new, s, halfw)

    # CURVATURE CLAMP: half-width <= 0.7 * local bend radius makes a fold
    # geometrically impossible (3-point circumradius, +-4 stations).
    n2 = len(cl_mid)
    radius = np.full(n2, 1e9)
    for i in range(4, n2 - 4):
        a, b, c = cl_mid[i - 4], cl_mid[i], cl_mid[i + 4]
        ab = np.hypot(*(b - a)); bc = np.hypot(*(c - b)); ca = np.hypot(*(a - c))
        area2 = abs((b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0]))
        if area2 > 1e-6:
            radius[i] = (ab * bc * ca) / (2.0 * area2)
    halfw = np.minimum(halfw, np.maximum(min_half, 0.7 * radius))

    # final half-width smoothing + clamps + gradient limit
    max_delta = 0.35 * ds
    halfw = _rsmooth(halfw, 9)
    np.clip(halfw, min_half, max_half, out=halfw)
    for _ in range(200):
        d = np.diff(halfw)
        over = np.abs(d) > max_delta
        if not over.any():
            break
        d = np.clip(d, -max_delta, max_delta)
        halfw[1:] = halfw[0] + np.cumsum(d)
        np.clip(halfw, min_half, max_half, out=halfw)
    return cl_mid, halfw, round(float(valid.mean()), 3)


# ---------------------------------------------------------------------------
# 3. Channel banks + Gmsh mesh (adapts P0 build_gmsh_channel, honoring gotchas)
# ---------------------------------------------------------------------------
def _offset_banks(cl: np.ndarray, width: float, offsets=None):
    x, y = cl[:, 0], cl[:, 1]
    dx = np.gradient(x); dy = np.gradient(y)
    seg = np.hypot(dx, dy); seg[seg == 0] = 1e-9
    nx = -dy / seg; ny = dx / seg
    if offsets is not None:
        lo, ro = offsets
        ds_med = float(np.median(seg))
        half_med = float(np.median((np.asarray(lo) + np.asarray(ro)) / 2.0))
        # HYBRID BY SCALE (Columbia fold fix v3): large offsets (wide rivers)
        # amplify tiny normal jitter into micro self-intersections, and that is
        # exactly the regime where width VARIATION is negligible (+-14% on the
        # Columbia). So: offsets > 3x station spacing -> GEOS offset_curve at
        # the CONSTANT median half-width (guaranteed simple by the geometry
        # kernel); small offsets (creeks, where variation is the whole point:
        # 16-46 m at Twin Falls) keep the per-station variable construction
        # (proven fold-free at that scale).
        if half_med > 3.0 * ds_med:
            try:
                import shapely.geometry as _sg
                axis = _sg.LineString(cl)
                def _side(dist):
                    try:
                        line = axis.offset_curve(dist)
                    except AttributeError:  # shapely<2
                        line = axis.parallel_offset(abs(dist),
                                                    "left" if dist > 0 else "right")
                    if line.geom_type == "MultiLineString":
                        line = max(line.geoms, key=lambda g: g.length)
                    pts = np.asarray(line.coords)
                    # GEOS may reverse the right-side curve; align to the axis
                    if np.hypot(*(pts[0] - cl[0])) > np.hypot(*(pts[-1] - cl[0])):
                        pts = pts[::-1]
                    return pts
                left = _side(+half_med)
                right = _side(-half_med)
                return left, right
            except Exception:  # noqa: BLE001 -- fall through to per-station
                pass
        left = np.column_stack([x + nx * lo, y + ny * lo])
        right = np.column_stack([x - nx * ro, y - ny * ro])
    else:
        left = np.column_stack([x + nx * width / 2, y + ny * width / 2])
        right = np.column_stack([x - nx * width / 2, y - ny * width / 2])
    return left, right


def _banks_valid(left: np.ndarray, right: np.ndarray) -> bool:
    """Reject if either bank self-intersects (tight bend folded the inner bank)."""
    import shapely.geometry as sg

    return sg.LineString(left).is_simple and sg.LineString(right).is_simple


def _water_polygon_domain(cl: np.ndarray, cfg: ReachConfig, ms: float):
    """The TRUE water-polygon mesh domain, or None to fall back to the ribbon.

    the ribbon outline (smoothed sampled half-widths +
    curvature clamps + straight caps) visibly mismatches the river. Instead of
    approximating, mesh the NHDArea water polygon DIRECTLY: clip the water
    union to a corridor around the reach, take the component under the
    centerline, and use its exterior as the outer boundary (holes = islands).
    The corridor's end cuts leave straight cap segments ON the end transect
    lines - those become the inflow/outflow boundaries.

    Returns (exterior_ring[N,2], hole_rings, cap_in_line, cap_out_line) where
    the cap lines are ((x0,y0),(x1,y1)) segments the caps lie on.
    """
    import shapely.geometry as sg
    from shapely.ops import unary_union

    water_polys = getattr(cfg, "water_polys_utm", None)
    if not water_polys:
        return None
    offsets = getattr(cfg, "bank_offsets", None)
    if offsets is None:
        return None
    half_max = float(np.max((np.asarray(offsets[0]) + np.asarray(offsets[1])) / 2.0))
    # The corridor exists ONLY to cut the reach at its two ends - laterally it
    # must never cut water (the back-channels behind Fisher
    # and Cottonwood islands were clipped off at ~1.3x the sampled half-width).
    W = 2.0 * max(4.0 * half_max, 2500.0)
    left, right = _offset_banks(cl, W, None)
    corridor = sg.Polygon(np.vstack([left, right[::-1]]))
    if not corridor.is_valid:
        corridor = corridor.buffer(0)
    water = unary_union([
        sg.Polygon(ext, holes=[h for h in holes if len(h) >= 4])
        for ext, holes in water_polys if len(ext) >= 4
    ]).buffer(0)
    clip = water.intersection(corridor)
    if clip.is_empty:
        return None
    mid = sg.Point(cl[len(cl) // 2])
    comps = list(getattr(clip, "geoms", [clip]))
    comps = [c for c in comps if isinstance(c, sg.Polygon) and not c.is_empty]
    if not comps:
        return None
    main = min(comps, key=lambda c: c.distance(mid))
    main = main.simplify(ms / 2.0)
    if main.is_empty or not main.is_valid or main.area < (10 * ms) ** 2:
        return None
    ext = np.asarray(main.exterior.coords)[:-1]
    holes = []
    for hole in main.interiors:
        hp = sg.Polygon(hole)
        if hp.area >= (2.5 * ms) ** 2 and len(hole.coords) >= 5:
            holes.append(np.asarray(hole.coords)[:-1])
    # end-cap lines = the corridor's end edges (transects at cl[0] / cl[-1])
    cap_in = (tuple(left[0]), tuple(right[0]))
    cap_out = (tuple(left[-1]), tuple(right[-1]))
    # COVERAGE GUARD (after the amputated back-channels): the
    # meshed domain must account for ~all of the RIVER'S OWN water between the
    # end transects. Reference = the connected water component under the
    # centerline, clipped by a laterally-UNBOUNDED slab (20 km half-width) so
    # a too-narrow corridor cannot hide what it cut off; disconnected ponds
    # and sloughs never depress the number. Rides metrics + the gate card.
    try:
        slab_l, slab_r = _offset_banks(cl, 40000.0, None)
        slab = sg.Polygon(np.vstack([slab_l, slab_r[::-1]]))
        if not slab.is_valid:
            slab = slab.buffer(0)
        river_comp = None
        for c in getattr(water, "geoms", [water]):
            if c.contains(mid) or c.distance(mid) < 50.0:
                river_comp = c
                break
        ref_area = float(river_comp.intersection(slab).area) if river_comp is not None else 0.0
        coverage = float(main.area / ref_area) if ref_area > 0 else 1.0
        coverage = min(coverage, 1.0)
    except Exception as exc:  # noqa: BLE001 -- guard must never block meshing
        LOG.warning("water-coverage computation failed (%s)", exc)
        coverage = 1.0
    if coverage < 0.90:
        LOG.warning(
            "water-coverage LOW: mesh domain covers %.0f%% of the river's "
            "water in the reach (%.1fM of %.1fM m2) - water may be unmeshed",
            coverage * 100, main.area / 1e6, ref_area / 1e6)
    LOG.info("water-polygon domain: %d exterior pts, %d island holes, "
             "area %.0f m2, water coverage %.1f%%",
             len(ext), len(holes), main.area, coverage * 100)
    return ext, holes, cap_in, cap_out, coverage


def _dist_to_segment(pts: np.ndarray, a, b) -> np.ndarray:
    """Distances from pts[N,2] to segment a-b (vectorized)."""
    a = np.asarray(a, dtype=float); b = np.asarray(b, dtype=float)
    ab = b - a
    denom = float(ab @ ab) or 1e-12
    t = np.clip(((pts - a) @ ab) / denom, 0.0, 1.0)
    proj = a + t[:, None] * ab
    return np.hypot(*(pts - proj).T)


def build_channel_mesh(cl: np.ndarray, cfg: ReachConfig):
    """Gmsh mesh of the real channel-following polygon; tagged boundary groups.

    Returns a mesh dict with 0-based ikle, rank-based IPOBO ring, and the
    inflow/outflow node sets (P0 gotchas 1-3, 7).
    """
    import gmsh

    # PRE-MESH GATE: refuse a degenerate reach (channel wider than the reach is
    # long) before gmsh can busy-loop on folded banks - a fast typed error, not
    # a 32-min hang (the guarded caller also validates in-parent before forking).
    validate_reach_geometry(cl, cfg)

    offsets = getattr(cfg, "bank_offsets", None)
    left, right = _offset_banks(cl, cfg.channel_width_m, offsets)
    # The WHOLE build runs inside a killable child process (build_channel_mesh_
    # guarded) with a hard wall-clock SIGKILL - the real watchdog, since a gmsh
    # C busy-loop cannot be preempted by an in-process signal. Dump the exact
    # geometry first so a failing case is reproducible offline.
    try:
        np.savez(str(Path(cfg.workdir) / "banks_debug.npz"),
                 cl=cl, left=left, right=right)
    except Exception:  # noqa: BLE001 -- debug dump is best-effort
        pass

    # gotcha 7: if banks self-intersect at a bend, smooth harder until simple
    tries = 0
    while not _banks_valid(left, right) and tries < 6:
        k = np.ones(5) / 5
        cl = np.column_stack([np.convolve(cl[:, 0], k, mode="same"),
                              np.convolve(cl[:, 1], k, mode="same")])
        cl[0] = cl[0]; cl[-1] = cl[-1]
        if offsets is not None:
            offsets = (np.convolve(offsets[0], k, mode="same"),
                       np.convolve(offsets[1], k, mode="same"))
        left, right = _offset_banks(cl, cfg.channel_width_m, offsets)
        tries += 1
    if not _banks_valid(left, right):
        raise RuntimeError(
            "MESH_BANKS_INVALID: bank offset curves still self-intersect "
            "after smoothing retries - refusing to mesh a folded channel"
        )
    banks_ok = _banks_valid(left, right)
    ms = cfg.mesh_size_m

    # TRUE water-polygon domain (the ribbon outline mismatches
    # the river). When it resolves, the mesh boundary IS the NHDArea bank line
    # and holes are the real islands; the ribbon below stays as the fallback.
    domain = None
    try:
        domain = _water_polygon_domain(cl, cfg, ms)
    except Exception as exc:  # noqa: BLE001 -- polygon domain is best-effort
        LOG.warning("water-polygon domain failed (%s) - ribbon fallback", exc)
    ext_pts = on_in = on_out = None
    island_rings: list[np.ndarray] = []
    if domain is not None:
        ext_pts, island_rings, cap_in, cap_out, water_coverage = domain
        d_in = _dist_to_segment(ext_pts, *cap_in)
        d_out = _dist_to_segment(ext_pts, *cap_out)
        on_in = d_in < ms
        on_out = d_out < ms
        n_in_edges = int(np.sum(on_in & np.roll(on_in, -1)))
        n_out_edges = int(np.sum(on_out & np.roll(on_out, -1)))
        if n_in_edges == 0 or n_out_edges == 0:
            LOG.warning(
                "water-polygon domain has no cap edges (in=%d out=%d) - "
                "ribbon fallback", n_in_edges, n_out_edges)
            domain = None
            island_rings = []

    # Ribbon fallback island holes: any ribbon area NOT covered by water
    # (interior holes AND channel-splitting islands like Cottonwood) becomes a
    # walled hole. Kept clear of the outer boundary; slivers below (2.5*h)^2
    # dropped (unmeshable at edge length h).
    water_polys = getattr(cfg, "water_polys_utm", None)
    if domain is None and water_polys:
        try:
            import shapely.geometry as sg
            from shapely.ops import unary_union

            ribbon = sg.Polygon(np.vstack([left, right[::-1]]))
            if not ribbon.is_valid:
                ribbon = ribbon.buffer(0)
            water = unary_union([
                sg.Polygon(ext, holes=[h for h in holes if len(h) >= 4])
                for ext, holes in water_polys if len(ext) >= 4
            ]).buffer(0)
            land = ribbon.buffer(-1.5 * ms).difference(water)
            geoms = getattr(land, "geoms", [land])
            for g in geoms:
                if g.is_empty or g.area < (2.5 * ms) ** 2:
                    continue
                g = g.simplify(ms / 2.0)
                if g.is_empty or not g.is_valid:
                    continue
                ext = np.asarray(g.exterior.coords)
                if len(ext) >= 5:
                    island_rings.append(ext[:-1])  # drop closing duplicate
            if island_rings:
                LOG.info("island holes: %d (areas %s m2)", len(island_rings),
                         [int(sg.Polygon(r).area) for r in island_rings])
        except Exception as exc:  # noqa: BLE001 -- islands are an enhancement,
            # never a mesh blocker; fall back to the hole-less ribbon
            LOG.warning("island-hole derivation failed (%s) - meshing without", exc)
            island_rings = []

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add(cfg.name)

    def add_pts(pts):
        return [gmsh.model.geo.addPoint(float(px), float(py), 0.0, ms)
                for px, py in pts]

    if domain is not None:
        # exterior = the REAL bank line as a closed chain of straight edges;
        # edges whose BOTH endpoints lie on an end-transect line become the
        # inflow/outflow caps, the rest are walls
        ptags = add_pts(ext_pts)
        n_ext = len(ptags)
        in_lines, out_lines, wall_lines, ordered = [], [], [], []
        for i in range(n_ext):
            j = (i + 1) % n_ext
            ln = gmsh.model.geo.addLine(ptags[i], ptags[j])
            ordered.append(ln)
            if on_in[i] and on_in[j]:
                in_lines.append(ln)
            elif on_out[i] and on_out[j]:
                out_lines.append(ln)
            else:
                wall_lines.append(ln)
        loop = gmsh.model.geo.addCurveLoop(ordered)
        wall_group_curves = wall_lines
        inflow_curves, outflow_curves = in_lines, out_lines
    else:
        lpts = add_pts(left)     # left bank upstream->downstream
        rpts = add_pts(right)
        left_wall = gmsh.model.geo.addSpline(lpts)
        right_wall = gmsh.model.geo.addSpline(rpts)
        inflow = gmsh.model.geo.addLine(rpts[0], lpts[0])     # upstream cap
        outflow = gmsh.model.geo.addLine(lpts[-1], rpts[-1])  # downstream cap
        loop = gmsh.model.geo.addCurveLoop(
            [left_wall, outflow, -right_wall, inflow])
        wall_group_curves = [left_wall, right_wall]
        inflow_curves, outflow_curves = [inflow], [outflow]
    hole_loops = []
    for hr in island_rings:
        # straight LINE chain, not a spline: splines overshoot at polygon
        # corners and can poke outside the surface, which kills generate(2)
        # silently (live: 9 islands -> zero triangles)
        hpts = add_pts(hr)
        hlines = [gmsh.model.geo.addLine(hpts[i], hpts[(i + 1) % len(hpts)])
                  for i in range(len(hpts))]
        hole_loops.append(gmsh.model.geo.addCurveLoop(hlines))
    surf = gmsh.model.geo.addPlaneSurface([loop, *hole_loops])
    gmsh.model.geo.synchronize()

    g_in = gmsh.model.addPhysicalGroup(1, inflow_curves)
    g_out = gmsh.model.addPhysicalGroup(1, outflow_curves)
    gmsh.model.addPhysicalGroup(1, wall_group_curves)
    gmsh.model.addPhysicalGroup(2, [surf])

    gmsh.model.mesh.generate(2)
    gmsh.model.mesh.removeDuplicateNodes()

    all_tags, all_coords, _ = gmsh.model.mesh.getNodes()
    all_coords = all_coords.reshape(-1, 3)
    coord_of = {int(t): all_coords[i] for i, t in enumerate(all_tags)}

    # gotcha 2: node set from triangle-referenced tags ONLY
    etypes, _, enodes = gmsh.model.mesh.getElements(2)
    tri_tags = None
    for et, en in zip(etypes, enodes):
        if et == 2:
            tri_tags = en.reshape(-1, 3).astype(np.int64)
    if tri_tags is None or len(tri_tags) == 0:
        gmsh.finalize()
        raise RuntimeError(
            "MESH_BUILD_EMPTY: gmsh generated no triangles (bad hole/boundary "
            f"geometry? islands={len(island_rings)})"
        )
    used = np.unique(tri_tags)
    t2i = {int(t): i for i, t in enumerate(used)}
    npoin = len(used)
    X = np.array([coord_of[int(t)][0] for t in used])
    Y = np.array([coord_of[int(t)][1] for t in used])
    ikle = np.array([[t2i[int(a)] for a in row] for row in tri_tags], dtype=np.int64)

    def pg_nodes(tag):
        nt, _ = gmsh.model.mesh.getNodesForPhysicalGroup(1, tag)
        return set(t2i[int(t)] for t in nt if int(t) in t2i)

    in_nodes = pg_nodes(g_in)
    out_nodes = pg_nodes(g_out)
    gmsh.finalize()

    # coincident-node guard
    from scipy.spatial import cKDTree
    dd, _ = cKDTree(np.column_stack([X, Y])).query(np.column_stack([X, Y]), k=2)
    mind = float(dd[:, 1].min())
    assert mind > 1e-3, f"coincident nodes (min {mind:.2e} m)"

    # CCW orientation
    a, b, c = ikle[:, 0], ikle[:, 1], ikle[:, 2]
    area2 = (X[b] - X[a]) * (Y[c] - Y[a]) - (X[c] - X[a]) * (Y[b] - Y[a])
    ikle[area2 < 0] = ikle[area2 < 0][:, ::-1]

    # boundary ring (edges in exactly one triangle) -> single CCW cycle
    ec = defaultdict(int); ed = {}
    for t in ikle:
        for k in range(3):
            u, v = int(t[k]), int(t[(k + 1) % 3])
            key = (min(u, v), max(u, v))
            ec[key] += 1; ed[key] = (u, v)
    bnd = [ed[k] for k, n in ec.items() if n == 1]
    nxt = {u: v for u, v in bnd}
    assert len(nxt) == len(bnd), "non-manifold boundary"
    # M3: with island holes the boundary is SEVERAL closed cycles. Walk them
    # all; the triangle-oriented directed edges already wind outer-CCW /
    # holes-CW (domain on the left), which is the TELEMAC convention. IPOBO
    # ranks run consecutively ring by ring, OUTER (longest) first.
    rings: list[list[int]] = []
    unvisited = set(nxt)
    while unvisited:
        start = next(iter(unvisited))
        walk = [start]; cur = nxt[start]
        while cur != start:
            walk.append(cur); cur = nxt[cur]
        unvisited -= set(walk)
        rings.append(walk)
    assert sum(len(w) for w in rings) == len(bnd), "boundary walk lost edges"
    rings.sort(key=len, reverse=True)
    n_islands = len(rings) - 1
    boundary_rings = [np.array(w, dtype=np.int64) for w in rings]
    ring = np.array([n for w in rings for n in w], dtype=np.int64)

    # gotcha 3: rank-based IPOBO
    nptfr = len(ring)
    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, node in enumerate(ring, start=1):
        ipob[node] = rank

    # classify ring nodes -> BC codes
    lihbor = np.full(nptfr, 2); liubor = np.full(nptfr, 2)
    livbor = np.full(nptfr, 2); litbor = np.full(nptfr, 2)
    cls = np.array(["wall"] * nptfr, dtype=object)
    for i, node in enumerate(ring):
        n = int(node)
        if n in in_nodes:
            lihbor[i], liubor[i], livbor[i], litbor[i] = 4, 5, 5, 5
            cls[i] = "inflow"
        elif n in out_nodes:
            lihbor[i], liubor[i], livbor[i], litbor[i] = 5, 4, 4, 4
            cls[i] = "outflow"

    return dict(X=X, Y=Y, ikle=ikle, npoin=npoin, ring=ring, ipob=ipob,
                nptfr=nptfr, lihbor=lihbor, liubor=liubor, livbor=livbor,
                litbor=litbor, cls=cls, in_nodes=in_nodes, out_nodes=out_nodes,
                n_in=int((cls == "inflow").sum()),
                n_out=int((cls == "outflow").sum()),
                n_islands=n_islands, boundary_rings=boundary_rings,
                domain_mode="water-polygon" if domain is not None else "ribbon",
                water_coverage_frac=(round(float(water_coverage), 4)
                                     if domain is not None else None),
                banks_ok=banks_ok, smooth_tries=tries, centerline=cl)


class MeshBuildTimeout(RuntimeError):
    """The channel-mesh build exceeded its wall-clock deadline and was killed.

    The hard watchdog (a gmsh C busy-loop cannot be preempted by an in-process
    signal, so the whole build runs in a killable child process that the parent
    SIGKILLs at the deadline). Names the same corrective retries as the
    degenerate-reach gate so the user has a path forward, never a silent hang."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = float(timeout_s)
        super().__init__(
            f"channel-mesh build exceeded the {self.timeout_s:.0f}s wall-clock "
            "deadline and was terminated. This usually means a near-degenerate "
            "reach geometry. Retry with a longer reach_length_km, an explicit "
            'river_name, or bank_source="constant_ribbon" with a smaller '
            "channel_width_m."
        )


def _mesh_build_child(cl: np.ndarray, cfg: "ReachConfig", result_path: str) -> None:
    """Child-process target: run build_channel_mesh, pickle the result/exception.

    gmsh state stays in this short-lived process so a fresh import per build has
    no stale global state, and the parent can SIGKILL a C busy-loop cleanly."""
    import pickle

    try:
        mesh = build_channel_mesh(cl, cfg)
        payload = {"status": "ok", "mesh": mesh}
    except ReachDegenerateError as exc:
        payload = {"status": "degenerate",
                   "reach_length_m": exc.reach_length_m,
                   "channel_width_m": exc.channel_width_m}
    except BaseException as exc:  # noqa: BLE001 -- serialize ANY failure honestly
        payload = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    with open(result_path, "wb") as fh:
        pickle.dump(payload, fh)


def build_channel_mesh_guarded(
    cl: np.ndarray,
    cfg: "ReachConfig",
    *,
    timeout_s: float | None = None,
):
    """build_channel_mesh under a HARD wall-clock watchdog (Bug 1b).

    Validates the reach geometry in-parent (fast typed ReachDegenerateError, no
    fork), then runs the gmsh build in a killable child process. On the deadline
    the child's whole process group is SIGKILLed - preempting a gmsh C busy-loop
    the old in-process SIGALRM could not - and MeshBuildTimeout is raised."""
    import multiprocessing as mp
    import os as _os
    import pickle
    import signal as _signal
    import tempfile

    if timeout_s is None:
        env = os.environ.get("TELEMAC_MESH_TIMEOUT_S")
        timeout_s = float(env) if env else _MESH_WALLCLOCK_TIMEOUT_S

    # Fast fail without forking / importing gmsh.
    validate_reach_geometry(cl, cfg)

    ctx = mp.get_context("fork")
    fd, result_path = tempfile.mkstemp(prefix="mesh_result_", suffix=".pkl",
                                       dir=str(cfg.workdir))
    _os.close(fd)
    proc = ctx.Process(target=_mesh_build_child, args=(cl, cfg, result_path))
    proc.start()
    proc.join(timeout_s)
    if proc.is_alive():
        # SIGKILL the whole group (C busy-loop ignores SIGTERM); then reap.
        try:
            _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.join(10)
        try:
            _os.unlink(result_path)
        except OSError:
            pass
        raise MeshBuildTimeout(timeout_s)

    try:
        with open(result_path, "rb") as fh:
            payload = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001 -- no/partial result == child crash
        raise RuntimeError(
            f"MESH_BUILD_FAILED: mesh child exited {proc.exitcode} without a "
            f"result ({type(exc).__name__}: {exc})"
        ) from exc
    finally:
        try:
            _os.unlink(result_path)
        except OSError:
            pass

    status = payload.get("status")
    if status == "ok":
        return payload["mesh"]
    if status == "degenerate":
        raise ReachDegenerateError(
            payload["reach_length_m"], payload["channel_width_m"]
        )
    raise RuntimeError(
        f"MESH_BUILD_FAILED: {payload.get('message', 'mesh child failed')}"
    )


# ---------------------------------------------------------------------------
# 4. DEM bed onto mesh nodes + enforced gentle downstream slope
# ---------------------------------------------------------------------------
# DEM retry ladder knobs. 2026-07-18 the Planetary Computer STAC
# endpoint served Azure Front Door 503 HTML and the old one-shot fetch killed
# runs outright; the data-source norm is primary -> fallback -> honest typed
# error. Module-level so tests (and a hot ops fix) can shrink the ladder.
_DEM_STAC_ATTEMPTS = 3
_DEM_STAC_BACKOFF_S = (5.0, 20.0, 60.0)
_3DEP_IMAGE_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
                   "3DEPElevation/ImageServer/exportImage")


def _retryable_dem_excs():
    """Network-shaped exceptions worth a retry on the STAC rung.

    pystac_client is guard-imported: it is always in the worker image but may
    be absent in offline test envs (the ladder still works without it).
    """
    import requests
    import rasterio

    excs = [requests.exceptions.RequestException,
            rasterio.errors.RasterioIOError]
    try:
        from pystac_client.exceptions import APIError
        excs.append(APIError)
    except ImportError:
        pass
    return tuple(excs)


def _sample_dem_stac(lon, lat, bbox):
    """Primary DEM rung: Planetary Computer STAC cop-dem-glo-30 point sample.

    Returns per-node elevations (NaN where unsampled), or None when the
    catalog has no tiles for the bbox (deterministic - retries cannot help).
    """
    import planetary_computer as pc
    import pystac_client
    import rasterio

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1")
    items = list(cat.search(collections=["cop-dem-glo-30"], bbox=bbox).items())
    if not items:
        return None
    z_raw = np.full(len(lon), np.nan)
    with rasterio.Env(GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_TIMEOUT="30"):
        for it in items:
            href = pc.sign(it).assets["data"].href
            with rasterio.open("/vsicurl/" + href) as src:
                samp = np.array(list(src.sample(np.column_stack([lon, lat]))),
                                dtype=float).ravel()
                nod = src.nodata
                if nod is not None:
                    samp[samp == nod] = np.nan
                take = np.isnan(z_raw) & ~np.isnan(samp)
                z_raw[take] = samp[take]
    return z_raw


def _sample_dem_3dep(lon, lat, bbox):
    """Fallback DEM rung: USGS 3DEP ImageServer exportImage point sample.

    Exports ONE bbox GeoTIFF at ~1 arcsecond (the GLO-30-equivalent grid, so
    the bed fit sees the same resolution class) and samples the SAME lon/lat
    node points the STAC rung samples - identical z_raw contract downstream.
    """
    import requests
    import rasterio  # noqa: F401 -- MemoryFile needs the rasterio env
    from rasterio.io import MemoryFile

    # ~1 arcsec pixels over the padded bbox; the ImageServer caps exports at
    # 4100 px/side so clamp (a capped reach just samples slightly coarser)
    ncols = int(np.clip(round((bbox[2] - bbox[0]) * 3600.0), 64, 4000))
    nrows = int(np.clip(round((bbox[3] - bbox[1]) * 3600.0), 64, 4000))
    resp = requests.get(_3DEP_IMAGE_URL, params={
        "bbox": ",".join(str(v) for v in bbox),
        "bboxSR": "4326",
        "imageSR": "4326",
        "size": f"{ncols},{nrows}",
        "format": "tiff",
        "pixelType": "F32",
        "f": "image",
    }, timeout=180)
    resp.raise_for_status()
    body = resp.content
    if body[:4] not in (b"II*\x00", b"MM\x00*"):
        # ArcGIS reports errors as HTTP-200 JSON/HTML - keep that honest
        raise RuntimeError(f"3DEP exportImage returned non-tiff: {body[:160]!r}")
    with MemoryFile(body) as mf, mf.open() as src:
        samp = np.array(list(src.sample(np.column_stack([lon, lat]))),
                        dtype=float).ravel()
        nod = src.nodata
        if nod is not None:
            samp[samp == nod] = np.nan
    samp[~np.isfinite(samp)] = np.nan
    samp[samp < -1.0e4] = np.nan  # ocean/void sentinels (e.g. -3.4e38)
    if not np.isfinite(samp).any():
        raise RuntimeError("3DEP exportImage returned no valid elevations for "
                           f"bbox {bbox} (outside 3DEP coverage?)")
    return samp


def _fetch_dem_samples(lon, lat, bbox):
    """DEM ladder: STAC x3 (5/20/60 s backoff) -> 3DEP -> typed error.

    Returns (z_raw, dem_source). Both rungs exhausted raises the plain
    RuntimeError the pipeline already surfaces as a typed metrics error
    (entrypoint.main catches it -> status=error) - no new error shape.
    """
    retryable = _retryable_dem_excs()
    last_err = "unreached"
    for attempt in range(1, _DEM_STAC_ATTEMPTS + 1):
        try:
            z = _sample_dem_stac(lon, lat, bbox)
        except retryable as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            LOG.warning("dem: STAC attempt %d/%d failed (%s)",
                        attempt, _DEM_STAC_ATTEMPTS, last_err)
            # No sleep after the FINAL attempt - go straight to the fallback
            # (a full outage should cost 5+20 s of backoff, not 85 s).
            if attempt < _DEM_STAC_ATTEMPTS:
                time.sleep(_DEM_STAC_BACKOFF_S[min(attempt - 1,
                                                   len(_DEM_STAC_BACKOFF_S) - 1)])
            continue
        except Exception as exc:  # noqa: BLE001 -- malformed item/JSON etc.
            # Non-retryable STAC failure modes (KeyError on a malformed item,
            # JSON decode on an HTTP-200 garbage body) must still reach the
            # 3DEP rung rather than escaping raw - retrying them is useless.
            last_err = f"{type(exc).__name__}: {exc}"
            LOG.warning("dem: STAC non-retryable failure (%s) - skipping to "
                        "fallback", last_err)
            break
        if z is None:
            last_err = f"no cop-dem-glo-30 tiles for bbox {bbox}"
            LOG.warning("dem: %s", last_err)
            break
        if not np.isfinite(z).any():
            last_err = "STAC sample returned no valid elevations"
            LOG.warning("dem: %s for bbox %s", last_err, bbox)
            break
        return z, "cop-dem-glo-30"
    LOG.warning("dem: falling back to USGS 3DEP for bbox %s", bbox)
    try:
        return _sample_dem_3dep(lon, lat, bbox), "usgs-3dep"
    except Exception as exc:  # noqa: BLE001 -- both rungs down -> honest error
        raise RuntimeError(
            f"DEM fetch failed for bbox {bbox}: Planetary Computer STAC "
            f"({last_err}) then USGS 3DEP fallback "
            f"({type(exc).__name__}: {exc})") from exc


def fetch_dem_bed(mesh: dict, cfg: ReachConfig, tr):
    """Sample Copernicus GLO-30 DEM at mesh nodes; fit a gentle downstream bed.

    Real canyon DEM is the SURFACE (canyon rim + water), noisy along the thalweg.
    We (a) sample raw DEM at each node (lon/lat), (b) compute along-channel
    distance s per node, (c) fit bed = z0 - slope*s using a robust downstream
    trend clamped to [min_bed_slope, max_bed_slope] so flow always moves.
    Both the measured DEM drop and the enforced slope are reported.
    """
    X, Y = mesh["X"], mesh["Y"]
    # node lon/lat (inverse transform)
    inv = tr  # Transformer 4326->utm; build inverse
    from pyproj import Transformer
    back = Transformer.from_crs(inv.target_crs, 4326, always_xy=True)
    lon, lat = back.transform(X, Y)
    pad = 0.01
    bbox = [float(lon.min() - pad), float(lat.min() - pad),
            float(lon.max() + pad), float(lat.max() + pad)]

    # retry ladder + 3DEP fallback (never a one-shot fetch)
    z_raw, dem_source = _fetch_dem_samples(lon, lat, bbox)

    # along-channel distance s: project each node onto the centerline polyline
    cl = mesh["centerline"]
    s_node = _project_s(X, Y, cl)

    valid = ~np.isnan(z_raw)
    # robust linear fit z ~ z0 - slope * s
    A = np.column_stack([np.ones(valid.sum()), s_node[valid]])
    coef, *_ = np.linalg.lstsq(A, z_raw[valid], rcond=None)
    z0_fit, slope_fit = coef[0], -coef[1]     # slope positive = downhill
    measured_slope = slope_fit
    slope = float(np.clip(slope_fit, cfg.min_bed_slope, cfg.max_bed_slope))
    z_up = float(np.nanpercentile(z_raw[valid], 20))  # robust upstream bed level
    # bed = clean monotonic downstream plane anchored at the fitted top
    Z = z_up - slope * s_node
    # fill any nan raw with fitted
    dem_meta = dict(
        dem_source=dem_source,
        dem_min=float(np.nanmin(z_raw)), dem_max=float(np.nanmax(z_raw)),
        n_dem_nan=int((~valid).sum()),
        measured_slope=float(measured_slope),
        enforced_slope=slope, bed_top_m=z_up,
        bed_drop_m=float(slope * s_node.max()),
        reach_len_m=float(s_node.max()))
    return Z, dem_meta


def _project_s(X, Y, cl):
    """Along-channel distance of each (X,Y) node projected onto centerline cl."""
    seglen = np.hypot(np.diff(cl[:, 0]), np.diff(cl[:, 1]))
    cum = np.concatenate([[0.0], np.cumsum(seglen)])
    s = np.zeros(len(X))
    for i in range(len(X)):
        px, py = X[i], Y[i]
        best_d = 1e18; best_s = 0.0
        for j in range(len(cl) - 1):
            ax, ay = cl[j]; bx, by = cl[j + 1]
            vx, vy = bx - ax, by - ay
            L2 = vx * vx + vy * vy
            if L2 == 0:
                continue
            t = ((px - ax) * vx + (py - ay) * vy) / L2
            t = min(1.0, max(0.0, t))
            cx, cy = ax + t * vx, ay + t * vy
            dd = (px - cx) ** 2 + (py - cy) ** 2
            if dd < best_d:
                best_d = dd; best_s = cum[j] + t * np.sqrt(L2)
        s[i] = best_s
    return s


# ---------------------------------------------------------------------------
# 4b. Bed-bathymetry COG (ADR 0231 in-worker input surfacing)
# ---------------------------------------------------------------------------
# The bed is sampled + fitted INSIDE this worker (fetch_dem_bed), so the composer
# has no emitter/uri for it -- the honest way to surface NATE's "if there is a
# river bed bathymetry I want it visualized" is for the worker to write the bed it
# actually solved on as a small EPSG:4326 COG next to the result and record its
# key in the result envelope; the composer then rounds it through
# publish_raster_input_cog as a role=context input. This generalizes: any future
# in-worker fetch surfaces the same way (write COG + record key -> composer emits).
#: pixel budget for the bed COG (kept SMALL -- it is a spot-check backdrop, not an
#: analysis raster; the reach is long+thin so we cap the long side, not total).
BED_COG_MAX_PX_PER_SIDE: int = 512
BED_COG_MIN_PX_PER_SIDE: int = 16
#: filename the supervisor uploads + the composer keys off (recorded in metrics).
BED_COG_FILENAME: str = "bed_bathymetry.tif"


def write_bed_cog(mesh: dict, Z, cfg: "ReachConfig", tr, path: str) -> dict:
    """Rasterize the solved bed elevations ``Z`` (mesh nodes) to a small 4326 COG.

    Reprojects the mesh nodes UTM -> EPSG:4326, linearly interpolates the per-node
    bed elevation onto a modest regular grid clipped to the channel footprint
    (nearest-node distance, so griddata does not paint the whole convex hull), and
    writes a tiled COG carrying the bed the TELEMAC solve actually ran on. Returns
    a metrics dict (``bed_cog`` filename + ``bed_cog_min_m`` / ``bed_cog_max_m`` /
    ``bed_cog_px``). Raises on any failure -- the caller wraps it best-effort so a
    bed-COG hiccup never voids a CORRECT END solve.
    """
    import math

    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    from scipy.interpolate import griddata
    from scipy.spatial import cKDTree
    from pyproj import Transformer

    X, Y = np.asarray(mesh["X"], dtype=float), np.asarray(mesh["Y"], dtype=float)
    z = np.asarray(Z, dtype=float)
    back = Transformer.from_crs(tr.target_crs, 4326, always_xy=True)
    lon, lat = back.transform(X, Y)
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    finite = np.isfinite(lon) & np.isfinite(lat) & np.isfinite(z)
    if finite.sum() < 3:
        raise RuntimeError("bed COG: fewer than 3 finite mesh nodes to rasterize")
    lon, lat, z = lon[finite], lat[finite], z[finite]

    min_lon, max_lon = float(lon.min()), float(lon.max())
    min_lat, max_lat = float(lat.min()), float(lat.max())
    # size the grid so the LONG side hits the pixel cap (a long thin reach), the
    # short side scaled to keep square-ish ground pixels, both clamped to a sane
    # floor -- keeps the COG small (a spot-check backdrop).
    span_lon = max(max_lon - min_lon, 1e-9)
    span_lat = max(max_lat - min_lat, 1e-9)
    mean_lat = 0.5 * (min_lat + max_lat)
    w_m = span_lon * 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    h_m = span_lat * 111_320.0
    if w_m >= h_m:
        ncols = BED_COG_MAX_PX_PER_SIDE
        nrows = int(round(BED_COG_MAX_PX_PER_SIDE * h_m / max(w_m, 1e-9)))
    else:
        nrows = BED_COG_MAX_PX_PER_SIDE
        ncols = int(round(BED_COG_MAX_PX_PER_SIDE * w_m / max(h_m, 1e-9)))
    nrows = int(np.clip(nrows, BED_COG_MIN_PX_PER_SIDE, BED_COG_MAX_PX_PER_SIDE))
    ncols = int(np.clip(ncols, BED_COG_MIN_PX_PER_SIDE, BED_COG_MAX_PX_PER_SIDE))

    gdx, gdy = span_lon / ncols, span_lat / nrows
    xc = min_lon + (np.arange(ncols) + 0.5) * gdx
    yc = max_lat - (np.arange(nrows) + 0.5) * gdy  # north -> south (COG row 0 = N)
    gx, gy = np.meshgrid(xc, yc)
    pts = np.column_stack([lon, lat])
    grid = griddata(pts, z, (gx, gy), method="linear")
    grid = np.asarray(grid, dtype="float32")
    # clip to the channel: a cell whose nearest node is > ~1.5 mean-cell away is
    # outside the meshed reach -> nodata (never paint the convex hull).
    tree = cKDTree(pts)
    dist, _ = tree.query(np.column_stack([gx.ravel(), gy.ravel()]), k=1)
    clip = 1.5 * float(max(gdx, gdy))
    grid[(dist.reshape(nrows, ncols) > clip)] = np.nan
    grid[~np.isfinite(grid)] = np.nan
    if not np.isfinite(grid).any():
        raise RuntimeError("bed COG: grid is entirely nodata after clipping")

    nodata = -9999.0
    out = np.where(np.isfinite(grid), grid, nodata).astype("float32")
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)
    profile = dict(
        driver="COG", dtype="float32", count=1, height=nrows, width=ncols,
        crs="EPSG:4326", transform=transform, nodata=nodata,
        compress="deflate", blocksize=256,
    )
    if os.path.exists(path):
        os.remove(path)
    try:
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(out, 1)
    except Exception:  # noqa: BLE001 -- some rasterio builds lack the COG driver
        # Fall back to a tiled GTiff (publish_layer re-tiles it anyway); still a
        # valid georeferenced raster the plugin can read.
        profile.update(driver="GTiff", tiled=True, blockxsize=256, blockysize=256)
        profile.pop("blocksize", None)
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(out, 1)

    finite_vals = grid[np.isfinite(grid)]
    return {
        "bed_cog": os.path.basename(path),
        "bed_cog_min_m": round(float(finite_vals.min()), 3),
        "bed_cog_max_m": round(float(finite_vals.max()), 3),
        "bed_cog_px": [int(nrows), int(ncols)],
    }


# ---------------------------------------------------------------------------
# 5. SELAFIN geometry + boundary conditions (from P0)
# ---------------------------------------------------------------------------
def write_slf(mesh, Z, path):
    from data_manip.extraction.telemac_file import TelemacFile
    if os.path.exists(path):
        os.remove(path)
    tf = TelemacFile(path, access="w")
    tf.add_header(f"P1 REAL RIVER {os.path.basename(path)}",
                  date=np.array([2026, 7, 14, 0, 0, 0]))
    tf.add_mesh(mesh["X"], mesh["Y"], mesh["ikle"], z=Z)
    tf._ipob3 = mesh["ipob"].astype(np.int32)
    tf._ipob2 = tf._ipob3
    tf._nptfr = int(mesh["nptfr"])
    tf._nbor = mesh["ring"].astype(np.int32)
    tf._knolg = np.arange(1, mesh["npoin"] + 1, dtype=np.int32)
    tf.add_variable("BOTTOM          ", "M               ")
    tf.add_data_value("BOTTOM          ", 0, Z)
    tf.write()
    tf.close()


def write_cli(mesh, path):
    ring = mesh["ring"]; nptfr = mesh["nptfr"]
    lines = []
    for k in range(nptfr):
        node1 = int(ring[k]) + 1
        rank = k + 1
        lih, liu = mesh["lihbor"][k], mesh["liubor"][k]
        liv, lit = mesh["livbor"][k], mesh["litbor"][k]
        lines.append(
            f"{lih} {liu} {liv}  0.000 0.000 0.000 0.000  {lit}  0.000 0.000 0.000 "
            f"{node1:>11d} {rank:>11d}   # {mesh['cls'][k]}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 6. Deck author + liquid-boundary mapping (gotcha 4)
# ---------------------------------------------------------------------------
#: Basename of the TELEMAC-2D SOURCES FILE (the time series for the finite
#: mid-reach dye pulse). Written by author_deck next to the .cas; referenced by
#: basename in the deck (the solver stages it into its temp workdir). The worker
#: entrypoint lists this in its outputs so the pulse forcing is uploaded as
#: evidence next to the result .slf.
SOURCES_FILENAME = "river_sources.txt"


def spill_point(mesh, cfg):
    """Mid-reach spill (X, Y, node index) at ``cfg.spill_frac`` of the channel.

    Walks the smoothed centerline to the ``spill_frac`` arc-length point, then
    snaps to the nearest INTERIOR mesh node (never a boundary-ring node, so the
    point source is a genuine in-channel release, not on the inflow/outflow cap
    or a wall). TELEMAC snaps ABSCISSAE/ORDINATES OF SOURCES to the nearest node
    anyway; we pre-snap so the reported coordinate is an actual wet node.
    """
    cl = mesh["centerline"]
    # an explicit user-picked release point (set as UTM by run_pipeline
    # from cfg.release_lon/lat) overrides the spill_frac walk - but only when
    # it lands within 2 channel widths of the mesh (else fall back + note).
    rel = getattr(cfg, "release_utm", None)
    px = py = None
    if rel is not None:
        rx, ry = float(rel[0]), float(rel[1])
        d2r = (mesh["X"] - rx) ** 2 + (mesh["Y"] - ry) ** 2
        # accept radius: 2 stated widths OR 1.5x the widest REAL bank span
        # (wide rivers like the Columbia dwarf the stated default width)
        lim = 2.0 * float(cfg.channel_width_m)
        off = getattr(cfg, "bank_offsets", None)
        if off is not None:
            lim = max(lim, 1.5 * float((off[0] + off[1]).max()))
        if float(np.sqrt(d2r.min())) <= lim:
            px, py = rx, ry
            mesh["release_point_used"] = True
        else:
            mesh["release_point_rejected_dist_m"] = round(float(np.sqrt(d2r.min())), 1)
    if px is None:
        seglen = np.hypot(np.diff(cl[:, 0]), np.diff(cl[:, 1]))
        cum = np.concatenate([[0.0], np.cumsum(seglen)])
        total = float(cum[-1])
        target = float(np.clip(cfg.spill_frac, 0.0, 1.0)) * total
        j = int(np.clip(np.searchsorted(cum, target), 1, len(cl) - 1))
        seg = max(cum[j] - cum[j - 1], 1e-9)
        st = (target - cum[j - 1]) / seg
        px = cl[j - 1, 0] + st * (cl[j, 0] - cl[j - 1, 0])
        py = cl[j - 1, 1] + st * (cl[j, 1] - cl[j - 1, 1])
    ring = set(int(n) for n in mesh["ring"])
    d2 = (mesh["X"] - px) ** 2 + (mesh["Y"] - py) ** 2
    for idx in np.argsort(d2):
        if int(idx) not in ring:
            return float(mesh["X"][idx]), float(mesh["Y"][idx]), int(idx)
    return float(px), float(py), -1


def write_sources_pulse(path, cfg):
    """Write the SOURCES FILE: a FINITE dye pulse then the point source stops.

    Columns are the TELEMAC-2D sources-file names (same reader as the liquid-
    boundary file): ``T`` (s), ``Q(1)`` (m3/s carrier discharge), ``TR(1,1)``
    (dye mg/L). Q + dye are held over ``[0, pulse_window_s]`` then step to zero
    (a spill that stops), so the slug travels downstream and dilutes/passes. The
    final time exceeds DURATION so time interpolation never runs off the end.
    """
    w = float(cfg.pulse_window_s)
    q = float(cfg.source_q_m3s)
    dye = float(cfg.dye_conc_mgl)
    tend = max(float(cfg.duration_s) + 100.0, w + 100.0)
    lines = [
        "#",
        "T Q(1) TR(1,1)",
        "s m3/s mg/l",
        f"0.0 {q:.3f} {dye:.3f}",
        f"{w:.3f} {q:.3f} {dye:.3f}",
        f"{w + 0.1:.3f} 0.0 0.0",
        f"{tend:.3f} 0.0 0.0",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


#: the authored WAQTEL steering file (its own DAMOCLES-parsed .cas, subject to
#: the same 72-char line limit as the main deck). Named in the t2d cas via
#: WAQTEL STEERING FILE and uploaded as forcing evidence by the supervisor.
WAQTEL_FILENAME = "t2d_river.waqtel"


def write_waqtel_decay(cfg, workdir: str) -> str:
    """Author the tiny WAQTEL steering file for first-order tracer DECAY.

    WATER QUALITY PROCESS = 17 (degradation): its nametrac branch loops over ALL
    existing user tracers and applies a first-order decay SINK, so it rides
    directly on the UNCHANGED dye tracer (NUMBER OF TRACERS = 1) - zero new
    tracers. Two keys (arrays sized to the tracer count = 1): LAW OF TRACERS
    DEGRADATION picks the law (1 = T90 bacterial die-off with the coefficient in
    HOURS, 2 = first-order k in h^-1, 3 = first-order k in d^-1) and COEFFICIENT
    1 FOR LAW OF TRACERS DEGRADATION is that per-tracer coefficient. Returns the
    steering filename written into ``workdir``. The DAMOCLES 72-char line limit
    applies here exactly as in the main deck, so every line is clamped.
    """
    law = int(getattr(cfg, "decay_law", 1))
    coef = float(getattr(cfg, "decay_coef", 2.0))
    lines = [
        "/------------------------------------------------------------------/",
        "/  WAQTEL steering - first-order tracer DEGRADATION (process 17)",
        f"/  law={law} (1=T90 h, 2=k h^-1, 3=k d^-1)  coef={coef:g}  ntrac=1 (dye)",
        "/------------------------------------------------------------------/",
        f"LAW OF TRACERS DEGRADATION           = {law}",
        f"COEFFICIENT 1 FOR LAW OF TRACERS DEGRADATION = {coef:g}",
    ]
    # DAMOCLES hard 72-char line limit (identical to author_deck's clamp): a
    # single over-long line derails the parser. Comments are safely sliced;
    # the two data lines are short by construction but clamped defensively.
    clamped = [ln[:72] if len(ln) > 72 else ln for ln in lines]
    over = [ln for ln in clamped if len(ln) > 72]
    if over:
        LOG.warning("waqtel steering lines still >72 chars after clamp: %r",
                    over[:3])
    path = os.path.join(workdir, WAQTEL_FILENAME)
    with open(path, "w") as f:
        f.write("\n".join(clamped) + "\n")
    LOG.info("waqtel decay steering authored: law=%d coef=%g -> %s",
             law, coef, WAQTEL_FILENAME)
    return WAQTEL_FILENAME


def write_waqtel_o2(cfg, workdir: str) -> str:
    """Author the WAQTEL steering file for the O2 module (WATER QUALITY PROCESS 2).

    The dissolved-oxygen SAG kinetics. Every English keyword name is verified vs
    the in-image waqtel.dico (v9.0). To reproduce the Streeter-Phelps closed form
    the eutrophication/benthic O2 sources are zeroed (BENTHIC DEMAND, PHOTOSYNTHESIS
    P, VEGETAL RESPIRATION R = 0) and nitrification is off (K4 = 0), leaving only
    first-order deoxygenation (K1) balanced by surface reaeration (K2). FORMULA FOR
    COMPUTING K2 = 0 uses the CONSTANT reaeration coefficient K22 (the S-P k2); a
    non-zero formula (1 TVA .. 5 combined) computes k2 from the modeled U/H instead.
    FORMULA FOR COMPUTING CS = 0 uses the CONSTANT saturation O2SATU (the S-P Cs),
    set from a temperature-dependent value upstream. WATER SALINITY = 0 (freshwater
    river; salinity only matters when CS is computed). Returns the steering filename
    written into ``workdir``; every line respects the DAMOCLES 72-char clamp.
    """
    k1 = float(getattr(cfg, "do_k1_per_day", 0.3))
    k2 = float(getattr(cfg, "do_k2_per_day", 0.9))
    formk2 = int(getattr(cfg, "do_k2_formula", 0))
    cs = float(getattr(cfg, "do_sat_mgl", 9.0))
    temp = float(getattr(cfg, "do_water_temp_c", 20.0))
    lines = [
        "/------------------------------------------------------------------/",
        "/  WAQTEL O2 steering - dissolved-oxygen sag (process 2)",
        f"/  k1={k1:g} d^-1  k2={k2:g} d^-1 (FORMK2={formk2})  Cs={cs:g} T={temp:g}C",
        "/------------------------------------------------------------------/",
        f"WATER TEMPERATURE                             = {temp:g}",
        "WATER SALINITY                                = 0.",
        f"CONSTANT OF DEGRADATION OF ORGANIC LOAD K1    = {k1:g}",
        "CONSTANT OF NITRIFICATION KINETIC K4          = 0.",
        f"FORMULA FOR COMPUTING K2                      = {formk2}",
        f"K2 REAERATION COEFFICIENT                     = {k2:g}",
        "FORMULA FOR COMPUTING CS                      = 0",
        f"O2 SATURATION DENSITY OF WATER (CS)           = {cs:g}",
        "BENTHIC DEMAND                                = 0.",
        "PHOTOSYNTHESIS P                              = 0.",
        "VEGETAL RESPIRATION R                         = 0.",
    ]
    clamped = [ln[:72] if len(ln) > 72 else ln for ln in lines]
    over = [ln for ln in clamped if len(ln) > 72]
    if over:
        LOG.warning("waqtel O2 steering lines still >72 chars after clamp: %r",
                    over[:3])
    path = os.path.join(workdir, WAQTEL_FILENAME)
    with open(path, "w") as f:
        f.write("\n".join(clamped) + "\n")
    LOG.info("waqtel O2 steering authored: k1=%g k2=%g formk2=%d Cs=%g -> %s",
             k1, k2, formk2, cs, WAQTEL_FILENAME)
    return WAQTEL_FILENAME


#: the worker-authored GAIA steering file (its own DAMOCLES-parsed .cas, same
#: 72-char line limit) named in the t2d cas via GAIA STEERING FILE, and the GAIA
#: result SELAFIN carrying CUMUL BED EVOL (deposition). Both ship as outputs.
GAIA_STEERING_FILENAME = "gaia_river.cas"
GAIA_RESULT_FILENAME = "gaia_river.slf"


def _normalize_gradation(raw) -> list[tuple[float, float]]:
    """Coerce a sediment_gradation spec to a clean [(d50_um, fraction), ...] list.

    Accepts a list/tuple of (d50_um, fraction) pairs (JSON round-trips them as
    2-lists) or of {'d50_um':.., 'fraction':..} dicts. Each d50 is clamped to
    [5, 2000] um (silt .. coarse sand, the single-class band); non-positive or
    unparseable entries drop. Fractions are floored at 0 and RENORMALIZED to sum
    to 1 (so a caller can pass raw weights); classes are sorted fine->coarse and
    capped at 6 (GAIA's cost grows per class - a demo gradation, not a full sieve
    curve). Returns [] when fewer than 2 valid classes survive (a single class is
    not a mixture), so the caller keeps the single-class path.
    """
    out: list[tuple[float, float]] = []
    for item in (raw or ()):
        try:
            if isinstance(item, dict):
                um = float(item.get("d50_um"))
                fr = float(item.get("fraction", 0.0))
            else:
                um = float(item[0])
                fr = float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (um > 0.0) or fr < 0.0:
            continue
        um = min(max(um, 5.0), 2000.0)
        out.append((um, fr))
    if len(out) < 2:
        return []                       # a single class is not a mixture
    out.sort(key=lambda p: p[0])
    out = out[:6]
    total = sum(fr for _, fr in out)
    if total <= 0.0:
        # no usable fractions -> equal split so the mix is still valid
        eq = 1.0 / len(out)
        return [(um, eq) for um, _ in out]
    return [(um, fr / total) for um, fr in out]


def write_gaia_deck(cfg, slf_name: str, cli_name: str, workdir: str) -> str:
    """Author the GAIA steering file for ONE supply-limited suspended class.

    Emits ~18 keywords (all under the DAMOCLES 72-char clamp) for GAIA's INTERNAL
    coupling to TELEMAC-2D: same GEOMETRY + BOUNDARY CONDITIONS files (river.slf /
    river.cli - GAIA's dico marks the BC file OBLIG and reuses the CONLIM reader),
    its own RESULTS FILE (gaia_river.slf) and ONE non-cohesive (NCO) suspended
    class. The class settles (CLASSES SETTLING VELOCITIES = -9 -> auto
    Stokes/Zanke/van Rijn by grain size) and is SUPPLY-LIMITED (LAYERS INITIAL
    THICKNESS = 0 so nothing erodes - only the injected pulse deposits); bedload is
    OFF (v1). The source concentration is the T2D source's concentration expressed
    in GAIA's SI unit kg/m3 (= dye_conc_mgl mg/L / 1000; the smoke confirmed the
    r2d suspended tracer reads in g/l == kg/m3). Bed-evolution output is ON via the
    'B,E' graphic printouts (E == CUMUL BED EVOL, the deposition map, in metres).

    v1 always emits NCO (non-cohesive): sand and silt are non-cohesive by
    construction and mud is approximated as very-fine non-cohesive sediment (the
    cohesive CO Krone/Partheniades path + its extra critical-shear/Partheniades
    keywords is v2, deliberately NOT emitted so the deck stays the in-image-proven
    shape). ``sediment_type`` only tunes the default grain size + narration.

    Returns the steering filename written into ``workdir``.
    """
    # source concentration: reuse the dye pulse concentration (mg/L) as the
    # generic source concentration, converted to GAIA's kg/m3 (g/l). Clamped >= 0.
    conc_kgm3 = max(float(getattr(cfg, "dye_conc_mgl", 100.0)) / 1000.0, 0.0)
    # d50 in METRES from microns; floored so a bogus value cannot zero the grain.
    d50_m = max(float(getattr(cfg, "grain_size_um", 200.0)), 1.0) * 1.0e-6
    density = float(getattr(cfg, "sediment_density", 2650.0))
    gradation = _normalize_gradation(getattr(cfg, "sediment_gradation", ()))
    dredging = bool(getattr(cfg, "dredging", False))
    if len(gradation) >= 2:
        # v3 MULTI-CLASS GRADED SEDIMENT: several non-cohesive size classes share
        # one erodible bed. Meyer-Peter-Mueller transport differs by grain size and
        # the Egiazaroff HIDING FACTOR (formula 1) couples the classes, so under a
        # flood the bed SORTS: fines winnow out of the high-shear thalweg (surface
        # armors, MEAN DIAMETER rises) and settle in slack water (surface fines).
        # SUSPENSION is OFF (pure bedload -> a clean sorting signal + NO suspended
        # tracer appended to T2D, same coupling as the v2 single-class scour path).
        # The D50 output var (surface mean diameter) carries the armoring/sorting
        # signature - constant for a single class, spatially varying here.
        bed_thick = max(float(getattr(cfg, "bed_thickness_m", 5.0)), 0.01)
        formula = int(getattr(cfg, "bedload_formula", 1) or 1)
        mofac = max(float(getattr(cfg, "morphological_factor", 10.0)), 1.0)
        diams = ";".join(f"{um * 1.0e-6:g}" for um, _ in gradation)
        fracs = ";".join(f"{fr:g}" for _, fr in gradation)
        types = ";".join("NCO" for _ in gradation)
        dens = ";".join(f"{density:g}" for _ in gradation)
        d50_list = "/".join(f"{um:g}" for um, _ in gradation)
        lines = [
            "/------------------------------------------------------------------/",
            "/  GAIA steering - v3 MULTI-CLASS GRADED bedload (grain sorting)",
            f"/  {len(gradation)} classes d50um={d50_list[:40]} hiding=Egiazaroff",
            "/------------------------------------------------------------------/",
            f"GEOMETRY FILE                   = {os.path.basename(slf_name)}",
            f"BOUNDARY CONDITIONS FILE        = {os.path.basename(cli_name)}",
            f"RESULTS FILE                    = {GAIA_RESULT_FILENAME}",
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'B,E,D50'",
            f"CLASSES TYPE OF SEDIMENT        = {types}",
            f"CLASSES SEDIMENT DIAMETERS      = {diams}",
            f"CLASSES SEDIMENT DENSITY        = {dens}",
            f"CLASSES INITIAL FRACTION        = {fracs}",
            "SUSPENSION FOR ALL SANDS        = NO",
            "BED LOAD FOR ALL SANDS          = YES",
            f"BED-LOAD TRANSPORT FORMULA FOR ALL SANDS = {formula}",
            "HIDING FACTOR FORMULA           = 1",
            f"LAYERS INITIAL THICKNESS        = {bed_thick:g}",
            f"MORPHOLOGICAL FACTOR            = {mofac:g}",
            "MASS-BALANCE                    = YES",
        ]
    elif bool(getattr(cfg, "erodible_bed", False)) or dredging:
        # v2 ERODIBLE-BED MORPHODYNAMICS: a real erodible bed stock + active bedload
        # transport, so the bed SCOURS (negative CUMUL BED EVOL) where the flow
        # steepens and re-deposits where it slackens. SUSPENSION is OFF (pure
        # bedload morphodynamics -> a clean scour/deposition signal and NO suspended
        # tracer appended to T2D, so the dye stays the sole hydraulic companion).
        # MORPHOLOGICAL FACTOR amplifies the bed change per hydraulic step so a
        # short demo hydrograph produces a readable scour depth. Keywords pinned
        # against gaia.dico v9.0. LAYERS INITIAL THICKNESS carries per-layer stock;
        # one generous layer keeps the whole reach erodible over the demo.
        bed_thick = max(float(getattr(cfg, "bed_thickness_m", 5.0)), 0.01)
        formula = int(getattr(cfg, "bedload_formula", 1) or 1)
        mofac = max(float(getattr(cfg, "morphological_factor", 10.0)), 1.0)
        lines = [
            "/------------------------------------------------------------------/",
            "/  GAIA steering - v2 ERODIBLE BED, bedload morphodynamics (scour)",
            f"/  type={str(getattr(cfg, 'sediment_type', 'sand'))[:8]} "
            f"d50={d50_m*1e6:g}um bed={bed_thick:g}m icf={formula} mofac={mofac:g}",
            "/------------------------------------------------------------------/",
            f"GEOMETRY FILE                   = {os.path.basename(slf_name)}",
            f"BOUNDARY CONDITIONS FILE        = {os.path.basename(cli_name)}",
            f"RESULTS FILE                    = {GAIA_RESULT_FILENAME}",
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'B,E'",
            "CLASSES TYPE OF SEDIMENT        = NCO",
            f"CLASSES SEDIMENT DIAMETERS      = {d50_m:g}",
            f"CLASSES SEDIMENT DENSITY        = {density:g}",
            "CLASSES INITIAL FRACTION        = 1.",
            "SUSPENSION FOR ALL SANDS        = NO",
            "BED LOAD FOR ALL SANDS          = YES",
            f"BED-LOAD TRANSPORT FORMULA FOR ALL SANDS = {formula}",
            f"LAYERS INITIAL THICKNESS        = {bed_thick:g}",
            f"MORPHOLOGICAL FACTOR            = {mofac:g}",
            "MASS-BALANCE                    = YES",
        ]
    else:
        lines = [
            "/------------------------------------------------------------------/",
            "/  GAIA steering - ONE suspended NCO class, supply-limited bed",
            "/  (LAYERS INITIAL THICKNESS = 0 -> only the injected pulse deposits)",
            f"/  type={str(getattr(cfg, 'sediment_type', 'sand'))[:8]} "
            f"d50={d50_m*1e6:g}um conc={conc_kgm3:g}kg/m3",
            "/------------------------------------------------------------------/",
            f"GEOMETRY FILE                   = {os.path.basename(slf_name)}",
            f"BOUNDARY CONDITIONS FILE        = {os.path.basename(cli_name)}",
            f"RESULTS FILE                    = {GAIA_RESULT_FILENAME}",
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'B,E'",
            "CLASSES TYPE OF SEDIMENT        = NCO",
            f"CLASSES SEDIMENT DIAMETERS      = {d50_m:g}",
            f"CLASSES SEDIMENT DENSITY        = {density:g}",
            "CLASSES INITIAL FRACTION        = 1.",
            "CLASSES SETTLING VELOCITIES     = -9.",
            "SUSPENSION FOR ALL SANDS        = YES",
            "BED LOAD FOR ALL SANDS          = NO",
            "SUSPENSION TRANSPORT FORMULA FOR ALL SANDS = 3",
            "LAYERS INITIAL THICKNESS        = 0.",
            "SCHEME FOR ADVECTION OF SUSPENDED SEDIMENTS = 1",
            f"SUSPENDED SEDIMENTS CONCENTRATION VALUES AT THE SOURCES = {conc_kgm3:g}",
            "MASS-BALANCE                    = YES",
        ]
    if dredging:
        # NESTOR dig/dump coupling (ADR 0254): enable the precompiled module and
        # name its own-format input files. Keywords pinned against gaia.dico v9.0
        # (NESTOR logical INDEX 25; NESTOR ACTION/POLYGON/SURFACE REFERENCE FILE).
        # The action + polygon (+ surface-ref) files are authored by
        # write_nestor_decks into the same workdir.
        # the surface reference file is MANDATORY for ANY action: Write_Node_Info
        # (called on every dig/dump to log the affected nodes) computes each node's
        # km chainage via Set_by_Profiles_Values_for, which hard-errors on a missing
        # NESTOR SURFACE REFERENCE FILE -- so it is emitted in BOTH modes (criterion
        # additionally reads the design grade z from it).
        nlines = [
            "/  NESTOR dredging (dig/dump on the erodible bed)",
            "NESTOR                          = YES",
            f"NESTOR ACTION FILE              = {NESTOR_ACTION_FILENAME}",
            f"NESTOR POLYGON FILE             = {NESTOR_POLYGON_FILENAME}",
            f"NESTOR SURFACE REFERENCE FILE   = {NESTOR_SURFACE_REF_FILENAME}",
        ]
        # insert before the trailing MASS-BALANCE line so the file stays tidy
        if lines and lines[-1].startswith("MASS-BALANCE"):
            lines = lines[:-1] + nlines + [lines[-1]]
        else:
            lines += nlines
    # DAMOCLES hard 72-char line limit (identical to author_deck's clamp): every
    # line is defensively sliced; comments are safe, the data lines are short by
    # construction (the keyword above is the longest at 55 + a small number).
    clamped = [ln[:72] if len(ln) > 72 else ln for ln in lines]
    over = [ln for ln in clamped if len(ln) > 72]
    if over:
        LOG.warning("gaia steering lines still >72 chars after clamp: %r",
                    over[:3])
    path = os.path.join(workdir, GAIA_STEERING_FILENAME)
    with open(path, "w") as f:
        f.write("\n".join(clamped) + "\n")
    LOG.info("gaia sediment steering authored: d50=%gum density=%g conc=%gkg/m3 "
             "-> %s", d50_m * 1e6, density, conc_kgm3, GAIA_STEERING_FILENAME)
    return GAIA_STEERING_FILENAME


# ---------------------------------------------------------------------------
# NESTOR dredging deck authoring (ADR 0254)
# ---------------------------------------------------------------------------
# NESTOR reads three own-format ASCII files, named in the GAIA steering via the
# NESTOR ACTION FILE / NESTOR POLYGON FILE / NESTOR SURFACE REFERENCE FILE
# keywords (gaia.dico v9.0, SUBMIT SINACT/SINPOL/SINREF). The grammar below is
# pinned to the in-image compiled fortran the baked libnestor4*.so builds from
# (sources/nestor/): readdigactions.f, readpolygons.f, isactioncompletelydefined.f,
# datestringtoseconds.f, set_by_profiles_values_for.f. Coordinates are in the mesh
# CRS (local UTM metres) since NESTOR's inside-polygon test runs on mesh XY.
NESTOR_ACTION_FILENAME = "nestor.act"
NESTOR_POLYGON_FILENAME = "nestor.pol"
NESTOR_SURFACE_REF_FILENAME = "nestor.ref"
#: the deterministic time origin the worker stamps into the t2d deck (ORIGINAL
#: DATE OF TIME / ORIGINAL HOUR OF TIME) so NESTOR action-file absolute dates map
#: to sim seconds through DateStringToSeconds (seconds since MARDAT/MARTIM).
NESTOR_TIME_ORIGIN = (2024, 1, 1, 0, 0, 0)
#: the three-numeral-prefixed field names (readdigactions/readpolygons demand a
#: ThreeDigitsNumeral prefix); the polygon NAME and the action FieldDig/FieldDump
#: are matched on the first three numerals. ThreeDigitsNumeral additionally
#: requires the FIRST digit be 1-9 (>= 100, in-image pin) -- a leading 0 is
#: rejected -- so the ids start at 101/102, not 001/002.
_NESTOR_DIG_FIELD = "101_channel"
_NESTOR_DUMP_FIELD = "102_spoil"


def _nestor_time_str(offset_s: float) -> str:
    """Format sim-seconds offset as NESTOR's yyyy.mm.dd-hh:mm:ss (exactly 19 ch).

    DateStringToSeconds reads seconds since the ORIGINAL DATE/HOUR OF TIME origin
    (MARDAT/MARTIM); the worker stamps NESTOR_TIME_ORIGIN into the deck, so an
    offset in sim seconds maps to an absolute date the parser accepts.
    """
    import datetime as _dt
    base = _dt.datetime(*NESTOR_TIME_ORIGIN)
    t = base + _dt.timedelta(seconds=float(offset_s))
    return t.strftime("%Y.%m.%d-%H:%M:%S")


def _channel_box_utm(mesh, cfg, station_frac: float,
                     length_m: float, width_m: float) -> list[tuple[float, float]]:
    """A channel-spanning rectangle in UTM around one centerline station.

    Picks the centerline vertex nearest ``station_frac`` of the arc length, takes
    the local along-channel tangent, and returns the 4 corners of a box
    length_m (along) x width_m (across) centred there. Used to build a dredge or
    disposal zone when no explicit polygon is supplied.
    """
    cl = np.asarray(mesh["centerline"], dtype=float)
    seg = np.diff(cl, axis=0)
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(seg[:, 0], seg[:, 1]))])
    total = float(arc[-1]) if arc[-1] > 0 else 1.0
    target = max(0.0, min(1.0, float(station_frac))) * total
    i = int(np.argmin(np.abs(arc - target)))
    c = cl[i]
    j = min(i + 1, len(cl) - 1)
    k = max(i - 1, 0)
    tan = cl[j] - cl[k]
    n = float(np.hypot(tan[0], tan[1]))
    if n < 1e-9:
        tvec = np.array([1.0, 0.0])
    else:
        tvec = tan / n
    perp = np.array([-tvec[1], tvec[0]])
    hl, hw = length_m / 2.0, width_m / 2.0
    corners = [
        c - hl * tvec - hw * perp,
        c + hl * tvec - hw * perp,
        c + hl * tvec + hw * perp,
        c - hl * tvec + hw * perp,
    ]
    return [(float(p[0]), float(p[1])) for p in corners]


def _dredge_zones_utm(mesh, cfg):
    """Resolve (dig_polygon, dump_polygon_or_None) in UTM for the NESTOR run.

    Explicit dredge_zone_utm / disposal_zone_utm win (gate-supplied geometry);
    otherwise a channel-spanning box is built from the centerline at the
    configured station fraction. The dredge box length defaults to 2x the channel
    width; its width spans the full channel (1.4x the nominal width so it fully
    brackets the wetted section).
    """
    width = float(getattr(cfg, "channel_width_m", 60.0)) * 1.4
    length = getattr(cfg, "dredge_zone_len_m", None)
    length = float(length) if length else 2.0 * float(getattr(cfg, "channel_width_m", 60.0))
    dig = list(getattr(cfg, "dredge_zone_utm", ()) or ())
    if len(dig) < 3:
        dig = _channel_box_utm(mesh, cfg, getattr(cfg, "dredge_station_frac", 0.5),
                               length, width)
    dump = None
    explicit_dump = list(getattr(cfg, "disposal_zone_utm", ()) or ())
    want_dump = bool(getattr(cfg, "dredge_disposal", False)) or len(explicit_dump) >= 3
    if want_dump:
        if len(explicit_dump) >= 3:
            dump = explicit_dump
        else:
            dump = _channel_box_utm(
                mesh, cfg, getattr(cfg, "dredge_disposal_station_frac", 0.85),
                length, width)
    return dig, dump


def write_nestor_polygon_file(dig_poly, dump_poly, workdir: str) -> str:
    """Author NESTOR's polygon file (readpolygons.f format).

    One block per zone: a ``NAME <id>_name`` line (3-digit numeral prefix,
    checked by ThreeDigitsNumeral) then the vertex ``x y`` lines (two reals,
    read free-format); the file MUST end with a bare ``ENDFILE`` line (no
    trailing blanks) or the reader errors on unexpected EOF. Coordinates are
    mesh UTM metres. Comment lines start with '#' or '/'.
    """
    lines = ["# NESTOR polygon file - dredge/dump zones (mesh UTM metres)"]
    lines.append(f"NAME {_NESTOR_DIG_FIELD}")
    for x, y in dig_poly:
        lines.append(f"{x:.3f} {y:.3f}")
    if dump_poly:
        lines.append(f"NAME {_NESTOR_DUMP_FIELD}")
        for x, y in dump_poly:
            lines.append(f"{x:.3f} {y:.3f}")
    lines.append("ENDFILE")
    path = os.path.join(workdir, NESTOR_POLYGON_FILENAME)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    LOG.info("nestor polygon file authored: dig=%d pts dump=%s -> %s",
             len(dig_poly), (f"{len(dump_poly)} pts" if dump_poly else "none"),
             NESTOR_POLYGON_FILENAME)
    return NESTOR_POLYGON_FILENAME


def write_nestor_action_file(cfg, workdir: str, has_dump: bool) -> str:
    """Author NESTOR's action file (readdigactions.f grammar).

    A top-level ``RESTART = NO`` (the reader hard-errors on its absence) then one
    ``ACTION``..``ENDACTION`` block, terminated by ``ENDFILE``. Keyword=value
    lines only; the keyword set + per-ActionType required fields are pinned to
    isactioncompletelydefined.f. Two modes:

    * scheduled -> ActionType = Dig_by_time (needs TimeStart/TimeEnd/FieldDig/
      DigVolume); an optional FieldDump (no DumpRate) makes NESTOR place the dug
      spoil over the same window (DumpMode 10, Dump_by_time).
    * criterion -> ActionType = Dig_by_criterion (needs TimeStart/TimeEnd/
      TimeRepeat/FieldDig/DigRate/CritDepth/DigDepth/MinVolume/MinVolumeRadius/
      ReferenceLevel); ReferenceLevel = GRID reads the design grade from the
      NESTOR SURFACE REFERENCE FILE.
    """
    dur = float(getattr(cfg, "duration_s", 3600.0))
    mode = str(getattr(cfg, "dredge_mode", "scheduled")).lower()
    t0 = _nestor_time_str(max(0.0, float(getattr(cfg, "dredge_start_frac", 0.15))) * dur)
    t1 = _nestor_time_str(min(1.0, float(getattr(cfg, "dredge_end_frac", 0.95))) * dur)
    lines = [
        "/ NESTOR action file - channel maintenance dredging (ADR 0254)",
        f"/ mode={mode}",
        # RESTART is read as a Fortran LOGICAL (READ(valueStr,*) Restart), so the
        # value MUST be a Fortran logical literal (F/.FALSE.), NOT DAMOCLES YES/NO.
        "RESTART = F",
        "ACTION",
    ]
    if mode == "criterion":
        # Dig_by_criterion: trigger where the silted bed rises within CritDepth of
        # the design grade; dig down to DigDepth below grade at DigRate. TimeRepeat
        # re-arms the closed loop across the run so re-siltation is re-dredged.
        rate = max(float(getattr(cfg, "dredge_rate_m_per_s", 5.0e-4)), 1.0e-9)
        crit = float(getattr(cfg, "dredge_crit_depth_m", 0.3))
        dig_depth = float(getattr(cfg, "dredge_dig_depth_m", 1.5))
        repeat = max(dur / 4.0, 1.0)
        lines += [
            f"  ActionType      = Dig_by_criterion",
            f"  FieldDig        = {_NESTOR_DIG_FIELD}",
            f"  TimeStart       = {t0}",
            f"  TimeEnd         = {t1}",
            f"  TimeRepeat      = {repeat:g}",
            f"  DigRate         = {rate:g}",
            f"  CritDepth       = {crit:g}",
            f"  DigDepth        = {dig_depth:g}",
            f"  MinVolume       = 0.",
            f"  MinVolumeRadius = 0.",
            # SECTIONS keeps refZ as the design grade interpolated from the NESTOR
            # SURFACE REFERENCE FILE profiles (Set_by_Profiles); GRID would instead
            # demand a gridded ZRL field NESTOR does not have here.
            f"  ReferenceLevel  = SECTIONS",
        ]
        if has_dump:
            lines.append(f"  FieldDump       = {_NESTOR_DUMP_FIELD}")
            lines.append(f"  DumpRate        = {rate:g}")
    else:
        vol = max(float(getattr(cfg, "dredge_volume_m3", 4000.0)), 1.0)
        lines += [
            f"  ActionType      = Dig_by_time",
            f"  FieldDig        = {_NESTOR_DIG_FIELD}",
            f"  TimeStart       = {t0}",
            f"  TimeEnd         = {t1}",
            f"  DigVolume       = {vol:g}",
        ]
        if has_dump:
            # FieldDump with no DumpRate -> DumpMode 10 (Dump_by_time): the dug
            # spoil is placed into the disposal field over the same window.
            lines.append(f"  FieldDump       = {_NESTOR_DUMP_FIELD}")
    lines += ["ENDACTION", "ENDFILE"]
    path = os.path.join(workdir, NESTOR_ACTION_FILENAME)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    LOG.info("nestor action file authored: mode=%s window=[%s,%s] dump=%s -> %s",
             mode, t0, t1, has_dump, NESTOR_ACTION_FILENAME)
    return NESTOR_ACTION_FILENAME


def write_nestor_surface_ref_file(cfg, mesh, workdir: str) -> str:
    """Author NESTOR's surface reference file (set_by_profiles_values_for.f format).

    Format: >= 2 cross-section profile lines, each ``x1 y1 z1 x2 y2 z2 km``
    (7 reals), terminated by a line starting ``END``. At each field node NESTOR
    interpolates refZ + km between the two bracketing profiles, so EVERY field
    node (dig AND dump, anywhere on the reach) must lie between two profiles and
    consecutive profiles must stay < 90 deg apart. The worker lays a fence of
    channel-crossing profiles at every few centerline stations spanning the whole
    reach (full-width, so all across-channel nodes are bracketed), each carrying
    the constant design navigation grade z. The end profiles are nudged just
    beyond the reach so the extreme nodes are enclosed too.
    """
    grade = float(getattr(cfg, "dredge_design_grade_m", None) or 0.0)
    cl = np.asarray(mesh["centerline"], dtype=float)
    # generous half-width so the profiles fully bracket the field polygons across
    # the channel (fields span ~0.7x channel width; use 2x the width per side).
    half_w = max(float(getattr(cfg, "channel_width_m", 60.0)) * 2.0, 30.0)
    seg = np.diff(cl, axis=0)
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(seg[:, 0], seg[:, 1]))])
    total = float(arc[-1]) or 1.0
    # sample ~1 profile every ~half the channel width along the reach (>= 3), so
    # consecutive profiles are near-parallel (small angle) even on a bend.
    step = max(int(len(cl) // max(int(total / max(half_w, 1.0)) + 2, 3)), 1)
    idxs = list(range(0, len(cl), step))
    if idxs[-1] != len(cl) - 1:
        idxs.append(len(cl) - 1)
    lines = ["# NESTOR surface reference file - design navigation grade profiles"]
    for k, i in enumerate(idxs):
        c = cl[i]
        j = min(i + 1, len(cl) - 1)
        p = max(i - 1, 0)
        tan = cl[j] - cl[p]
        nrm = float(np.hypot(tan[0], tan[1])) or 1.0
        tvec = tan / nrm
        perp = np.array([-tvec[1], tvec[0]])
        # nudge the two end profiles outward so the extreme nodes are enclosed
        push = 0.0
        if i == 0:
            push = -5.0
        elif i == len(cl) - 1:
            push = 5.0
        c2 = c + push * tvec
        a = c2 - half_w * perp
        b = c2 + half_w * perp
        km = float(arc[i] / total)
        lines.append(
            f"{a[0]:.3f} {a[1]:.3f} {grade:.3f} "
            f"{b[0]:.3f} {b[1]:.3f} {grade:.3f} {km:.5f}")
    lines.append("END")
    path = os.path.join(workdir, NESTOR_SURFACE_REF_FILENAME)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    LOG.info("nestor surface reference file authored: %d profiles grade=%.3f m -> %s",
             len(idxs), grade, NESTOR_SURFACE_REF_FILENAME)
    return NESTOR_SURFACE_REF_FILENAME


def write_nestor_decks(cfg, mesh, workdir: str) -> dict:
    """Author all NESTOR input files for a dredging run; return the emitted names.

    Resolves the dig/dump zones (explicit UTM polygons or channel boxes), writes
    the polygon file, the action file, and -- criterion mode only -- the surface
    reference file carrying the design grade (auto-resolved from the dig-zone mean
    bed when not supplied). Returns {'action','polygon','surface_ref'|None,
    'has_dump'} for the GAIA-steering keyword wiring.
    """
    dig_poly, dump_poly = _dredge_zones_utm(mesh, cfg)
    has_dump = dump_poly is not None
    write_nestor_polygon_file(dig_poly, dump_poly, workdir)
    write_nestor_action_file(cfg, workdir, has_dump)
    # the surface reference file is MANDATORY in both modes (Write_Node_Info needs
    # it for km chainage); resolve the design grade from the mean bed over the dig
    # zone when the caller left it unset -- criterion digs to grade - DigDepth,
    # scheduled ignores refZ but the file must still parse (7 reals per profile).
    if getattr(cfg, "dredge_design_grade_m", None) is None:
        Z = mesh.get("Z")
        Z = np.asarray(Z, dtype=float) if Z is not None else None
        if Z is None and mesh.get("bed_z") is not None:
            Z = np.asarray(mesh["bed_z"], dtype=float)
        X = np.asarray(mesh["X"], dtype=float)
        grade = None
        if Z is not None:
            inside = _points_in_poly(X, np.asarray(mesh["Y"], dtype=float),
                                     np.asarray(dig_poly, dtype=float))
            if inside.any():
                grade = float(np.mean(Z[inside]))
            else:
                grade = float(np.mean(Z))
        cfg.dredge_design_grade_m = grade if grade is not None else 0.0
    surface_ref = write_nestor_surface_ref_file(cfg, mesh, workdir)
    return dict(action=NESTOR_ACTION_FILENAME, polygon=NESTOR_POLYGON_FILENAME,
                surface_ref=surface_ref, has_dump=has_dump)


def _points_in_poly(X, Y, poly) -> "np.ndarray":
    """Boolean mask of mesh nodes inside a convex/simple polygon (ray casting)."""
    px = poly[:, 0]
    py = poly[:, 1]
    n = len(poly)
    inside = np.zeros(len(X), dtype=bool)
    j = n - 1
    for i in range(n):
        cond = ((py[i] > Y) != (py[j] > Y)) & (
            X < (px[j] - px[i]) * (Y - py[i]) / (py[j] - py[i] + 1e-30) + px[i])
        inside ^= cond
        j = i
    return inside


# M2-spike-proven oil steering parameters (oilspill.f reader format). Fractions
# sum to 1.0 per preset; HAP rows = FM TB SOLU KDISS KVOL.
OIL_PRESETS: dict[str, dict] = {
    "light_crude": dict(
        compo=[(0.5, 645.0), (0.3, 830.0)],
        hap=[(0.2, 673.0, 0.018, 1.0e-5, 5.0e-5)],
        rho=850.0, eta=1.0e-5, voldev=20.0, tamb=288.0, etal=1),
    "diesel": dict(
        compo=[(0.6, 560.0), (0.25, 700.0)],
        hap=[(0.15, 610.0, 0.005, 1.0e-5, 8.0e-5)],
        rho=840.0, eta=4.0e-6, voldev=10.0, tamb=288.0, etal=1),
    "heavy_fuel": dict(
        compo=[(0.75, 900.0), (0.2, 1050.0)],
        hap=[(0.05, 800.0, 0.001, 5.0e-6, 1.0e-5)],
        rho=960.0, eta=5.0e-4, voldev=30.0, tamb=288.0, etal=1),
}

_OIL_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "oil_templates")


def write_oil_inputs(cfg, sx: float, sy: float, workdir: str) -> None:
    """Author the oil-module inputs next to the cas (M2-spike-proven layout).

    Writes ``oil_spill.txt`` from the preset and ``user_fortran/oil_flot.f``
    from the template with the release moved to the SPILL POINT (the default
    OIL_FLOT is a hardcoded Loire demo at LT=10000).
    """
    p = OIL_PRESETS.get(str(cfg.oil_preset), OIL_PRESETS["light_crude"])
    lines = [f"{cfg.oil_preset.upper()} - trid3nt oil preset", str(len(p["compo"])),
             "FM_COMPO TB_COMPO"]
    lines += [f"{fm} {tb}" for fm, tb in p["compo"]]
    lines += ["NB_HAP", str(len(p["hap"])), "FM_HAP TB_HAP SOLU KDISS KVOL"]
    lines += [" ".join(str(v) for v in row) for row in p["hap"]]
    lines += ["RHO_OIL", str(p["rho"]), "ETA_OIL", str(p["eta"]),
              "VOLDEV", str(p["voldev"]), "TAMB", str(p["tamb"]),
              "ETAL", str(p["etal"])]
    with open(os.path.join(workdir, "oil_spill.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    tpl = open(os.path.join(_OIL_TEMPLATE_DIR, "oil_flot_template.f")).read()
    tpl = tpl.replace("IF(LT.EQ.60)", f"IF(LT.EQ.{int(cfg.oil_release_step)})")
    # the template release coords (whatever the spike pinned) -> this reach's
    # spill point; the template stores them as <num>.D0 literals
    import re as _re
    tpl = _re.sub(r"COORD_X=\d+\.D0", f"COORD_X={sx:.0f}.D0", tpl)
    tpl = _re.sub(r"COORD_Y=\d+\.D0", f"COORD_Y={sy:.0f}.D0", tpl)
    uf = os.path.join(workdir, "user_fortran")
    os.makedirs(uf, exist_ok=True)
    with open(os.path.join(uf, "oil_flot.f"), "w") as f:
        f.write(tpl)
    LOG.info("oil inputs authored: preset=%s release=(%.0f,%.0f) step=%d",
             cfg.oil_preset, sx, sy, cfg.oil_release_step)


def parse_drogues(path: str) -> list[tuple[float, list[tuple[float, float]]]]:
    """TecPlot ASCII drogues file -> [(t_seconds, [(x, y), ...]), ...]."""
    import re as _re

    zones: list[tuple[float, list[tuple[float, float]]]] = []
    cur_t, cur = None, []
    for line in open(path):
        if line.startswith("ZONE"):
            if cur_t is not None:
                zones.append((cur_t, cur))
            m = _re.search(r"SOLUTIONTIME=\s*([\d.]+)", line)
            cur_t, cur = (float(m.group(1)) if m else 0.0), []
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                cur.append((float(parts[1]), float(parts[2])))
            except ValueError:
                pass
    if cur_t is not None:
        zones.append((cur_t, cur))
    return zones


def author_deck(cfg, mesh, slf, cli, res, cas_path, lb_order, bed):
    """Write the .cas (+ the SOURCES FILE for the finite spill pulse).

    lb_order maps the TELEMAC liquid-boundary index (1-based, in boundary-walk
    order) -> 'inflow' or 'outflow'; PRESCRIBED lists are written in that order
    (gotcha 4). bed = dem_meta dict.

    DYE forcing = a FINITE PULSE at a mid-reach POINT SOURCE (not the old
    continuous upstream-inflow injection): clean flow (inflow Q, outflow stage)
    drives the reach with ZERO dye at the boundaries; the point source injects
    dye for ``pulse_window_s`` then turns off, so the plume advects downstream
    and dilutes/passes rather than saturating the whole reach.
    """
    bed_outflow = bed["bed_top_m"] - bed["bed_drop_m"]
    outflow_stage = bed_outflow + cfg.init_depth_m
    is_do_sag = str(getattr(cfg, "substance_class", "tracer")).lower() == "do_sag"
    q = []; elev = []; tracer = []
    for role in lb_order:
        if role == "inflow":
            q.append(f"{cfg.inflow_q_m3s}")
            elev.append("0.0")
            if is_do_sag:
                # WAQTEL O2 appends DISSOLVED O2, ORGANIC LOAD, NH4 LOAD after the
                # dye tracer (boundary-major PRESCRIBED, tr.f IRANK order): the
                # fully-mixed discharge rides in here (DO + CBOD), the dye stays 0.
                tracer += ["0.0",
                           f"{float(cfg.do_sag_upstream_do_mgl):g}",
                           f"{float(cfg.do_sag_bod_mgl):g}", "0.0"]
            else:
                tracer.append("0.0")   # clean flow -- dye enters via the point source
        else:  # outflow: prescribe a downstream stage = bed + target depth
            q.append("0.0")
            elev.append(f"{outflow_stage:.3f}")
            if is_do_sag:
                tracer += ["0.0", "0.0", "0.0", "0.0"]   # exit boundary: free
            else:
                tracer.append("0.0")

    sx, sy, snode = spill_point(mesh, cfg)
    # do_sag models the reach STARTING at the fully-mixed discharge: the CBOD + DO
    # ride in at the INFLOW boundary (PRESCRIBED TRACERS VALUES), so there is NO
    # point-source dye pulse. Omitting the SOURCES FILE + source keywords avoids a
    # single-tracer source array colliding with the O2 module's 4 tracers.
    if not is_do_sag:
        src_path = os.path.join(os.path.dirname(os.path.abspath(cas_path)),
                                SOURCES_FILENAME)
        write_sources_pulse(src_path, cfg)
        sources_file_line = f"SOURCES FILE                    = {SOURCES_FILENAME}\n"
        sources_block = (
            "MAXIMUM NUMBER OF SOURCES        = 20\n"
            f"ABSCISSAE OF SOURCES             = {sx:.3f}\n"
            f"ORDINATES OF SOURCES             = {sy:.3f}\n"
            "WATER DISCHARGE OF SOURCES       = 0.0\n"
            "VALUES OF THE TRACERS AT THE SOURCES = 0.0\n"
        )
    else:
        sources_file_line = ""
        sources_block = ""

    # TELEMAC-PHYS-1 constitutive-physics literals. SAFETY INVARIANT: when the
    # ReachConfig override is None (unset) the deck emits the EXACT historical
    # literal string ("3" / "33." / "1.E-1"), byte-identical to every prior run;
    # a set value is formatted with %g (a valid TELEMAC float) and flows through.
    # TELEMAC cas REAL keywords conventionally carry a decimal point (the
    # hardcoded defaults are "33." / "1.E-1"). ``%g`` alone renders an
    # integer-valued float without one (45.0 -> "45"), so a SET override gets a
    # trailing "." when it has no decimal/exponent -- defensive for DAMOCLES +
    # matches the default style. UNSET stays the exact historical literal
    # (byte-identical guarantee).
    def _cas_real(v: float) -> str:
        s = f"{float(v):g}"
        return s if any(c in s for c in ".eE") else s + "."

    _fric_law = 3 if cfg.friction_law is None else int(cfg.friction_law)
    _fric_coef = "33." if cfg.friction_coefficient is None \
        else _cas_real(cfg.friction_coefficient)
    _vel_diff = "1.E-1" if cfg.velocity_diffusivity is None \
        else _cas_real(cfg.velocity_diffusivity)
    _tracer_diff = "1.E-1" if cfg.tracer_diffusivity is None \
        else _cas_real(cfg.tracer_diffusivity)

    # WIND-STRESS FORCING (constant OPTION FOR WIND = 1). Emitted ONLY when
    # wind_speed_mps > 0; unset (0.0) leaves the deck byte-identical (no WIND
    # lines). The meteorological FROM-direction is resolved into velocity
    # components pointing in the direction the wind BLOWS TOWARD, in the mesh's
    # UTM frame (x=easting, y=northing): wind FROM north (0 deg) drives water
    # southward (wy<0), wind FROM west (270 deg) drives it eastward (wx>0).
    import math as _math
    _wind_speed = float(getattr(cfg, "wind_speed_mps", 0.0) or 0.0)
    if _wind_speed > 0.0:
        _th = _math.radians(float(getattr(cfg, "wind_dir_from_deg", 0.0) or 0.0))
        _wx = -_wind_speed * _math.sin(_th)   # FROM-dir -> blows TOWARD
        _wy = -_wind_speed * _math.cos(_th)
        _cd = getattr(cfg, "wind_drag_coef", None)
        _cd_line = "" if _cd is None else \
            f"COEFFICIENT OF WIND INFLUENCE   = {_cas_real(_cd)}\n"
        wind_block = (
            "WIND                            = YES\n"
            "OPTION FOR WIND                 = 1\n"
            f"{_cd_line}"
            f"WIND VELOCITY ALONG X           = {_cas_real(_wx)}\n"
            f"WIND VELOCITY ALONG Y           = {_cas_real(_wy)}\n"
            "THRESHOLD DEPTH FOR WIND        = 1.\n"
        )
    else:
        wind_block = ""

    # DISTRIBUTED ON-MESH RAINFALL / EVAPORATION. Emitted ONLY when
    # rain_or_evap_mm_per_day is set (non-None); unset leaves the deck
    # byte-identical (no RAIN lines, RAIN OR EVAPORATION absent = NO). The
    # native TELEMAC-2D source term applies a uniform water flux at every wet
    # node - independent of the inflow-boundary hydrograph (q above). Signed:
    # positive = rain (water in), negative = evaporation (water out).
    _rain_rate = getattr(cfg, "rain_or_evap_mm_per_day", None)
    if _rain_rate is not None:
        # When RAIN is active AND tracers exist, TELEMAC-2D (DAMOCLES) REQUIRES
        # the rainwater tracer concentration (keyword VALUES OF TRACERS IN THE
        # RAIN / MNEMO TRAIN) with one value per tracer - omitting it aborts with
        # "GIVE AS MANY VALUES AS TRACERS". Rainwater carries ZERO dye / sediment
        # (a clean-rain default). Tracer count: do_sag = 4 (dye + DO + CBOD +
        # NH4), sediment (GAIA) = 2 (dye + suspended class), else 1 (dye).
        _subst = str(getattr(cfg, "substance_class", "tracer")).lower()
        # sediment: v1 SUSPENSION appends a 2nd tracer (dye + suspended class); v2
        # ERODIBLE bedload appends none (dye only). do_sag = 4 (dye + DO + CBOD + NH4).
        _sed_suspended = _subst == "sediment" and not bool(
            getattr(cfg, "erodible_bed", False))
        _n_tracers = 4 if _subst == "do_sag" else (2 if _sed_suspended else 1)
        _train = ";".join(["0."] * _n_tracers)
        rain_block = (
            "RAIN OR EVAPORATION             = YES\n"
            f"RAIN OR EVAPORATION IN MM PER DAY = {_cas_real(float(_rain_rate))}\n"
            f"VALUES OF TRACERS IN THE RAIN   = {_train}\n"
        )
    else:
        rain_block = ""

    cas = f"""/-------------------------------------------------------------------/
/  TELEMAC-2D  P1 REAL RIVER DYE  -  {cfg.name}
/  Mesh from NHDPlus flowlines (Gmsh, tagged physical groups) -> rank IPOBO.
/  Clean flow (inflow->outflow) drives the reach; a FINITE dye pulse is
/  released at a mid-reach point source (~{cfg.spill_frac:.0%} along, node
/  {snode}, x={sx:.1f} y={sy:.1f}) for {cfg.pulse_window_s:.0f}s then stops, so
/  the plume advects downstream following the REAL river curves and dilutes.
/  Liquid-boundary order (walk): {lb_order}
/-------------------------------------------------------------------/
GEOMETRY FILE                   = {os.path.basename(slf)}
BOUNDARY CONDITIONS FILE        = {os.path.basename(cli)}
RESULTS FILE                    = {os.path.basename(res)}
{sources_file_line}/
TITLE : '{cfg.name} REAL RIVER DYE PULSE'
VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'
GRAPHIC PRINTOUT PERIOD         = {cfg.graphic_period}
LISTING PRINTOUT PERIOD         = 500
/
DURATION                        = {cfg.duration_s}
TIME STEP                       = {cfg.time_step_s}
/
INITIAL CONDITIONS              = 'CONSTANT DEPTH'
INITIAL DEPTH                   = {cfg.init_depth_m:.3f}
/
PRESCRIBED FLOWRATES            = {';'.join(q)}
PRESCRIBED ELEVATIONS           = {';'.join(elev)}
/
{sources_block}/
LAW OF BOTTOM FRICTION          = {_fric_law}
FRICTION COEFFICIENT            = {_fric_coef}
VELOCITY DIFFUSIVITY            = {_vel_diff}
{wind_block}{rain_block}/
EQUATIONS                       = 'SAINT-VENANT FE'
TREATMENT OF THE LINEAR SYSTEM  = 2
TYPE OF ADVECTION               = 1;5
SUPG OPTION                     = 0;0
MASS-LUMPING ON H : 1.
CONTINUITY CORRECTION : YES
SOLVER                          = 1
SOLVER ACCURACY                 = 1.E-6
MAXIMUM NUMBER OF ITERATIONS FOR SOLVER = 500
IMPLICITATION FOR DEPTH         = 0.6
IMPLICITATION FOR VELOCITY      = 0.6
TIDAL FLATS                             = YES
OPTION FOR THE TREATMENT OF TIDAL FLATS = 1
TREATMENT OF NEGATIVE DEPTHS            = 2
H CLIPPING     : NO
/
NUMBER OF TRACERS               = 1
NAMES OF TRACERS                = 'DYE             MG/L'
INITIAL VALUES OF TRACERS       = 0.
PRESCRIBED TRACERS VALUES       = {';'.join(tracer)}
SCHEME FOR ADVECTION OF TRACERS          = 1
COEFFICIENT FOR DIFFUSION OF TRACERS     = {_tracer_diff}
"""
    if str(getattr(cfg, "substance_class", "tracer")).lower() == "oil":
        # M3 oil class: the module rides ON TOP of the tracer solve (its
        # soluble fraction feeds T1); presence of the steering file activates
        # it (v9). The slick releases at the THALWEG near the spill point -
        # the deepest interior node within 300 m - because OIL_BEACHING kills
        # floats in shallow margins (live: 100 particles dead in ~80 steps at
        # a shallow release node while the same preset thrived 250 m away).
        # CLEARANCE-snap (live-bisected 2026-07-18): floats released near a
        # wall/island boundary are silently dropped from the drogues tracker
        # within ~minutes (oil balance still counts their mass as surface),
        # while a release 437m clear survived 100/100 for the full run. Pick
        # the interior node with MAX distance-from-any-boundary within 400m
        # of the spill point, tie-broken by deeper bed.
        ox, oy = sx, sy
        X_, Y_ = mesh["X"], mesh["Y"]
        interior = mesh["ipob"] == 0
        near = (np.hypot(X_ - sx, Y_ - sy) < 400.0) & interior
        if np.any(near):
            from scipy.spatial import cKDTree
            bx = np.column_stack([X_[~interior], Y_[~interior]])
            clr, _ = cKDTree(bx).query(
                np.column_stack([X_[near], Y_[near]]))
            score = clr.copy()
            bed_z = mesh.get("bed_z")
            if bed_z is not None:
                bz = np.asarray(bed_z)[near]
                score = clr - 0.01 * (bz - bz.min())  # clearance first, depth tie-break
            idx = np.where(near)[0][np.argmax(score)]
            ox, oy = float(X_[idx]), float(Y_[idx])
            LOG.info("oil release clearance-snapped: (%.0f,%.0f) -> (%.0f,%.0f) "
                     "(wall clearance %.0f m)", sx, sy, ox, oy,
                     float(clr[np.argmax(score)]))
        write_oil_inputs(cfg, ox, oy, os.path.dirname(os.path.abspath(cas_path)))
        cas += (
            "/\n"
            "FORTRAN FILE                    = user_fortran\n"
            "OIL SPILL STEERING FILE         = oil_spill.txt\n"
            f"MAXIMUM NUMBER OF DROGUES       = {int(cfg.n_drogues)}\n"
            f"PRINTOUT PERIOD FOR DROGUES     = "
            f"{max(int(cfg.drogues_period_s / max(cfg.time_step_s, 1e-6)), 1)}\n"
            "ASCII DROGUES FILE              = drogues.txt\n"
        )

    if str(getattr(cfg, "substance_class", "tracer")).lower() == "decay":
        # WAQTEL v1a decay class (mutually exclusive with oil): couple WAQTEL
        # with WATER QUALITY PROCESS = 17 (first-order tracer DEGRADATION). Its
        # nametrac branch applies a decay SINK to every existing user tracer, so
        # it rides directly on the UNCHANGED dye tracer (NUMBER OF TRACERS = 1) -
        # ZERO new tracers, ZERO postprocess/contract change, ZERO SOURCES change
        # (the pulse column stays the dye concentration). Only three keys land in
        # the t2d cas; the decay law + coefficient live in the tiny steering file.
        write_waqtel_decay(cfg, os.path.dirname(os.path.abspath(cas_path)))
        cas += (
            "/\n"
            "COUPLING WITH                   = 'WAQTEL'\n"
            f"WAQTEL STEERING FILE            = {WAQTEL_FILENAME}\n"
            "WATER QUALITY PROCESS           = 17\n"
        )

    if is_do_sag:
        # WAQTEL O2 class (mutually exclusive with oil/decay/sediment): couple
        # WAQTEL with WATER QUALITY PROCESS = 2 (the O2 module). nametrac_waqtel
        # appends THREE tracers after the dye - DISSOLVED O2 (T2), ORGANIC LOAD /
        # CBOD (T3), NH4 LOAD (T4) - so the deck must (a) OUTPUT them (add T2,T3,T4
        # to the graphic printouts) and (b) size INITIAL VALUES OF TRACERS to the
        # FOUR tracers (the single-value default only covers the dye). PRESCRIBED
        # TRACERS VALUES is already widened to 4-per-boundary in the lb_order loop
        # above (the mixed CBOD + DO ride in at the inflow). The O2 kinetics
        # (k1/k2/Cs) live in the steering file. In-image V&V (2026-08-07): the
        # sag reproduces the Streeter-Phelps 1925 closed form to 0.011 mg/L.
        write_waqtel_o2(cfg, os.path.dirname(os.path.abspath(cas_path)))
        cas = cas.replace(
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'",
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1,T2,T3,T4'")
        cas = cas.replace(
            "INITIAL VALUES OF TRACERS       = 0.",
            "INITIAL VALUES OF TRACERS       = 0.;"
            f"{float(cfg.do_sag_upstream_do_mgl):g};0.;0.")
        cas += (
            "/\n"
            "COUPLING WITH                   = 'WAQTEL'\n"
            f"WAQTEL STEERING FILE            = {WAQTEL_FILENAME}\n"
            "WATER QUALITY PROCESS           = 2\n"
        )

    if str(getattr(cfg, "substance_class", "tracer")).lower() == "sediment":
        # GAIA v1 sediment class (mutually exclusive with oil/decay): couple GAIA
        # internally to TELEMAC-2D. In-image smoke (2026-07-19) PINNED the wiring:
        # GAIA appends its ONE suspended class as a SECOND t2d tracer, so the T2D
        # deck must (a) OUTPUT it - add T2 to VARIABLES FOR GRAPHIC PRINTOUTS so
        # the suspended concentration lands in r2d_river.slf as 'NCOH SEDIMENT1'
        # [g/l == kg/m3] - and (b) size PRESCRIBED TRACERS VALUES for BOTH tracers
        # x EVERY liquid boundary (1 dye + 1 gaia class; the smoke errored "MORE
        # PRESCRIBED TRACER VALUES ARE REQUIRED" until the count covered both).
        # The dye tracer stays as the REQUIRED hydraulic companion: a gaia-only
        # tracer (no user tracer) trips DEBIMP "SUPERCRITICAL ENTRY WITH FREE
        # DEPTH" on the Q-prescribed inflow (proven), while the same deck WITH the
        # dye tracer solves to CORRECT END OF RUN with the mass balance closing.
        # The postprocess picks the SEDIMENT tracer (not the dye) for the
        # concentration COG and reads CUMUL BED EVOL from gaia_river.slf for the
        # deposition COG. All new gaia cas lines respect the 72-char DAMOCLES clamp
        # (author_gaia_deck clamps its own file; these three t2d lines are short).
        write_gaia_deck(cfg, os.path.basename(slf), os.path.basename(cli),
                        os.path.dirname(os.path.abspath(cas_path)))
        if not bool(getattr(cfg, "erodible_bed", False)):
            # v1 SUPPLY-LIMITED SUSPENSION: GAIA appends its ONE suspended class as
            # a SECOND t2d tracer, so the deck must OUTPUT it (add T2) and size
            # PRESCRIBED TRACERS VALUES for BOTH tracers x every liquid boundary.
            cas = cas.replace(
                "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'",
                "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1,T2'")
            # widen PRESCRIBED TRACERS VALUES to (dye + gaia class) x n_liquid_bounds
            # zeros (clean boundaries - dye + sediment both enter via the point
            # source / gaia source keyword, never the open boundaries).
            n_tr_vals = 2 * max(len(lb_order), 1)
            cas = cas.replace(
                "PRESCRIBED TRACERS VALUES       = " + ";".join(tracer),
                "PRESCRIBED TRACERS VALUES       = " + ";".join(["0."] * n_tr_vals))
        # else: v2 ERODIBLE-BED bedload path appends NO suspended tracer (SUSPENSION
        # FOR ALL SANDS = NO), so the dye stays the sole t2d tracer - the deck's
        # single-tracer graphic printouts + PRESCRIBED TRACERS VALUES are already
        # correct. Only the GAIA coupling lines below are added.
        cas += (
            "/\n"
            "COUPLING WITH                   = 'GAIA'\n"
            f"GAIA STEERING FILE              = {GAIA_STEERING_FILENAME}\n"
        )
        if bool(getattr(cfg, "dredging", False)):
            # NESTOR dredging (ADR 0254): author the action + polygon (+ surface-
            # reference) files and stamp a deterministic time origin so the action-
            # file absolute dates (yyyy.mm.dd-hh:mm:ss) map to sim seconds through
            # NESTOR's DateStringToSeconds (seconds since MARDAT/MARTIM). GAIA
            # inherits MARDAT/MARTIM from TELEMAC-2D in coupled mode.
            write_nestor_decks(cfg, mesh,
                               os.path.dirname(os.path.abspath(cas_path)))
            y, mo, d, hh, mm, ss = NESTOR_TIME_ORIGIN
            cas += (
                f"ORIGINAL DATE OF TIME           = {y};{mo};{d}\n"
                f"ORIGINAL HOUR OF TIME           = {hh};{mm};{ss}\n"
            )

    # DAMOCLES hard 72-char line limit: one over-long line (e.g. a long
    # geocoded reach name in a comment or the TITLE) derails the parser into
    # "KEY-WORD ... IS UNKNOWN" on a LATER, valid line. Live-hit 2026-07-18:
    # 'longview_cowlitz_county_washington_98632_united_' made an 86-char
    # comment + ~80-char TITLE and DAMOCLES blamed 'GEOMETRY FILE' at line 10.
    # Comments are safely sliced; the quoted TITLE is shortened keeping quotes.
    lines = []
    for ln in cas.splitlines():
        if len(ln) <= 72:
            lines.append(ln)
        elif ln.startswith("/"):
            lines.append(ln[:72])
        elif ln.startswith("TITLE"):
            lines.append(f"TITLE : '{cfg.name[:40]} DYE PULSE'"[:72])
        else:
            lines.append(ln)  # data lines are worker-generated and short
    cas = "\n".join(lines) + "\n"
    over = [ln for ln in lines if len(ln) > 72]
    if over:
        LOG.warning("cas lines still >72 chars after clamp: %r", over[:3])
    with open(cas_path, "w") as f:
        f.write(cas)


def map_liquid_boundaries(listing_text, mesh, tr_back=None):
    """Parse the solver listing's LIQUID BOUNDARIES block and map each numbered
    liquid boundary -> 'inflow'/'outflow' by comparing its reported COORDINATES
    to our tagged inflow/outflow cap-node centroids (gotcha 4).

    TELEMAC v9 listing format:
        THERE IS     2 LIQUID BOUNDARIES:
         BOUNDARY    1 :
          BEGINS AT BOUNDARY POINT: ... GLOBAL NUMBER: ...
          AND COORDINATES:     720978.4           4717564.
          ENDS AT ...
    """
    import re

    def centroid(nodes):
        idx = np.array(sorted(nodes))
        return np.array([mesh["X"][idx].mean(), mesh["Y"][idx].mean()])
    c_in = centroid(mesh["in_nodes"])
    c_out = centroid(mesh["out_nodes"])

    # isolate the LIQUID BOUNDARIES section (up to SOLID BOUNDARIES)
    m0 = re.search(r"LIQUID BOUNDARIES", listing_text)
    if not m0:
        return None
    tail = listing_text[m0.end():]
    m1 = re.search(r"SOLID BOUNDARIES", tail)
    block = tail[:m1.start()] if m1 else tail

    order = {}
    # each "BOUNDARY  N :" followed later by first "COORDINATES:  x   y"
    for bm in re.finditer(r"BOUNDARY\s+(\d+)\s*:", block):
        lbnum = int(bm.group(1))
        sub = block[bm.end():]
        cm = re.search(r"COORDINATES:\s*([-\d.Ee+]+)\s+([-\d.Ee+]+)", sub)
        if not cm:
            continue
        p = np.array([float(cm.group(1)), float(cm.group(2))])
        role = "inflow" if np.hypot(*(p - c_in)) < np.hypot(*(p - c_out)) else "outflow"
        order[lbnum] = role
    if order:
        return [order[k] for k in sorted(order)]
    return None


# ---------------------------------------------------------------------------
# 7. Solver
# ---------------------------------------------------------------------------
def run_solver(cas_path, res_path, cwd, timeout=1200):
    if os.path.exists(res_path):
        os.remove(res_path)          # gotcha 6
    log = subprocess.run(
        ["telemac2d.py", os.path.basename(cas_path)],
        cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = log.stdout + "\n" + log.stderr
    ok = "CORRECT END OF RUN" in out
    return ok, out
