"""MODFLOW 6 GWF + GWT deck construction for groundwater solute-transport.

Sprint-13 Stage 1 (MOD), job-0221. Owner: engine.

This module assembles a *complete* MODFLOW 6 simulation deck for a
groundwater-contamination ("spill") scenario via FloPy. A single MF6 binary
(`mf6`, version-pinned 6.5.0 in the solver container - see
`reports/inflight/sprint-13-mod-1-modflow-container-design-20260609/design.md`
section 2) executes *both* model types from one simulation namefile:

  * **GWF** (Groundwater Flow) - steady-state saturated flow. A west→east
    constant-head gradient drives a uniform regional flow field that advects
    the plume. This is the hydraulic head field that the transport model
    reads.
  * **GWT** (Groundwater Transport) - transient advection-dispersion of a
    conservative tracer. A mass-loading source (`SRC` package) injects the
    contaminant at the spill cell; advection (`ADV`) and dispersion (`DSP`)
    spread it; output control (`OC`) saves the concentration array.

The two models are coupled by a GWF-GWT exchange (`GWFGWT`) plus the transport
source-sink mixing package (`SSM`) so the flow field built by GWF drives
transport. Reaction kinetics (sorption, biodegradation) are intentionally
**out of scope for v0.1** - the demo contaminant is a conservative tracer
(design.md section 2).

Determinism boundary (engine invariant 1/2): this is pure deterministic
Python - NO LLM call anywhere in this module. It composes FloPy package
constructors in a fixed, tested sequence and returns a typed deck manifest
whose fields carry every number a downstream tool would narrate.

Contract note: `build_modflow_deck` takes plain keyword arguments whose names
match the `MODFLOWRunArgs` Pydantic contract (authored in parallel by
job-0222 and bound in Stage 2 / job-0227). This module deliberately does NOT
import from `trid3nt_contracts` - the binding happens upstream.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import flopy
import numpy as np
from flopy.utils import CellBudgetFile, HeadFile
from pyproj import CRS, Transformer

# ---------------------------------------------------------------------------
# Demo-scope model constants (design.md section 6 / OQ-MOD-6).
#
# These are v0.1 demo simplifications. A production groundwater model requires
# proper hydrogeologic data (aquifer top/bottom from well logs, anisotropy,
# heterogeneity, recharge). Real models supply these; here the spill scenario
# is parameterised only by (location, contaminant, release_rate, duration,
# aquifer_k, porosity) and the rest is defaulted to geologically reasonable
# demo values. Each default is surfaced explicitly here, not buried.
# ---------------------------------------------------------------------------

DOMAIN_HALF_WIDTH_M = 1000.0  # half the ~2 km square domain
CELL_SIZE_M = 50.0  # 50 m cells -> 40 x 40 structured grid
N_LAYERS = 1  # single layer acceptable for v0.1

# Aquifer geometry (a flat single layer; demo simplification per OQ-MOD-6).
AQUIFER_TOP_M = 0.0  # local datum top of the saturated layer
AQUIFER_THICKNESS_M = 30.0  # saturated thickness -> bottom = top - 30 m
AQUIFER_BOTTOM_M = AQUIFER_TOP_M - AQUIFER_THICKNESS_M

# Regional hydraulic gradient driving west->east flow. 0.002 (2 m/km) is a
# typical shallow-aquifer gradient; over the 2 km domain that is a 4 m head
# drop across the constant-head boundaries.
REGIONAL_GRADIENT = 0.002

# Dispersivity (m). Longitudinal alpha_L scaled to the plume travel length;
# 10 m is a standard intermediate-scale value. Transverse ratios per Gelhar.
LONGITUDINAL_DISPERSIVITY_M = 10.0
TRANSVERSE_HORIZONTAL_RATIO = 0.1
TRANSVERSE_VERTICAL_RATIO = 0.01

# Source concentration handling: the SRC package injects mass directly
# (units: mass/time), so we convert the contract's kg/s into MODFLOW's
# internal mass unit (grams) per day. Reported concentration is then mg/L
# when porosity-scaled pore volumes are in m^3 and mass in g (1 g/m^3 =
# 1 mg/L). The SRC `smassrate` is therefore g/day.
SECONDS_PER_DAY = 86400.0
KG_TO_G = 1000.0

# MODFLOW time unit for this deck: DAYS (TDIS time_units). All rates below are
# therefore expressed per day, and lengths in METERS.
TIME_UNITS = "DAYS"
LENGTH_UNITS = "METERS"

# ---------------------------------------------------------------------------
# River-coupling demo defaults (sprint-17 J9 river-seepage). The RIV package is
# the simplest head-dependent river<->aquifer flux boundary: per reach cell
# (cellid, stage, cond, rbot) with leakage Q = cond*(stage - h) capped at
# cond*(stage - rbot) once the aquifer head drops below the streambed bottom.
# These are v0.1 demo simplifications, narrated as demo values exactly like the
# OQ-3 aquifer K / porosity. A real model samples stage + streambed elevation
# from a DEM and derives conductance from streambed K, length and width.
# ---------------------------------------------------------------------------

#: Per-reach-cell RIV conductance (m^2/day) when the caller supplies none. The
#: spike used a flat 100 m^2/day -> 835 m^3/day leakage over 18 cells; 50 is a
#: conservative default.
DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY = 50.0

#: Water depth (m) above the streambed bottom used to set stage from a sampled
#: (or default) rbot when no explicit stage is supplied: stage = rbot + depth.
DEFAULT_RIVER_STAGE_DEPTH_M = 1.5

#: When no DEM is available the demo streambed bottom (rbot) sits a little above
#: the local aquifer head so the reach is a real head-dependent boundary (not a
#: degenerate no-op). Expressed relative to AQUIFER_TOP_M (local datum).
DEFAULT_RIVER_RBOT_ABOVE_TOP_M = 0.5


# ---------------------------------------------------------------------------
# SFR (streamflow-routing) demo defaults (module wave; stream_depletion
# archetype). SFR6 upgrades the fixed-stage RIV boundary to a routed stream
# network (per-reach stage + Manning discharge + the GWF<->stream exchange), so
# the model answers "how does pumping this well affect the river". These are
# v0.1 demo simplifications narrated as demo values exactly like the OQ-3
# aquifer K / porosity and the J9 RIV streambed defaults. A real run reads
# width/inflow from NHDPlus VAA / NWM / NWIS. All units are METERS + DAYS (the
# deck's TDIS units); the SFR length_conversion=1.0 + time_conversion=86400.0
# make Manning's flow internally consistent (unit_conversion is DEPRECATED
# since mf6 6.4.2).
# ---------------------------------------------------------------------------

#: Demo channel width (m) written to every SFR reach packagedata rwid when the
#: caller supplies no river_width_m. In the 5-15 m demo band for a small river.
DEFAULT_SFR_WIDTH_M = 8.0

#: Demo streambed thickness (m) written to every SFR reach packagedata rbth.
DEFAULT_SFR_BED_THICKNESS_M = 1.0

#: Demo streambed hydraulic conductivity (m/day) written to every reach
#: packagedata rhk (reach<->aquifer leakage). Reuses the DEFAULT_RIVER_CONDUCTANCE
#: lineage (silty streambed ~ 0.5 m/day) so the SFR leakage magnitude tracks the
#: RIV seepage path's demo streambed.
DEFAULT_SFR_STREAMBED_K_M_DAY = 0.5

#: Demo Manning roughness (packagedata man) - a natural-channel value.
DEFAULT_SFR_MANNING_N = 0.035

#: Demo headwater INFLOW (m^3/day) written to the most-upstream reach perioddata
#: when the caller supplies no river_inflow_m3_s. ~0.06 m^3/s of baseflow -> a
#: small perennial stream the near-stream pumping can visibly deplete.
DEFAULT_SFR_INFLOW_M3_DAY = 5000.0

#: Demo streambed-top (rtp) gradient (m/m) for the linear fallback profile when
#: no DEM rbot is supplied. Strictly > 0 so SFR Manning flow does not pool.
DEFAULT_SFR_STREAMBED_GRADIENT = 0.001

#: Demo streambed top of the MOST-UPSTREAM reach relative to AQUIFER_TOP_M. The
#: streambed sits just below the local aquifer datum so the reach stage (rtp +
#: depth) is a real head-dependent boundary near the water table (mirrors the
#: smoke fixture where rtp sat just below the strt head).
DEFAULT_SFR_HEADWATER_RTP_BELOW_TOP_M = 0.5

#: Minimum strictly-positive per-reach streambed gradient (rgrd) after clamping.
#: SFR errors / pools on a zero or negative Manning slope, so downstream rtp
#: differences are floored here.
MIN_SFR_STREAMBED_GRADIENT = 1e-4


# ---------------------------------------------------------------------------
# CSUB (aquifer-system compaction / land subsidence) demo defaults (module
# wave; land_subsidence archetype). CSUB layers onto the EXISTING
# sustainable_yield transient WEL deck: the pumping drawdown pushes the effective
# stress in a compressible fine-grained interbed past its preconsolidation,
# producing PERMANENT (inelastic) aquifer-system compaction -> a ground
# subsidence bowl (cm). v1 = ONE no-delay HEAD_BASED interbed per pumped
# footprint cell; preconsolidation = the initial head (pcs0=0) so any drawdown
# drives inelastic compaction. These are v0.1 demo simplifications narrated as
# demo values (no site clay-fraction fetcher yet -- the subsidence MAGNITUDE is
# set by Ssv * interbed_thickness, so they are narrated honestly, never as site
# precision). All units are m^-1 (specific storage) or dimensionless (fraction).
# Pinned by the local mf6 6.5.0 smoke (fixtures/csub_smoke):
#   * output text tags CSUB-COMPACTION / CSUB-ZDISPLACE (the latter TRUNCATED to
#     16 chars; NOT "CSUB-ZDISPLACEMENT");
#   * subsidence is POSITIVE-DOWN;
#   * mf6 HARD-REQUIRES STO ss == 0 in all active cells when CSUB is present, so
#     the storage double-count fix is engine-enforced (see the STO block below).
# ---------------------------------------------------------------------------

#: Inelastic (virgin) specific storage Ssv of the compressible interbed, m^-1 --
#: the number that SETS the subsidence magnitude. Corcoran-Clay-scale compressible
#: fine-grained interbed (San Joaquin Valley benchmark corridor).
DEFAULT_CSUB_SSV_INELASTIC = 2e-3

#: Elastic (recompression) specific storage Sse of the interbed, m^-1 -- one-to-two
#: orders BELOW Ssv (the elastic/inelastic contrast IS the subsidence physics).
DEFAULT_CSUB_SSE_ELASTIC = 5e-5

#: Coarse-grained ELASTIC skeletal specific storage Ss of the aquifer matrix, m^-1
#: (the CSUB cg_ske_cr). REPLACES the STO ss the plain run used (mf6 forces STO
#: ss==0 under CSUB), so it matches the aquifer elastic Ss -- equals the default
#: DEFAULT_AQUIFER_SS so the CSUB-run head decline matches the plain run.
DEFAULT_CSUB_CG_SKE = 1e-5

#: Interbed thickness as a FRACTION of the model layer thickness (the CSUB
#: packagedata thick with cell_fraction=True). ~0.5 -> the interbed occupies about
#: half the ~30 m demo layer. Ultimate compaction scales via dz = Ssv * b * dh.
DEFAULT_CSUB_INTERBED_THICK_FRAC = 0.5

#: STO specific storage ss written when CSUB is present. mf6 6.5.0 HARD-ERRORS
#: unless STO ss == 0 in all active cells with CSUB (CSUB owns skeletal storage
#: via cg_ske_cr) -- the engine-enforced double-count guard. Pinned in the smoke.
CSUB_STO_SS_FLOOR = 0.0


# ---------------------------------------------------------------------------
# Archetype demo defaults (sprint-18 Wave-1). The three new MODFLOW archetypes
# (sustainable_yield / mine_dewatering / regional_water_budget) reuse the same
# 40x40x50 m UTM grid + west->east REGIONAL_GRADIENT CHD as the spill/seepage
# deck; only the stress packages + temporal mode differ. Each default is a v0.1
# demo simplification, narrated as a demo value exactly like the OQ-3 aquifer K /
# porosity and the J9 RIV streambed defaults. A real model supplies these.
# ---------------------------------------------------------------------------

#: Specific yield (drainable porosity) for the GwfSto transient storage term when
#: the caller supplies none. Sandy-aquifer demo value.
DEFAULT_AQUIFER_SY = 0.2

#: Specific storage (1/m) for the GwfSto transient storage term. Confined-aquifer
#: demo value.
DEFAULT_AQUIFER_SS = 1e-5

#: Number of transient stress periods for a transient archetype when neither
#: ``sim_years`` nor ``n_periods`` is supplied (e.g. four seasonal periods).
DEFAULT_N_TRANSIENT_PERIODS = 4

#: Length (days) of each transient stress period when derived from a period count
#: rather than ``sim_years`` (a 90-day season per period -> ~1 year for 4).
DEFAULT_TRANSIENT_PERIOD_DAYS = 90.0

#: Time steps per transient stress period (sub-stepping for a stable transient
#: solve + a few saved frames per period).
DEFAULT_STEPS_PER_TRANSIENT_PERIOD = 10

#: Per-cell DRN conductance (m^2/day) for the mine-dewatering pit drain ring when
#: the caller supplies none. High enough that the drain holds the pit head near
#: the drain elevation (the dewatered target).
DEFAULT_DRAIN_CONDUCTANCE_M2_DAY = 100.0

#: When the caller gives no drain elevation, dewater the pit to this depth below
#: the local aquifer datum (AQUIFER_TOP_M) so the DRN actively removes water.
DEFAULT_DRAIN_DEPTH_BELOW_TOP_M = 10.0


# ---------------------------------------------------------------------------
# Wave-2 archetype demo defaults (sprint-18 Wave-2). The three new MODFLOW
# archetypes (MAR / ASR / wetland_hydroperiod) reuse the SAME 40x40x50 m UTM grid
# + west->east REGIONAL_GRADIENT CHD as the Wave-1 decks; only the stress packages
# (RCH / seasonal WEL / RCH+EVT) + temporal cycling differ. Each default is a v0.1
# demo simplification, narrated as a demo value exactly like the OQ-3 aquifer K /
# porosity and the Wave-1 STO/DRN defaults. A real model supplies these.
# ---------------------------------------------------------------------------

#: Days per "month" for the MAR/ASR seasonal cadence (``recharge_months`` /
#: ``injection_months`` / ``recovery_months`` -> period lengths in days). A flat
#: 30-day month keeps the seasonal schedule deterministic and easy to narrate.
DEFAULT_DAYS_PER_MONTH = 30.0

#: Specific yield for the MAR mounding water-table response when the caller gives
#: none (an unconfined NPF icelltype=1 + STO sy controls how high the mound rises).
DEFAULT_MAR_SY = 0.2

#: Wetland-soil specific yield for the wetland_hydroperiod unconfined seasonal
#: water-table response when the caller supplies none. Mirrors the contract's
#: ``DEFAULT_WETLAND_SY`` (kept local: this module does NOT import trid3nt_contracts).
DEFAULT_WETLAND_SY = 0.2

#: Infiltration-basin recharge rate (m/day) for the MAR archetype when the caller
#: supplies none. A managed spreading basin adds ~0.01 m/day of net recharge to the
#: water table over the basin footprint -- enough to raise a several-metre mound
#: over the demo's 30 m aquifer (K ~ 8.6 m/day) without flooding the cells. A higher
#: caller-supplied rate mounds proportionally higher.
DEFAULT_MAR_INFILTRATION_M_DAY = 0.01

#: Number of recharge "months" the MAR basin floods when neither ``recharge_months``
#: nor ``n_periods`` is supplied (one flooding season).
DEFAULT_MAR_RECHARGE_MONTHS = 4

#: ASR injection / recovery "months" per half-cycle when the caller supplies none
#: (inject through the wet half-year, recover through the dry half-year).
DEFAULT_ASR_INJECTION_MONTHS = 3
DEFAULT_ASR_RECOVERY_MONTHS = 3

#: Number of ASR inject/recover cycles when the caller supplies none (one full
#: seasonal cycle = inject then recover).
DEFAULT_ASR_N_CYCLES = 1

#: ASR injection / recovery rates (m^3/day, POSITIVE magnitudes) when the caller
#: supplies none. The adapter applies the MF6 WEL sign (inject = +, recover = -).
DEFAULT_ASR_INJECTION_RATE_M3_DAY = 1000.0
DEFAULT_ASR_RECOVERY_RATE_M3_DAY = 1000.0

#: Wetland-hydroperiod EVT defaults when the caller supplies none. The ET surface
#: sits at the local aquifer datum (AQUIFER_TOP_M); ET draws water down at a peak
#: rate that decays linearly to zero at the extinction depth below the surface.
DEFAULT_WETLAND_ET_MAX_RATE_M_DAY = 0.004  # ~1.5 m/yr peak PET, demo value
DEFAULT_WETLAND_ET_EXTINCTION_DEPTH_M = 2.0  # ET ceases ~2 m below the wetland surface

#: Per-period wetland recharge schedule (m/day) when the caller supplies none. A
#: simple wet/dry seasonal alternation over ``DEFAULT_N_TRANSIENT_PERIODS`` periods
#: so the seasonal head range (the hydroperiod) is non-degenerate.
DEFAULT_WETLAND_RECHARGE_WET_M_DAY = 0.003
DEFAULT_WETLAND_RECHARGE_DRY_M_DAY = 0.0005


# ---------------------------------------------------------------------------
# Wave-4 PRT capture-zone defaults (sprint-18 Wave-4). Both capture_zone and
# wellhead_protection archetypes run a steady GWF at LOCAL (0,0) origin, then a
# separate PRT sim reading the REVERSED GWF output (the canonical MF6 example
# ex-prt-mp7-p02 backward-tracking approach). The grid is the SAME 40x40x50 m
# UTM grid as all other archetypes; only the NPF save flags + the PRT block differ.
# ---------------------------------------------------------------------------

#: PRT capture-zone domain: larger than the spill 2 km domain so particle
#: pathlines have room to reach the recharge boundary. The grid tracks back to
#: a CHD west-inflow boundary at a realistic travel-time scale.
PRT_DOMAIN_HALF_WIDTH_M = 2050.0  # 41 cells of 100 m -> 4100 x 4100 m domain
PRT_CELL_SIZE_M = 100.0           # 100 m cells match the proven script

#: Aquifer geometry for the PRT grid. Single confined layer (flat datum, local).
PRT_AQUIFER_TOP_M = 50.0    # match the proven script (top = 50 m, bottom = 0 m)
PRT_AQUIFER_BOTTOM_M = 0.0

#: Default particle release ring radius (m). Must be inside the well cell so
#: every release point maps to the well cell. 30 % of PRT_CELL_SIZE_M = 30 m.
DEFAULT_PRT_RING_RADIUS_M = 0.30 * PRT_CELL_SIZE_M

#: PRT aquifer porosity when the caller does not supply one (controls travel time).
DEFAULT_PRT_POROSITY = 0.25

#: Default extraction rate (m^3/day) for the PRT well when the caller does not
#: supply ``pumping_rate_m3_day``. A moderate municipal supply well.
DEFAULT_PRT_PUMPING_RATE_M3_DAY = 800.0

#: Default travel-time isochrone cutoffs (years) when the caller does not supply
#: ``capture_zone_travel_time_years``.  ``capture_zone`` is the general
#: zone-of-contribution ([1, 5, 10] yr); ``wellhead_protection`` uses the EPA WHPA
#: fixed-travel-time tiers ([2, 5, 10] yr -- the 2-year IMMEDIATE zone).  The
#: composer normally threads explicit tiers, so these defaults only fire on a
#: direct adapter call with no tiers.
DEFAULT_CAPTURE_ZONE_TRAVEL_TIME_YEARS: list[float] = [1.0, 5.0, 10.0]
DEFAULT_WELLHEAD_PROTECTION_TRAVEL_TIME_YEARS: list[float] = [2.0, 5.0, 10.0]


def _default_travel_time_years(archetype: str) -> list[float]:
    """Archetype-specific default isochrone tiers (years) when none supplied."""
    if archetype == "wellhead_protection":
        return list(DEFAULT_WELLHEAD_PROTECTION_TRAVEL_TIME_YEARS)
    return list(DEFAULT_CAPTURE_ZONE_TRAVEL_TIME_YEARS)

#: Default PRT max tracking time (years). Particles are tracked until they hit a
#: boundary or this time cap. 50 years is long enough for a 2 km domain at
#: typical aquifer velocities (K ~ 10 m/day, gradient ~ 0.002, ne ~ 0.25 ->
#: seepage velocity ~0.08 m/day -> 2000 m / 0.08 m/day ~ 25000 days ~ 68 years).
DEFAULT_PRT_MAX_TRACKING_YEARS = 75.0


# ---------------------------------------------------------------------------
# Wave-5 saltwater_intrusion defaults (sprint-18 Wave-5). The Henry-style
# FIELD-SCALE coastal transect uses a vertical nrow=1 slice: ~1 km transect
# length (ncol * delr), ~50 m saturated aquifer depth (nlay * delv). Salinity
# is transported in PPT (0 = fresh, 35 = seawater). Density EOS via BUY:
#   drhodc = (1025 - 1000) / (35 - 0) = 0.714 kg/m3 per ppt.
# The seaward boundary is GHB+AUX (constant salt-water head) and the inland
# boundary is WEL+AUX (freshwater inflow) -- the two-IMS, GWF-first pattern
# from the proven henry_buy_proof.py script.
# ---------------------------------------------------------------------------

#: Horizontal cell width (m) along the transect. 100 columns * 10 m = 1 km.
DEFAULT_SI_DELR_M = 10.0

#: Number of horizontal columns across the transect. 100 * 10 m = 1 km domain.
DEFAULT_SI_NCOL = 100

#: Vertical cell height (m) per layer. 20 layers * 2.5 m = 50 m aquifer depth.
DEFAULT_SI_DELV_M = 2.5

#: Number of vertical layers in the Henry slice (caller-adjustable via
#: ``n_vertical_layers``). Default 20 gives a 50 m aquifer at 2.5 m per cell.
DEFAULT_SI_NLAY = 20

#: Slice thickness (m) in the nrow=1 direction (unit-width 2D cross-section).
DEFAULT_SI_DELC_M = 1.0

#: Aquifer top elevation (m; sea-level datum). The seaward GHB holds head at
#: this level; the inland WEL drives fresh water in at the same elevation.
DEFAULT_SI_TOP_M = 0.0

#: Seawater salinity (ppt) for the GHB+AUX boundary and the GWT IC (starts
#: fully salty, fresh inflow displaces to equilibrium -- Henry convention).
DEFAULT_SI_CSALT_PPT = 35.0

#: Freshwater salinity (ppt). Used for the inland WEL+AUX boundary and the
#: BUY crhoref (density reference concentration) so fresh water has density
#: equal to denseref (1000 kg/m3).
DEFAULT_SI_CFRESH_PPT = 0.0

#: Reference fresh-water density (kg/m3) for the BUY EOS.
DEFAULT_SI_DENSEREF = 1000.0

#: Seawater density at csalt (kg/m3) for the BUY EOS.
DEFAULT_SI_DENSESALT = 1025.0

#: BUY drhodc (kg/m3 per ppt). Derived from (densesalt - denseref) / csalt.
DEFAULT_SI_DRHODC = (DEFAULT_SI_DENSESALT - DEFAULT_SI_DENSEREF) / DEFAULT_SI_CSALT_PPT

#: Hydraulic conductivity (m/day) for the saltwater-intrusion aquifer. Sandy
#: coastal aquifer demo default (8.64 m/day ~ 1e-4 m/s). The caller can
#: override by supplying ``aquifer_k_ms``.
DEFAULT_SI_K_M_DAY = 8.64

#: Porosity for the saltwater-intrusion aquifer (sand demo value).
DEFAULT_SI_POROSITY = 0.35

#: Molecular diffusion coefficient (m2/day) for salinity transport. Henry
#: canonical value: 0.57024 m2/day (6.6e-6 m2/s * 86400 s/day).
DEFAULT_SI_DIFFC_M2_DAY = 0.57024

#: Total freshwater inflow (m3/day) through the inland WEL boundary when the
#: caller does not supply ``freshwater_inflow_m3_day``. 5.7024 m3/day is the
#: Henry Case-A benchmark (matches the diffusion flux at the 1 m-wide slice).
DEFAULT_SI_INFLOW_M3_DAY = 5.7024

#: Transient simulation length (days). One period of nsteps time steps ramping
#: the system to a quasi-steady saltwater wedge (Henry: 0.5 day x 500 steps).
#: For the field-scale (1 km) domain we run longer to equilibrate.
DEFAULT_SI_PERIOD_DAYS = 250.0

#: Number of time steps within the single transient period. 500 sub-steps
#: resolves the sharp wedge front while keeping the run fast on small grids.
DEFAULT_SI_NSTEPS = 500


@dataclass
class DeckManifest:
    """Typed description of a written MODFLOW 6 deck.

    Every field carries a number a downstream tool (postprocess /
    `run_modflow_job`, job-0227) reads - never prose. `model_crs` (an EPSG
    code, e.g. "EPSG:32617") is the key OQ-MOD-3 field the postprocess step
    needs to reproject the concentration COG back to EPSG:4326.
    """

    sim_dir: str
    sim_name: str
    gwf_name: str
    gwt_name: str
    model_crs: str  # e.g. "EPSG:32617" - projected metric CRS of the grid
    # Grid georegistration (so postprocess can build the affine transform):
    xorigin: float  # projected easting of grid lower-left corner (m)
    yorigin: float  # projected northing of grid lower-left corner (m)
    nrow: int
    ncol: int
    nlay: int
    delr: float  # column width (m)
    delc: float  # row height (m)
    # Spill cell (0-based grid indices) and its projected coordinates:
    spill_row: int
    spill_col: int
    spill_easting_m: float
    spill_northing_m: float
    spill_lat: float
    spill_lon: float
    # Source loading actually written into the SRC package:
    mass_rate_g_per_day: float
    release_rate_kg_s: float
    duration_days: float
    n_transport_steps: int
    contaminant: str
    aquifer_k_ms: float
    porosity: float
    # River-coupling (sprint-17 J9; all default to the no-river spill deck):
    river_coupled: bool = False  # True iff a RIV package was written
    river_cell_count: int = 0  # number of RIV reach cells draped onto the grid
    river_reach_len_m: float = 0.0  # cumulative reach length over the in-grid cells
    river_conductance_m2_day: float = 0.0  # per-cell RIV conductance written
    along_river_source: bool = False  # True iff the SRC was placed along the reach
    # --- Archetype branch (sprint-18 Wave-1; ADDITIVE, default = spill/seepage) -
    # ``archetype is None`` is the EXISTING spill/seepage GWF+GWT deck; the three
    # new archetypes are GWF-only (no GWT block, no GWFGWT exchange). Every field
    # below stays at its default for the spill/seepage path (byte-identical deck).
    archetype: str | None = None  # None | sustainable_yield | mine_dewatering | regional_water_budget
    gwt_present: bool = True  # True iff a GWT (transport) model was written
    transient: bool = False  # True iff a transient TDIS + GwfSto were written
    n_stress_periods: int = 2  # TDIS period count (spill/seepage = 2: steady + transient)
    n_transient_periods: int = 1  # transient (non-spin-up) period count
    # sustainable_yield (WEL pumping-well drawdown):
    well_row: int = -1  # 0-based grid row of the pumping well (-1 = no well)
    well_col: int = -1  # 0-based grid col of the pumping well (-1 = no well)
    well_easting_m: float = 0.0  # projected easting of the well cell centre (m)
    well_northing_m: float = 0.0  # projected northing of the well cell centre (m)
    well_lat: float = 0.0  # well latitude (EPSG:4326)
    well_lon: float = 0.0  # well longitude (EPSG:4326)
    pumping_rate_m3_day: float = 0.0  # WEL discharge written (negative = extraction)
    aquifer_sy: float = 0.0  # GwfSto specific yield written (0.0 = no STO)
    aquifer_ss: float = 0.0  # GwfSto specific storage written (1/m)
    # mine_dewatering (DRN pit dewatering):
    drain_cell_count: int = 0  # number of DRN drain cells draped over the pit
    drain_elevation_m: float = 0.0  # DRN drain elevation written (deck datum m)
    drain_conductance_m2_day: float = 0.0  # per-cell DRN conductance written
    npf_icelltype: int = 0  # NPF icelltype (0 = confined; 1 = unconfined water table)
    # regional_water_budget (zonal CBC partition):
    zone_partition: str | None = None  # zone-split scheme written (None = whole-domain)
    n_zones: int = 0  # number of zones in the written ZONE array (0 = none)
    # --- Wave-2 archetypes (sprint-18 Wave-2; ADDITIVE) --------------------------
    # MAR (RCH groundwater mounding) -- recharge basin draped over the grid.
    recharge_cell_count: int = 0  # number of RCH cells (basin footprint cells)
    infiltration_rate_m_day: float = 0.0  # POSITIVE recharge flux written (m/day)
    recharge_active_periods: int = 0  # transient periods over which RCH is active
    # ASR (seasonal WEL inject/recover) -- the sign-flipping schedule scalars.
    injection_rate_m3_day: float = 0.0  # POSITIVE injection magnitude written
    recovery_rate_m3_day: float = 0.0  # POSITIVE recovery magnitude written
    n_cycles: int = 0  # number of inject/recover cycles written
    injection_periods: int = 0  # count of positive-q (injection) stress periods
    recovery_periods: int = 0  # count of negative-q (recovery) stress periods
    # wetland_hydroperiod (RCH-schedule + EVT seasonal water-table range).
    wetland_cell_count: int = 0  # number of RCH/EVT cells (wetland footprint cells)
    et_surface_m: float = 0.0  # EVT surface elevation written (deck datum m)
    et_max_rate_m_day: float = 0.0  # EVT max ET rate written (m/day)
    et_extinction_depth_m: float = 0.0  # EVT extinction depth written (m)
    newton_under_relaxation: bool = False  # True iff IMS used NEWTON + BICGSTAB
    # --- multi_species transport (sprint-18 Wave-3; ADDITIVE) -------------------- #
    # ``archetype == "multi_species"``: ONE shared GWF + N ModflowGwt models (one per
    # solute species) + N ModflowGwfgwt flow<->transport exchanges, all in ONE
    # simulation / ONE mf6 run. Every field below stays at its default for the
    # single-species spill path (byte-identical deck).
    multi_species: bool = False  # True iff a multi_species (N-GWT) deck was written
    species_names: list[str] = field(default_factory=list)  # ordered species names
    species_ucn_files: list[str] = field(default_factory=list)  # per-species .ucn (deck order)
    gwt_model_names: list[str] = field(default_factory=list)  # per-species GWT model names
    n_gwfgwt_exchanges: int = 0  # number of ModflowGwfgwt flow<->transport exchanges
    n_gwtgwt_exchanges: int = 0  # number of ModflowGwtGwt species-coupling exchanges (decay chain)
    species_with_parent: list[str] = field(default_factory=list)  # daughter species (parent set)
    decay_chain_coupled: bool = False  # True iff any parent->daughter GwtGwt exchange was written
    # --- Wave-4 PRT capture-zone (sprint-18 Wave-4; ADDITIVE) -------------------- #
    # ``archetype in ('capture_zone', 'wellhead_protection')``: TWO separate sims --
    # a steady GWF built at LOCAL (0,0) origin + a PRT sim that reads the REVERSED
    # GWF output and forward-tracks a ring of particles released at the well
    # (backward capture-zone delineation). Every field below stays at its default for
    # all other archetypes (byte-identical manifests for the existing 7 archetypes).
    prt_present: bool = False  # True iff a PRT sim was written alongside the GWF
    # PRT grid is built at local (0,0); the true UTM origin is stored separately so
    # postprocess can translate the particle pathlines back to real coordinates.
    xoffset_m: float = 0.0     # true UTM easting of the PRT grid lower-left corner (m)
    yoffset_m: float = 0.0     # true UTM northing of the PRT grid lower-left corner (m)
    model_utm_epsg: int = 0    # integer EPSG code of the model UTM CRS (e.g. 32617)
    # Well cell (PRT grid, 0-based row/col). Re-uses the existing well_row/well_col
    # fields from the sustainable_yield archetype; they default to -1 (no well).
    # Well easting/northing/lat/lon are the REAL coordinates (NOT local-origin).
    n_particles: int = 0       # number of particles in the PRT release ring
    capture_zone_travel_time_years: list[float] = field(default_factory=list)
    # --- Wave-5 saltwater_intrusion (sprint-18 Wave-5; ADDITIVE) -------------------- #
    # ``archetype == "saltwater_intrusion"``: GWF (BUY variable-density) + GWT in ONE
    # sim, using a vertical nrow=1 slice (Henry geometry) with seaward GHB+AUX (salt)
    # and inland WEL+AUX (fresh). Salinity transported in PPT (0..csalt). Density EOS
    # via BUY: drhodc = 0.714 kg/m3 per ppt (at denseref=1000, densesalt=1025, csalt=35
    # ppt). Every field below stays at its default for all other archetypes.
    saltwater_intrusion: bool = False   # True iff a BUY variable-density deck was written
    # Vertical-slice grid geometry (overrides the plan-view nlay/nrow/ncol/delr/delc):
    si_nlay: int = 0            # number of vertical layers in the Henry slice
    si_ncol: int = 0            # number of horizontal columns across the transect
    si_delr: float = 0.0       # horizontal cell width (m) along the transect
    si_delv: float = 0.0       # vertical cell height (m) per layer
    sea_level_top: float = 0.0  # top of the aquifer in deck units (m; 0 = sea level)
    # Transect endpoints in EPSG:4326 (A = seaward, B = inland):
    transect_lat_a: float = 0.0   # latitude of the seaward endpoint (A)
    transect_lon_a: float = 0.0   # longitude of the seaward endpoint (A)
    transect_lat_b: float = 0.0   # latitude of the inland endpoint (B)
    transect_lon_b: float = 0.0   # longitude of the inland endpoint (B)
    seawater_salinity_ppt: float = 0.0   # GHB+AUX boundary salinity (ppt); also IC strt
    # Headline scalar written to the manifest after the real run (0.0 before run):
    intrusion_length_m: float = 0.0  # bottom-layer 50%-isochlor toe penetration (m)
    # --- module wave: stream_depletion SFR routed river<->aquifer exchange ------ #
    # ``archetype == "stream_depletion"``: a transient GWF (WEL well) + a routed
    # MODFLOW-6 SFR6 stream network draped from the fetched flowline. The SFR
    # package forces an asymmetric matrix so the IMS flips to BICGSTAB (recorded
    # via ``sfr_present`` / ``newton_under_relaxation``). Every field below stays
    # at its default for all other archetypes (byte-identical manifests).
    sfr_present: bool = False   # True iff a ModflowGwfsfr package was written
    n_reaches: int = 0          # number of SFR reaches draped onto the grid
    # Ordered reach cell echo for postprocess georegistration: one
    # ``[ifno, row, col, reach_len_m]`` per reach in path (headwater->outlet) order.
    sfr_reach_cells: list[list[float]] = field(default_factory=list)
    sfr_inflow_m3_day: float = 0.0   # headwater INFLOW written to reach 0 (m^3/day)
    sfr_width_m: float = 0.0         # channel width written to every reach (m)
    sfr_streambed_k_m_day: float = 0.0  # streambed K written to every reach (m/day)
    sfr_manning_n: float = 0.0       # Manning roughness written to every reach
    # --- module wave: land_subsidence CSUB aquifer-system compaction ----------- #
    # ``archetype == "land_subsidence"``: a transient GWF (WEL well) + a CSUB
    # package (ONE no-delay HEAD_BASED interbed per pumped footprint cell). CSUB
    # owns the coarse skeletal storage, so the STO ss is dropped to 0 (mf6-enforced
    # double-count guard). Every field below stays at its default for all other
    # archetypes (byte-identical manifests).
    csub_present: bool = False   # True iff a ModflowGwfcsub package was written
    n_interbeds: int = 0         # number of CSUB interbeds over the pumped footprint
    # Ordered interbed cell echo for postprocess georegistration: one
    # ``[icsubno, row, col]`` per interbed in footprint (row-major) order.
    csub_interbed_cells: list[list[float]] = field(default_factory=list)
    csub_ssv_inelastic_m: float = 0.0  # inelastic Ssv written to every interbed (m^-1)
    csub_sse_elastic_m: float = 0.0    # elastic Sse written to every interbed (m^-1)
    csub_interbed_thick_frac: float = 0.0  # interbed thickness fraction of the layer
    # Files written (relative to sim_dir), for manifest/upload assembly:
    files: list[str] = field(default_factory=list)

    def total_released_mass_kg(self) -> float:
        """Plausibility yardstick: release_rate_kg_s x duration in seconds."""
        return self.release_rate_kg_s * self.duration_days * SECONDS_PER_DAY


def _utm_crs_for_lonlat(lon: float, lat: float) -> CRS:
    """Pick the WGS84/UTM zone whose central meridian best fits the point.

    A projected metric CRS is mandatory: SFINCS and MODFLOW transport both run
    on a metric grid (engine domain discipline: "SFINCS runs in a projected
    (metric) CRS"). UTM keeps distortion sub-metre over a 2 km domain, far
    better than a single global projection.
    """
    zone = int(math.floor((lon + 180.0) / 6.0) % 60) + 1
    # EPSG 326xx = northern hemisphere, 327xx = southern.
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


# ---------------------------------------------------------------------------
# River-draping geometry (PURE - no flopy, no network; unit-testable on a
# synthetic grid + river). The river polyline is projected to the deck's UTM
# grid, then rasterized into the set of (row, col) cells it traverses, with the
# in-cell reach length per cell so conductance can scale with reach length.
# ---------------------------------------------------------------------------


def _easting_northing_to_cell(
    east: float,
    north: float,
    *,
    xorigin: float,
    yorigin: float,
    delr: float,
    delc: float,
    nrow: int,
    ncol: int,
) -> tuple[int, int] | None:
    """Map a projected (easting, northing) to a (row, col) grid index.

    Returns None when the point is outside the grid. Row 0 is the NORTH row
    (flopy convention: yorigin is the lower-left corner, row 0 is northernmost),
    so the row offset is measured from the grid TOP (yorigin + nrow*delc) down.
    """
    col = int((east - xorigin) // delr)
    north_top = yorigin + nrow * delc
    row = int((north_top - north) // delc)
    if row < 0 or row >= nrow or col < 0 or col >= ncol:
        return None
    return (row, col)


def _drape_polyline_onto_grid(
    vertices_en: list[tuple[float, float]],
    *,
    xorigin: float,
    yorigin: float,
    delr: float,
    delc: float,
    nrow: int,
    ncol: int,
) -> list[tuple[int, int, float]]:
    """Rasterize a projected polyline into the grid cells it traverses.

    Args:
        vertices_en: the polyline vertices as projected ``(easting, northing)``
            tuples (metres, in the deck's UTM CRS) in path order.

    Returns:
        A list of ``(row, col, reach_len_m)`` per UNIQUE cell, in first-touch
        order, where ``reach_len_m`` is the cumulative length of the polyline
        that falls inside that cell. Cells outside the grid are dropped. The
        per-cell length lets the RIV conductance scale with the reach length the
        cell carries (C = K_bed * L * W / M).

    The algorithm walks each segment in small sub-steps (a fraction of the cell
    size) and accumulates sub-step length into the cell the sub-step midpoint
    falls in. This is robust to diagonal reaches and segments shorter than a
    cell, and is fully deterministic.
    """
    if len(vertices_en) < 2:
        # A single vertex: assign it to its cell with a nominal half-cell length.
        cells: "dict[tuple[int, int], float]" = {}
        order: list[tuple[int, int]] = []
        if vertices_en:
            cell = _easting_northing_to_cell(
                vertices_en[0][0],
                vertices_en[0][1],
                xorigin=xorigin,
                yorigin=yorigin,
                delr=delr,
                delc=delc,
                nrow=nrow,
                ncol=ncol,
            )
            if cell is not None:
                cells[cell] = 0.5 * (delr + delc) / 2.0
                order.append(cell)
        return [(r, c, cells[(r, c)]) for (r, c) in order]

    # Sub-step length: a quarter of the smaller cell dimension so even diagonal
    # crossings sample each cell at least twice.
    step = min(delr, delc) / 4.0
    cells = {}
    order = []

    def _touch(row: int, col: int, length: float) -> None:
        key = (row, col)
        if key not in cells:
            cells[key] = 0.0
            order.append(key)
        cells[key] += length

    for (e0, n0), (e1, n1) in zip(vertices_en[:-1], vertices_en[1:]):
        seg_len = math.hypot(e1 - e0, n1 - n0)
        if seg_len <= 0.0:
            continue
        n_sub = max(1, int(math.ceil(seg_len / step)))
        sub_len = seg_len / n_sub
        for i in range(n_sub):
            # midpoint of sub-step i
            t = (i + 0.5) / n_sub
            em = e0 + t * (e1 - e0)
            nm = n0 + t * (n1 - n0)
            cell = _easting_northing_to_cell(
                em,
                nm,
                xorigin=xorigin,
                yorigin=yorigin,
                delr=delr,
                delc=delc,
                nrow=nrow,
                ncol=ncol,
            )
            if cell is not None:
                _touch(cell[0], cell[1], sub_len)

    return [(r, c, cells[(r, c)]) for (r, c) in order]


def build_riv_records(
    river_cells: list[tuple[int, int, float]],
    *,
    conductance_m2_day: float,
    stage_fn,
    rbot_fn,
    chd_cols: tuple[int, int] | None = None,
    ncol: int = 0,
) -> list[list]:
    """Build the MF6 RIV stress-period records from draped river cells.

    Each record is ``[(lay, row, col), stage, cond, rbot]`` (layer 0). Cells
    that fall on a CHD boundary column (the west/east constant-head columns) are
    SKIPPED - a cell cannot be both a constant-head boundary and a RIV boundary
    in this single-layer demo (the spike skips boundary columns for the same
    reason).

    Args:
        river_cells: ``(row, col, reach_len_m)`` per cell from
            ``_drape_polyline_onto_grid``.
        conductance_m2_day: the per-cell RIV conductance to write.
        stage_fn: ``(row, col) -> stage_m`` callable (deck datum metres).
        rbot_fn: ``(row, col) -> rbot_m`` callable (deck datum metres).
        chd_cols: ``(west_col, east_col)`` boundary columns to skip, or None.
        ncol: grid column count (for the default west/east boundary skip when
            chd_cols is None).

    Returns:
        The list of RIV records. Stage is clamped to be strictly above rbot so
        every written reach cell is a real head-dependent boundary.
    """
    if chd_cols is None and ncol > 0:
        chd_cols = (0, ncol - 1)
    skip = set(chd_cols) if chd_cols is not None else set()
    records: list[list] = []
    for (row, col, _len) in river_cells:
        if col in skip:
            continue
        rbot = float(rbot_fn(row, col))
        stage = float(stage_fn(row, col))
        if stage <= rbot:
            stage = rbot + DEFAULT_RIVER_STAGE_DEPTH_M
        records.append([(0, row, col), stage, float(conductance_m2_day), rbot])
    return records


def _smooth_monotonic_rtp(rtp_raw: list[float]) -> list[float]:
    """Force a streambed-top profile monotonically NON-INCREASING downstream.

    SFR requires the streambed to descend (or stay level) from headwater to
    outlet so Manning routing has a valid slope. A DEM-sampled rtp can wobble
    upward (noise, a culvert, a mis-snapped cell); this walks the profile in
    path order and clamps each reach top to at most its upstream neighbour minus
    a tiny epsilon so the slope is strictly resolvable. The FIRST (headwater)
    value is kept as-is. Pure arithmetic - no flopy, no mf6.
    """
    if not rtp_raw:
        return []
    out = [float(rtp_raw[0])]
    eps = MIN_SFR_STREAMBED_GRADIENT  # a hair of drop even on flat inputs
    for v in rtp_raw[1:]:
        prev = out[-1]
        out.append(min(float(v), prev - eps))
    return out


def _build_sfr_reaches(
    river_cells: list[tuple[int, int, float]],
    *,
    rwid: float,
    rhk: float,
    man: float,
    rbth: float,
    inflow_m3_day: float,
    n_stress_periods: int,
    rtp_by_cell: dict[tuple[int, int], float] | None = None,
) -> dict[str, Any]:
    """Turn path-ordered draped cells into MF6 SFR6 deck inputs.

    ``river_cells`` is the ``_drape_polyline_onto_grid`` output: ``(row, col,
    reach_len_m)`` per UNIQUE cell in path (headwater->outlet) order - exactly the
    ordered-reach input SFR needs. Reach ``ifno`` is the path index; the
    connectivity is the simple chain ``[i, +(i-1), -(i+1)]`` (upstream positive,
    downstream negative) MF6 SFR uses for a single stem (no diversions v0.1).

    packagedata columns (mf6 SFR6):
        ``(ifno, cellid, rlen, rwid, rgrd, rtp, rbth, rhk, man, ncon, ustrf, ndv)``
      * rlen  = the per-cell reach length from the draping (metres);
      * rwid  = demo channel width;
      * rgrd  = the strictly-positive downstream streambed gradient, derived from
                the smoothed rtp differences and floored at MIN_SFR_STREAMBED_GRADIENT;
      * rtp   = the streambed top, monotonic non-increasing downstream (from the
                DEM ``rtp_by_cell`` when supplied, else a linear demo profile);
      * rbth  = demo streambed thickness; rhk = demo streambed K; man = Manning n;
      * ncon  = 1 at the two ends, 2 in the interior; ustrf = 1.0; ndv = 0.

    perioddata puts the headwater INFLOW on reach 0 in EVERY stress period (the
    baseline steady period AND the pumped transient periods carry the same
    inflow - the pumping delta, not the inflow, is the depletion signal).

    Returns a dict with ``packagedata`` / ``connectiondata`` / ``perioddata`` /
    ``obs`` (the continuous-observation mapping registering per-reach stage,
    downstream-flow and the sfr GWF-exchange term to ``<gwf>.sfr.obs.csv``) plus
    ``reach_meta`` (the ordered ``(ifno, row, col, reach_len_m)`` list echoed onto
    the manifest for postprocess georegistration) and ``n_reaches``. Pure Python
    (no flopy) so the geometry is unit-testable without the mf6 binary.
    """
    n = len(river_cells)
    if n < 1:
        raise ValueError("SFR requires >= 1 draped reach cell (empty river polyline?)")

    # --- streambed top (rtp): DEM-sampled else a linear demo profile --------- #
    if rtp_by_cell:
        rtp_raw = [
            float(rtp_by_cell.get((row, col), AQUIFER_TOP_M
                  - DEFAULT_SFR_HEADWATER_RTP_BELOW_TOP_M
                  - i * DEFAULT_SFR_STREAMBED_GRADIENT * max(rlen, 1.0)))
            for i, (row, col, rlen) in enumerate(river_cells)
        ]
    else:
        # Linear demo profile descending from the headwater at the demo gradient.
        rtp_raw = [
            AQUIFER_TOP_M
            - DEFAULT_SFR_HEADWATER_RTP_BELOW_TOP_M
            - i * DEFAULT_SFR_STREAMBED_GRADIENT * max(rlen, 1.0)
            for i, (_row, _col, rlen) in enumerate(river_cells)
        ]
    rtp = _smooth_monotonic_rtp(rtp_raw)

    # --- per-reach downstream gradient (rgrd), strictly > 0 ------------------- #
    # rgrd[i] = (rtp[i] - rtp[i+1]) / rlen[i], clamped > 0; the LAST reach reuses
    # the previous gradient (no downstream neighbour to difference against).
    rgrd: list[float] = []
    for i in range(n):
        rlen_i = max(float(river_cells[i][2]), 1.0)
        if i < n - 1:
            drop = rtp[i] - rtp[i + 1]
            g = drop / rlen_i
        else:
            g = rgrd[-1] if rgrd else DEFAULT_SFR_STREAMBED_GRADIENT
        rgrd.append(max(g, MIN_SFR_STREAMBED_GRADIENT))

    packagedata: list[tuple] = []
    connectiondata: list[list[int]] = []
    reach_meta: list[tuple[int, int, int, float]] = []
    for i, (row, col, rlen) in enumerate(river_cells):
        ncon = 2 if 0 < i < n - 1 else 1
        cellid = (0, int(row), int(col))
        packagedata.append(
            (
                i,               # ifno (0-based reach number)
                cellid,          # cellid (lay, row, col)
                float(rlen),     # rlen (per-cell reach length, m)
                float(rwid),     # rwid (channel width, m)
                float(rgrd[i]),  # rgrd (streambed gradient, m/m, > 0)
                float(rtp[i]),   # rtp (streambed top, m; monotonic downstream)
                float(rbth),     # rbth (streambed thickness, m)
                float(rhk),      # rhk (streambed K, m/day)
                float(man),      # man (Manning roughness)
                ncon,            # ncon (number of connected reaches)
                1.0,             # ustrf (upstream fraction; single stem)
                0,               # ndv (no diversions v0.1)
            )
        )
        chain = [i]
        if i > 0:
            chain.append(i - 1)         # upstream connection (positive)
        if i < n - 1:
            chain.append(-(i + 1))      # downstream connection (negative)
        connectiondata.append(chain)
        reach_meta.append((i, int(row), int(col), float(rlen)))

    # --- perioddata: headwater INFLOW on reach 0 in every period ------------- #
    period_records = [(0, "INFLOW", float(inflow_m3_day))]
    perioddata = {p: period_records for p in range(max(1, n_stress_periods))}

    # --- continuous OBS: stage / downstream-flow / sfr exchange per reach ---- #
    # boundnames are UPPERCASED by mf6 into the csv header (STAGE_R{i} / FLOW_R{i}
    # / GWF_R{i}); the postprocess parser matches that casing (pinned by the
    # Phase-1 smoke fixture). Column ORDER follows this registration order.
    obs_entries: list[tuple[str, str, tuple[int]]] = []
    for i in range(n):
        obs_entries.append((f"stage_r{i}", "stage", (i,)))
    for i in range(n):
        obs_entries.append((f"flow_r{i}", "downstream-flow", (i,)))
    for i in range(n):
        obs_entries.append((f"gwf_r{i}", "sfr", (i,)))
    obs = {"{gwf}.sfr.obs.csv": obs_entries}

    return {
        "packagedata": packagedata,
        "connectiondata": connectiondata,
        "perioddata": perioddata,
        "obs": obs,
        "reach_meta": reach_meta,
        "n_reaches": n,
    }


def _footprint_cells_around(
    wr: int, wc: int, *, nrow: int, ncol: int
) -> list[tuple[int, int]]:
    """The pumped-cell footprint: the WEL cell + its 8 in-grid neighbours.

    Pure arithmetic (no flopy). Returns the unique in-bounds ``(row, col)`` cells
    of the 3x3 neighbourhood centred on ``(wr, wc)`` in a stable, deterministic
    order (row-major). Cells outside the grid are dropped so a near-edge well
    still yields a valid (smaller) footprint. This is the v1 subsidence footprint
    over which one CSUB interbed is placed per cell.
    """
    cells: list[tuple[int, int]] = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            r, c = wr + dr, wc + dc
            if 0 <= r < nrow and 0 <= c < ncol:
                cells.append((r, c))
    return cells


def _build_csub_interbeds(
    footprint_cells: list[tuple[int, int]],
    *,
    ssv: float,
    sse: float,
    thick_frac: float,
    theta: float,
) -> dict[str, Any]:
    """Turn a pumped-cell footprint into MF6 CSUB deck inputs (ONE no-delay
    HEAD_BASED interbed per cell).

    ``footprint_cells`` is a list of ``(row, col)`` cells (layer 0 assumed). Pure
    Python (no flopy) so the interbed geometry + OBS shape are unit-testable
    without the mf6 binary, mirroring ``_build_sfr_reaches``.

    packagedata columns (mf6 CSUB, ``compression_indices=False`` so ssv_cc/sse_cr
    are specific-storage values Ssv/Sse in m^-1; ``cell_fraction=True`` so thick is
    a fraction of the cell thickness):
        ``(icsubno, cellid, cdelay, pcs0, thick_frac, rnb, ssv_cc, sse_cr, theta,
           kv, h0, boundname)``
      * cdelay   = "nodelay" (no delay interbeds in v1);
      * pcs0     = 0.0 (preconsolidation OFFSET = 0 -> with head_based the INITIAL
                   head is the preconsolidation head, so any drawdown is
                   inelastic-capable -> PERMANENT subsidence, the demo signature);
      * thick_frac = the interbed thickness as a fraction of the cell thickness;
      * rnb      = 1.0 (single equivalent interbed; > 1 is only for delay beds);
      * ssv_cc   = INELASTIC (virgin) Ssv, sse_cr = ELASTIC (recompression) Sse
                   with Ssv >> Sse (the elastic/inelastic contrast);
      * theta    = interbed porosity;
      * kv       = 1.0 (ignored for nodelay; a positive dummy avoids the DELAY
                   validation);
      * h0       = 0.0 (initial interbed head offset);
      * boundname = ``sub_r{i}`` (mf6 UPPERCASES it in the OBS csv header ->
                    COMPACTION_R{i} / INE_R{i} / ELA_R{i}, pinned by the smoke).

    The continuous OBS (registered to ``<gwf>.csub.obs.csv``) records, per
    interbed: ``interbed-compaction`` (total, m), ``inelastic-compaction`` (INE,
    the permanent share) and ``elastic-compaction`` (ELA, the recoverable share).
    The postprocess parses these for ``inelastic_fraction = sum(INE) / (sum(INE)
    + sum(ELA))``. The OBS types + the uppercased casing are pinned by the Phase-1
    smoke fixture (fixtures/csub_smoke).

    Returns a dict with ``packagedata`` / ``obs`` / ``interbed_meta`` (the ordered
    ``[icsubno, row, col]`` echo for postprocess georegistration) and
    ``n_interbeds``. Pure Python; the flopy ``ModflowGwfcsub`` call lives in the
    deck builder.
    """
    n = len(footprint_cells)
    if n < 1:
        raise ValueError("CSUB requires >= 1 footprint cell (empty pumped footprint?)")
    packagedata: list[tuple] = []
    interbed_meta: list[tuple[int, int, int]] = []
    for i, (row, col) in enumerate(footprint_cells):
        cellid = (0, int(row), int(col))
        packagedata.append(
            (
                i,                    # icsubno (0-based interbed number)
                cellid,               # cellid (lay, row, col)
                "nodelay",            # cdelay (no delay interbeds v1)
                0.0,                  # pcs0 (preconsolidation OFFSET = 0)
                float(thick_frac),    # thick (fraction of cell thickness)
                1.0,                  # rnb (single equivalent interbed)
                float(ssv),           # ssv_cc (inelastic Ssv, m^-1)
                float(sse),           # sse_cr (elastic Sse, m^-1)
                float(theta),         # theta (interbed porosity)
                1.0,                  # kv (ignored for nodelay; positive dummy)
                0.0,                  # h0 (initial interbed head offset)
                f"sub_r{i}",          # boundname (-> SUB_R{i} in the OBS csv)
            )
        )
        interbed_meta.append((i, int(row), int(col)))

    # --- continuous OBS: total / inelastic / elastic compaction per interbed - #
    # boundname-keyed obs (mf6 UPPERCASES -> COMPACTION_R{i} / INE_R{i} / ELA_R{i}).
    obs_entries: list[tuple[str, str, str]] = []
    for i in range(n):
        obs_entries.append((f"compaction_r{i}", "interbed-compaction", f"sub_r{i}"))
    for i in range(n):
        obs_entries.append((f"ine_r{i}", "inelastic-compaction", f"sub_r{i}"))
    for i in range(n):
        obs_entries.append((f"ela_r{i}", "elastic-compaction", f"sub_r{i}"))
    obs = {"{gwf}.csub.obs.csv": obs_entries}

    return {
        "packagedata": packagedata,
        "obs": obs,
        "interbed_meta": interbed_meta,
        "n_interbeds": n,
    }


def _resolve_transient_periods(
    *,
    sim_years: float | None,
    n_periods: int | None,
) -> list[tuple[float, int, float]]:
    """Resolve the transient stress-period schedule for a transient archetype.

    Returns the per-period ``(perlen_days, nstp, tsmult)`` rows for the TRANSIENT
    periods ONLY (the steady-state spin-up period 0 is prepended by
    ``_add_transient_sto_tdis``). The schedule is derived, in priority order:

      1. ``sim_years`` set -> ``n_periods`` (or the demo default count) equal
         periods spanning ``sim_years`` years (365 days/year).
      2. ``n_periods`` set (no sim_years) -> that many ``DEFAULT_TRANSIENT_PERIOD_DAYS``
         seasonal periods.
      3. neither -> ``DEFAULT_N_TRANSIENT_PERIODS`` seasonal periods.

    Every period uses ``DEFAULT_STEPS_PER_TRANSIENT_PERIOD`` time steps (a single
    tsmult of 1.0). Pure + deterministic (no flopy, no I/O).
    """
    if sim_years is not None and sim_years > 0:
        nper = int(n_periods) if (n_periods and n_periods >= 1) else DEFAULT_N_TRANSIENT_PERIODS
        total_days = float(sim_years) * 365.0
        perlen = total_days / nper
    elif n_periods is not None and n_periods >= 1:
        nper = int(n_periods)
        perlen = DEFAULT_TRANSIENT_PERIOD_DAYS
    else:
        nper = DEFAULT_N_TRANSIENT_PERIODS
        perlen = DEFAULT_TRANSIENT_PERIOD_DAYS
    return [(float(perlen), DEFAULT_STEPS_PER_TRANSIENT_PERIOD, 1.0) for _ in range(nper)]


def _resolve_monthly_periods(
    *,
    n_months: int,
    days_per_month: float = DEFAULT_DAYS_PER_MONTH,
) -> list[tuple[float, int, float]]:
    """Resolve ``n_months`` equal transient stress periods, one per "month".

    The MAR and ASR archetypes step the recharge/inject/recover schedule on a
    monthly cadence (a flat ``DEFAULT_DAYS_PER_MONTH``-day month). Returns the
    per-period ``(perlen_days, nstp, tsmult)`` rows for the TRANSIENT periods ONLY
    (the steady spin-up period 0 is prepended by ``_add_transient_sto_tdis``). Pure
    + deterministic (no flopy, no I/O).
    """
    nper = max(1, int(n_months))
    return [
        (float(days_per_month), DEFAULT_STEPS_PER_TRANSIENT_PERIOD, 1.0)
        for _ in range(nper)
    ]


def _build_asr_well_schedule(
    *,
    injection_periods: int,
    recovery_periods: int,
    n_cycles: int,
) -> list[str]:
    """Build the per-transient-period inject/recover label cycle for the ASR well.

    Returns one label per TRANSIENT period (index 0 = first transient period after
    the steady spin-up): a run of ``"inject"`` then ``"recover"``, repeated
    ``n_cycles`` times. The adapter maps ``"inject"`` -> +injection_rate and
    ``"recover"`` -> -recovery_rate when writing the WEL stress_period_data. Pure +
    deterministic (no flopy, no I/O).
    """
    inj = max(1, int(injection_periods))
    rec = max(1, int(recovery_periods))
    cycles = max(1, int(n_cycles))
    schedule: list[str] = []
    for _ in range(cycles):
        schedule.extend(["inject"] * inj)
        schedule.extend(["recover"] * rec)
    return schedule


def _add_transient_sto_tdis(
    sim,
    gwf,
    *,
    transient_periods: list[tuple[float, int, float]],
    sy: float,
    ss: float,
    gwf_name: str,
    iconvert: int = 0,
    spinup_perlen_days: float = 1.0,
) -> int:
    """Add a transient TDIS (steady spin-up + N transient periods) + a GwfSto.

    The transient archetypes (sustainable_yield, and any future transient GWF-only
    archetype) reuse this so the temporal mode + storage term are written in ONE
    tested place. The schedule is: period 0 = a single-step STEADY-state spin-up
    so the head field equilibrates before any transient stress, then the supplied
    ``transient_periods`` as transient periods.

    The ``ModflowGwfsto`` declares ``steady_state={0: True}`` and
    ``transient={i: True}`` for every transient period i (1..N), with ``iconvert``
    (0 = confined storage, 1 = convertible water-table storage), specific yield
    ``sy`` and specific storage ``ss``. STEADY archetypes (mine_dewatering) do NOT
    call this -- they keep the single steady period + no STO.

    Returns the total TDIS stress-period count (1 spin-up + len(transient_periods)).
    """
    perioddata: list[tuple[float, int, float]] = [
        (float(spinup_perlen_days), 1, 1.0),  # steady-state spin-up
    ]
    perioddata.extend(transient_periods)
    nper = len(perioddata)
    flopy.mf6.ModflowTdis(
        sim,
        time_units=TIME_UNITS,
        nper=nper,
        perioddata=perioddata,
    )
    transient_map = {i: True for i in range(1, nper)}  # periods 1..N are transient
    flopy.mf6.ModflowGwfsto(
        gwf,
        iconvert=iconvert,
        ss=ss,
        sy=sy,
        steady_state={0: True},
        transient=transient_map,
        save_flows=True,
        filename=f"{gwf_name}.sto",
    )
    return nper


def _build_gwf_only_archetype_deck(
    *,
    archetype: str,
    lat: float,
    lon: float,
    crs,
    to_utm,
    xorigin: float,
    yorigin: float,
    nrow: int,
    ncol: int,
    delr: float,
    delc: float,
    k_m_per_day: float,
    aquifer_k_ms: float,
    porosity: float,
    sim_dir: Path,
    sim_name: str,
    gwf_name: str,
    write: bool,
    # sustainable_yield
    well_location_latlon: tuple[float, float] | None,
    pumping_rate_m3_day: float | None,
    aquifer_sy: float | None,
    aquifer_ss: float | None,
    sim_years: float | None,
    n_periods: int | None,
    # mine_dewatering
    pit_footprint_lonlat: list[tuple[float, float]] | None,
    drain_elevation_m: float | None,
    drain_conductance_m2_day: float | None,
    well_pumping_rate_m3_day: float | None,
    # regional_water_budget
    zone_partition: str | None,
    # MAR (managed aquifer recharge -> RCH mounding)
    basin_footprint_lonlat: list[tuple[float, float]] | None = None,
    infiltration_rate_m_day: float | None = None,
    recharge_months: int | None = None,
    # ASR (aquifer storage & recovery -> seasonal WEL inject/recover)
    injection_rate_m3_day: float | None = None,
    recovery_rate_m3_day: float | None = None,
    injection_months: int | None = None,
    recovery_months: int | None = None,
    n_cycles: int | None = None,
    # wetland_hydroperiod (seasonal water-table range under a wetland)
    wetland_footprint_lonlat: list[tuple[float, float]] | None = None,
    recharge_schedule_m_day: list[float] | None = None,
    et_surface_m: float | None = None,
    et_max_rate_m_day: float | None = None,
    et_extinction_depth_m: float | None = None,
    specific_yield: float | None = None,
    # stream_depletion (module wave: SFR routed river<->aquifer exchange)
    river_polyline_lonlat: list[tuple[float, float]] | None = None,
    river_rbot_by_cell: dict[tuple[int, int], float] | None = None,
    river_inflow_m3_s: float | None = None,
    river_width_m: float | None = None,
    streambed_k_m_day: float | None = None,
    manning_n: float | None = None,
    # land_subsidence (module wave: CSUB aquifer-system compaction)
    csub_ssv_inelastic_m: float | None = None,
    csub_sse_elastic_m: float | None = None,
    csub_interbed_thick_frac: float | None = None,
    csub_cg_ske_m: float | None = None,
    # constitutive advanced-physics (levers STEP 3): ALREADY-VALIDATED resolved
    # dict (regional_gradient / streambed_k_m_day / sfr_manning_n). None/{} =>
    # every phys.get below returns the historical constant => byte-identical.
    advanced_physics: dict | None = None,
) -> DeckManifest:
    """Assemble a GWF-ONLY archetype deck (no GWT block, no GWFGWT exchange).

    Shared GWF scaffold for the three sprint-18 Wave-1 archetypes. The grid,
    west->east REGIONAL_GRADIENT CHD, IC and OC (HEAD + BUDGET, ALL) are identical
    across them; only the temporal mode + the stress packages differ:

      * ``sustainable_yield``  -> transient (STO via ``_add_transient_sto_tdis``) +
        a sustained WEL extraction well. Headline = the drawdown cone (.hds).
      * ``mine_dewatering``    -> STEADY, NPF icelltype=1 (unconfined water table) +
        a DRN ring over the pit footprint (+ optional sump WEL). Headline = the DRN
        budget term (the pump-to-dewater rate).
      * ``regional_water_budget`` -> STEADY GWF, NO new stress package; the
        deliverable is the CBC budget partition (read agent-side). An optional ZONE
        array is written when ``zone_partition`` is set.

    OC saves HEAD + BUDGET ALL so the agent-side phase can read the .hds drawdown
    and the .cbc DRN/CHD/WEL/STO budget terms.
    """
    sim = flopy.mf6.MFSimulation(
        sim_name=sim_name,
        sim_ws=str(sim_dir),
        exe_name="mf6",
        version="mf6",
    )

    # wetland_hydroperiod solves an UNCONFINED + EVT (head-dependent sink) system
    # whose Picard/linear-CG iteration diverges; MF6's NEWTON formulation +
    # BICGSTAB is required for a robust solve. The other archetypes keep the
    # standard MODERATE/BICGSTAB linear solve. NEWTON is declared on the GWF model
    # (newtonoptions) AND the IMS linear_acceleration is forced to BICGSTAB.
    # stream_depletion drapes a routed SFR6 network onto the GWF grid. SFR forces
    # an ASYMMETRIC coefficient matrix ("PRODUCES AN ASYMMETRIC COEFFICIENT
    # MATRIX... USE BICGSTAB", proven live under the default CG), and the
    # near-stream unconfined water table wants NEWTON for a robust solve - so
    # stream_depletion joins wetland_hydroperiod on the NEWTON + BICGSTAB path.
    # ``sfr_present`` is the explicit flag: the linear_acceleration flip is gated
    # on it so a NON-SFR archetype deck is never perturbed by the SFR path.
    sfr_present = archetype == "stream_depletion"
    # land_subsidence layers a CSUB package onto the sustainable_yield transient
    # WEL deck. HEAD_BASED CSUB is LINEAR in head (no head-dependent CSUB
    # nonlinearity) and does NOT force an asymmetric matrix, so it needs NEITHER
    # NEWTON NOR a linear_acceleration change - the scaffold's BICGSTAB/MODERATE
    # default holds, and every non-CSUB archetype deck stays byte-identical.
    # ``csub_present`` gates the whole CSUB branch, mirroring ``sfr_present``.
    csub_present = archetype == "land_subsidence"
    use_newton = archetype in ("wetland_hydroperiod", "stream_depletion")

    gwf = flopy.mf6.ModflowGwf(
        sim,
        modelname=gwf_name,
        model_nam_file=f"{gwf_name}.nam",
        save_flows=True,
        newtonoptions="NEWTON" if use_newton else None,
    )
    ims_gwf = flopy.mf6.ModflowIms(
        sim,
        filename=f"{gwf_name}.ims",
        complexity="MODERATE" if not use_newton else "COMPLEX",
        outer_dvclose=1e-6,
        inner_dvclose=1e-6,
        # BICGSTAB is required whenever SFR is present (asymmetric matrix) AND for
        # the NEWTON archetypes; the pre-existing archetypes keep their BICGSTAB
        # default untouched (this helper never used CG), so no existing deck moves.
        linear_acceleration="BICGSTAB",
    )
    sim.register_ims_package(ims_gwf, [gwf_name])

    # --- Temporal mode + storage --------------------------------------------- #
    # Transient archetypes: sustainable_yield (multi-period drawdown), MAR (monthly
    # recharge mounding), ASR (seasonal inject/recover cycles), wetland_hydroperiod
    # (per-period recharge schedule + EVT). mine_dewatering / regional_water_budget
    # stay STEADY.
    transient = archetype in (
        "sustainable_yield",
        "MAR",
        "ASR",
        "wetland_hydroperiod",
        "stream_depletion",
        "land_subsidence",
    )
    sy = float(aquifer_sy) if aquifer_sy is not None else DEFAULT_AQUIFER_SY
    ss = float(aquifer_ss) if aquifer_ss is not None else DEFAULT_AQUIFER_SS
    # STORAGE double-count guard (land_subsidence): CSUB supplies the coarse
    # skeletal storage via cg_ske_cr, and mf6 6.5.0 HARD-REQUIRES STO ss == 0 in
    # all active cells when CSUB is present (proven in the fixtures/csub_smoke
    # run: STO ss > 0 -> "Specific storage values ... must be zero ..." error).
    # Drop ss to the floor so the aquifer does NOT store water twice - the
    # CSUB-run head decline then matches the plain sustainable_yield run.
    if csub_present:
        ss = CSUB_STO_SS_FLOOR
    # MAR/ASR/wetland override sy with their own demo defaults (water-table response).
    if archetype == "MAR" and aquifer_sy is None:
        sy = DEFAULT_MAR_SY
    elif archetype == "wetland_hydroperiod":
        sy = float(specific_yield) if specific_yield is not None else DEFAULT_WETLAND_SY

    # Resolve the per-archetype transient schedule (transient periods only; the
    # steady spin-up period 0 is prepended by _add_transient_sto_tdis).
    asr_schedule: list[str] = []
    if archetype == "MAR":
        n_months = (
            int(recharge_months)
            if recharge_months
            else (int(n_periods) if n_periods else DEFAULT_MAR_RECHARGE_MONTHS)
        )
        transient_periods = _resolve_monthly_periods(n_months=n_months)
    elif archetype == "ASR":
        inj_m = int(injection_months) if injection_months else DEFAULT_ASR_INJECTION_MONTHS
        rec_m = int(recovery_months) if recovery_months else DEFAULT_ASR_RECOVERY_MONTHS
        n_cyc = int(n_cycles) if n_cycles else DEFAULT_ASR_N_CYCLES
        asr_schedule = _build_asr_well_schedule(
            injection_periods=inj_m, recovery_periods=rec_m, n_cycles=n_cyc
        )
        transient_periods = _resolve_monthly_periods(n_months=len(asr_schedule))
    elif archetype == "wetland_hydroperiod":
        # One transient period per scheduled recharge rate; default to a wet/dry
        # alternation over DEFAULT_N_TRANSIENT_PERIODS when no schedule is given.
        if recharge_schedule_m_day:
            n_wp = len(recharge_schedule_m_day)
        elif n_periods:
            n_wp = int(n_periods)
        else:
            n_wp = DEFAULT_N_TRANSIENT_PERIODS
        transient_periods = _resolve_monthly_periods(n_months=n_wp)
    elif archetype in ("sustainable_yield", "stream_depletion", "land_subsidence"):
        # stream_depletion + land_subsidence reuse the sustainable_yield transient
        # schedule: a steady baseline (WEL off) then the pumped period(s). The
        # steady spin-up holds the CSUB preconsolidation; the transient periods
        # draw down and drive the compaction - ONE solve, same single-solve
        # discipline the SFR smoke proved (no separate baseline run needed).
        transient_periods = _resolve_transient_periods(
            sim_years=sim_years, n_periods=n_periods
        )
    else:
        transient_periods = []

    if transient:
        n_stress_periods = 1 + len(transient_periods)
        n_transient_periods = len(transient_periods)
    else:
        # STEADY single period (mine_dewatering / regional_water_budget).
        flopy.mf6.ModflowTdis(
            sim,
            time_units=TIME_UNITS,
            nper=1,
            perioddata=[(1.0, 1, 1.0)],
        )
        n_stress_periods = 1
        n_transient_periods = 0

    # --- DIS ----------------------------------------------------------------- #
    flopy.mf6.ModflowGwfdis(
        gwf,
        length_units=LENGTH_UNITS,
        nlay=N_LAYERS,
        nrow=nrow,
        ncol=ncol,
        delr=delr,
        delc=delc,
        top=AQUIFER_TOP_M,
        botm=AQUIFER_BOTTOM_M,
        xorigin=xorigin,
        yorigin=yorigin,
        filename=f"{gwf_name}.dis",
    )
    try:
        gwf.modelgrid.set_coord_info(xoff=xorigin, yoff=yorigin, crs=crs.to_epsg())
    except Exception:  # pragma: no cover - older flopy signature fallback
        pass

    # --- IC + NPF ------------------------------------------------------------ #
    # regional_gradient: CONSTITUTIVE lever. Default EQUALS REGIONAL_GRADIENT so
    # an unset run is byte-identical (same phys.get seam as alh/ath1).
    phys = dict(advanced_physics or {})
    domain_width_m = ncol * delr
    head_west = AQUIFER_TOP_M + float(
        phys.get("regional_gradient", REGIONAL_GRADIENT)
    ) * domain_width_m
    head_east = AQUIFER_TOP_M
    flopy.mf6.ModflowGwfic(gwf, strt=head_west, filename=f"{gwf_name}.ic")

    # Unconfined water table (icelltype=1) is required where the head response IS
    # the answer and the cells must be allowed to rise/fall: mine_dewatering (cells
    # de-saturate at the drain), MAR (the recharge MOUND rises above the confined
    # head), wetland_hydroperiod (the seasonal water-table range + the EVT
    # head-dependent sink). sustainable_yield / ASR / regional_water_budget stay
    # confined (icelltype=0).
    npf_icelltype = (
        1
        if archetype
        in ("mine_dewatering", "MAR", "wetland_hydroperiod", "stream_depletion")
        else 0
    )
    flopy.mf6.ModflowGwfnpf(
        gwf,
        save_flows=True,
        icelltype=npf_icelltype,
        k=k_m_per_day,
        filename=f"{gwf_name}.npf",
    )

    # --- STO (transient archetypes only) ------------------------------------- #
    if transient:
        _add_transient_sto_tdis(
            sim,
            gwf,
            transient_periods=transient_periods,
            sy=sy,
            ss=ss,
            gwf_name=gwf_name,
            iconvert=npf_icelltype,
        )

    # --- CHD regional gradient (same as the spill deck) ---------------------- #
    chd_records = []
    for r in range(nrow):
        chd_records.append([(0, r, 0), head_west])
        chd_records.append([(0, r, ncol - 1), head_east])
    chd_spd = {i: chd_records for i in range(n_stress_periods)}
    flopy.mf6.ModflowGwfchd(
        gwf,
        stress_period_data=chd_spd,
        filename=f"{gwf_name}.chd",
    )

    # --- Per-archetype stress packages --------------------------------------- #
    well_row = well_col = -1
    well_east = well_north = 0.0
    well_lat = well_lon = 0.0
    pump_rate = 0.0
    drain_cell_count = 0
    drain_elev_written = 0.0
    drain_cond_written = 0.0
    zone_partition_written: str | None = None
    n_zones = 0
    # Wave-2 accumulators (default to the no-package value for every archetype).
    recharge_cell_count = 0
    infiltration_rate_written = 0.0
    recharge_active_periods = 0
    injection_rate_written = 0.0
    recovery_rate_written = 0.0
    asr_n_cycles_written = 0
    asr_injection_periods = 0
    asr_recovery_periods = 0
    wetland_cell_count = 0
    et_surface_written = 0.0
    et_max_rate_written = 0.0
    et_extinction_written = 0.0
    # stream_depletion SFR accumulators (default to the no-SFR value).
    sfr_n_reaches = 0
    sfr_reach_cells_meta: list[list[float]] = []
    sfr_inflow_written = 0.0
    sfr_width_written = 0.0
    sfr_streambed_k_written = 0.0
    sfr_manning_written = 0.0

    # land_subsidence CSUB accumulators (default to the no-CSUB value).
    csub_n_interbeds = 0
    csub_interbed_cells_meta: list[list[float]] = []
    csub_ssv_written = 0.0
    csub_sse_written = 0.0
    csub_thick_frac_written = 0.0

    if archetype in ("sustainable_yield", "stream_depletion", "land_subsidence"):
        # stream_depletion + land_subsidence place the SAME sustained-extraction
        # WEL as sustainable_yield (the pumping whose river/subsidence impact we
        # measure); the SFR network / CSUB interbeds are added after this block.
        if well_location_latlon is None:
            raise ValueError(
                f"{archetype} archetype requires well_location_latlon"
            )
        if pumping_rate_m3_day is None:
            raise ValueError(
                f"{archetype} archetype requires pumping_rate_m3_day"
            )
        wlat, wlon = float(well_location_latlon[0]), float(well_location_latlon[1])
        if not (-90.0 <= wlat <= 90.0) or not (-180.0 <= wlon <= 180.0):
            raise ValueError(f"well_location_latlon out of range: {(wlat, wlon)!r}")
        well_east, well_north = to_utm.transform(wlon, wlat)
        cell = _easting_northing_to_cell(
            well_east,
            well_north,
            xorigin=xorigin,
            yorigin=yorigin,
            delr=delr,
            delc=delc,
            nrow=nrow,
            ncol=ncol,
        )
        if cell is None:
            # Clamp to the nearest in-grid cell (the well must land on the grid).
            col = max(0, min(ncol - 1, int((well_east - xorigin) // delr)))
            row = max(
                0,
                min(nrow - 1, int(((yorigin + nrow * delc) - well_north) // delc)),
            )
            cell = (row, col)
        well_row, well_col = cell
        well_lat, well_lon = wlat, wlon
        # The contract carries pumping_rate_m3_day as a POSITIVE extraction
        # magnitude (sustainable_yield is always an extraction question); apply the
        # MF6 sign internally (negative WEL q = discharge/extraction). Without the
        # -abs the well injected and drawdown read as zero (caught by the real-mf6
        # proof gate).
        pump_rate = -abs(float(pumping_rate_m3_day))
        # WEL is active in EVERY transient period (sustained pumping). The
        # steady-state spin-up (period 0) runs WITHOUT the well so the drawdown
        # is measured against the undisturbed regional head.
        wel_record = [[(0, well_row, well_col), pump_rate]]
        wel_spd = {0: []}
        for i in range(1, n_stress_periods):
            wel_spd[i] = wel_record
        flopy.mf6.ModflowGwfwel(
            gwf,
            stress_period_data=wel_spd,
            save_flows=True,
            filename=f"{gwf_name}.wel",
            pname="wel-0",
        )

    elif archetype == "mine_dewatering":
        if not pit_footprint_lonlat:
            raise ValueError(
                "mine_dewatering archetype requires a pit_footprint_lonlat"
            )
        drain_cond_written = (
            float(drain_conductance_m2_day)
            if drain_conductance_m2_day is not None
            else DEFAULT_DRAIN_CONDUCTANCE_M2_DAY
        )
        drain_elev_written = (
            float(drain_elevation_m)
            if drain_elevation_m is not None
            else AQUIFER_TOP_M - DEFAULT_DRAIN_DEPTH_BELOW_TOP_M
        )
        # Drape the pit footprint onto the grid. A polygon ring is draped as a
        # polyline (its boundary), then the interior is filled so the whole pit
        # footprint is drained (not just the ring).
        verts_en = [to_utm.transform(plon, plat) for (plon, plat) in pit_footprint_lonlat]
        ring = list(verts_en)
        if len(ring) >= 3 and ring[0] != ring[-1]:
            ring.append(ring[0])  # close the ring so the boundary is continuous
        draped = _drape_polyline_onto_grid(
            ring,
            xorigin=xorigin,
            yorigin=yorigin,
            delr=delr,
            delc=delc,
            nrow=nrow,
            ncol=ncol,
        )
        pit_cells = _fill_polygon_cells(
            [(r, c) for (r, c, _l) in draped], nrow=nrow, ncol=ncol
        )
        # Skip the CHD boundary columns (a cell cannot be both CHD and DRN).
        skip_cols = {0, ncol - 1}
        drn_records = [
            [(0, r, c), drain_elev_written, drain_cond_written]
            for (r, c) in pit_cells
            if c not in skip_cols
        ]
        if not drn_records:
            raise ValueError(
                "mine_dewatering pit_footprint_lonlat draped to zero in-grid "
                "drain cells (footprint outside the model grid?)"
            )
        drn_spd = {i: drn_records for i in range(n_stress_periods)}
        flopy.mf6.ModflowGwfdrn(
            gwf,
            stress_period_data=drn_spd,
            save_flows=True,
            filename=f"{gwf_name}.drn",
            pname="drn-0",
        )
        drain_cell_count = len(drn_records)
        # Optional supplemental sump WEL (a pit dewatered by drains + pumping).
        if well_pumping_rate_m3_day is not None and float(well_pumping_rate_m3_day) != 0.0:
            # Place the sump at the pit centroid cell.
            crow = sum(r for (r, _c) in pit_cells) // len(pit_cells)
            ccol = sum(c for (_r, c) in pit_cells) // len(pit_cells)
            ccol = max(1, min(ncol - 2, ccol))  # keep off the CHD columns
            # positive magnitude -> negative MF6 discharge (sump always extracts).
            pump_rate = -abs(float(well_pumping_rate_m3_day))
            flopy.mf6.ModflowGwfwel(
                gwf,
                stress_period_data={i: [[(0, crow, ccol), pump_rate]] for i in range(n_stress_periods)},
                save_flows=True,
                filename=f"{gwf_name}.wel",
                pname="wel-0",
            )
            well_row, well_col = crow, ccol

    elif archetype == "regional_water_budget":
        # No new stress package -- the deliverable is the CBC budget partition.
        # When a zone_partition is requested, write the optional ZONE array so an
        # agent-side ZoneBudget-style partition can read it.
        if zone_partition:
            zone_partition_written = str(zone_partition)
            zone_array, n_zones = _build_zone_array(zone_partition, nrow=nrow, ncol=ncol)
            # FloPy has no first-class ZONE package; write a plain external array
            # the agent-side partition reads. We persist it as a CSV sidecar so it
            # ships with the deck without perturbing any MF6 input file.
            if write:
                zpath = sim_dir / f"{gwf_name}.zones.csv"
                lines = [",".join(str(int(v)) for v in row) for row in zone_array]
                zpath.write_text("\n".join(lines) + "\n")

    elif archetype == "MAR":
        # Managed aquifer recharge: an infiltration basin floods a footprint with a
        # POSITIVE recharge flux over the recharge periods; the RCH package raises
        # the (unconfined, icelltype=1) water table -> the mounding head field. The
        # basin is draped onto the grid as a list-based RCH (one record per basin
        # cell) when a footprint is supplied, else a uniform RCHA over the whole
        # domain. Recharge is OFF in the steady spin-up (period 0) so the mound is
        # measured against the undisturbed regional head.
        infiltration_rate_written = (
            float(infiltration_rate_m_day)
            if infiltration_rate_m_day is not None
            else DEFAULT_MAR_INFILTRATION_M_DAY
        )
        recharge_active_periods = n_transient_periods
        if basin_footprint_lonlat:
            basin_cells = _drape_footprint_to_cells(
                basin_footprint_lonlat,
                to_utm=to_utm,
                xorigin=xorigin,
                yorigin=yorigin,
                delr=delr,
                delc=delc,
                nrow=nrow,
                ncol=ncol,
                skip_cols={0, ncol - 1},  # a CHD cell cannot also be RCH
            )
            if not basin_cells:
                raise ValueError(
                    "MAR basin_footprint_lonlat draped to zero in-grid recharge "
                    "cells (footprint outside the model grid?)"
                )
            rch_record = [
                [(0, r, c), infiltration_rate_written] for (r, c) in basin_cells
            ]
            # OFF in the steady spin-up; ON in every transient period.
            rch_spd = {0: []}
            for i in range(1, n_stress_periods):
                rch_spd[i] = rch_record
            flopy.mf6.ModflowGwfrch(
                gwf,
                stress_period_data=rch_spd,
                save_flows=True,
                filename=f"{gwf_name}.rch",
                pname="rch-0",
            )
            recharge_cell_count = len(rch_record)
        else:
            # No basin footprint -> a uniform array recharge (RCHA) over the domain.
            recharge_array = {0: 0.0}
            for i in range(1, n_stress_periods):
                recharge_array[i] = infiltration_rate_written
            flopy.mf6.ModflowGwfrcha(
                gwf,
                recharge=recharge_array,
                save_flows=True,
                filename=f"{gwf_name}.rcha",
            )
            recharge_cell_count = nrow * ncol

    elif archetype == "ASR":
        # Aquifer storage & recovery: ONE well at well_location_latlon INJECTS at a
        # positive q for the injection periods then RECOVERS (extracts) at a
        # negative q for the recovery periods, cycled n_cycles. The seasonal
        # stress_period_data flips the WEL sign per the asr_schedule resolved above.
        if well_location_latlon is None:
            raise ValueError("ASR archetype requires well_location_latlon")
        injection_rate_written = (
            float(injection_rate_m3_day)
            if injection_rate_m3_day is not None
            else DEFAULT_ASR_INJECTION_RATE_M3_DAY
        )
        recovery_rate_written = (
            float(recovery_rate_m3_day)
            if recovery_rate_m3_day is not None
            else DEFAULT_ASR_RECOVERY_RATE_M3_DAY
        )
        wlat, wlon = float(well_location_latlon[0]), float(well_location_latlon[1])
        if not (-90.0 <= wlat <= 90.0) or not (-180.0 <= wlon <= 180.0):
            raise ValueError(f"well_location_latlon out of range: {(wlat, wlon)!r}")
        well_east, well_north = to_utm.transform(wlon, wlat)
        cell = _easting_northing_to_cell(
            well_east,
            well_north,
            xorigin=xorigin,
            yorigin=yorigin,
            delr=delr,
            delc=delc,
            nrow=nrow,
            ncol=ncol,
        )
        if cell is None:
            col = max(1, min(ncol - 2, int((well_east - xorigin) // delr)))
            row = max(
                0,
                min(nrow - 1, int(((yorigin + nrow * delc) - well_north) // delc)),
            )
            cell = (row, col)
        well_row, well_col = cell
        # Keep the ASR well off the CHD boundary columns (a cell cannot be both).
        well_col = max(1, min(ncol - 2, well_col))
        well_lat, well_lon = wlat, wlon
        inject_q = abs(injection_rate_written)
        recover_q = -abs(recovery_rate_written)
        # Period 0 = steady spin-up (NO well). Periods 1..N follow asr_schedule.
        wel_spd = {0: []}
        for i, label in enumerate(asr_schedule, start=1):
            q = inject_q if label == "inject" else recover_q
            wel_spd[i] = [[(0, well_row, well_col), q]]
        flopy.mf6.ModflowGwfwel(
            gwf,
            stress_period_data=wel_spd,
            save_flows=True,
            filename=f"{gwf_name}.wel",
            pname="wel-0",
        )
        # The headline recovery rate carried into the manifest as pump_rate (the
        # extraction magnitude the agent narrates). aquifer_sy/ss are written below.
        pump_rate = recover_q
        asr_n_cycles_written = int(n_cycles) if n_cycles else DEFAULT_ASR_N_CYCLES
        asr_injection_periods = sum(1 for s in asr_schedule if s == "inject")
        asr_recovery_periods = sum(1 for s in asr_schedule if s == "recover")

    elif archetype == "wetland_hydroperiod":
        # Seasonal water-table range under a wetland: a per-period RCH schedule
        # (wet/dry recharge) drives the unconfined (icelltype=1, NEWTON) water table
        # up and down while an EVT head-dependent sink draws it back at the surface.
        # The seasonal head range over the wetland IS the hydroperiod. flopy carries
        # the last stress-period block forward, but we emit EVERY period explicitly
        # so each scheduled recharge rate is unambiguous.
        if not wetland_footprint_lonlat:
            raise ValueError(
                "wetland_hydroperiod archetype requires a wetland_footprint_lonlat"
            )
        wetland_cells = _drape_footprint_to_cells(
            wetland_footprint_lonlat,
            to_utm=to_utm,
            xorigin=xorigin,
            yorigin=yorigin,
            delr=delr,
            delc=delc,
            nrow=nrow,
            ncol=ncol,
            skip_cols={0, ncol - 1},  # a CHD cell cannot also be RCH/EVT
        )
        if not wetland_cells:
            raise ValueError(
                "wetland_hydroperiod wetland_footprint_lonlat draped to zero "
                "in-grid cells (footprint outside the model grid?)"
            )
        wetland_cell_count = len(wetland_cells)
        # Resolve the per-transient-period recharge schedule. When the caller gives
        # a schedule use it (one rate per transient period, last value forward-filled
        # if short); else alternate a wet/dry default.
        if recharge_schedule_m_day:
            sched = [float(v) for v in recharge_schedule_m_day]
        else:
            sched = [
                DEFAULT_WETLAND_RECHARGE_WET_M_DAY
                if (p % 2 == 0)
                else DEFAULT_WETLAND_RECHARGE_DRY_M_DAY
                for p in range(n_transient_periods)
            ]
        # RCH: period 0 (steady spin-up) = 0; periods 1..N from the schedule
        # (forward-fill the last value if the schedule is shorter than N periods).
        rch_spd = {0: []}
        for i in range(1, n_stress_periods):
            rate = sched[min(i - 1, len(sched) - 1)] if sched else 0.0
            rch_spd[i] = [[(0, r, c), float(rate)] for (r, c) in wetland_cells]
        flopy.mf6.ModflowGwfrch(
            gwf,
            stress_period_data=rch_spd,
            save_flows=True,
            filename=f"{gwf_name}.rch",
            pname="rch-0",
        )
        # EVT: a head-dependent ET sink over the wetland footprint, active in every
        # period (ET happens during the spin-up too -- it is a standing climate
        # flux, not a transient stress). surface = et_surface_m, max rate at the
        # surface, linearly decaying to zero at the extinction depth below it.
        et_surface_written = (
            float(et_surface_m) if et_surface_m is not None else AQUIFER_TOP_M
        )
        et_max_rate_written = (
            float(et_max_rate_m_day)
            if et_max_rate_m_day is not None
            else DEFAULT_WETLAND_ET_MAX_RATE_M_DAY
        )
        et_extinction_written = (
            float(et_extinction_depth_m)
            if et_extinction_depth_m is not None
            else DEFAULT_WETLAND_ET_EXTINCTION_DEPTH_M
        )
        evt_record = [
            [(0, r, c), et_surface_written, et_max_rate_written, et_extinction_written]
            for (r, c) in wetland_cells
        ]
        evt_spd = {i: evt_record for i in range(n_stress_periods)}
        flopy.mf6.ModflowGwfevt(
            gwf,
            stress_period_data=evt_spd,
            save_flows=True,
            filename=f"{gwf_name}.evt",
            pname="evt-0",
        )

    # --- SFR: routed river<->aquifer exchange (stream_depletion) -------------- #
    # Drape the fetched NHDPlus flowline onto the grid as path-ordered reaches
    # (headwater->outlet), build the SFR6 packagedata/connectiondata/perioddata +
    # continuous OBS (stage / downstream-flow / sfr GWF-exchange per reach ->
    # <gwf>.sfr.obs.csv). length_conversion=1.0 + time_conversion=86400.0 make
    # Manning's flow internally consistent in METERS/DAYS (unit_conversion is
    # DEPRECATED since mf6 6.4.2). The postprocess parses the OBS csv; the reach
    # cell echo goes onto the manifest for georegistration.
    if sfr_present:
        if not river_polyline_lonlat:
            raise ValueError(
                "stream_depletion archetype requires a river_polyline_lonlat "
                "(fetch_river_geometry -> resolve_river_polyline_lonlat)"
            )
        verts_en = [
            to_utm.transform(float(plon), float(plat))
            for (plon, plat) in river_polyline_lonlat
        ]
        river_cells = _drape_polyline_onto_grid(
            verts_en,
            xorigin=xorigin,
            yorigin=yorigin,
            delr=delr,
            delc=delc,
            nrow=nrow,
            ncol=ncol,
        )
        if not river_cells:
            raise ValueError(
                "stream_depletion river_polyline_lonlat draped to zero in-grid "
                "reach cells (flowline outside the model grid?)"
            )
        sfr_width_written = (
            float(river_width_m) if river_width_m is not None else DEFAULT_SFR_WIDTH_M
        )
        # SFR streambed K / Manning n: an explicit run-arg still wins; otherwise
        # fall back to the advanced_physics override, then the historical constant
        # (phys.get => byte-identical when unset). Same seam as regional_gradient.
        sfr_streambed_k_written = (
            float(streambed_k_m_day)
            if streambed_k_m_day is not None
            else float(phys.get("streambed_k_m_day", DEFAULT_SFR_STREAMBED_K_M_DAY))
        )
        sfr_manning_written = (
            float(manning_n)
            if manning_n is not None
            else float(phys.get("sfr_manning_n", DEFAULT_SFR_MANNING_N))
        )
        # INFLOW: the contract carries river_inflow_m3_s (m^3/s); SFR perioddata is
        # in the deck's time unit (DAYS) so convert to m^3/day.
        sfr_inflow_written = (
            float(river_inflow_m3_s) * SECONDS_PER_DAY
            if river_inflow_m3_s is not None
            else DEFAULT_SFR_INFLOW_M3_DAY
        )
        sfr_build = _build_sfr_reaches(
            river_cells,
            rwid=sfr_width_written,
            rhk=sfr_streambed_k_written,
            man=sfr_manning_written,
            rbth=DEFAULT_SFR_BED_THICKNESS_M,
            inflow_m3_day=sfr_inflow_written,
            n_stress_periods=n_stress_periods,
            rtp_by_cell=river_rbot_by_cell,
        )
        sfr_n_reaches = int(sfr_build["n_reaches"])
        sfr_reach_cells_meta = [list(m) for m in sfr_build["reach_meta"]]
        # OBS boundnames are registered on the package; the csv filename resolves
        # to "<gwf>.sfr.obs.csv" (the postprocess-parse target).
        obs_map = {
            key.format(gwf=gwf_name): entries
            for key, entries in sfr_build["obs"].items()
        }
        sfr = flopy.mf6.ModflowGwfsfr(
            gwf,
            save_flows=True,
            print_input=True,
            print_flows=True,
            length_conversion=1.0,
            time_conversion=86400.0,
            nreaches=sfr_n_reaches,
            packagedata=sfr_build["packagedata"],
            connectiondata=sfr_build["connectiondata"],
            perioddata=sfr_build["perioddata"],
            stage_filerecord=f"{gwf_name}.sfr.stg",
            budget_filerecord=f"{gwf_name}.sfr.bud",
            observations=obs_map,
            filename=f"{gwf_name}.sfr",
            pname="sfr-0",
        )

    # --- CSUB: aquifer-system compaction / land subsidence (land_subsidence) -- #
    # Layer ONE no-delay HEAD_BASED interbed per pumped footprint cell (the WEL
    # cell + its 8 neighbours). The pumping drawdown drives inelastic (permanent)
    # compaction because pcs0=0 makes the initial head the preconsolidation head.
    # CSUB writes the total compaction grid + the z-displacement grid (the
    # subsidence bowl) + a per-interbed OBS csv (total / inelastic / elastic
    # compaction). The STO ss was already dropped to 0 above (mf6-enforced
    # double-count guard). PINNED by the smoke: output text tags CSUB-COMPACTION /
    # CSUB-ZDISPLACE (the latter TRUNCATED to 16 chars); subsidence positive-down.
    if csub_present:
        csub_ssv_written = (
            float(csub_ssv_inelastic_m)
            if csub_ssv_inelastic_m is not None
            else DEFAULT_CSUB_SSV_INELASTIC
        )
        csub_sse_written = (
            float(csub_sse_elastic_m)
            if csub_sse_elastic_m is not None
            else DEFAULT_CSUB_SSE_ELASTIC
        )
        csub_thick_frac_written = (
            float(csub_interbed_thick_frac)
            if csub_interbed_thick_frac is not None
            else DEFAULT_CSUB_INTERBED_THICK_FRAC
        )
        cg_ske_written = (
            float(csub_cg_ske_m) if csub_cg_ske_m is not None else DEFAULT_CSUB_CG_SKE
        )
        footprint = _footprint_cells_around(
            int(well_row), int(well_col), nrow=nrow, ncol=ncol
        )
        csub_build = _build_csub_interbeds(
            footprint,
            ssv=csub_ssv_written,
            sse=csub_sse_written,
            thick_frac=csub_thick_frac_written,
            theta=porosity,
        )
        csub_n_interbeds = int(csub_build["n_interbeds"])
        csub_interbed_cells_meta = [list(m) for m in csub_build["interbed_meta"]]
        obs_map = {
            key.format(gwf=gwf_name): entries
            for key, entries in csub_build["obs"].items()
        }
        csub_pkg = flopy.mf6.ModflowGwfcsub(
            gwf,
            save_flows=True,
            boundnames=True,
            head_based=True,
            initial_preconsolidation_head=True,
            cell_fraction=True,          # thick is a fraction of cell thickness
            compression_indices=False,   # ssv_cc/sse_cr are Ss values (m^-1)
            ninterbeds=csub_n_interbeds,
            maxsig0=0,
            cg_ske_cr=cg_ske_written,
            cg_theta=porosity,
            packagedata=csub_build["packagedata"],
            compaction_filerecord=f"{gwf_name}.csub.compaction.bin",
            zdisplacement_filerecord=f"{gwf_name}.csub.zdisp.bin",
            filename=f"{gwf_name}.csub",
            pname="csub-0",
        )
        csub_pkg.obs.initialize(
            filename=f"{gwf_name}.csub.obs", continuous=obs_map
        )

    # --- OC: save HEAD + BUDGET ALL ------------------------------------------ #
    flopy.mf6.ModflowGwfoc(
        gwf,
        head_filerecord=f"{gwf_name}.hds",
        budget_filerecord=f"{gwf_name}.cbc",
        saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")],
        filename=f"{gwf_name}.oc",
    )

    manifest = DeckManifest(
        sim_dir=str(sim_dir),
        sim_name=sim_name,
        gwf_name=gwf_name,
        gwt_name="",  # GWF-only: no transport model
        model_crs=f"EPSG:{crs.to_epsg()}",
        xorigin=xorigin,
        yorigin=yorigin,
        nrow=nrow,
        ncol=ncol,
        nlay=N_LAYERS,
        delr=delr,
        delc=delc,
        # The spill cell fields are unused for GWF-only archetypes; carry the
        # grid centre so the manifest stays well-formed (not prose).
        spill_row=nrow // 2,
        spill_col=ncol // 2,
        spill_easting_m=xorigin + (ncol // 2 + 0.5) * delr,
        spill_northing_m=(yorigin + nrow * delc) - (nrow // 2 + 0.5) * delc,
        spill_lat=lat,
        spill_lon=lon,
        mass_rate_g_per_day=0.0,
        release_rate_kg_s=0.0,
        duration_days=0.0,
        n_transport_steps=0,
        contaminant="",
        aquifer_k_ms=aquifer_k_ms,
        porosity=porosity,
        archetype=archetype,
        gwt_present=False,
        transient=transient,
        n_stress_periods=n_stress_periods,
        n_transient_periods=n_transient_periods,
        well_row=well_row,
        well_col=well_col,
        well_easting_m=float(well_east),
        well_northing_m=float(well_north),
        well_lat=float(well_lat),
        well_lon=float(well_lon),
        pumping_rate_m3_day=pump_rate,
        aquifer_sy=sy if transient else 0.0,
        aquifer_ss=ss if transient else 0.0,
        drain_cell_count=drain_cell_count,
        drain_elevation_m=drain_elev_written,
        drain_conductance_m2_day=drain_cond_written,
        npf_icelltype=npf_icelltype,
        zone_partition=zone_partition_written,
        n_zones=n_zones,
        # --- Wave-2 archetypes ---------------------------------------------- #
        recharge_cell_count=recharge_cell_count,
        infiltration_rate_m_day=infiltration_rate_written,
        recharge_active_periods=recharge_active_periods,
        injection_rate_m3_day=injection_rate_written,
        recovery_rate_m3_day=recovery_rate_written,
        n_cycles=asr_n_cycles_written,
        injection_periods=asr_injection_periods,
        recovery_periods=asr_recovery_periods,
        wetland_cell_count=wetland_cell_count,
        et_surface_m=et_surface_written,
        et_max_rate_m_day=et_max_rate_written,
        et_extinction_depth_m=et_extinction_written,
        newton_under_relaxation=use_newton,
        # --- module wave: stream_depletion SFR --------------------------------- #
        sfr_present=sfr_present,
        n_reaches=sfr_n_reaches,
        sfr_reach_cells=sfr_reach_cells_meta,
        sfr_inflow_m3_day=sfr_inflow_written,
        sfr_width_m=sfr_width_written,
        sfr_streambed_k_m_day=sfr_streambed_k_written,
        sfr_manning_n=sfr_manning_written,
        # --- module wave: land_subsidence CSUB --------------------------------- #
        csub_present=csub_present,
        n_interbeds=csub_n_interbeds,
        csub_interbed_cells=csub_interbed_cells_meta,
        csub_ssv_inelastic_m=csub_ssv_written,
        csub_sse_elastic_m=csub_sse_written,
        csub_interbed_thick_frac=csub_thick_frac_written,
    )

    if write:
        sim.write_simulation()
        manifest.files = sorted(
            str(p.relative_to(sim_dir)) for p in sim_dir.rglob("*") if p.is_file()
        )
    return manifest


def _fill_polygon_cells(
    boundary_cells: list[tuple[int, int]], *, nrow: int, ncol: int
) -> list[tuple[int, int]]:
    """Fill a draped polygon boundary into all interior (row, col) cells.

    Given the set of grid cells the polygon BOUNDARY traverses, return every cell
    inside-or-on the polygon via a per-row span fill: for each row that the
    boundary touches, fill the columns between the min and max boundary column in
    that row (inclusive). This is a deterministic, dependency-free fill that is
    exact for convex footprints and a reasonable filled-hull for concave ones --
    adequate for a demo pit footprint draped onto a 40x40 grid. A single boundary
    cell (point/tiny pit) returns just that cell.

    Returned in (row, col) sorted order so the DRN records are deterministic.
    """
    if not boundary_cells:
        return []
    by_row: dict[int, list[int]] = {}
    for (r, c) in boundary_cells:
        by_row.setdefault(r, []).append(c)
    filled: set[tuple[int, int]] = set()
    for r, cols in by_row.items():
        cmin, cmax = min(cols), max(cols)
        for c in range(cmin, cmax + 1):
            if 0 <= r < nrow and 0 <= c < ncol:
                filled.add((r, c))
    return sorted(filled)


def _drape_footprint_to_cells(
    footprint_lonlat: list[tuple[float, float]],
    *,
    to_utm,
    xorigin: float,
    yorigin: float,
    delr: float,
    delc: float,
    nrow: int,
    ncol: int,
    skip_cols: set[int] | None = None,
) -> list[tuple[int, int]]:
    """Drape a (lon, lat) polygon footprint onto the filled set of in-grid cells.

    Shared by the MAR recharge-basin and wetland footprints (the same ring-close +
    boundary-drape + interior-fill the mine_dewatering pit uses). Projects the
    footprint to the deck's UTM grid, drapes its (closed) boundary onto cells,
    fills the interior, then drops any ``skip_cols`` (the CHD boundary columns).
    Returns the sorted ``(row, col)`` list. Pure (uses ``to_utm`` only).
    """
    verts_en = [to_utm.transform(plon, plat) for (plon, plat) in footprint_lonlat]
    ring = list(verts_en)
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(ring[0])  # close the ring so the boundary is continuous
    draped = _drape_polyline_onto_grid(
        ring,
        xorigin=xorigin,
        yorigin=yorigin,
        delr=delr,
        delc=delc,
        nrow=nrow,
        ncol=ncol,
    )
    cells = _fill_polygon_cells(
        [(r, c) for (r, c, _l) in draped], nrow=nrow, ncol=ncol
    )
    skip = skip_cols or set()
    return [(r, c) for (r, c) in cells if c not in skip]


def _build_zone_array(
    zone_partition: str, *, nrow: int, ncol: int
) -> tuple[list[list[int]], int]:
    """Build a per-cell zone-id array for the regional_water_budget partition.

    Returns ``(zone_array, n_zones)``. The only first-class scheme is
    ``"upgradient_downgradient"`` -- a two-zone west/east split across the regional
    CHD gradient (zone 1 = upgradient west half, zone 2 = downgradient east half).
    Any other (non-empty) string falls back to that same two-zone split so a named
    partition the adapter does not special-case still produces a usable array
    (the agent-side partition maps the label). Deterministic, pure.
    """
    mid = ncol // 2
    zone_array = [
        [1 if c < mid else 2 for c in range(ncol)] for _r in range(nrow)
    ]
    return zone_array, 2


# ---------------------------------------------------------------------------
# multi_species transport (sprint-18 Wave-3). One shared GWF flow field drives N
# independent ModflowGwt transport models (one per solute species), each coupled
# to the SHARED GWF by its own ModflowGwfgwt exchange. All N transport models +
# the GWF + every exchange live in ONE mfsim.nam and run in ONE mf6 invocation.
# ---------------------------------------------------------------------------


def _normalize_species(species: list) -> list[dict]:
    """Normalize a heterogeneous ``species`` list into validated plain dicts.

    Accepts either ``SpeciesSpec``-like objects (any object exposing the
    ``name`` / ``release_rate_kg_s`` / ``sorption_kd`` / ``decay_per_day`` /
    ``parent`` attributes) OR plain dicts with those keys. Returns one dict per
    species in input order with every key present (missing optionals -> None).

    Validates (engine determinism: fail loud, never silently drop a species):
      * every name is a non-empty string and UNIQUE within the list;
      * release_rate_kg_s >= 0 (0.0 allowed for a pure daughter product);
      * sorption_kd / decay_per_day, when set, are >= 0;
      * a ``parent`` (when set) names another species in the SAME list.
    """
    if not species:
        raise ValueError("multi_species requires a non-empty species list")

    def _get(s, key):
        if isinstance(s, dict):
            return s.get(key)
        return getattr(s, key, None)

    out: list[dict] = []
    seen: set[str] = set()
    for s in species:
        name = _get(s, "name")
        if not name or not str(name).strip():
            raise ValueError(f"species name must be a non-empty string, got {name!r}")
        name = str(name)
        if name in seen:
            raise ValueError(f"duplicate species name in species list: {name!r}")
        seen.add(name)
        rate = _get(s, "release_rate_kg_s")
        if rate is None:
            raise ValueError(f"species {name!r} missing release_rate_kg_s")
        rate = float(rate)
        if rate < 0.0:
            raise ValueError(
                f"species {name!r} release_rate_kg_s must be >= 0, got {rate!r}"
            )
        kd = _get(s, "sorption_kd")
        if kd is not None and float(kd) < 0.0:
            raise ValueError(f"species {name!r} sorption_kd must be >= 0, got {kd!r}")
        decay = _get(s, "decay_per_day")
        if decay is not None and float(decay) < 0.0:
            raise ValueError(
                f"species {name!r} decay_per_day must be >= 0, got {decay!r}"
            )
        out.append(
            {
                "name": name,
                "release_rate_kg_s": rate,
                "sorption_kd": float(kd) if kd is not None else None,
                "decay_per_day": float(decay) if decay is not None else None,
                "parent": _get(s, "parent"),
            }
        )
    # Parent references must resolve to a species in the same list.
    for spec in out:
        parent = spec["parent"]
        if parent is not None and str(parent) not in seen:
            raise ValueError(
                f"species {spec['name']!r} parent {parent!r} not found in species list"
            )
    return out


# MF6 enforces a HARD 16-character limit on MODELNAME: a longer name aborts the
# whole simulation at namefile-write time (caught with a real 3-species mf6 run --
# "Vinyl Chloride" -> "gwt_vinyl_chloride" is 18 chars and killed the run). Every
# per-species GWT model name MUST stay <= this.
MF6_MODELNAME_MAXLEN = 16


def _gwt_model_name_for_species(name: str) -> str:
    """Map a species name to a filesystem-safe, length-bounded GWT model name.

    MF6 model names must be a safe token (no spaces/punctuation that would break
    the namefile or the per-model file stems) AND <= 16 chars (``MF6_MODELNAME_MAXLEN``;
    an overflow aborts the entire multi-species sim at write). We lower-case,
    replace any non-alphanumeric run with a single underscore, prefix ``gwt_``,
    and -- when the result would exceed 16 chars -- truncate the stem and append a
    short deterministic hash of the FULL sanitised stem, so two long names that
    share a 16-char prefix still get distinct, reproducible model names. The
    transform is a pure function of the species name, so the postprocess side
    mirrors it (``postprocess_modflow._sanitise_species_to_stem``) to map a
    ``gwt_<species>.ucn`` file back to the user's species label BY VALUE.

    The species list is name-unique (``_normalize_species``) but two names could
    collide after sanitisation/truncation; the caller de-duplicates the resulting
    model names (length-aware, see ``_with_dedup_suffix``) so each GWT gets a
    unique stem.
    """
    safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(name))
    safe = "_".join(part for part in safe.split("_") if part) or "species"  # collapse runs
    candidate = f"gwt_{safe}"
    if len(candidate) <= MF6_MODELNAME_MAXLEN:
        return candidate
    # Truncate the stem + disambiguate with a 4-hex hash of the full stem so
    # distinct long names never collide after truncation: 4 ("gwt_") + 7 + 1
    # ("_") + 4 (hash) = 16.
    digest = hashlib.sha1(safe.encode("utf-8")).hexdigest()[:4]
    head_budget = MF6_MODELNAME_MAXLEN - len("gwt_") - 1 - len(digest)
    return f"gwt_{safe[:head_budget]}_{digest}"


def _with_dedup_suffix(base: str, suffix: int) -> str:
    """Append a numeric de-dup ``_<suffix>`` while keeping the name <= 16 chars.

    Two distinct species names can collapse to the same sanitised/truncated GWT
    model name; the caller disambiguates with a numeric suffix. The suffix must
    not push the name back over MF6's 16-char MODELNAME limit, so we truncate the
    base to make room for the tag.
    """
    tag = f"_{suffix}"
    head = base[: MF6_MODELNAME_MAXLEN - len(tag)]
    return f"{head}{tag}"


def _build_multi_species_deck(
    *,
    species: list,
    lat: float,
    lon: float,
    crs,
    to_utm,
    xorigin: float,
    yorigin: float,
    nrow: int,
    ncol: int,
    delr: float,
    delc: float,
    k_m_per_day: float,
    aquifer_k_ms: float,
    porosity: float,
    duration_days: float,
    spill_row: int,
    spill_col: int,
    spill_cell_east: float,
    spill_cell_north: float,
    sim_dir: Path,
    sim_name: str,
    gwf_name: str,
    write: bool,
    save_concentration_all_steps: bool,
    # constitutive advanced-physics (levers STEP 3): resolved dict; regional_gradient
    # is the only lever this GWF+GWT archetype reads. None/{} => byte-identical.
    advanced_physics: dict | None = None,
) -> DeckManifest:
    """Assemble a multi_species GWF + N-GWT deck (ONE shared flow field, N plumes).

    Builds ONE steady-state GWF flow model (the EXACT same 40x40x50 m UTM grid +
    west->east REGIONAL_GRADIENT CHD as the single-species spill deck) and, for
    EACH species, a complete ModflowGwt transport model
    (DIS/IC/ADV(TVD)/DSP/MST/SRC/SSM/OC) named ``gwt_<species>`` plus a
    ModflowGwfgwt exchange linking the shared GWF to that species' GWT. Each
    species' SRC injects its own ``release_rate_kg_s`` (-> g/day) at the shared
    spill cell, and its OC writes ``gwt_<species>.ucn`` (a per-species
    CONCENTRATION HeadFile the postprocess globs and reads per species).

    Parent->daughter decay chains: the ``parent`` field is RECORDED on the
    manifest (``species_with_parent``) but the species-to-species mass-ingrowth
    coupling is NOT yet wired -- MF6's ``GWT6-GWT6`` exchange couples two GWT
    models across a SPATIAL grid interface (domain decomposition), not chemical
    parent->daughter ingrowth on a shared grid, so writing one would NOT model
    the decay chain. Each species therefore transports independently (its own
    first-order decay removes mass; the daughter is sourced only by its own SRC).
    ``decay_chain_coupled`` stays False until a real ingrowth coupling lands;
    ``n_gwtgwt_exchanges`` is 0. This is the honest "independent species first"
    path the Wave-3 kickoff sanctions.
    """
    specs = _normalize_species(species)

    # Per-species transport step count tracks duration exactly like the spill deck.
    n_transport_steps = int(max(1, min(round(duration_days), 365)))
    conc_save = "ALL" if save_concentration_all_steps else "LAST"

    sim = flopy.mf6.MFSimulation(
        sim_name=sim_name,
        sim_ws=str(sim_dir),
        exe_name="mf6",
        version="mf6",
    )
    # Two periods (steady-state flow spin-up + transient transport), identical to
    # the single-species spill deck so the SRC stays off during spin-up.
    flopy.mf6.ModflowTdis(
        sim,
        time_units=TIME_UNITS,
        nper=2,
        perioddata=[
            (1.0, 1, 1.0),
            (float(duration_days), n_transport_steps, 1.0),
        ],
    )

    # --- Shared GWF flow model (one per simulation) -------------------------- #
    ims_gwf = flopy.mf6.ModflowIms(
        sim,
        filename=f"{gwf_name}.ims",
        complexity="SIMPLE",
        outer_dvclose=1e-6,
        inner_dvclose=1e-6,
        linear_acceleration="CG",
    )
    gwf = flopy.mf6.ModflowGwf(
        sim,
        modelname=gwf_name,
        model_nam_file=f"{gwf_name}.nam",
        save_flows=True,
    )
    sim.register_ims_package(ims_gwf, [gwf_name])

    flopy.mf6.ModflowGwfdis(
        gwf,
        length_units=LENGTH_UNITS,
        nlay=N_LAYERS,
        nrow=nrow,
        ncol=ncol,
        delr=delr,
        delc=delc,
        top=AQUIFER_TOP_M,
        botm=AQUIFER_BOTTOM_M,
        xorigin=xorigin,
        yorigin=yorigin,
        filename=f"{gwf_name}.dis",
    )
    try:
        gwf.modelgrid.set_coord_info(xoff=xorigin, yoff=yorigin, crs=crs.to_epsg())
    except Exception:  # pragma: no cover - older flopy signature fallback
        pass

    # regional_gradient: CONSTITUTIVE lever (default EQUALS REGIONAL_GRADIENT ->
    # byte-identical when unset; same phys.get seam as the spill deck).
    phys = dict(advanced_physics or {})
    domain_width_m = ncol * delr
    head_west = AQUIFER_TOP_M + float(
        phys.get("regional_gradient", REGIONAL_GRADIENT)
    ) * domain_width_m
    head_east = AQUIFER_TOP_M
    flopy.mf6.ModflowGwfic(gwf, strt=head_west, filename=f"{gwf_name}.ic")
    flopy.mf6.ModflowGwfnpf(
        gwf,
        save_flows=True,
        icelltype=0,
        k=k_m_per_day,
        filename=f"{gwf_name}.npf",
    )
    chd_records = []
    for r in range(nrow):
        chd_records.append([(0, r, 0), head_west])
        chd_records.append([(0, r, ncol - 1), head_east])
    flopy.mf6.ModflowGwfchd(
        gwf,
        stress_period_data={0: chd_records, 1: chd_records},
        filename=f"{gwf_name}.chd",
    )
    flopy.mf6.ModflowGwfoc(
        gwf,
        head_filerecord=f"{gwf_name}.hds",
        budget_filerecord=f"{gwf_name}.cbc",
        saverecord=[("HEAD", "LAST"), ("BUDGET", "LAST")],
        filename=f"{gwf_name}.oc",
    )

    # --- One ModflowGwt + one ModflowGwfgwt exchange per species ------------- #
    species_names: list[str] = []
    gwt_model_names: list[str] = []
    species_ucn_files: list[str] = []
    species_with_parent: list[str] = []
    used_model_names: set[str] = set()

    for spec in specs:
        sp_name = spec["name"]
        gwt_name = _gwt_model_name_for_species(sp_name)
        # De-duplicate sanitised model names (distinct species names that collapse
        # to the same token get a numeric suffix so each GWT keeps a unique stem).
        # Length-aware: the suffix must not push the name over MF6's 16-char limit.
        if gwt_name in used_model_names:
            suffix = 2
            candidate = _with_dedup_suffix(gwt_name, suffix)
            while candidate in used_model_names:
                suffix += 1
                candidate = _with_dedup_suffix(gwt_name, suffix)
            gwt_name = candidate
        used_model_names.add(gwt_name)

        ims_gwt = flopy.mf6.ModflowIms(
            sim,
            filename=f"{gwt_name}.ims",
            complexity="MODERATE",
            outer_dvclose=1e-6,
            inner_dvclose=1e-6,
            linear_acceleration="BICGSTAB",
        )
        gwt = flopy.mf6.ModflowGwt(
            sim,
            modelname=gwt_name,
            model_nam_file=f"{gwt_name}.nam",
            save_flows=True,
        )
        sim.register_ims_package(ims_gwt, [gwt_name])

        flopy.mf6.ModflowGwtdis(
            gwt,
            length_units=LENGTH_UNITS,
            nlay=N_LAYERS,
            nrow=nrow,
            ncol=ncol,
            delr=delr,
            delc=delc,
            top=AQUIFER_TOP_M,
            botm=AQUIFER_BOTTOM_M,
            xorigin=xorigin,
            yorigin=yorigin,
            filename=f"{gwt_name}.dis",
        )
        flopy.mf6.ModflowGwtic(gwt, strt=0.0, filename=f"{gwt_name}.ic")
        flopy.mf6.ModflowGwtadv(gwt, scheme="TVD", filename=f"{gwt_name}.adv")
        flopy.mf6.ModflowGwtdsp(
            gwt,
            alh=LONGITUDINAL_DISPERSIVITY_M,
            ath1=LONGITUDINAL_DISPERSIVITY_M * TRANSVERSE_HORIZONTAL_RATIO,
            atv=LONGITUDINAL_DISPERSIVITY_M * TRANSVERSE_VERTICAL_RATIO,
            filename=f"{gwt_name}.dsp",
        )

        # Per-species MST: porosity (shared aquifer) + optional per-species linear
        # sorption (Kd) and first-order decay. decay_sorbed is required by MF6 when
        # BOTH decay AND sorption are active (the Wave-1 DECAY_SORBED bugfix), and
        # defaults to the aqueous decay rate.
        mst_kwargs: dict = {"porosity": porosity, "filename": f"{gwt_name}.mst"}
        kd = spec["sorption_kd"]
        sorption_active = kd is not None and float(kd) > 0.0
        if sorption_active:
            mst_kwargs["sorption"] = "LINEAR"
            mst_kwargs["distcoef"] = float(kd)
            mst_kwargs["bulk_density"] = 1600.0
        decay = spec["decay_per_day"]
        decay_active = decay is not None and float(decay) > 0.0
        if decay_active:
            mst_kwargs["first_order_decay"] = True
            mst_kwargs["decay"] = float(decay)
            if sorption_active:
                mst_kwargs["decay_sorbed"] = float(decay)
        flopy.mf6.ModflowGwtmst(gwt, **mst_kwargs)

        # Per-species mass-loading source at the SHARED spill cell, active only in
        # the transient transport period (period 1, 0-based) -- the same off-in-
        # spin-up pattern as the single-species deck. A pure daughter product with
        # release_rate_kg_s == 0.0 writes a zero-rate SRC record (a real, but empty,
        # source so the deck shape is uniform across species).
        sp_mass_rate_g_per_day = spec["release_rate_kg_s"] * KG_TO_G * SECONDS_PER_DAY
        src_record = [[(0, spill_row, spill_col), sp_mass_rate_g_per_day]]
        flopy.mf6.ModflowGwtsrc(
            gwt,
            stress_period_data={0: [], 1: src_record},
            filename=f"{gwt_name}.src",
        )
        flopy.mf6.ModflowGwtssm(
            gwt,
            sources=None,
            filename=f"{gwt_name}.ssm",
        )
        flopy.mf6.ModflowGwtoc(
            gwt,
            concentration_filerecord=f"{gwt_name}.ucn",
            budget_filerecord=f"{gwt_name}.cbc",
            saverecord=[("CONCENTRATION", conc_save), ("BUDGET", "LAST")],
            filename=f"{gwt_name}.oc",
        )

        # The flow<->transport exchange linking the SHARED GWF to THIS species' GWT.
        flopy.mf6.ModflowGwfgwt(
            sim,
            exgtype="GWF6-GWT6",
            exgmnamea=gwf_name,
            exgmnameb=gwt_name,
            filename=f"gwfgwt_{gwt_name}.exg",
        )

        species_names.append(sp_name)
        gwt_model_names.append(gwt_name)
        species_ucn_files.append(f"{gwt_name}.ucn")
        if spec["parent"] is not None:
            species_with_parent.append(sp_name)

    # The headline spill mass-rate carried into the manifest is the FIRST species'
    # rate (the manifest's single mass_rate_g_per_day field is a per-deck scalar;
    # the full per-species rates are reconstructable from species_ucn_files order).
    headline_rate_g_per_day = specs[0]["release_rate_kg_s"] * KG_TO_G * SECONDS_PER_DAY

    manifest = DeckManifest(
        sim_dir=str(sim_dir),
        sim_name=sim_name,
        gwf_name=gwf_name,
        # gwt_name is the FIRST species' GWT model (the manifest single-GWT field);
        # the full per-species list is gwt_model_names.
        gwt_name=gwt_model_names[0],
        model_crs=f"EPSG:{crs.to_epsg()}",
        xorigin=xorigin,
        yorigin=yorigin,
        nrow=nrow,
        ncol=ncol,
        nlay=N_LAYERS,
        delr=delr,
        delc=delc,
        spill_row=spill_row,
        spill_col=spill_col,
        spill_easting_m=spill_cell_east,
        spill_northing_m=spill_cell_north,
        spill_lat=lat,
        spill_lon=lon,
        mass_rate_g_per_day=headline_rate_g_per_day,
        release_rate_kg_s=specs[0]["release_rate_kg_s"],
        duration_days=float(duration_days),
        n_transport_steps=n_transport_steps,
        contaminant=species_names[0],
        aquifer_k_ms=aquifer_k_ms,
        porosity=porosity,
        archetype="multi_species",
        gwt_present=True,
        multi_species=True,
        species_names=species_names,
        species_ucn_files=species_ucn_files,
        gwt_model_names=gwt_model_names,
        n_gwfgwt_exchanges=len(gwt_model_names),
        n_gwtgwt_exchanges=0,
        species_with_parent=species_with_parent,
        decay_chain_coupled=False,
    )

    if write:
        sim.write_simulation()
        manifest.files = sorted(
            str(p.relative_to(sim_dir)) for p in sim_dir.rglob("*") if p.is_file()
        )
    return manifest


def _build_prt_capture_zone_deck(
    *,
    archetype: str,
    lat: float,
    lon: float,
    crs,
    to_utm,
    k_m_per_day: float,
    aquifer_k_ms: float,
    porosity: float,
    sim_dir: Path,
    sim_name: str,
    gwf_name: str,
    write: bool,
    well_location_latlon: tuple[float, float] | None,
    pumping_rate_m3_day: float | None,
    n_particles: int,
    capture_zone_travel_time_years: list[float] | None,
    # constitutive advanced-physics (levers STEP 3): resolved dict; regional_gradient
    # is the only lever this GWF-only PRT archetype reads. None/{} => byte-identical.
    advanced_physics: dict | None = None,
) -> DeckManifest:
    """Assemble the STEADY GWF deck for a PRT capture-zone or wellhead-protection run.

    This function builds and optionally writes the GWF-only part of the two-sim
    PRT backward-tracking workflow.  The GWF model is built at LOCAL (0,0) origin
    to avoid the mf6 6.7.0 eager-coordinate-check float-precision bug with large
    UTM origins, and because ``CellBudgetFile.reverse()`` drops the grid origin
    so particle coordinates come out in local space anyway. The true UTM origin
    (``xoffset_m``, ``yoffset_m``) is stored in the manifest so the postprocess
    step can translate back to real coordinates at polygon-export time.

    The grid uses ``PRT_CELL_SIZE_M`` (100 m) cells and ``PRT_DOMAIN_HALF_WIDTH_M``
    (2050 m) half-width -> a 41 x 41 cell, 4100 x 4100 m domain, matching the
    proven script at ``/tmp/prt_capture_zone/run_prt_capture.py``.

    NPF MUST declare ``save_flows=True, save_specific_discharge=True,
    save_saturation=True`` -- all three are required for ``ModflowPrtfmi`` to find
    the SPDIS and SATURATION terms in the .cbc when it reads the reversed budget.

    The well WEL extraction is placed at the grid centre (or the caller-supplied
    ``well_location_latlon`` snapped to the nearest in-grid cell) and is active in
    the single steady period.

    Args:
        archetype:   ``'capture_zone'`` or ``'wellhead_protection'``.
        lat, lon:    AOI centre (EPSG:4326).
        crs:         pyproj CRS of the model UTM zone.
        to_utm:      pyproj Transformer EPSG:4326 -> ``crs`` (always_xy=True).
        k_m_per_day: hydraulic conductivity in m/day (already converted from
                     ``aquifer_k_ms``).
        aquifer_k_ms: raw K in m/s (for manifest).
        porosity:    effective porosity (controls PRT travel time).
        sim_dir:     absolute path where the GWF deck is written.
        sim_name:    MF6 simulation name (``mfsim`` by default).
        gwf_name:    GWF model name (``gwf_model``).
        write:       if True, call ``sim.write_simulation()``.
        well_location_latlon: optional (lat, lon) of the pumping well. When None
                     the well is placed at the grid centre (AOI point).
        pumping_rate_m3_day: POSITIVE extraction magnitude (m^3/day). The adapter
                     applies the MF6 WEL sign internally (negative = extraction).
                     Defaults to ``DEFAULT_PRT_PUMPING_RATE_M3_DAY`` when None.
        n_particles: number of particles in the release ring.
        capture_zone_travel_time_years: isochrone cutoff times (years). When None,
                     defaults are archetype-specific (``_default_travel_time_years``):
                     ``capture_zone`` -> [1, 5, 10]; ``wellhead_protection`` ->
                     [2, 5, 10] (EPA WHPA tiers).

    Returns:
        DeckManifest with ``prt_present=True``, ``well_row``, ``well_col``,
        ``n_particles``, ``capture_zone_travel_time_years``, ``xoffset_m``,
        ``yoffset_m``, and ``model_utm_epsg`` populated. All other manifest
        fields are at their defaults (byte-identical to the existing archetypes
        for callers that read only those fields).
    """
    # ---------------------------------------------------------------------- #
    # Grid sizing: 41x41 cells at 100 m -> 4100 x 4100 m domain (local origin).
    # PRT_DOMAIN_HALF_WIDTH_M is 2050 m so the well at grid centre has a full
    # 2050 m radius to the CHD boundary in every direction.
    # ---------------------------------------------------------------------- #
    ncol = int(round(2 * PRT_DOMAIN_HALF_WIDTH_M / PRT_CELL_SIZE_M))  # 41
    nrow = ncol
    delr = PRT_CELL_SIZE_M
    delc = PRT_CELL_SIZE_M

    # True UTM coordinates of the AOI centre (the well target).
    aoi_east, aoi_north = to_utm.transform(lon, lat)

    # The GWF grid is built at LOCAL (0,0) origin. The true UTM lower-left
    # corner of the grid is (aoi_east - half, aoi_north - half). We store it
    # on the manifest so postprocess can translate particle tracks back to UTM.
    xoffset_m = aoi_east - PRT_DOMAIN_HALF_WIDTH_M
    yoffset_m = aoi_north - PRT_DOMAIN_HALF_WIDTH_M
    model_utm_epsg = crs.to_epsg()

    # ---------------------------------------------------------------------- #
    # Well cell: snap the caller-supplied lat/lon to the LOCAL grid, or use the
    # grid centre when no well location is given.
    # ---------------------------------------------------------------------- #
    pump_rate = -abs(float(pumping_rate_m3_day)) if pumping_rate_m3_day is not None \
        else -abs(DEFAULT_PRT_PUMPING_RATE_M3_DAY)

    if well_location_latlon is not None:
        wlat, wlon = float(well_location_latlon[0]), float(well_location_latlon[1])
        if not (-90.0 <= wlat <= 90.0) or not (-180.0 <= wlon <= 180.0):
            raise ValueError(
                f"well_location_latlon out of range for capture_zone: {(wlat, wlon)!r}"
            )
        well_east_true, well_north_true = to_utm.transform(wlon, wlat)
        # Convert to LOCAL grid coordinates (0-origin) for the cell index.
        well_east_local = well_east_true - xoffset_m
        well_north_local = well_north_true - yoffset_m
        col = int(well_east_local // delr)
        north_top_local = nrow * delc  # local grid top (no yorigin offset)
        row = int((north_top_local - well_north_local) // delc)
        # Clamp to interior cells (never the CHD boundary columns).
        well_row = max(1, min(nrow - 2, row))
        well_col = max(1, min(ncol - 2, col))
        well_lat, well_lon_val = wlat, wlon
    else:
        # Centre of the 41x41 local grid.
        well_row = nrow // 2  # 20 for a 41-cell grid
        well_col = ncol // 2
        well_lat, well_lon_val = lat, lon

    # True UTM coordinates of the chosen well cell centre (for manifest).
    # cell_centre_local_x = (well_col + 0.5) * delr
    # cell_centre_local_y = (nrow - well_row - 0.5) * delc (flopy row-0 = north)
    well_east_m = xoffset_m + (well_col + 0.5) * delr
    well_north_m = yoffset_m + (nrow - well_row - 0.5) * delc

    back_to_4326 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    _wlon, _wlat = back_to_4326.transform(well_east_m, well_north_m)
    well_lat, well_lon_val = _wlat, _wlon

    # ---------------------------------------------------------------------- #
    # PRT/isochrone parameters.
    # ---------------------------------------------------------------------- #
    tz_years = (
        list(capture_zone_travel_time_years)
        if capture_zone_travel_time_years
        else _default_travel_time_years(archetype)
    )

    # ---------------------------------------------------------------------- #
    # GWF simulation (STEADY, single period of 1 day -> one time step).
    # ---------------------------------------------------------------------- #
    sim = flopy.mf6.MFSimulation(
        sim_name=sim_name,
        sim_ws=str(sim_dir),
        exe_name="mf6",
        version="mf6",
    )
    flopy.mf6.ModflowTdis(
        sim,
        time_units=TIME_UNITS,
        nper=1,
        perioddata=[(1.0, 1, 1.0)],
    )
    ims = flopy.mf6.ModflowIms(
        sim,
        filename=f"{gwf_name}.ims",
        complexity="SIMPLE",
        outer_dvclose=1e-8,
        inner_dvclose=1e-9,
        linear_acceleration="CG",
    )
    gwf = flopy.mf6.ModflowGwf(
        sim,
        modelname=gwf_name,
        model_nam_file=f"{gwf_name}.nam",
        save_flows=True,
    )
    sim.register_ims_package(ims, [gwf_name])

    # DIS: local (0,0) origin -- do NOT pass xorigin/yorigin.
    flopy.mf6.ModflowGwfdis(
        gwf,
        length_units=LENGTH_UNITS,
        nlay=N_LAYERS,
        nrow=nrow,
        ncol=ncol,
        delr=delr,
        delc=delc,
        top=PRT_AQUIFER_TOP_M,
        botm=PRT_AQUIFER_BOTTOM_M,
        filename=f"{gwf_name}.dis",
    )

    # IC: initial head = aquifer top (hydrostatic start, overridden by CHD).
    flopy.mf6.ModflowGwfic(gwf, strt=PRT_AQUIFER_TOP_M, filename=f"{gwf_name}.ic")

    # NPF: MUST set save_flows + save_specific_discharge + save_saturation.
    # Missing save_saturation -> FMI 'SATURATION NOT FOUND' error in PRT.
    flopy.mf6.ModflowGwfnpf(
        gwf,
        save_flows=True,
        save_specific_discharge=True,
        save_saturation=True,
        icelltype=0,  # confined: T = K * thickness (independent of head)
        k=k_m_per_day,
        filename=f"{gwf_name}.npf",
    )

    # CHD: west->east regional gradient (high head west, low head east).
    # Use the same REGIONAL_GRADIENT as all other archetypes; domain_width in
    # local coords is ncol * delr = 4100 m -> head drop = 0.002 * 4100 = 8.2 m.
    # regional_gradient: CONSTITUTIVE lever (default EQUALS REGIONAL_GRADIENT ->
    # byte-identical when unset; same phys.get seam as the spill deck).
    phys = dict(advanced_physics or {})
    domain_width_m = ncol * delr
    head_west = PRT_AQUIFER_TOP_M + float(
        phys.get("regional_gradient", REGIONAL_GRADIENT)
    ) * domain_width_m
    head_east = PRT_AQUIFER_TOP_M
    chd_records = []
    for r in range(nrow):
        chd_records.append([(0, r, 0), head_west])          # west boundary
        chd_records.append([(0, r, ncol - 1), head_east])   # east boundary
    flopy.mf6.ModflowGwfchd(
        gwf,
        stress_period_data={0: chd_records},
        filename=f"{gwf_name}.chd",
    )

    # WEL: pumping extraction at the well cell (active in the single period).
    flopy.mf6.ModflowGwfwel(
        gwf,
        stress_period_data={0: [[(0, well_row, well_col), pump_rate]]},
        save_flows=True,
        filename=f"{gwf_name}.wel",
        pname="wel-0",
    )

    # OC: save HEAD + BUDGET (ALL) -- required for CellBudgetFile.reverse().
    budget_filerecord = f"{gwf_name}.cbc"
    head_filerecord = f"{gwf_name}.hds"
    flopy.mf6.ModflowGwfoc(
        gwf,
        budget_filerecord=budget_filerecord,
        head_filerecord=head_filerecord,
        saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")],
        filename=f"{gwf_name}.oc",
    )

    manifest = DeckManifest(
        sim_dir=str(sim_dir),
        sim_name=sim_name,
        gwf_name=gwf_name,
        gwt_name="",  # no GWT block for capture_zone / wellhead_protection
        model_crs=f"EPSG:{model_utm_epsg}",
        xorigin=xoffset_m,  # true UTM lower-left easting (for manifest contract)
        yorigin=yoffset_m,  # true UTM lower-left northing
        nrow=nrow,
        ncol=ncol,
        nlay=N_LAYERS,
        delr=delr,
        delc=delc,
        # Spill cell: re-use for the well cell so the postprocess phase has the
        # well position in the same manifest fields as sustainable_yield.
        spill_row=well_row,
        spill_col=well_col,
        spill_easting_m=well_east_m,
        spill_northing_m=well_north_m,
        spill_lat=well_lat,
        spill_lon=well_lon_val,
        mass_rate_g_per_day=0.0,  # no contaminant source
        release_rate_kg_s=0.0,
        duration_days=0.0,
        n_transport_steps=0,
        contaminant="",
        aquifer_k_ms=aquifer_k_ms,
        porosity=porosity,
        # Archetype branch fields.
        archetype=archetype,
        gwt_present=False,
        transient=False,
        n_stress_periods=1,
        n_transient_periods=0,
        # Well info (reuse the sustainable_yield fields).
        well_row=well_row,
        well_col=well_col,
        well_easting_m=well_east_m,
        well_northing_m=well_north_m,
        well_lat=well_lat,
        well_lon=well_lon_val,
        pumping_rate_m3_day=pump_rate,
        # PRT-specific fields (Wave-4).
        prt_present=True,
        xoffset_m=xoffset_m,
        yoffset_m=yoffset_m,
        model_utm_epsg=model_utm_epsg,
        n_particles=n_particles,
        capture_zone_travel_time_years=tz_years,
    )

    if write:
        sim.write_simulation()
        manifest.files = sorted(
            str(p.relative_to(sim_dir)) for p in sim_dir.rglob("*") if p.is_file()
        )
    return manifest


def build_and_run_prt_from_gwf(
    deck: DeckManifest,
    gwf_run_dir: str | Path,
    mf6_bin: str,
) -> Path:
    """Reverse the GWF outputs, build + write + run the PRT sim, return its directory.

    This is the second half of the two-sim PRT capture-zone workflow. It is called
    AFTER ``mf6`` has been run on the GWF deck built by ``_build_prt_capture_zone_deck``.
    It performs four steps:

    1. Reverse the GWF ``.hds`` and ``.cbc`` files via
       ``flopy.utils.HeadFile.reverse()`` / ``CellBudgetFile.reverse()``.
    2. Build a PRT simulation (``ModflowPrt + ModflowPrtdis + ModflowPrtmip +
       ModflowPrtprp + ModflowPrtoc + ModflowPrtfmi + ModflowEms``) in a
       ``prt/`` subdirectory of ``gwf_run_dir``. The PRT sim reads the reversed
       GWF output files via ``ModflowPrtfmi`` (the MF6 Flow Model Interface
       package), which is the canonical USGS method for backward tracking
       (mf6 example ex-prt-mp7-p02).
    3. Writes the PRT simulation deck (``psim.write_simulation()``).
    4. Runs ``mf6`` (subprocess) in the PRT directory and asserts
       ``"Normal termination of simulation"`` in stdout.

    DESIGN NOTES:

    * PRT is EXPLICIT (no iterative solve). Do NOT register an IMS. Instead,
      register with ``ModflowEms`` (the explicit model solution) --
      ``psim.register_solution_package(ems, [prt.name])``.
    * ``ModflowPrtprp`` with a ``boundname`` column REQUIRES ``boundnames=True``.
    * ``extend_tracking=True`` is ESSENTIAL for steady-state: without it the
      particles stop tracking after the single stress period ends.
    * ``ModflowPrtoc`` with only ``trackcsv_filerecord`` (no ``saverecord``)
      avoids the spurious "BUDGET save file not specified" error.
    * ``ModflowPrtmip`` (porosity) is REQUIRED -- it drives travel time.

    Args:
        deck:       the ``DeckManifest`` returned by ``_build_prt_capture_zone_deck``.
                    Must have ``deck.prt_present == True``.
        gwf_run_dir: absolute path of the directory where ``mf6`` was run for the
                    GWF deck.  Must contain ``{gwf_name}.hds`` and
                    ``{gwf_name}.cbc`` after the GWF run.
        mf6_bin:    path to the ``mf6`` 6.7.0 binary.

    Returns:
        ``Path`` to the PRT working directory (``gwf_run_dir / 'prt'``). The
        directory contains ``{prt_name}.trk.csv`` (the particle track CSV), the
        binary track file, and all PRT input files.

    Raises:
        FileNotFoundError: if the required GWF output files are missing.
        RuntimeError:      if ``mf6`` does not emit ``"Normal termination"``.
    """
    if not deck.prt_present:
        raise ValueError("build_and_run_prt_from_gwf: deck.prt_present is False")

    gwf_dir = Path(gwf_run_dir)
    gwf_name = deck.gwf_name
    prt_name = "prtmodel"
    prt_ws = gwf_dir / "prt"
    prt_ws.mkdir(parents=True, exist_ok=True)

    # GWF output paths (in the GWF run directory).
    hds_path = gwf_dir / f"{gwf_name}.hds"
    cbc_path = gwf_dir / f"{gwf_name}.cbc"
    for p in (hds_path, cbc_path):
        if not p.exists():
            raise FileNotFoundError(
                f"build_and_run_prt_from_gwf: GWF output not found: {p}"
            )

    # Step 1: REVERSE the GWF head + budget files for backward tracking.
    # The canonical MF6 backward-tracking pattern (ex-prt-mp7-p02): forward GWF
    # run, then temporally reverse its outputs, then forward-track through the
    # reversed field -> particles propagate up-gradient (backward in physical time).
    hds_rev_path = gwf_dir / f"{gwf_name}.hds.rev"
    cbc_rev_path = gwf_dir / f"{gwf_name}.cbc.rev"

    # Load with tdis so the reversal gets the correct time discretisation.
    # We re-load the GWF sim from disk to obtain the tdis object.
    gsim_reload = flopy.mf6.MFSimulation.load(
        sim_ws=str(gwf_dir),
        sim_name=deck.sim_name,
        exe_name=mf6_bin,
    )
    cbb = CellBudgetFile(str(cbc_path), tdis=gsim_reload.tdis)
    cbb.reverse(str(cbc_rev_path))
    hds_obj = HeadFile(str(hds_path), tdis=gsim_reload.tdis)
    hds_obj.reverse(str(hds_rev_path))

    # Step 2: build the PRT simulation (separate sim, reads reversed GWF via FMI).
    psim = flopy.mf6.MFSimulation(
        sim_name="prt",
        sim_ws=str(prt_ws),
        exe_name=mf6_bin,
        version="mf6",
    )
    # TDIS: mirror the GWF (1 period, 1 step, 1 day). The PRT sim steps through
    # the REVERSED flow field, which has the same time discretisation.
    flopy.mf6.ModflowTdis(
        psim,
        time_units=TIME_UNITS,
        nper=1,
        perioddata=[(1.0, 1, 1.0)],
    )

    # Create the PRT model.
    prt = flopy.mf6.ModflowPrt(psim, modelname=prt_name)

    # DIS: must mirror the GWF grid exactly (same dimensions + local-origin geometry).
    flopy.mf6.ModflowPrtdis(
        prt,
        nlay=deck.nlay,
        nrow=deck.nrow,
        ncol=deck.ncol,
        delr=deck.delr,
        delc=deck.delc,
        top=PRT_AQUIFER_TOP_M,
        botm=PRT_AQUIFER_BOTTOM_M,
        length_units=LENGTH_UNITS,
        filename=f"{prt_name}.dis",
    )

    # MIP: porosity REQUIRED -- drives travel-time calculation.
    flopy.mf6.ModflowPrtmip(prt, porosity=deck.porosity, filename=f"{prt_name}.mip")

    # Build the particle release ring at the well cell (local coordinates).
    # The GWF grid is at local (0,0) so cell centres are in local space.
    # local grid: row 0 = northernmost row; ycellcenter for row r = (nrow-r-0.5)*delc.
    wi = deck.well_row
    wj = deck.well_col
    cx = (wj + 0.5) * deck.delr              # local x of well cell centre
    cy = (deck.nrow - wi - 0.5) * deck.delc  # local y of well cell centre
    n_ring = deck.n_particles
    angles = np.linspace(0, 2 * np.pi, n_ring, endpoint=False)
    radius = DEFAULT_PRT_RING_RADIUS_M  # 30 m (inside the 100 m cell)
    zrpt = (PRT_AQUIFER_TOP_M + PRT_AQUIFER_BOTTOM_M) / 2.0  # mid-aquifer depth

    releasepts = []
    for n, a in enumerate(angles):
        xrpt = cx + radius * np.cos(a)
        yrpt = cy + radius * np.sin(a)
        # PRP packagedata tuple: (irptno, (k,i,j), x, y, z, boundname)
        releasepts.append((n, (0, wi, wj), xrpt, yrpt, zrpt, f"p{n}"))

    trackbin = f"{prt_name}.trk"
    trackcsv = f"{prt_name}.trk.csv"

    flopy.mf6.ModflowPrtprp(
        prt,
        pname="prp1",
        nreleasepts=len(releasepts),
        packagedata=releasepts,
        boundnames=True,             # REQUIRED: packagedata has a boundname column
        perioddata={0: ["FIRST"]},   # release once at start of the (reversed) period
        extend_tracking=True,        # ESSENTIAL for steady state: keep tracking
        exit_solve_tolerance=1e-5,
        filename=f"{prt_name}.prp",
    )

    # OC: track binary + CSV. Do NOT pass saverecord to avoid the
    # "BUDGET save file not specified" error (no budget_filerecord set).
    flopy.mf6.ModflowPrtoc(
        prt,
        track_filerecord=trackbin,
        trackcsv_filerecord=trackcsv,
        filename=f"{prt_name}.oc",
    )

    # FMI: point PRT at the REVERSED GWF head + budget files (absolute paths).
    pd = [
        ("GWFHEAD", str(hds_rev_path.resolve())),
        ("GWFBUDGET", str(cbc_rev_path.resolve())),
    ]
    flopy.mf6.ModflowPrtfmi(prt, packagedata=pd, filename=f"{prt_name}.fmi")

    # PRT is EXPLICIT: register with ModflowEms, NOT IMS.
    ems = flopy.mf6.ModflowEms(psim, filename=f"{prt_name}.ems")
    psim.register_solution_package(ems, [prt.name])

    # Step 3: write the PRT deck.
    psim.write_simulation()

    # Step 4: run mf6 and assert normal termination.
    proc = subprocess.run(
        [mf6_bin],
        cwd=str(prt_ws),
        capture_output=True,
        text=True,
    )
    stdout = proc.stdout or ""
    if "Normal termination of simulation" not in stdout:
        raise RuntimeError(
            f"PRT mf6 run did not terminate normally (returncode={proc.returncode}):\n"
            + stdout[-2000:]
        )

    return prt_ws


def _build_saltwater_intrusion_deck(
    *,
    # Transect endpoints (A = seaward, B = inland) in (lat, lon) order.
    # When None the seaward endpoint is placed one grid-length west of the
    # spill point -- the deck is still physically valid; the agent must supply
    # real endpoints for a geolocated postprocess cross-section.
    coastal_transect_latlon: tuple[tuple[float, float], tuple[float, float]] | None,
    # Grid geometry: n_vertical_layers is caller-supplied; ncol + delr derive
    # from the standard field-scale defaults. delv is set so the total aquifer
    # depth is nlay * delv.
    n_vertical_layers: int,
    # Hydraulic conductivity (m/day) -- the same value as the spill/PRT decks
    # but applied to the confined (icelltype=0) vertical slice.
    k_m_per_day: float,
    aquifer_k_ms: float,
    porosity: float,
    seawater_salinity_ppt: float,
    freshwater_inflow_m3_day: float | None,
    sim_dir: Path,
    sim_name: str,
    gwf_name: str,
    write: bool,
) -> DeckManifest:
    """Build a Henry-style variable-density saltwater-intrusion deck (Wave-5).

    Constructs ONE ``MFSimulation`` containing:

    - A GWF flow model with the BUY (buoyancy) package for variable-density
      flow. The grid is a vertical ``nrow=1`` Henry slice (``nlay`` layers deep,
      ``ncol`` columns wide). The seaward boundary (last column, all layers) is
      a GHB with an AUX ``CONCENTRATION`` column set to ``seawater_salinity_ppt``
      (salt enters when sea head exceeds aquifer head). The inland boundary
      (first column, all layers) is a WEL with an AUX ``CONCENTRATION=0``
      (fresh water inflow).

    - A GWT solute-transport model that advects salinity (PPT) with
      ``ADV UPSTREAM``, molecular diffusion (``DSP xt3d_off diffc``), and mass
      storage (``MST porosity``). The initial condition is ``strt=csalt`` (start
      fully salty -- Henry convention; fresh inflow displaces to equilibrium).
      ``SSM`` links the GHB-1 and WEL-1 AUX columns to transport so the
      seaward boundary injects salt and the inland boundary injects fresh water.

    - A ``ModflowGwfgwt`` flow-transport exchange coupling the two models.

    - TWO separate IMS solvers: GWF IMS registered FIRST (MF6 hard requirement
      when BUY is present). The GWF IMS uses ``BICGSTAB`` and ``relaxation_factor
      =0.97`` to handle the nonlinear density-dependent flow system.

    Salinity is carried in PPT (0 = fresh, ``seawater_salinity_ppt`` = sea
    water). The BUY ``drhodc = (1025 - 1000) / 35 = 0.714 kg/m3 per ppt`` so
    fresh water (0 ppt) has density ``denseref=1000 kg/m3`` and seawater (35 ppt)
    has density ~1025 kg/m3 -- the canonical Henry EOS.

    The headline scalar is the **intrusion length**: the most-inland distance (m)
    the bottom-layer 50%-isochlor (``0.5 * csalt``) penetrates from the seaward
    edge of the domain. A positive value means salt has intruded inland; zero
    means no wedge has formed (fresh aquifer).

    DeckManifest saltwater_intrusion fields written:
        ``saltwater_intrusion=True``, ``si_nlay``, ``si_ncol``, ``si_delr``,
        ``si_delv``, ``sea_level_top``, ``transect_lat_a/lon_a/lat_b/lon_b``,
        ``seawater_salinity_ppt``. ``intrusion_length_m`` stays 0.0 (set by
        postprocess after reading the .ucn output).

    The manifest's standard ``nlay/nrow/ncol/delr/delc`` fields are also written
    (``nlay=si_nlay``, ``nrow=1``, ``ncol=si_ncol``, ``delr=si_delr``,
    ``delc=DEFAULT_SI_DELC_M``) so generic grid-inspection code works.

    Args:
        coastal_transect_latlon: ``((lat_a, lon_a), (lat_b, lon_b))`` with A the
            seaward endpoint and B the inland endpoint. Both in EPSG:4326. When
            ``None`` the manifest carries zeros (no georegistration).
        n_vertical_layers:  number of vertical layers (clipped to [4, 80]).
        k_m_per_day:        horizontal hydraulic conductivity (m/day).
        aquifer_k_ms:       same K in m/s (stored on manifest).
        porosity:           effective porosity for transport.
        seawater_salinity_ppt: applied salinity (ppt) at the seaward GHB+AUX
            boundary. Also used as the GWT IC ``strt`` (fully-salty start).
        freshwater_inflow_m3_day: total fresh-water inflow through the inland
            WEL boundary (m3/day, positive). When ``None`` the Henry benchmark
            default is used (``DEFAULT_SI_INFLOW_M3_DAY``).
        sim_dir:    Path to the working directory where files are written.
        sim_name:   MFSimulation ``sim_name`` (normally "mfsim").
        gwf_name:   Name of the GWF model (normally "gwf_model").
        write:      If ``True``, write the deck to disk.

    Returns:
        ``DeckManifest`` with all saltwater-intrusion Wave-5 fields populated.

    Raises:
        ValueError: if ``n_vertical_layers`` is outside [4, 80].
    """
    nlay = max(4, min(80, int(n_vertical_layers)))
    ncol = DEFAULT_SI_NCOL
    delr = DEFAULT_SI_DELR_M
    delc = DEFAULT_SI_DELC_M
    delv = DEFAULT_SI_DELV_M
    top = DEFAULT_SI_TOP_M

    # Aquifer bottom steps downward by delv per layer from top.
    botm = [top - (k + 1) * delv for k in range(nlay)]

    # Use the caller-supplied hydraulic K; fall back to the demo default if
    # the inherited k_m_per_day is implausibly low (the spill placeholder 0.0).
    k = k_m_per_day if k_m_per_day > 0.0 else DEFAULT_SI_K_M_DAY

    csalt = float(seawater_salinity_ppt)
    cfresh = DEFAULT_SI_CFRESH_PPT
    inflow = (
        float(freshwater_inflow_m3_day)
        if freshwater_inflow_m3_day is not None
        else DEFAULT_SI_INFLOW_M3_DAY
    )

    # Density EOS: salinity in PPT, denseref=1000, densesalt=1025 at csalt ppt.
    denseref = DEFAULT_SI_DENSEREF
    drhodc = (DEFAULT_SI_DENSESALT - denseref) / max(csalt - cfresh, 1.0)

    diffc = DEFAULT_SI_DIFFC_M2_DAY

    gwtname = "gwt_model"

    # ---------------------------------------------------------------------- #
    # Simulation + time discretisation. ONE transient period ramps the system
    # to a quasi-steady wedge (Henry convention: short period, many steps).
    # ---------------------------------------------------------------------- #
    sim = flopy.mf6.MFSimulation(
        sim_name=sim_name,
        sim_ws=str(sim_dir),
        exe_name="mf6",
        version="mf6",
    )
    flopy.mf6.ModflowTdis(
        sim,
        time_units=TIME_UNITS,
        nper=1,
        perioddata=[(DEFAULT_SI_PERIOD_DAYS, DEFAULT_SI_NSTEPS, 1.0)],
    )

    # ---------------------------------------------------------------------- #
    # TWO SEPARATE IMS: GWF registered FIRST (MF6 hard requirement with BUY).
    # BUY makes the flow system nonlinear -> use BICGSTAB + relaxation.
    # ---------------------------------------------------------------------- #
    ims_gwf = flopy.mf6.ModflowIms(
        sim,
        filename=f"{gwf_name}.ims",
        print_option="SUMMARY",
        complexity="MODERATE",
        outer_dvclose=1e-6,
        outer_maximum=100,
        inner_dvclose=1e-7,
        inner_maximum=300,
        linear_acceleration="BICGSTAB",
        relaxation_factor=0.97,
        rcloserecord=[1e-6, "strict"],
    )
    ims_gwt = flopy.mf6.ModflowIms(
        sim,
        filename=f"{gwtname}.ims",
        print_option="SUMMARY",
        complexity="MODERATE",
        outer_dvclose=1e-6,
        outer_maximum=100,
        inner_dvclose=1e-7,
        inner_maximum=300,
        linear_acceleration="BICGSTAB",
    )

    # ---------------------------------------------------------------------- #
    # GWF flow model.
    # ---------------------------------------------------------------------- #
    gwf = flopy.mf6.ModflowGwf(sim, modelname=gwf_name, save_flows=True)
    # Register GWF IMS first (BUY requirement).
    sim.register_ims_package(ims_gwf, [gwf_name])

    flopy.mf6.ModflowGwfdis(
        gwf,
        length_units=LENGTH_UNITS,
        nlay=nlay,
        nrow=1,
        ncol=ncol,
        delr=delr,
        delc=delc,
        top=top,
        botm=botm,
        filename=f"{gwf_name}.dis",
    )
    # IC: start at sea level (head = top; overridden quickly by GHB).
    flopy.mf6.ModflowGwfic(gwf, strt=top, filename=f"{gwf_name}.ic")

    # NPF: confined (icelltype=0) + save flags required for postprocess.
    flopy.mf6.ModflowGwfnpf(
        gwf,
        save_flows=True,
        save_specific_discharge=True,
        save_saturation=True,
        icelltype=0,
        k=k,
        filename=f"{gwf_name}.npf",
    )

    # BUY: links GWT salinity to fluid density via the EOS.
    # packagedata row: (irhospec, drhodc, crhoref, modelname, auxspeciesname)
    # crhoref = cfresh (0.0 ppt) so fresh water has density = denseref.
    buy_pd = [(0, drhodc, cfresh, gwtname, "CONCENTRATION")]
    flopy.mf6.ModflowGwfbuy(
        gwf,
        denseref=denseref,
        nrhospecies=1,
        packagedata=buy_pd,
        filename=f"{gwf_name}.buy",
    )

    # Seaward boundary (last column, all layers): GHB + AUX CONCENTRATION=salt.
    # Conductance = K * delv * delc / (0.5 * delr) (half-cell distance).
    ghb_cond = k * delv * delc / (0.5 * delr)
    ghb_spd = [[(lk, 0, ncol - 1), top, ghb_cond, csalt] for lk in range(nlay)]
    flopy.mf6.ModflowGwfghb(
        gwf,
        stress_period_data=ghb_spd,
        auxiliary="CONCENTRATION",
        pname="GHB-1",
        filename=f"{gwf_name}.ghb",
    )

    # Inland boundary (first column, all layers): WEL + AUX CONCENTRATION=fresh.
    wel_q = inflow / nlay
    wel_spd = [[(lk, 0, 0), wel_q, cfresh] for lk in range(nlay)]
    flopy.mf6.ModflowGwfwel(
        gwf,
        stress_period_data=wel_spd,
        auxiliary="CONCENTRATION",
        pname="WEL-1",
        filename=f"{gwf_name}.wel",
    )

    flopy.mf6.ModflowGwfoc(
        gwf,
        head_filerecord=f"{gwf_name}.hds",
        budget_filerecord=f"{gwf_name}.cbc",
        saverecord=[("HEAD", "LAST"), ("BUDGET", "LAST")],
        filename=f"{gwf_name}.oc",
    )

    # ---------------------------------------------------------------------- #
    # GWT solute-transport model.
    # ---------------------------------------------------------------------- #
    gwt = flopy.mf6.ModflowGwt(sim, modelname=gwtname, save_flows=True)
    # Register GWT IMS AFTER GWF (MF6 sequence requirement).
    sim.register_ims_package(ims_gwt, [gwtname])

    flopy.mf6.ModflowGwtdis(
        gwt,
        length_units=LENGTH_UNITS,
        nlay=nlay,
        nrow=1,
        ncol=ncol,
        delr=delr,
        delc=delc,
        top=top,
        botm=botm,
        filename=f"{gwtname}.dis",
    )
    # IC: start fully salty (Henry convention; fresh inflow flushes to equilibrium).
    flopy.mf6.ModflowGwtic(gwt, strt=csalt, filename=f"{gwtname}.ic")

    # ADV UPSTREAM: robust on the sharp wedge front (TVD oscillates here).
    flopy.mf6.ModflowGwtadv(gwt, scheme="UPSTREAM", filename=f"{gwtname}.adv")

    # DSP: xt3d_off=True avoids the XT3D cross-term which can destabilize
    # the narrow Henry slice. Molecular diffusion carries the mixing.
    flopy.mf6.ModflowGwtdsp(
        gwt, xt3d_off=True, diffc=diffc, filename=f"{gwtname}.dsp"
    )
    flopy.mf6.ModflowGwtmst(gwt, porosity=porosity, filename=f"{gwtname}.mst")

    # SSM: links GWF boundary AUX 'CONCENTRATION' to transport. The SSM source
    # names MUST match the GWF package pnames exactly (GHB-1 / WEL-1) so that
    # the seaward GHB injects salt and the inland WEL injects fresh water.
    # Without this the wedge does NOT form (no-salt boundary is the default).
    flopy.mf6.ModflowGwtssm(
        gwt,
        sources=[("GHB-1", "AUX", "CONCENTRATION"), ("WEL-1", "AUX", "CONCENTRATION")],
        filename=f"{gwtname}.ssm",
    )

    flopy.mf6.ModflowGwtoc(
        gwt,
        concentration_filerecord=f"{gwtname}.ucn",
        saverecord=[("CONCENTRATION", "LAST")],
        filename=f"{gwtname}.oc",
    )

    # ---------------------------------------------------------------------- #
    # Flow <-> transport exchange.
    # ---------------------------------------------------------------------- #
    flopy.mf6.ModflowGwfgwt(
        sim,
        exgtype="GWF6-GWT6",
        exgmnamea=gwf_name,
        exgmnameb=gwtname,
        filename="gwfgwt.exg",
    )

    # ---------------------------------------------------------------------- #
    # Transect endpoints for georegistration (A = seaward, B = inland).
    # ---------------------------------------------------------------------- #
    if coastal_transect_latlon is not None:
        pt_a, pt_b = coastal_transect_latlon
        ta_lat, ta_lon = float(pt_a[0]), float(pt_a[1])
        tb_lat, tb_lon = float(pt_b[0]), float(pt_b[1])
    else:
        ta_lat = ta_lon = tb_lat = tb_lon = 0.0

    manifest = DeckManifest(
        sim_dir=str(sim_dir),
        sim_name=sim_name,
        gwf_name=gwf_name,
        gwt_name=gwtname,
        # No UTM plan-view georegistration for the vertical slice; xorigin/yorigin
        # are zero (the transect endpoints on the manifest geolocate the cross-section).
        model_crs="EPSG:4326",
        xorigin=0.0,
        yorigin=0.0,
        nrow=1,
        ncol=ncol,
        nlay=nlay,
        delr=delr,
        delc=delc,
        # Spill fields are not meaningful for this archetype; zero them out.
        spill_row=0,
        spill_col=0,
        spill_easting_m=0.0,
        spill_northing_m=0.0,
        spill_lat=ta_lat,
        spill_lon=ta_lon,
        mass_rate_g_per_day=0.0,
        release_rate_kg_s=0.0,
        duration_days=DEFAULT_SI_PERIOD_DAYS,
        n_transport_steps=DEFAULT_SI_NSTEPS,
        contaminant="",
        aquifer_k_ms=aquifer_k_ms,
        porosity=porosity,
        # Archetype fields.
        archetype="saltwater_intrusion",
        gwt_present=True,
        transient=True,
        n_stress_periods=1,
        n_transient_periods=1,
        # Wave-5 saltwater_intrusion fields.
        saltwater_intrusion=True,
        si_nlay=nlay,
        si_ncol=ncol,
        si_delr=delr,
        si_delv=delv,
        sea_level_top=top,
        transect_lat_a=ta_lat,
        transect_lon_a=ta_lon,
        transect_lat_b=tb_lat,
        transect_lon_b=tb_lon,
        seawater_salinity_ppt=csalt,
        intrusion_length_m=0.0,  # populated by postprocess after reading .ucn
    )

    if write:
        sim.write_simulation()
        manifest.files = sorted(
            str(p.relative_to(sim_dir)) for p in sim_dir.rglob("*") if p.is_file()
        )
    return manifest


def build_modflow_deck(
    spill_location_latlon: tuple[float, float],
    contaminant: str,
    release_rate_kg_s: float,
    duration_days: float,
    aquifer_k_ms: float,
    porosity: float,
    workdir: str | Path,
    *,
    sim_name: str = "mfsim",
    write: bool = True,
    # --- River-coupling (sprint-17 J9; ADDITIVE, all optional) ------------- #
    river_polyline_lonlat: list[tuple[float, float]] | None = None,
    river_stage_m: float | None = None,
    river_stage_depth_m: float | None = None,
    streambed_conductance_m2_day: float | None = None,
    river_rbot_by_cell: dict[tuple[int, int], float] | None = None,
    river_stage_by_cell: dict[tuple[int, int], float] | None = None,
    along_river_source: bool = False,
    # --- stream_depletion SFR forcing (module wave; ADDITIVE, all optional) - #
    # Threaded into the SFR deck branch of ``_build_gwf_only_archetype_deck``
    # when ``archetype == "stream_depletion"``; ignored otherwise. All four are
    # demo-defaulted in the helper (narrated as demo assumptions).
    river_inflow_m3_s: float | None = None,
    river_width_m: float | None = None,
    streambed_k_m_day: float | None = None,
    manning_n: float | None = None,
    # --- land_subsidence CSUB forcing (module wave; ADDITIVE, all optional) - #
    # Threaded into the CSUB deck branch of ``_build_gwf_only_archetype_deck``
    # when ``archetype == "land_subsidence"``; ignored otherwise. All four are
    # demo-defaulted in the helper (narrated as demo assumptions).
    csub_ssv_inelastic_m: float | None = None,
    csub_sse_elastic_m: float | None = None,
    csub_interbed_thick_frac: float | None = None,
    csub_cg_ske_m: float | None = None,
    # --- Archetype switch (sprint-18 Wave-1; ADDITIVE, all optional) -------- #
    # archetype is None -> the EXISTING spill/seepage GWF+GWT deck (byte-identical).
    # The three new archetypes are GWF-only and dispatch to
    # ``_build_gwf_only_archetype_deck``; the spill-only kwargs above are ignored.
    archetype: str | None = None,
    well_location_latlon: tuple[float, float] | None = None,
    pumping_rate_m3_day: float | None = None,
    aquifer_sy: float | None = None,
    aquifer_ss: float | None = None,
    sim_years: float | None = None,
    n_periods: int | None = None,
    pit_footprint_lonlat: list[tuple[float, float]] | None = None,
    drain_elevation_m: float | None = None,
    drain_conductance_m2_day: float | None = None,
    well_pumping_rate_m3_day: float | None = None,
    zone_partition: str | None = None,
    # --- Wave-2 archetypes (sprint-18 Wave-2; ADDITIVE, all optional) ------- #
    # MAR (managed aquifer recharge -> RCH/RCHA mounding)
    basin_footprint_lonlat: list[tuple[float, float]] | None = None,
    infiltration_rate_m_day: float | None = None,
    recharge_months: int | None = None,
    # ASR (aquifer storage & recovery -> seasonal WEL inject/recover)
    injection_rate_m3_day: float | None = None,
    recovery_rate_m3_day: float | None = None,
    injection_months: int | None = None,
    recovery_months: int | None = None,
    n_cycles: int | None = None,
    # wetland_hydroperiod (RCH-schedule + EVT seasonal water-table range)
    wetland_footprint_lonlat: list[tuple[float, float]] | None = None,
    recharge_schedule_m_day: list[float] | None = None,
    et_surface_m: float | None = None,
    et_max_rate_m_day: float | None = None,
    et_extinction_depth_m: float | None = None,
    specific_yield: float | None = None,
    # --- multi_species transport (sprint-18 Wave-3; ADDITIVE, optional) ----- #
    # When ``archetype == "multi_species"`` the adapter builds ONE shared GWF +
    # one ModflowGwt per species + one ModflowGwfgwt per species. ``species`` is
    # an ordered list of per-species specs: either ``SpeciesSpec``-like objects
    # (any object exposing .name/.release_rate_kg_s/.sorption_kd/.decay_per_day/
    # .parent attributes) OR plain dicts with those keys. ``species is None`` =>
    # the byte-identical single-contaminant spill deck.
    species: list | None = None,
    # --- Wave-4 PRT capture-zone / wellhead_protection (ADDITIVE, optional) - #
    # ``archetype in ('capture_zone', 'wellhead_protection')``: build ONLY the
    # GWF deck here (the caller runs mf6 on it, then calls
    # ``build_and_run_prt_from_gwf`` to reverse + run the PRT sim).
    # ``n_particles`` and ``capture_zone_travel_time_years`` control the release
    # ring size and the isochrone cutoffs written into the manifest.
    n_particles: int = 16,
    capture_zone_travel_time_years: list[float] | None = None,
    prt_max_tracking_years: float | None = None,
    # --- Wave-5 saltwater_intrusion (sprint-18 Wave-5; ADDITIVE, optional) -- #
    # ``archetype == "saltwater_intrusion"``: Henry-style field-scale coastal
    # transect; GWF (BUY variable-density) + GWT in ONE sim. All three args are
    # optional and fall back to the Henry field-scale demo defaults when None.
    coastal_transect_latlon: tuple[tuple[float, float], tuple[float, float]] | None = None,
    seawater_salinity_ppt: float = 35.0,
    n_vertical_layers: int = 20,
    freshwater_inflow_m3_day: float | None = None,
    # --- advanced-physics overrides (levers STEP 3; ADDITIVE, optional) ----- #
    advanced_physics: dict | None = None,
    save_concentration_all_steps: bool = True,
) -> DeckManifest:
    """Assemble a complete MF6 GWF+GWT spill deck and (optionally) write it.

    Build a physically meaningful minimal MODFLOW 6 simulation for a
    groundwater-contamination scenario: a steady-state groundwater flow model
    (GWF) driving a transient advection-dispersion solute-transport model
    (GWT). The deck is written to disk via FloPy and the function returns a
    typed `DeckManifest` describing it.

    Use this when:
        you need to turn spill parameters (location, contaminant, release
        rate, duration, aquifer hydraulic conductivity, porosity) into a
        runnable MODFLOW 6 input deck for the groundwater-contamination engine
        (Case 2). The caller uploads the resulting files to the cache bucket
        and submits the solver Cloud Run Job (job-0227).

    Do NOT use this for:
        surface-water / inundation flooding (use `build_sfincs_model`);
        reactive transport with sorption or biodegradation (out of scope for
        v0.1 - this builds a conservative-tracer model only); or any case
        requiring real hydrogeologic layering (this is a single-layer demo
        grid centred on the spill point).

    Args:
        spill_location_latlon: (lat, lon) of the spill, EPSG:4326 degrees. The
            structured grid is centred on this point and georegistered in the
            best-fit UTM zone.
        contaminant: contaminant name (carried into the manifest for
            narration; the transport math treats it as a conservative tracer).
        release_rate_kg_s: contaminant mass-loading rate, kilograms per
            second. Converted internally to grams/day for the MF6 `SRC`
            package; `mass_rate_g_per_day` records the written value.
        duration_days: simulated release + transport duration in days; sets
            the transient stress-period length and the number of transport
            time steps.
        aquifer_k_ms: saturated hydraulic conductivity, metres per second.
            Converted to m/day for the NPF package (MF6 length/time units are
            METERS/DAYS for this deck).
        porosity: effective porosity (0-1), used by the transport mobile-
            storage term so advective velocity = Darcy flux / porosity.
        workdir: directory to write the deck into (created if absent).
        sim_name: simulation name (default "mfsim"); the simulation namefile
            is "<sim_name>.nam".
        write: if True (default), write all input files to disk. If False,
            build the FloPy objects and return the manifest without writing
            (used by unit tests that only assert the in-memory deck shape).
        river_polyline_lonlat: an optional river polyline as ``(lon, lat)``
            vertices (EPSG:4326) to drape onto the structured grid as a RIV
            head-dependent river<->aquifer flux boundary (sprint-17 J9). When
            None the deck is the original spill-only deck (no RIV, no along-
            river source) and every river field on the manifest stays at its
            no-river default. The vertices are projected to the deck's UTM grid
            and rasterized into the grid cells they traverse.
        river_stage_m: explicit river stage (water-surface elevation, deck datum
            metres) applied to EVERY RIV reach cell. Takes precedence over
            ``river_stage_by_cell`` and DEM-derived stage.
        river_stage_depth_m: water depth (m) above the streambed bottom used to
            set stage from rbot when no explicit stage is supplied (stage = rbot
            + depth). Defaults to ``DEFAULT_RIVER_STAGE_DEPTH_M``.
        streambed_conductance_m2_day: per-reach-cell RIV conductance (m^2/day).
            Defaults to ``DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY``.
        river_rbot_by_cell: optional ``{(row, col): rbot_m}`` of DEM-sampled
            streambed-bottom elevations per reach cell (the workflow samples the
            DEM and passes this; the adapter stays pure). Cells absent from the
            map fall back to a flat demo rbot above the local aquifer head.
        river_stage_by_cell: optional ``{(row, col): stage_m}`` of per-cell
            stage (e.g. rbot + depth from the DEM). Overridden by
            ``river_stage_m`` when that is given.
        along_river_source: when True the contaminant SRC mass-loading is placed
            at the RIV reach cells (the seepage source enters where the river
            leaks into the aquifer) instead of the single spill cell. Requires a
            ``river_polyline_lonlat``; ignored (with the SRC staying at the spill
            cell) when no river is supplied.
        species: multi_species transport (sprint-18 Wave-3 - ADDITIVE). When
            ``archetype == "multi_species"`` this is an ordered list of per-species
            specs (``SpeciesSpec``-like objects OR plain dicts with ``name`` /
            ``release_rate_kg_s`` / ``sorption_kd`` / ``decay_per_day`` /
            ``parent`` keys). The adapter builds ONE shared steady-state GWF flow
            field + one ``ModflowGwt`` transport model per species (named
            ``gwt_<species>``, writing ``gwt_<species>.ucn``) + one
            ``ModflowGwfgwt`` flow<->transport exchange per species, all in ONE
            simulation / ONE mf6 run. ``species is None`` (the default) keeps the
            single-contaminant spill deck byte-identical. NOTE: a species'
            ``parent`` is recorded on the manifest but the parent->daughter
            mass-ingrowth coupling is not yet wired (MF6's GWT6-GWT6 exchange is
            spatial domain-decomposition, not chemical ingrowth) - each species
            transports independently for now.

    Returns:
        DeckManifest: typed deck description (paths, grid georegistration,
        spill cell, source loading, `model_crs`). Every field is a number a
        downstream tool reads; nothing is prose-for-number.
    """
    lat, lon = float(spill_location_latlon[0]), float(spill_location_latlon[1])
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError(f"spill_location_latlon out of range: {(lat, lon)!r}")
    # The release_rate / duration validations are SPILL/SEEPAGE-only -- the three
    # GWF-only archetypes (archetype is not None) carry no contaminant source, so
    # the agent-side phase passes placeholder spill params. Aquifer K + porosity
    # + the (lat, lon) grid centre are meaningful for EVERY archetype (the grid is
    # centred on the AOI point and the NPF reads K), so those stay unconditional.
    if archetype is None:
        if release_rate_kg_s <= 0:
            raise ValueError(
                f"release_rate_kg_s must be > 0, got {release_rate_kg_s!r}"
            )
        if duration_days <= 0:
            raise ValueError(f"duration_days must be > 0, got {duration_days!r}")
    elif archetype == "multi_species":
        # multi_species is a GWF+GWT transport deck: duration_days IS meaningful
        # (it sets the transient transport period length, exactly like the spill
        # deck). The per-species release_rate lives on each SpeciesSpec, so the
        # top-level release_rate_kg_s is a placeholder and is NOT validated here.
        if duration_days <= 0:
            raise ValueError(
                f"multi_species duration_days must be > 0, got {duration_days!r}"
            )
        if not species:
            raise ValueError(
                "multi_species archetype requires a non-empty species list"
            )
    if aquifer_k_ms <= 0:
        raise ValueError(f"aquifer_k_ms must be > 0, got {aquifer_k_ms!r}")
    if not (0.0 < porosity < 1.0):
        raise ValueError(f"porosity must be in (0,1), got {porosity!r}")

    sim_dir = Path(workdir)
    sim_dir.mkdir(parents=True, exist_ok=True)

    gwf_name = "gwf_model"
    gwt_name = "gwt_model"

    # --- Grid georegistration -------------------------------------------------
    # Project the spill point to a metric UTM zone, then build a square grid
    # centred on it. The grid lower-left corner (xorigin, yorigin) anchors the
    # affine transform the postprocess step uses to reproject the COG.
    crs = _utm_crs_for_lonlat(lon, lat)
    to_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    spill_east, spill_north = to_utm.transform(lon, lat)

    ncol = int(round(2 * DOMAIN_HALF_WIDTH_M / CELL_SIZE_M))  # 40
    nrow = ncol
    delr = CELL_SIZE_M
    delc = CELL_SIZE_M

    # Lower-left corner so the spill lands at the grid centre.
    xorigin = spill_east - DOMAIN_HALF_WIDTH_M
    yorigin = spill_north - DOMAIN_HALF_WIDTH_M

    # Spill cell indices. MODFLOW rows increase from the TOP (north) downward;
    # FloPy's `yorigin` is the lower-left corner and row 0 is the northernmost.
    # Cell-centre of the spill = grid centre, so row/col are the middle cells.
    spill_col = int((spill_east - xorigin) // delc)
    # row 0 is north (top); convert from south-referenced offset:
    north_offset_m = (yorigin + nrow * delc) - spill_north
    spill_row = int(north_offset_m // delc)
    spill_row = max(0, min(nrow - 1, spill_row))
    spill_col = max(0, min(ncol - 1, spill_col))

    # Projected coordinates of the chosen spill cell centre (for manifest).
    spill_cell_east = xorigin + (spill_col + 0.5) * delc
    spill_cell_north = (yorigin + nrow * delc) - (spill_row + 0.5) * delc

    # --- Unit conversions -----------------------------------------------------
    k_m_per_day = aquifer_k_ms * SECONDS_PER_DAY
    mass_rate_g_per_day = release_rate_kg_s * KG_TO_G * SECONDS_PER_DAY

    # --- Archetype switch (sprint-18 Wave-1) ---------------------------------
    # A non-None archetype is one of the three NEW GWF-only MODFLOW questions
    # (sustainable_yield / mine_dewatering / regional_water_budget). They reuse
    # the SAME georegistration computed above (UTM zone, grid origin, 40x40x50 m
    # grid) and the SAME west->east REGIONAL_GRADIENT CHD, but build a GWF-only
    # deck (no GWT/SRC/SSM/DSP/MST block, no GWFGWT exchange) via the shared
    # ``_build_gwf_only_archetype_deck`` helper. ``archetype is None`` falls
    # through to the EXISTING spill/seepage GWF+GWT deck below (byte-identical).
    if archetype is not None:
        if archetype not in (
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
        ):
            raise ValueError(f"unknown MODFLOW archetype: {archetype!r}")
        # Wave-5 saltwater_intrusion: GWF (BUY variable-density) + GWT in ONE sim,
        # vertical nrow=1 Henry-style slice with seaward GHB+AUX and inland WEL+AUX.
        # Bypasses the plan-view UTM georegistration used by other archetypes.
        if archetype == "saltwater_intrusion":
            return _build_saltwater_intrusion_deck(
                coastal_transect_latlon=coastal_transect_latlon,
                n_vertical_layers=n_vertical_layers,
                k_m_per_day=k_m_per_day,
                aquifer_k_ms=aquifer_k_ms,
                porosity=porosity,
                seawater_salinity_ppt=seawater_salinity_ppt,
                freshwater_inflow_m3_day=freshwater_inflow_m3_day,
                sim_dir=sim_dir,
                sim_name=sim_name,
                gwf_name=gwf_name,
                write=write,
            )
        # Wave-4 PRT archetypes: a two-sim workflow (GWF built here; the caller
        # runs mf6 on it then calls build_and_run_prt_from_gwf for the PRT phase).
        # The GWF grid is built at LOCAL (0,0) origin inside the helper (the true
        # UTM offset is stored on the manifest as xoffset_m / yoffset_m).
        if archetype in ("capture_zone", "wellhead_protection"):
            return _build_prt_capture_zone_deck(
                archetype=archetype,
                lat=lat,
                lon=lon,
                crs=crs,
                to_utm=to_utm,
                k_m_per_day=k_m_per_day,
                aquifer_k_ms=aquifer_k_ms,
                porosity=porosity,
                sim_dir=sim_dir,
                sim_name=sim_name,
                gwf_name=gwf_name,
                write=write,
                well_location_latlon=well_location_latlon,
                pumping_rate_m3_day=pumping_rate_m3_day,
                n_particles=n_particles,
                capture_zone_travel_time_years=capture_zone_travel_time_years,
                advanced_physics=advanced_physics,
            )
        # multi_species is a GWF+GWT deck (ONE shared GWF + N transport models),
        # NOT a GWF-only archetype, so it dispatches to its own builder.
        if archetype == "multi_species":
            return _build_multi_species_deck(
                species=species,
                lat=lat,
                lon=lon,
                crs=crs,
                to_utm=to_utm,
                xorigin=xorigin,
                yorigin=yorigin,
                nrow=nrow,
                ncol=ncol,
                delr=delr,
                delc=delc,
                k_m_per_day=k_m_per_day,
                aquifer_k_ms=aquifer_k_ms,
                porosity=porosity,
                duration_days=duration_days,
                spill_row=spill_row,
                spill_col=spill_col,
                spill_cell_east=spill_cell_east,
                spill_cell_north=spill_cell_north,
                sim_dir=sim_dir,
                sim_name=sim_name,
                gwf_name=gwf_name,
                write=write,
                save_concentration_all_steps=save_concentration_all_steps,
                advanced_physics=advanced_physics,
            )
        return _build_gwf_only_archetype_deck(
            archetype=archetype,
            lat=lat,
            lon=lon,
            crs=crs,
            to_utm=to_utm,
            xorigin=xorigin,
            yorigin=yorigin,
            nrow=nrow,
            ncol=ncol,
            delr=delr,
            delc=delc,
            k_m_per_day=k_m_per_day,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            sim_dir=sim_dir,
            sim_name=sim_name,
            gwf_name=gwf_name,
            write=write,
            well_location_latlon=well_location_latlon,
            pumping_rate_m3_day=pumping_rate_m3_day,
            aquifer_sy=aquifer_sy,
            aquifer_ss=aquifer_ss,
            sim_years=sim_years,
            n_periods=n_periods,
            pit_footprint_lonlat=pit_footprint_lonlat,
            drain_elevation_m=drain_elevation_m,
            drain_conductance_m2_day=drain_conductance_m2_day,
            well_pumping_rate_m3_day=well_pumping_rate_m3_day,
            zone_partition=zone_partition,
            basin_footprint_lonlat=basin_footprint_lonlat,
            infiltration_rate_m_day=infiltration_rate_m_day,
            recharge_months=recharge_months,
            injection_rate_m3_day=injection_rate_m3_day,
            recovery_rate_m3_day=recovery_rate_m3_day,
            injection_months=injection_months,
            recovery_months=recovery_months,
            n_cycles=n_cycles,
            wetland_footprint_lonlat=wetland_footprint_lonlat,
            recharge_schedule_m_day=recharge_schedule_m_day,
            et_surface_m=et_surface_m,
            et_max_rate_m_day=et_max_rate_m_day,
            et_extinction_depth_m=et_extinction_depth_m,
            specific_yield=specific_yield,
            # --- module wave: stream_depletion SFR routed exchange ------------ #
            # The river geometry reuses the SAME river-coupling kwargs the RIV
            # seepage path threads (river_polyline_lonlat + river_rbot_by_cell);
            # the four SFR forcing fields are demo-defaulted in the helper.
            river_polyline_lonlat=river_polyline_lonlat,
            river_rbot_by_cell=river_rbot_by_cell,
            river_inflow_m3_s=river_inflow_m3_s,
            river_width_m=river_width_m,
            streambed_k_m_day=streambed_k_m_day,
            manning_n=manning_n,
            # --- module wave: land_subsidence CSUB compaction ----------------- #
            # Four demo-defaulted CSUB forcing fields threaded into the CSUB deck
            # branch; ignored unless archetype == "land_subsidence".
            csub_ssv_inelastic_m=csub_ssv_inelastic_m,
            csub_sse_elastic_m=csub_sse_elastic_m,
            csub_interbed_thick_frac=csub_interbed_thick_frac,
            csub_cg_ske_m=csub_cg_ske_m,
            advanced_physics=advanced_physics,
        )

    # Transport time stepping: aim for ~daily resolution but cap step count so
    # tiny demos stay fast and long demos stay bounded.
    n_transport_steps = int(max(1, min(round(duration_days), 365)))

    # --- Simulation + time discretisation ------------------------------------
    sim = flopy.mf6.MFSimulation(
        sim_name=sim_name,
        sim_ws=str(sim_dir),
        exe_name="mf6",
        version="mf6",
    )

    # Two periods:
    #   1) steady-state spin-up (1 step) so the flow field equilibrates and the
    #      GWF model has a defined head field at transport start;
    #   2) transient transport period of `duration_days`, n_transport_steps.
    flopy.mf6.ModflowTdis(
        sim,
        time_units=TIME_UNITS,
        nper=2,
        perioddata=[
            (1.0, 1, 1.0),  # steady-state period: 1-day length, 1 step
            (float(duration_days), n_transport_steps, 1.0),  # transient
        ],
    )

    # Iterative model solution - one for flow, one for transport (separate IMS
    # is the MF6-recommended pattern for GWF+GWT so the nonlinear transport
    # solve does not destabilise the linear flow solve).
    ims_gwf = flopy.mf6.ModflowIms(
        sim,
        filename=f"{gwf_name}.ims",
        complexity="SIMPLE",
        outer_dvclose=1e-6,
        inner_dvclose=1e-6,
        linear_acceleration="CG",
    )
    ims_gwt = flopy.mf6.ModflowIms(
        sim,
        filename=f"{gwt_name}.ims",
        complexity="MODERATE",
        outer_dvclose=1e-6,
        inner_dvclose=1e-6,
        linear_acceleration="BICGSTAB",
    )

    # --- GWF (flow) model -----------------------------------------------------
    gwf = flopy.mf6.ModflowGwf(
        sim,
        modelname=gwf_name,
        model_nam_file=f"{gwf_name}.nam",
        save_flows=True,
    )
    sim.register_ims_package(ims_gwf, [gwf_name])

    dis = flopy.mf6.ModflowGwfdis(
        gwf,
        length_units=LENGTH_UNITS,
        nlay=N_LAYERS,
        nrow=nrow,
        ncol=ncol,
        delr=delr,
        delc=delc,
        top=AQUIFER_TOP_M,
        botm=AQUIFER_BOTTOM_M,
        xorigin=xorigin,
        yorigin=yorigin,
        filename=f"{gwf_name}.dis",
    )
    # Tag the model grid CRS so any FloPy-side georeferencing is correct.
    try:
        gwf.modelgrid.set_coord_info(
            xoff=xorigin, yoff=yorigin, crs=crs.to_epsg()
        )
    except Exception:  # pragma: no cover - older flopy signature fallback
        pass

    # Constant-head gradient: west column high, east column low -> west->east
    # flow. Head drop = gradient x domain width. regional_gradient: CONSTITUTIVE
    # lever (default EQUALS REGIONAL_GRADIENT -> byte-identical when unset; the
    # spill/seepage `phys` dict is built below, so read advanced_physics here).
    domain_width_m = ncol * delr
    head_west = AQUIFER_TOP_M + float(
        (advanced_physics or {}).get("regional_gradient", REGIONAL_GRADIENT)
    ) * domain_width_m
    head_east = AQUIFER_TOP_M
    flopy.mf6.ModflowGwfic(gwf, strt=head_west, filename=f"{gwf_name}.ic")
    flopy.mf6.ModflowGwfnpf(
        gwf,
        save_flows=True,
        icelltype=0,  # confined: transmissivity independent of head
        k=k_m_per_day,
        filename=f"{gwf_name}.npf",
    )

    chd_records = []
    for r in range(nrow):
        chd_records.append([(0, r, 0), head_west])  # west boundary (col 0)
        chd_records.append([(0, r, ncol - 1), head_east])  # east boundary
    flopy.mf6.ModflowGwfchd(
        gwf,
        stress_period_data={0: chd_records, 1: chd_records},
        filename=f"{gwf_name}.chd",
    )

    # --- RIV package: drape the river polyline onto the grid (sprint-17 J9) --
    # The RIV head-dependent boundary couples the river to the aquifer: per
    # reach cell leakage Q = cond*(stage - h), capped at cond*(stage - rbot)
    # once the aquifer head drops below the streambed bottom. The set of reach
    # cells and per-cell stage/rbot/conductance are derived deterministically
    # here from the projected polyline + the (DEM-sampled or demo) elevations.
    riv_records: list = []
    river_cell_count = 0
    river_reach_len_m = 0.0
    conductance = (
        float(streambed_conductance_m2_day)
        if streambed_conductance_m2_day is not None
        else DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY
    )
    river_cells: list[tuple[int, int, float]] = []
    if river_polyline_lonlat:
        # Project the polyline vertices to the deck's UTM grid.
        vertices_en = [to_utm.transform(vlon, vlat) for (vlon, vlat) in river_polyline_lonlat]
        river_cells = _drape_polyline_onto_grid(
            vertices_en,
            xorigin=xorigin,
            yorigin=yorigin,
            delr=delr,
            delc=delc,
            nrow=nrow,
            ncol=ncol,
        )
        depth = (
            float(river_stage_depth_m)
            if river_stage_depth_m is not None
            else DEFAULT_RIVER_STAGE_DEPTH_M
        )
        default_rbot = AQUIFER_TOP_M + DEFAULT_RIVER_RBOT_ABOVE_TOP_M

        def _rbot_fn(row: int, col: int) -> float:
            if river_rbot_by_cell and (row, col) in river_rbot_by_cell:
                return float(river_rbot_by_cell[(row, col)])
            return default_rbot

        def _stage_fn(row: int, col: int) -> float:
            if river_stage_m is not None:
                return float(river_stage_m)
            if river_stage_by_cell and (row, col) in river_stage_by_cell:
                return float(river_stage_by_cell[(row, col)])
            return _rbot_fn(row, col) + depth

        riv_records = build_riv_records(
            river_cells,
            conductance_m2_day=conductance,
            stage_fn=_stage_fn,
            rbot_fn=_rbot_fn,
            chd_cols=(0, ncol - 1),
            ncol=ncol,
        )
        if riv_records:
            flopy.mf6.ModflowGwfriv(
                gwf,
                stress_period_data={0: riv_records, 1: riv_records},
                save_flows=True,
                filename=f"{gwf_name}.riv",
                pname="riv-0",
            )
            written_cells = {(rec[0][1], rec[0][2]) for rec in riv_records}
            river_cell_count = len(riv_records)
            river_reach_len_m = sum(
                length for (r, c, length) in river_cells if (r, c) in written_cells
            )

    river_coupled = river_cell_count > 0

    flopy.mf6.ModflowGwfoc(
        gwf,
        head_filerecord=f"{gwf_name}.hds",
        budget_filerecord=f"{gwf_name}.cbc",
        saverecord=[("HEAD", "LAST"), ("BUDGET", "LAST")],
        filename=f"{gwf_name}.oc",
    )

    # --- GWT (transport) model -----------------------------------------------
    gwt = flopy.mf6.ModflowGwt(
        sim,
        modelname=gwt_name,
        model_nam_file=f"{gwt_name}.nam",
        save_flows=True,
    )
    sim.register_ims_package(ims_gwt, [gwt_name])

    flopy.mf6.ModflowGwtdis(
        gwt,
        length_units=LENGTH_UNITS,
        nlay=N_LAYERS,
        nrow=nrow,
        ncol=ncol,
        delr=delr,
        delc=delc,
        top=AQUIFER_TOP_M,
        botm=AQUIFER_BOTTOM_M,
        xorigin=xorigin,
        yorigin=yorigin,
        filename=f"{gwt_name}.dis",
    )
    flopy.mf6.ModflowGwtic(gwt, strt=0.0, filename=f"{gwt_name}.ic")
    flopy.mf6.ModflowGwtadv(gwt, scheme="TVD", filename=f"{gwt_name}.adv")

    # --- advanced-physics overrides (levers STEP 3) -------------------------- #
    # The agent passes an ALREADY-VALIDATED resolved dict (range/type checked by
    # physics_registry.validate_and_resolve_physics("modflow", ...)). None / {}
    # => byte-identical conservative-tracer deck (every default below reproduces
    # today's exact GwtDsp / GwtMst call). The keys mirror the registry
    # deck_target pointers (GwtDsp:alh / GwtDsp:ath1 / GwtMst:distcoef /
    # GwtMst:bulk_density / GwtMst:decay).
    phys = dict(advanced_physics or {})
    alh = float(phys.get("long_dispersivity_m", LONGITUDINAL_DISPERSIVITY_M))
    if "trans_dispersivity_m" in phys:
        ath1 = float(phys["trans_dispersivity_m"])
    else:
        ath1 = LONGITUDINAL_DISPERSIVITY_M * TRANSVERSE_HORIZONTAL_RATIO
    # Vertical dispersivity (atv): CONSTITUTIVE lever. Default EQUALS the
    # historical ratio-locked value so an unset run is byte-identical; a set
    # vert_dispersivity_m flows straight through (same phys.get seam as alh/ath1).
    if "vert_dispersivity_m" in phys:
        atv = float(phys["vert_dispersivity_m"])
    else:
        atv = LONGITUDINAL_DISPERSIVITY_M * TRANSVERSE_VERTICAL_RATIO
    flopy.mf6.ModflowGwtdsp(
        gwt,
        alh=alh,
        ath1=ath1,
        atv=atv,
        filename=f"{gwt_name}.dsp",
    )

    # Mobile storage: porosity controls pore velocity (v = q / porosity).
    # advanced_physics may additionally enable LINEAR sorption (distcoef = Kd +
    # bulk_density => a retardation factor) and FIRST_ORDER decay -- both DEFAULT
    # OFF (a conservative tracer) so an absent key is byte-identical.
    mst_kwargs: dict = {"porosity": porosity, "filename": f"{gwt_name}.mst"}
    kd = phys.get("sorption_kd")
    sorption_active = kd is not None and float(kd) > 0.0
    if sorption_active:
        mst_kwargs["sorption"] = "LINEAR"
        mst_kwargs["distcoef"] = float(kd)
        mst_kwargs["bulk_density"] = float(phys.get("bulk_density", 1600.0))
    decay = phys.get("decay_rate_per_day")
    decay_active = decay is not None and float(decay) > 0.0
    if decay_active:
        mst_kwargs["first_order_decay"] = True
        mst_kwargs["decay"] = float(decay)
        # LIVE BUG FIX (sprint-18 Wave-1): MF6 REQUIRES decay_sorbed in the
        # GRIDDATA block whenever BOTH first-order decay AND sorption are active
        # ("DECAY_SORBED not provided in GRIDDATA block but decay and sorption are
        # active"). Default the sorbed-phase decay coefficient to the aqueous
        # decay value (decay of the sorbed contaminant proceeds at the same
        # first-order rate as the dissolved phase unless the caller overrides it).
        if sorption_active:
            decay_sorbed = phys.get("decay_sorbed_per_day")
            mst_kwargs["decay_sorbed"] = (
                float(decay_sorbed) if decay_sorbed is not None else float(decay)
            )
    flopy.mf6.ModflowGwtmst(gwt, **mst_kwargs)

    # Mass-loading source. SRC injects mass/time directly (g/day here)
    # regardless of local concentration - the spill-loading model. The source
    # is active ONLY in the transient transport period (period 1, 0-based), NOT
    # the steady-state flow spin-up (period 0). This keeps the released-mass
    # yardstick exact: total injected = mass_rate x duration, not mass_rate x
    # (1 spin-up day + duration). Empty list in period 0 deactivates it there.
    #
    # sprint-17 J9: when along_river_source is True AND a river was draped, the
    # source is distributed ALONG the RIV reach cells (the contaminant enters
    # where the river leaks into the aquifer - the river-seepage plume), with
    # the SAME total mass rate (split evenly across the reach cells) so the
    # released-mass yardstick is preserved. Otherwise it stays at the spill cell.
    source_along_river = bool(along_river_source and river_coupled and riv_records)
    if source_along_river:
        reach_cellids = [tuple(rec[0]) for rec in riv_records]  # [(lay,row,col), ...]
        per_cell_rate = mass_rate_g_per_day / float(len(reach_cellids))
        src_record = [[cellid, per_cell_rate] for cellid in reach_cellids]
    else:
        src_record = [[(0, spill_row, spill_col), mass_rate_g_per_day]]
    flopy.mf6.ModflowGwtsrc(
        gwt,
        stress_period_data={0: [], 1: src_record},
        filename=f"{gwt_name}.src",
    )

    # Source-sink mixing is required by GWT whenever the flow model has any
    # boundary package (CHD here). With no AUXMIXED concentrations declared,
    # inflow across the west constant-head boundary carries zero concentration
    # (clean regional recharge) - the physically correct default for a tracer
    # entering from up-gradient. An empty SSM (sources=None) is the MF6 idiom.
    flopy.mf6.ModflowGwtssm(
        gwt,
        sources=None,
        filename=f"{gwt_name}.ssm",
    )

    # levers STEP 3: save ALL transport steps (not just LAST) so the agent can
    # publish a concentration ANIMATION (plume-concentration-ts). The existing
    # final-step plume reads totim=times[-1], so saving ALL is byte-identical for
    # that quantity -- it simply ALSO keeps the intermediate steps the animation
    # needs. ``save_concentration_all_steps=False`` restores the old LAST-only OC
    # (kept as a reversible seam). BUDGET stays LAST (the seepage path reads only
    # the final RIV budget).
    conc_save = "ALL" if save_concentration_all_steps else "LAST"
    flopy.mf6.ModflowGwtoc(
        gwt,
        concentration_filerecord=f"{gwt_name}.ucn",
        budget_filerecord=f"{gwt_name}.cbc",
        saverecord=[("CONCENTRATION", conc_save), ("BUDGET", "LAST")],
        filename=f"{gwt_name}.oc",
    )

    # --- GWF-GWT exchange -----------------------------------------------------
    # Couples the flow solution to transport: GWT reads GWF cell-by-cell flows.
    flopy.mf6.ModflowGwfgwt(
        sim,
        exgtype="GWF6-GWT6",
        exgmnamea=gwf_name,
        exgmnameb=gwt_name,
        filename="gwfgwt.exg",
    )

    manifest = DeckManifest(
        sim_dir=str(sim_dir),
        sim_name=sim_name,
        gwf_name=gwf_name,
        gwt_name=gwt_name,
        model_crs=f"EPSG:{crs.to_epsg()}",
        xorigin=xorigin,
        yorigin=yorigin,
        nrow=nrow,
        ncol=ncol,
        nlay=N_LAYERS,
        delr=delr,
        delc=delc,
        spill_row=spill_row,
        spill_col=spill_col,
        spill_easting_m=spill_cell_east,
        spill_northing_m=spill_cell_north,
        spill_lat=lat,
        spill_lon=lon,
        mass_rate_g_per_day=mass_rate_g_per_day,
        release_rate_kg_s=release_rate_kg_s,
        duration_days=float(duration_days),
        n_transport_steps=n_transport_steps,
        contaminant=contaminant,
        aquifer_k_ms=aquifer_k_ms,
        porosity=porosity,
        river_coupled=river_coupled,
        river_cell_count=river_cell_count,
        river_reach_len_m=float(river_reach_len_m),
        river_conductance_m2_day=conductance if river_coupled else 0.0,
        along_river_source=source_along_river,
    )

    if write:
        sim.write_simulation()
        manifest.files = sorted(
            str(p.relative_to(sim_dir))
            for p in sim_dir.rglob("*")
            if p.is_file()
        )

    return manifest


# Convenience alias matching the design doc's `build_deck` reference
# (design.md section 9 names the function `build_deck`; the kickoff names it
# `build_modflow_deck`). Both resolve to the same implementation.
build_deck = build_modflow_deck
