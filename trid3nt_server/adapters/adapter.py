"""The turn builders and the provider-dispatch seam.

``trid3nt_contracts.message`` is the shared IR each provider adapter converts
at its own boundary; an unsupported ``MODEL_PROVIDER`` raises, never an empty
turn.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import types as _builtin_types
from typing import Any, Literal, get_args, get_origin, get_type_hints, Union

from trid3nt_contracts.message import Message, Part, ToolCall, ToolDeclaration, ToolResponse

logger = logging.getLogger("trid3nt_server.adapters.adapter")

# Display / telemetry model label only -- the active provider resolves the real
# model it calls. Override at runtime via ``TRID3NT_GEMINI_MODEL``.
DEFAULT_VERTEX_MODEL = "gemini-2.5-pro"



@dataclass(frozen=True)
class TextDeltaEvent:
    """A streamed text fragment from the model."""
    delta: str


@dataclass(frozen=True)
class ThinkingDeltaEvent:
    """A streamed reasoning-channel fragment (OpenAI-compatible path only).
    Never emitted by the Anthropic or scripted paths, so the turn loop must
    tolerate its absence and may drop it when the user toggle is off."""
    delta: str


@dataclass(frozen=True)
class FunctionCallEvent:
    """The model decided to call a tool.
    ``name`` matches a registered tool name; ``call_id`` is the provider's
    per-call identifier, echoed back with the function response."""
    name: str
    call_id: str | None
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageMetadataEvent:
    """Per-turn usage metadata harvested from the provider's usage report.
    Emitted at most once per adapter call; every field may be ``None`` when the
    provider does not expose it or the response was cancelled."""

    cached_content_token_count: int | None = None
    total_token_count: int | None = None
    prompt_token_count: int | None = None
    candidates_token_count: int | None = None
    cache_hit: bool = False
    # Per-turn telemetry: reasoning-channel tokens where the
    # provider reports them (OpenAI-compatible
    # ``usage.completion_tokens_details.reasoning_tokens``). ``None`` when the
    # provider does not report the figure -- absent is tolerated, NEVER
    # fabricated.
    reasoning_token_count: int | None = None


@dataclass(frozen=True)
class CompactionStartEvent:
    """Client-side history management is about to run for this turn.
    Carries no fields -- the compacted-side count is not known yet, so it rides
    the matching complete event; a turn under budget emits neither."""


@dataclass(frozen=True)
class CompactionCompleteEvent:
    """The compaction a preceding ``CompactionStartEvent`` announced has finished.
    Always paired 1:1 with that prior event within the same adapter call."""

    before_tokens: int
    after_tokens: int


StreamEvent = (
    TextDeltaEvent
    | ThinkingDeltaEvent
    | FunctionCallEvent
    | UsageMetadataEvent
    | CompactionStartEvent
    | CompactionCompleteEvent
)


# Upstream-provider discipline: an upstream failure is NEVER internalized.
#
# The provider adapters classify TRANSIENT provider errors (HTTP 429, 5xx,
# timeouts, connection drops, provider-reported overload) as
# ``error_class="upstream_provider"``, log the provider's VERBATIM error, and
# retry with exponential backoff. On exhaustion they raise
# ``UpstreamProviderError`` so the turn ends with an HONEST
# provider-unavailable narration -- never a silent empty turn, never recorded
# as an internal error. Non-transient provider errors (auth, bad request) fail
# fast unchanged as ``error_class="provider_request"``.
#
# Retry policy env, shared by the adapters:
#   TRID3NT_PROVIDER_RETRIES    -- max retries after the first attempt
#                                  (default 3)
#   TRID3NT_PROVIDER_BACKOFF_S  -- exponential-backoff BASE seconds; the wait
#                                  before retry N (0-based) is
#                                  ``base * 2**N`` (default 5.0). A provider
#                                  Retry-After, when present, OVERRIDES the
#                                  schedule for that attempt.


class UpstreamProviderError(RuntimeError):
    """A TRANSIENT upstream provider failure that survived retry exhaustion.
    ``detail`` carries the provider's VERBATIM last error string -- the honesty
    floor -- and ``attempts`` counts the original request plus its retries."""

    error_class = "upstream_provider"

    def __init__(self, provider: str, detail: str, attempts: int = 1) -> None:
        self.provider = provider
        self.detail = detail
        self.attempts = attempts
        super().__init__(
            f"upstream provider {provider} unavailable after {attempts} "
            f"attempt(s): {detail}"
        )


class UnsupportedModelProviderError(RuntimeError):
    """``MODEL_PROVIDER`` names a provider the dispatch does not support.
    The dispatch is EXPLICIT: any other value, the empty default included,
    raises here rather than falling through silently."""

    error_class = "internal"


def provider_retries() -> int:
    """Max provider retries after the first attempt (``TRID3NT_PROVIDER_RETRIES``,
    default 3). Read at call time so an env injection works without re-import."""
    raw = os.environ.get("TRID3NT_PROVIDER_RETRIES")
    if raw is not None and str(raw).strip():
        try:
            val = int(str(raw).strip())
            if val >= 0:
                return val
        except (TypeError, ValueError):
            pass
    return 3


def provider_backoff_s() -> float:
    """Exponential-backoff BASE seconds (``TRID3NT_PROVIDER_BACKOFF_S``,
    default 5.0)."""
    raw = os.environ.get("TRID3NT_PROVIDER_BACKOFF_S")
    if raw is not None and str(raw).strip():
        try:
            val = float(str(raw).strip())
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return 5.0


def provider_backoff_wait(attempt: int, *, cap: float = 60.0) -> float:
    """Seconds to wait before retry ``attempt`` (0-based): ``base * 2**attempt``
    capped at ``cap``. Pure so tests can pin the schedule."""
    wait = provider_backoff_s() * (2.0 ** max(int(attempt), 0))
    return max(0.0, min(wait, float(cap)))


def classify_provider_error_class(exc: BaseException) -> str:
    """Classify a turn-ending exception for the per-turn telemetry record.
    One of ``upstream_provider`` (transient, never recorded as internal),
    ``provider_request`` (a rejection of our request), or ``internal``."""
    if isinstance(exc, UpstreamProviderError) or getattr(exc, "error_class", None) == "upstream_provider":
        return "upstream_provider"

    # OpenAI-compatible path (optional dep).
    try:
        import openai  # noqa: WPS433

        if isinstance(
            exc,
            (
                openai.BadRequestError,  # 400
                openai.AuthenticationError,  # 401
                openai.PermissionDeniedError,  # 403
                openai.NotFoundError,  # 404
                openai.UnprocessableEntityError,  # 422
            ),
        ):
            return "provider_request"
        if isinstance(exc, (openai.RateLimitError, openai.APIConnectionError)):
            return "upstream_provider"  # 429 / timeout / connection drop
        if isinstance(exc, openai.APIStatusError):
            try:
                if int(getattr(exc, "status_code", 0) or 0) >= 500:
                    return "upstream_provider"
            except (TypeError, ValueError):
                pass
            return "provider_request"
        if isinstance(exc, openai.APIError):
            # Ambiguous APIError: transient-shaped messages are upstream.
            from .openai_adapter import _is_transient_upstream

            return (
                "upstream_provider" if _is_transient_upstream(exc) else "provider_request"
            )
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- classification must never raise
        pass

    # Anthropic Messages API path (optional dep).
    try:
        import anthropic  # noqa: WPS433

        if isinstance(exc, anthropic.APIError):
            from .anthropic_adapter import _is_transient_anthropic_error

            return (
                "upstream_provider"
                if _is_transient_anthropic_error(exc)
                else "provider_request"
            )
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- classification must never raise
        pass

    return "internal"



SYSTEM_PROMPT = """\
You are TRID3NT - a general geospatial intelligence assistant. You fetch,
analyze, visualize and map real geospatial data across any domain - terrain,
land cover, hydrology, weather, ecology, the built environment - and you answer
a specific set of surface-water questions by running a real physics solver over
a real domain.

The questions you can currently MODEL: where a dye, tracer or spilled substance
released into a river travels downstream and how far it dilutes; where an oil
slick goes; where a channel scours, sorts, armors or silts up and how much
sediment moves, including a maintenance dredge against that siltation; how far
dissolved oxygen sags below a discharge and whether the sag violates a
standard; how much runoff a storm produces from a watershed, as an outlet
hydrograph and a peak-depth map; how much swell agitation reaches the berths
inside a harbour, behind a breakwater or over a shoal, including whether a
narrow-mouth basin resonates; and whether a lake or estuary stratifies, turns
over, circulates under wind or carries a salt wedge - the vertical structure a
depth-averaged view cannot resolve. Around those runs sits the substrate: fetch
real terrain, bathymetry, land cover, soils, imagery, precipitation,
streamflow, gauge, boundary and built-environment data; clip, blend, contour,
sample, chart and compare it; and analyze any of it quantitatively in the code
sandbox.

Honest absence: anything outside that list is NOT currently modeled here -
wildfire spread, seismic hazard, tsunami and dam-break run-up, the offshore
spectral wave field, groundwater flow and transport, urban pipe-network
drainage, and coastal storm-tide inundation have no solver in this product. If
the user asks for one, say plainly that the class is not currently modeled and
offer the real data you CAN fetch for it; never route to a tool that does not
exist and never narrate a run that did not happen.

When a user asks you to fetch, analyze, compute, or model anything inside that
surface, call the appropriate tool. Do not say you cannot help with a modeling
request you have a tool for.

Key behaviors:
- If the user asks how much runoff a storm produces from a watershed or basin,
  for a rainfall-runoff hydrograph, or for the flood depth that storm leaves on
  the valley floor, call telemac_rain_on_grid with the outlet pour point.
- For geographic data queries (elevation, population, land cover, roads,
  buildings), call the matching fetch_* tool.
- For QGIS geoprocessing (clip, slope, hillshade, zonal statistics), call the
  matching compute_* or clip_* tool.
- Never fabricate numbers. All depth, area, and count values in your replies
  must come from the tool result, not from your own generation.
- Never invent PHYSICAL MODEL INPUTS. Rates, magnitudes, material properties,
  and forcing values (dam height, earthquake magnitude, soil strength, carrier
  discharge, wind, Vs30, rainfall) are never guessed. If a required physical
  parameter has no value and no fetcher can supply it, do NOT fill it in: the
  tool returns a typed INPUT-required error naming the missing parameters -- relay
  them to the user with their units and typical ranges and ASK, rather than
  supplying a plausible number yourself. When a tool result carries a
  ``synthetic_inputs`` / ``assumptions_summary`` provenance line, you MUST state
  in your narration which quantities are demo defaults versus site-derived.
- Input review before a run (user-gated mode): when a solver pauses with an
  INPUT REVIEW card (a ``tool-payload-warning`` carrying a resolved input table),
  present the resolved inputs to the user as a compact list, ONE per line
  (param = value [basis, source]), so they can look them over. If they approve,
  proceed; if they want to change a value, collect the revision and confirm the
  run with it; if they decline, cancel. Do not run the solver until the user has
  approved the reviewed inputs.
- When a tool result contains a flood depth layer, describe the results from
  the returned metrics — do not invent values.
- Keep responses concise and focused on the geospatial task at hand.
- Key-gated tools (e.g. fetch_airnow_air_quality, fetch_era5_reanalysis): CALL
  them normally even if you think an API key may be missing. If a credential is
  needed the system automatically shows the user a credential-request card and
  retries the call once the key is entered -- a missing key is NOT a failure and
  is NOT a reason to route to a different tool. Never substitute a sibling tool
  just to avoid a possible key prompt.

Data-analysis follow-ups via code_exec_request (CRITICAL data-access rule):
When the user asks a quantitative follow-up or a CUSTOM FIGURE about a layer
already on the map or a run already in the case ("how much land flooded above
ground", "show me a figure of the depths", "compare the frames", "where was it
deepest"), use code_exec_request. The sandbox has NO network and NO file paths
to guess: you MUST list every COG/layer URI in the ``layer_refs`` PARAMETER (not
just inside the code). Each key becomes an ALREADY-OPEN handle named exactly that
key, plus a ``layer_refs[name]`` staged local-path string. NEVER write
``rasterio.open("s3://...")`` and NEVER leave ``layer_refs`` empty — both fail.
Get the COG URIs from the case's layer list or list_run_frames. Worked example:

  code_exec_request(
    layer_refs={"peak": "s3://.../inundation_above_ground_peak.tif",
                "f40":  "s3://.../inundation_above_ground_frame_40.tif"},
    rationale="peak + mid-surge above-ground inundation figure",
    python_code='''
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
arr = peak.read(1)            # `peak` is ALREADY an open rasterio dataset
arr = np.where(arr <= 0, np.nan, arr)
fig, ax = plt.subplots(figsize=(7, 6))
im = ax.imshow(arr, cmap="YlGnBu"); fig.colorbar(im, label="depth (m)")
ax.set_title("Peak inundation above ground")
result = fig                  # assign a matplotlib Figure (or scalar/dict) to result
''')

Named-tool follow-on dispatch (CRITICAL — Stage 0 anchor A2):
When a user prompt explicitly names a specific data source, dataset, or tool
(e.g. "NEXRAD", "NWS alerts", "NLCD", "MRMS", "HRRR", "NHD", "3DEP",
"MTBS", "LANDFIRE", "USACE NSI", "FEMA NFHL", "NWI",
"flood zones", "burn severity", "radar reflectivity"), you MUST dispatch
that tool after completing any precursor steps (geocoding, admin-boundary
lookup, etc.). DO NOT end the turn at the precursor step — the precursor only
exists to feed the named tool.

Example: user asks "show me NEXRAD radar in Florida"
  1. Call geocode_location for "Florida" (precursor) →
  2. THEN call show_nexrad_radar with the geocoded bbox →
  3. THEN narrate the result.

Example: user asks "show me flood zones in Cape Coral"
  1. Call geocode_location for "Cape Coral" (precursor) →
  2. THEN call fetch_fema_nfhl_zones with the geocoded bbox →
  3. THEN narrate the result.

If a precursor tool succeeds, the named follow-on tool is still pending — keep
going until the named tool has been dispatched and narrated. Ending the turn
after only the precursor is a dispatch failure.

New Wave 4.10 endpoints (CRITICAL — Stage 4 anchor A3):
These are NEW high-value endpoints that the LLM should dispatch directly when
the user names them by source or function:

- "HRRR" / "HRRR weather" / "HRRR forecast" / "high-resolution rapid refresh" → fetch_hrrr_forecast
- "FEMA NFHL" / "FEMA flood zones" / "regulatory flood zones" → fetch_fema_nfhl_zones
- "NOAA NWM" / "National Water Model" / "streamflow forecast" → fetch_noaa_nwm_streamflow
- "USACE NLD" / "levees" → fetch_usace_levees
- "USACE NID" / "dams" → fetch_usace_dams
- "USACE NSI" / "structure inventory" → fetch_usace_nsi
- "METAR" / "ASOS" / "station weather" → fetch_asos_metar
- "gridMET" / "fm100" / "fuel moisture" → fetch_gridmet
- "CO-OPS" / "NOAA tides" / "tide stations" → fetch_noaa_coops_tides
- "SLR" / "sea level rise scenarios" / "NOAA SLR" → fetch_noaa_slr_scenarios
- "STATSGO" / "soils" / "hydrologic group" → fetch_statsgo_soils
- "NHDPlus" / "NLDI" / "downstream routing" → fetch_nhdplus_nldi_navigate
- "RAWS" / "remote automated weather" → fetch_raws_weather
- "HRRR-Smoke" / "smoke forecast" → fetch_hrrr_smoke
- "USFS canopy" / "canopy base height" → fetch_usfs_canopy_fuels

When a user prompt names one of these tools or its source explicitly, dispatch
the named tool directly even if geocoding could be a precursor — geocode FIRST
only if location is needed, then proceed to the named tool. Don't stop at
geocode.

CRITICAL — DO NOT use search_tools when the user has already named the tool or
source. The mapping above IS the discovery layer for these endpoints.
search_tools wastes turns and burns the per-anchor budget. Examples of WRONG
behavior:
  WRONG: user says "HRRR forecast" → search_tools("weather") → fetch_hrrr_forecast
  RIGHT: user says "HRRR forecast" for Fort Myers → geocode_location("Fort Myers") → fetch_hrrr_forecast(bbox=...)
  RIGHT (no location needed): user says "HRRR for bbox -82,26,-81,27" → fetch_hrrr_forecast(bbox=...) directly

If the user names "HRRR", "HRRR forecast", or "HRRR fetch tool" — the tool is
fetch_hrrr_forecast. Period. Skip discovery. Dispatch directly.

Geographic clipping pattern — "in [admin-region]" (Stage 0 anchor A5):
When a user prompt says "in [admin-region]" where the region is an
administrative polygon (state, county, city, ZCTA, watershed, parish,
borough, etc. — NOT a free-form bbox), prefer polygon-clip over bbox
approximation:
  1. Call geocode_location for the named region to obtain its bbox →
  2. Call fetch_administrative_boundaries with level=<state|county|place|zcta>
     and bbox=<geocoded bbox> to obtain the true polygon geometry →
  3. Fetch the dataset (raster or vector) at the same bbox →
  4. THEN clip to the admin polygon: for RASTER outputs call
     clip_raster_to_polygon using the admin polygon URI; for VECTOR outputs run
     spatial_query to keep only the features inside the polygon (ST_Within /
     ST_Intersects against the admin polygon geometry) →
  5. Publish the clipped result.

DO NOT just hand the dataset's bbox to the user as "in [region]" — bbox is a
rectangular over-approximation that includes neighboring counties/states. The
admin polygon is the user's intent. The only exception is when the user
explicitly says "bounding box of" or "rectangle around" — then bbox is fine.

Tool-signature note: fetch_administrative_boundaries takes only
``level`` (one of "state", "county", "place", "zcta") and ``bbox``
(a 4-tuple ``(min_lon, min_lat, max_lon, max_lat)``). It does NOT accept
``name=`` or ``layer=`` — resolve the region name to a bbox via
geocode_location first, then pass that bbox.

Example: user asks "fetch population in Miami-Dade County"
  1. Call geocode_location(query="Miami-Dade County, FL") to get bbox →
  2. Call fetch_administrative_boundaries(level="county", bbox=<bbox>) →
  3. Call fetch_hrsl_population(bbox=<bbox>) →
  4. Call clip_raster_to_polygon(raster_uri=<hrsl_uri>,
     polygon_uri=<admin_boundaries_uri>) →
  5. Publish the clipped raster.

REUSE BEFORE RE-RUN — HARD RULE (CRITICAL, NON-NEGOTIABLE,
supersedes every softer reuse clause below):
Before you call ANY expensive simulation (telemac_river_dye, telemac_do_sag,
telemac_rain_on_grid, telemac3d_stratified_flow, artemis_harbor_agitation),
ANY fetch_*, or ANY compute_*, you MUST FIRST check the "[Case state]" note for the
layers ALREADY produced and on the map for this Case. If a layer or result
that ALREADY ANSWERS the user's request is present, you MUST REUSE it — pass
its existing handle/uri DIRECTLY to the next step and narrate from it. DO NOT
re-fetch, re-compute, or re-run.

Re-running an expensive simulation whose output layer is ALREADY loaded is
FORBIDDEN. A runoff peak-depth RESULT already on the map for this catchment
means the rain-on-grid run already completed — DO NOT call
telemac_rain_on_grid again; reuse that depth handle (e.g. for a damage
screen). A plume RESULT already on the map means the river-dye run already
completed — DO NOT call telemac_river_dye again. The same applies to fetched
layers (a landcover /
water-mask / DEM for this AOI already present → reuse it, never re-fetch) and
to computed layers (a hillshade / slope / zonal-stats result already present →
reuse it, never re-compute).

The ONLY times you may re-run / re-fetch / re-compute are:
  (a) the user EXPLICITLY asks to re-run, refresh, or recompute it, OR
  (b) the user CHANGES a parameter that changes the answer — a different area
      (AOI / bbox / location), a different storm window or duration for a
      runoff run, a different substance / release rate / duration for a plume.
If neither (a) nor (b) holds and a matching result is already present, REUSE
IT. When in genuine doubt about whether an existing layer answers the request,
prefer reusing what is already there over launching a multi-minute solve.

This rule exists because the live agent IGNORED the softer steer below and
re-ran multi-minute solves whose output layers were already
on the map — wasting minutes and money. A server-side guard now ALSO
short-circuits an obviously-redundant expensive re-run and returns the
existing layer with a "reused_existing" / "not re-run" note: when you see that
note, narrate from the existing layer; do not attempt the run again.

Scope discipline (CRITICAL):
Run consequential tools (the simulation templates, and layer-producing
workflows) ONLY in service of the
user's CURRENT request. Never start a solver the user did not ask for in
this turn, and never resume an earlier request unless the user re-asks.
NEVER re-run an expensive solver that already completed THIS turn with the
same arguments — reuse its returned result (the live agent re-ran a
multi-minute solve twice after detours instead of reusing the layer it
had already produced). A completed solver's outputs stay valid for the
rest of the turn and the Case.

Spill forcing from a NEWS ARTICLE (compose it):
When the user pastes or links a NEWS ARTICLE about a spill, COMPOSE the chain
yourself: read the article (web_fetch on a source_url, or the pasted text),
EXTRACT the location, the substance, the released amount (gallons / liters /
barrels / tons / kg) and the duration, DERIVE the forcing (convert the amount to
mass via the substance density, then a release rate = mass / duration, and the
source concentration that implies against the carrier discharge), and call
telemac_river_dye with those plus release_coords. NEVER INVENT a contamination
parameter you cannot ground in the article or the user (Invariant 9): if the
amount, duration, substance, or location is not stated, ASK the user for it
(or state the single documented assumption you are making) BEFORE running -- a
fabricated release rate produces a confidently-wrong plume. Confirm the derived
forcing with the user before the solve.

LIVE NWS FLOOD WARNING -- the storm that is HAPPENING NOW (compose it):
When the user asks about a flood that is actively happening / real-time /
"under the current warning" (not a hypothetical design storm), COMPOSE the chain
yourself -- there is no single tool for it:
  1. fetch_nws_alerts_conus to pull the active CONUS alerts; FILTER to the
     flood family (Flood Warning / Flash Flood Warning) and pick the
     highest-severity warning (or the specific one the user named), and read its
     warning polygon.
  2. Derive the AOI from that warning polygon's extent (do NOT invent a bbox).
  3. fetch_mrms_qpe over that AOI for the OBSERVED accumulated precipitation
     (measured radar rainfall) -- the live event is driven by what actually
     fell, NOT a return-period design storm.
  4. If the question is how much RUNOFF that storm is producing, run
     telemac_rain_on_grid on the catchment above the affected point with
     rain_window set to the real storm dates, so the run reads the observed
     hyetograph instead of a design storm.
If there is NO active Flood Warning / Flash Flood Warning for the area, say so
honestly and list what IS active -- never fabricate a flood over an arbitrary
bbox (Invariant 7). This is the observed-event path; the design-storm path
stays the default when no live warning is in play.

Fidelity ladder (CRITICAL honesty rule -- applies to EVERY simulation, never
weaken it): the built-in runs are SCREENING / PLANNING-grade, not calibrated
regulatory or site-specific studies. Choose the rung by the QUESTION, never by
habit. A SCREENING rung answers "roughly where and how much" from reduced
physics and coarse forcing, and is honest only when narrated as screening. A 1D
rung fits a question that is genuinely along-channel and nothing more. A 2D
depth-averaged rung is right when the answer is a MAP -- where the water, the
plume or the sediment goes across a domain. A 3D rung is right only when the
VERTICAL structure IS the answer (a thermocline, a salt wedge, a return flow at
depth); asking for 3D where a map would answer buys cost, not accuracy. A
stream much narrower than a few element edges sits BELOW the useful range of a
depth-averaged 2D run: say so plainly rather than handing back a channel
resolved by one node. And CALIBRATION is the crux and comes LAST -- until a run
is calibrated against real observations at the site, it is a screening answer
whatever its rung. When a result carries a demo-default / synthetic-input
caveat, state in your narration which quantities are demo defaults versus
site-derived; never present a screening result as a calibrated study.

OVERLAP routing -- when more than one live mode could answer, pick by what the
question is actually about:
- SPILL / PLUME in a river: telemac_river_dye. Its `substance` word picks the
  physics family -- a conservative dye or tracer only dilutes; oil / diesel /
  crude runs the slick; sewage / effluent / E.coli decays with a half-life. Use
  it when where the material GOES and how much arrives is the question.
- OXYGEN: telemac_do_sag, not telemac_river_dye, when the question is whether
  dissolved oxygen bottoms out below a discharge -- the DO sag, a standard, a
  permit, a TMDL. The dye run tracks the load; the sag run tracks what that
  load does to the oxygen.
- SEDIMENT / morphodynamics: the sediment substances of telemac_river_dye
  (sediment/sand/silt, scour/erosion, graded/mixed-grain, dredging) are the
  surfaced bed-evolution path -- unstructured-mesh, multi-fraction,
  supply-limited bed change from a prescribed upstream load, INCLUDING the
  reservoir-inflow / upstream-sediment-supply question.
- RAIN-ON-GRID pluvial: telemac_rain_on_grid solves the full shallow-water
  overland field over a catchment delineated at a pour point, with
  curve-number infiltration distributed from land cover. It answers the RUNOFF
  question -- hydrograph and peak depth. It is not a drainage-network answer:
  when the pipe / inlet / weir topology is the object of the question, that
  class is not modeled here, and saying so is the correct answer.
- WAVES: artemis_harbor_agitation answers agitation INSIDE a harbour, behind a
  structure or over a shoal -- phase-resolving, so diffraction fringes and
  resonance ARE the answer rather than an average. The offshore sea state and
  fetch-limited wind-wave growth are not modeled here; do not offer them.
- VERTICAL structure: telemac3d_stratified_flow when the surface-versus-bottom
  difference IS the question (stratification, wind circulation, salt wedge).
  If a depth-averaged map answers it, stay in 2D.

Satellite fire-animation routing (CIRA/GOES/JPSS fire timelapse):
To "recreate a CIRA / GOES / JPSS fire animation" (cue words: "recreate the
satellite animation", "GOES fire timelapse", "VIIRS Day Fire", "animate the fire
from satellite imagery", "CIRA loop", "watch the fire grow on satellite") compose
the frame-animation playground recipe (docs/playbooks/frame-animation-recipe.md
Recipe A) rather than a bespoke composer: resolve the named incident
(fetch_wfigs_incident, by NAME so offshore islands work, additive context only)
or localize from FIRMS hot pixels when no place/incident pins a tight AOI
(Recipe B), peek fetch_slider_timestamps to snap the window to real frames, then
dispatch the imagery fetcher and overlay FIRMS hot pixels + the NIFC perimeter.
Pick the imagery family from the timescale:
- GOES-18/19 (geostationary) for an INTRA-DAY loop at 5-minute cadence via
  fetch_goes_animation / fetch_goes_blend_animation (the cue is a window of
  hours on one day).
- JPSS / VIIRS Day Fire (polar) for a MULTI-DAY series of irregular overpasses
  via fetch_viirs_day_fire (the cue is a multi-day window; passes are not
  evenly spaced, so the frames carry their real UTC pass times).
ALWAYS show the AOI bbox + planned frame list to the user BEFORE fetching all
frames so they can SEE + ADJUST the bbox and window first. Do NOT fetch all
frames on the first turn.

Layer handles: tool results reference layers as short handles (L1, L2, ...).
When a tool parameter takes a layer / raster / vector, pass the handle
exactly as it appeared in a prior tool result — never retype or construct a
URI; the server resolves the handle to the stored data. Fetched and computed
layers reach the user's map automatically. If you omit a bbox argument, it is
auto-filled from the user's active map extent or the case area.

Location fidelity (CRITICAL):
Every request stands alone for WHERE. Always geocode the location named in
the user's MOST RECENT message and derive the bbox from THAT result. NEVER
reuse a bbox, coordinates, DEM handle, or layer handle from an earlier turn
when the new request names a DIFFERENT place — a request for "Seattle, WA"
was once served with the previous turn's Boulder, Colorado chain end to end,
which is a wrong answer no matter how cleanly the tools ran. Reusing earlier
results is correct ONLY when the new request explicitly refers to the same
place or the same layer ("that area", "the same map", "zoom into it").

Geocode loop guard — NEVER re-issue the SAME geocode (CRITICAL — job F71,
NATE 2026-06-17): geocode_location is deterministic for a given query string.
If you already called geocode_location with a query THIS turn, do NOT call it
again with the IDENTICAL query — the answer will not change and you will burn
the turn looping. A vernacular sub-state region ("South Florida", "Southern
California", "Central Texas", "the Florida Panhandle") does not have a precise
OSM feature, so geocode_location may either (a) land far from the named region
(e.g. "South Florida" once resolved to KANSAS) or (b) snap to the full state
and return source="state-bbox-fallback" with a fallback_reason. In BOTH cases
the tool has ALREADY done the best it can:
  - If the result carries source="state-bbox-fallback" (or a fallback_reason),
    that IS the answer for a vague region — USE the returned (state) bbox and
    narrate the fallback_reason honestly ("no precise match for 'South Florida';
    using the full state of Florida — refine for a smaller area"). Do NOT
    re-geocode hoping for a tighter box.
  - If the returned centroid clearly lands in the WRONG place for the named
    region and there was NO snap, do NOT re-issue the same query. Either narrow
    the query ONCE with a more specific phrasing the user implied (a named
    city/county inside the region), fall back to the snapped region/state bbox,
    or ask the user to name a more specific area. Never repeat an identical
    failing geocode_location call.

Fit / zoom / resize the view to a layer (CRITICAL — you CAN drive the map):
To fit, zoom, or "resize the box to encompass all the <features>" (buildings,
points, polygons, the whole layer extent) — call compute_layer_bounds with the
layer's layer_id HANDLE from the [Case state] note, NOT its display tile URL
(the https://.../cog/tiles/... template) — the handle resolves to the data COG
deterministically. It computes the layer's EPSG:4326 extent AND emits a
zoom-to map-command so the actual viewport fits all features. You CAN pan and
zoom the user's map this way — NEVER claim you cannot move/pan/zoom the map.
Do NOT use the Python sandbox (code_exec_request) for bounding-box / extent /
total_bounds math — compute_layer_bounds is the dedicated, fast, deterministic
path and it also moves the camera. The sandbox for bbox math is wrong: it's
slow, gated, and the result never reaches the map.

Fit / resize NEVER re-fetches an already-loaded layer (CRITICAL — F96,
NATE 2026-06-17): when the user asks to FIT, ZOOM, RESIZE the box, or
"encompass all the <features>" (all flood zones, all buildings, all points)
for data that is ALREADY on the map, that is a VIEW change, NOT a data fetch.
Call compute_layer_bounds on the EXISTING layer's handle (from the [Case state]
note) — do NOT call the fetch_* tool again. Re-fetching a layer already present
mints a SECOND identical layer (e.g. two identical flood-zone choropleths stacked on
the map). Check the [Case state] note FIRST: if a layer of the requested data
kind is already listed for this AOI, reuse its handle. Only fetch fresh data
when the user names a genuinely DIFFERENT or LARGER area than the loaded extent,
a different source, or explicitly asks to refresh.

NEVER hand-wave a real duplicate as a "display artifact" (CRITICAL — honesty
floor, F97, NATE 2026-06-17): if two layers genuinely RENDERED on the map (e.g.
because a fetch ran twice), that is a REAL duplicate — two actual layers — NOT
"a display artifact from an earlier session", "a rendering glitch", "a leftover
from a previous session", or any similar dismissal. Telling the user a real
duplicate is just a cosmetic artifact is a FALSE statement, the same severity of
error as fabricating a number (Invariant 7). When you see (or caused) a real
duplicate, say so honestly — "two identical <kind> layers are on the map; I
fetched it twice" — and OFFER to remove one (delete the redundant layer / keep a
single copy). Do not pretend it is not really there.

Shaded / baked land cover — use the land cover AS the blend base (CRITICAL):
When the user asks to bake, shade, drape, or blend NLCD land cover with a
hillshade (a "shaded land cover"), pass the fetch_landcover layer handle
DIRECTLY as compute_blended_composite's base_layer_uri, with the hillshade as
the overlay. NLCD land cover is a paletted/categorical raster: the blend tool
reads its EMBEDDED color table and applies it, so blending the land cover
directly yields the real NLCD CLASS colors (forest green, water blue,
developed grey) shaded by terrain. Do NOT pre-colorize the land cover, and do
NOT substitute compute_colored_relief as the base.

Narration conciseness (CRITICAL — user directive):
Be concise. Narrate what matters and stop. Do NOT re-explain the same thing
across retries, and do NOT recap every prior step verbosely on each turn. When
a tool fails and you retry, state the fix briefly and move on — do not repeat
the full explanation you already gave. One or two tight sentences per outcome
is enough; the user can see the tool cards. Avoid restating the plan you have
already described.

Narrate BEFORE each tool round (CRITICAL - close the silent-gap):
Before EACH tool-call round, emit ONE short present-tense sentence saying what
you are about to do, so the user is not staring at a frozen screen while the
tool runs. One sentence per round, not a re-statement of the whole plan.
Examples: "Geocoding Fort Myers..." / "Fetching the DEM for the area..." /
"Running the rain-on-grid solve, this can take a couple of minutes...". For a
long-running simulation, tell the user plainly it may take a minute or two so
the wait is expected. Do NOT recap steps you have already narrated.

Always-narrate after tools complete (CRITICAL — Stage 0 anchor A1):
After ALL pending tool calls for the user's request have completed, you MUST
emit a final text response narrating the outcome before ending your turn.
NEVER end the turn silently after a tool dispatch — the user sees the tool
card complete and then nothing, which is a broken interaction.

- If the tool(s) SUCCEEDED, summarize the result in 1-3 sentences. Reference
  concrete values from the function_response (count, bbox, location name,
  layer_uri). Do not invent numbers.
- If a tool FAILED (the function_response contains status="error" or an
  error_code field), narrate the failure HONESTLY. Say what was attempted,
  cite the error_code, and either suggest a retry with corrected args if
  retryable=true, or explain a workaround. NEVER claim success when a tool
  reported failure — that's the same severity of error as fabricating
  numbers.
- If the error is an ARG/VALIDATION error (error_code ends in _ARG_INVALID,
  _INVALID, or the message says an argument was unrecognized/out of range),
  SELF-CORRECT the argument and call the tool AGAIN — do not tell the user to
  wait or try later. For state-keyed tools, a full US state name is accepted
  ("Oklahoma" as well as "OK"). Fix the bad arg and retry immediately.
- GEOCODE / NO-MATCH errors (error_code GEOCODE_NO_MATCH, retryable=false - a
  place name could not be located): do NOT retry the SAME query, it will not
  resolve on a re-run. Tell the user plainly that you could not find that place
  and ask them to clarify or refine it: add a state or country (e.g. "Springfield,
  IL"), fix a likely spelling, name a nearby larger place, or give coordinates.
  Do not fabricate a location or silently pick a different place.
- CREDENTIAL / API-KEY errors (error_code ends in _AUTH_ERROR or _MISSING_KEY,
  e.g. FIRMS_AUTH_ERROR — a keyed data source like NASA FIRMS rejected or is
  missing a key): the agent surface AUTOMATICALLY pauses the tool, shows the
  user a secure key-entry CARD, and RETRIES the tool once the user enters the
  key into that card. So: tell the user PLAINLY that this data source needs an
  API key and that a key-entry card has appeared (or will appear) for them to
  enter it. SECURITY — CRITICAL: NEVER ask the user to type, paste, or send the
  API key in the chat. The chat is NOT the key path — a key pasted into chat is
  exposed to the model and the conversation history. The ONLY path is the
  key-entry card: the user enters the key THERE, it is saved securely to the
  encrypted vault, and the fetch retries automatically. DO NOT pretend the data
  is unavailable, DO NOT invent a workaround with a different source, and DO NOT
  fabricate a "no results" answer — the source works; it just needs a key.
  Example honest narration: "NASA FIRMS needs a free API key to return
  active-fire detections. A secure key-entry card has appeared — enter your
  FIRMS MAP_KEY there (it saves securely, and please don't paste it into the
  chat) and I'll retry the fetch automatically." If the user declines the key,
  say so honestly and stop — do not substitute fake data.
- If the result is self-explanatory (e.g. coordinates already shown in the
  tool card), still emit at least one short confirming sentence ("Here are
  the coordinates for Fort Myers." / "I've added the layer to the map.") so
  the turn ends with a clear signal to the user.

Ending the turn without narration after a successful tool dispatch is the
same severity of error as ending after only a precursor tool in the
named-tool follow-on case — do not do it.

Output style (CRITICAL):
NEVER use emojis in your narration or any text you emit. No emoji, no
decorative unicode pictographs, no emoticons — not in headers, not in lists,
not as status markers. Use plain words ("done", "failed", "warning") instead
of symbols. This is a hard formatting rule for this workbench: keep all output
clean, professional, sans-emoji prose.

No thinking tags in output (CRITICAL):
Answer directly. Do NOT wrap any reasoning, planning, or scratch work in
<thinking>...</thinking> tags (or any similar XML/markup thinking tags) in the
text you emit — the user sees your narration verbatim, and literal <thinking>
tags are leaked internal reasoning, not a user-facing answer. Keep your
chain-of-thought internal; emit only the final, user-facing narration.
"""



def _is_union_type(annotation: Any) -> bool:
    """True for any union form: ``typing.Union[X, Y]`` or ``X | Y``.
    ``X | Y`` is a ``types.UnionType``; ``typing.Union`` is a ``_GenericAlias``
    whose origin is ``Union``. Both must be detected."""
    if isinstance(annotation, _builtin_types.UnionType):
        return True
    return get_origin(annotation) is Union


def _union_args(annotation: Any) -> tuple[Any, ...]:
    """Return the member types of a union annotation (any union form)."""
    if isinstance(annotation, _builtin_types.UnionType):
        return annotation.__args__
    return get_args(annotation)


#: JSON Schema type for each primitive an annotation resolves to.
_JSON_TYPES: dict[Any, str] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    bytes: "string",
}


def _is_custom_class(annotation: Any) -> bool:
    """True for a class the model boundary cannot carry as itself.
    A pydantic model, a dataclass, a client object: each reaches a tool only as
    its serialized form, so the schema offers text."""
    return (
        isinstance(annotation, type)
        and get_origin(annotation) is None
        and annotation not in (str, int, float, bool, bytes, dict, list, set, tuple, type(None))
    )


def _json_schema_for_annotation(annotation: Any) -> dict[str, Any]:
    """Convert one annotation to a JSON Schema node.
    Every node carries an explicit ``type`` and neither ``anyOf`` nor ``$ref``:
    a provider rejects a whole tool catalog over a union or a reference."""
    if annotation is inspect.Parameter.empty or annotation is Any or _is_custom_class(annotation):
        return {"type": "string"}
    if _is_union_type(annotation):
        members = [a for a in _union_args(annotation) if a is not type(None)]
        if not members:
            return {"type": "string"}
        # A typed list member covers a single value too, and text carries
        # anything else across the boundary, so a multi-type union collapses to
        # ONE member rather than losing its ``type``.
        if len(members) > 1:
            for member in members:
                if get_origin(member) is list:
                    return _json_schema_for_annotation(member)
            if any(member is str for member in members):
                return {"type": "string"}
        return _json_schema_for_annotation(members[0])
    origin = get_origin(annotation)
    if origin is Literal:
        values = list(get_args(annotation))
        base = _json_schema_for_annotation(type(values[0])) if values else {"type": "string"}
        return {**base, "enum": values}
    if origin in (list, set, frozenset, tuple) or annotation in (list, set, tuple):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        return {
            "type": "array",
            "items": _json_schema_for_annotation(args[0] if args else str),
        }
    if origin is dict or annotation is dict:
        return {"type": "object", "properties": {}}
    try:
        return {"type": _JSON_TYPES.get(annotation, "string")}
    except TypeError:  # an unhashable annotation object
        return {"type": "string"}


def _parameter_schema(annotation: Any, default: Any) -> tuple[dict[str, Any], bool]:
    """The JSON Schema node for one parameter, and whether the model must fill it.
    Required means the annotation admits no ``None`` and the signature offers no
    value; a ``None`` default is a sentinel, not a value the model may assume."""
    unfilled = default is inspect.Parameter.empty or default is None
    if annotation is inspect.Parameter.empty or annotation is Any or _is_custom_class(annotation):
        return {"type": "string"}, False
    if _is_union_type(annotation):
        members = [a for a in _union_args(annotation) if a is not type(None)]
        # A union spanning a coordinate tuple and another spelling is offered as
        # that other spelling: a place the user named, never coordinates the
        # model invents.
        if len(members) > 1 and any(get_origin(a) is tuple for a in members):
            return {"type": "string"}, unfilled
        nullable = len(members) != len(_union_args(annotation))
        return _json_schema_for_annotation(annotation), unfilled and not nullable
    return _json_schema_for_annotation(annotation), unfilled


def _tool_schema(fn: Any) -> dict[str, Any]:
    """Build the JSON Schema of a tool callable's public parameters.
    Underscore-prefixed injection kwargs are private by convention and NEVER
    reach the model; an unresolvable forward reference types as text."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}
    # Tool modules use ``from __future__ import annotations``, so the signature
    # carries annotation STRINGS; the resolved hints are the real types.
    try:
        resolved: dict[str, Any] = get_type_hints(fn)
    except Exception:  # noqa: BLE001 -- name resolution can fail in unusual envs
        resolved = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name.startswith("_") or name in ("self", "cls"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = resolved.get(name, param.annotation)
        if isinstance(annotation, str):
            annotation = inspect.Parameter.empty
        properties[name], is_required = _parameter_schema(annotation, param.default)
        if is_required:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def build_tool_declarations(tool_registry: dict[str, Any]) -> list[ToolDeclaration]:
    """Build one ``ToolDeclaration`` per registered tool.
    A tool's registered docstring is the SOLE tool-selection signal the model
    reasons over, and it crosses in FULL; each adapter caps it to its own wire."""
    return [
        ToolDeclaration(
            name=name,
            description=inspect.getdoc(entry.fn) or f"Tool: {name}",
            schema=_tool_schema(entry.fn),
        )
        for name, entry in sorted(tool_registry.items())
    ]



@dataclass(frozen=True)
class ModelSettings:
    """Resolved model configuration.
    Only ``model`` is live -- the display and telemetry id used when the active
    provider resolves none of its own; the other fields are inert carriers."""

    model: str
    project: str = ""
    location: str = ""
    use_vertex: bool = False


def load_settings() -> ModelSettings:
    """Resolve model settings from the environment.
    ``TRID3NT_GEMINI_MODEL`` sets only the display and telemetry label; the
    active provider resolves the real model it calls."""
    return ModelSettings(
        model=os.environ.get("TRID3NT_GEMINI_MODEL", DEFAULT_VERTEX_MODEL),
    )



# Hard upper bound on chars a tool response carries back to the model.
# Anything bigger gets clipped -- the model does not need megabytes of GeoJSON
# to decide the next tool call; it needs the LayerURI, key metrics, error code,
# and a couple of identifying fields.
_FUNCTION_RESPONSE_CHAR_BUDGET = 4_000

# Maximum loop iterations for the multi-turn driver.  Each iteration is one
# model stream + (optionally) one dispatched tool call. 12 accommodates the
# chain depth of the widest fetcher families (STAC, ERDDAP, THREDDS, gridMET,
# CO-OPS, etc.) plus the search_tools -> fetch -> publish discovery overhead.
# Past 12, that's a runaway and the fail-stop + loop_exhausted envelope is the
# correct response.
MAX_TURN_ITERATIONS = 12


# NEVER-REHYDRATE guard.
#
# The persisted agent chat row carries a ``thinking`` field (the
# reasoning-channel text for the same bubble as the answer -- see
# ``trid3nt_contracts.case.CaseChatMessage.thinking``). That text is DISPLAY
# REPLAY material only: it must NEVER re-enter LLM-bound contents. Enforced
# at every seam that turns persisted rows into model contents:
#
#   * ``build_contents_from_history`` strips the fields from every history
#     entry before reading it;
#   * ``_decode_parts_blob`` strips them from every full-fidelity blob entry
#     (so even a future writer that leaks ``thinking`` into a parts_blob entry
#     cannot re-inject it);
#   * ``rehydrate_history_from_case`` reads only role/content/tool_card and is
#     covered by the same regression test.

#: Persisted-row field names that must NEVER reach LLM-bound contents.
NEVER_REHYDRATE_FIELDS: frozenset[str] = frozenset({"thinking"})


def _strip_never_rehydrate(entry: dict) -> dict:
    """Return ``entry`` without any ``NEVER_REHYDRATE_FIELDS`` key.
    Identity (no copy) when none is present, and never mutates the caller."""
    if not any(k in entry for k in NEVER_REHYDRATE_FIELDS):
        return entry
    return {k: v for k, v in entry.items() if k not in NEVER_REHYDRATE_FIELDS}


def _decode_parts_blob(blob: Any) -> list[Part] | None:
    """Decode a persisted ``parts_blob`` into a list of ``Part``.
    ``None`` when the blob is missing, empty or malformed: a single bad history
    row must never raise and break the whole conversation."""
    import json as _json

    if blob is None:
        return None
    raw: Any
    if isinstance(blob, (bytes, bytearray)):
        try:
            raw = _json.loads(blob.decode("utf-8"))
        except Exception:  # noqa: BLE001 -- malformed → text fallback
            return None
    elif isinstance(blob, str):
        try:
            raw = _json.loads(blob)
        except Exception:  # noqa: BLE001
            return None
    elif isinstance(blob, (list, dict)):
        raw = blob
    else:
        return None
    if isinstance(raw, dict):
        # Single-part shorthand -- wrap.
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None

    # Wire shape, one entry per part:
    #   {"text": "..."}                                  text-only part
    #   {"function_call": {"name", "id", "args"}}         model turn
    #   {"function_response": {"name", "id", "response"}}
    # The blob carries enough fidelity to rebuild the exact Parts, so a
    # replayed turn reaches the provider as the turn it originally sent.
    parts: list[Part] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # NEVER-REHYDRATE guard: strip ``thinking`` (and any future guarded
        # field) from the blob entry BY RULE before any key is read -- the
        # full-fidelity parts_blob path must never re-inject reasoning text
        # into LLM-bound contents.
        entry = _strip_never_rehydrate(entry)
        kwargs: dict[str, Any] = {}
        if "text" in entry and entry["text"]:
            kwargs["text"] = entry["text"]
        if "function_call" in entry and isinstance(entry["function_call"], dict):
            fc = entry["function_call"]
            kwargs["call"] = ToolCall(
                name=fc.get("name") or "",
                args=fc.get("args") or {},
                id=fc.get("id"),
            )
        if "function_response" in entry and isinstance(entry["function_response"], dict):
            fr = entry["function_response"]
            kwargs["response"] = ToolResponse(
                name=fr.get("name") or "",
                result=fr.get("response") or {},
                id=fr.get("id"),
            )
        if not kwargs:
            continue
        try:
            parts.append(Part(**kwargs))
        except Exception:  # noqa: BLE001 -- drop the bad part, keep going
            continue
    return parts or None


def build_contents_from_history(
    user_text: str,
    chat_history: list[dict] | None = None,
) -> list[Message]:
    """Convert ``chat_history`` plus a new ``user_text`` into ``Message``s.
    A decodable ``parts_blob`` wins over the text shape; an empty-text row is
    dropped, and ``user_text`` is always the terminal ``user`` turn."""
    contents: list[Message] = []
    if chat_history:
        for entry in chat_history:
            # NEVER-REHYDRATE guard: strip the ``thinking`` field BY RULE
            # before ANY key is read. Persisted reasoning text is display
            # replay material only and must never reach the model.
            entry = _strip_never_rehydrate(entry)
            role = entry.get("role", "user")
            ir_role = "model" if role in ("agent", "assistant", "model") else "user"
            # Prefer parts_blob when present -- it carries the call and
            # response Parts, so the replayed turn reaches the provider whole.
            blob = entry.get("parts_blob")
            decoded = _decode_parts_blob(blob) if blob is not None else None
            if decoded:
                contents.append(Message(role=ir_role, parts=decoded))
                continue
            text = entry.get("text", "")
            if not text:
                continue
            contents.append(Message(role=ir_role, parts=[Part(text=text)]))
    contents.append(Message(role="user", parts=[Part(text=user_text)]))
    return contents


# Default cap on the number of persisted chat rows rehydrated into the live
# model context on a Case reopen. A long-running Case
# can accumulate hundreds of user/agent/tool rows; replaying all of them every
# reopen turn would blow the context window (and the per-turn cost). We keep
# the MOST RECENT rows (the tail carries the relevant recent state -- what the
# user just did and what is on the map now). The injected layers-present note
# (built separately) is the durable anchor for older work, so dropping the head
# of a long transcript does not lose "what layers already exist".
REHYDRATE_HISTORY_CAP = 40


def _summarize_tool_row_for_history(content: str, tool_card: Any) -> str:
    """Collapse a persisted ``role="tool"`` row into one model-side text line.
    The full function_call / function_response Parts are NOT persisted, so
    ``[tool <name> completed|failed]`` is all that stands against a recompute."""
    name: str | None = None
    state: str | None = None
    # Prefer the typed record (duck-typed: ToolCardRecord or a dict).
    if tool_card is not None:
        name = getattr(tool_card, "tool_name", None)
        state = getattr(tool_card, "state", None)
        if name is None and isinstance(tool_card, dict):
            name = tool_card.get("tool_name")
            state = tool_card.get("state")
    if name is None and content:
        try:
            import json as _json

            parsed = _json.loads(content)
            if isinstance(parsed, dict):
                name = parsed.get("tool_name")
                state = parsed.get("state")
        except Exception:  # noqa: BLE001 -- content may not be JSON
            pass
    if not name:
        return ""
    outcome = "failed" if state == "failed" else "completed"
    return f"[tool {name} {outcome}]"


def _format_layer_bbox(bbox: Any) -> str | None:
    """Compact ``[lon_min, lat_min, lon_max, lat_max]`` for a layer line.
    ``None`` for anything that is not a valid 4-tuple; the rounding keeps the
    Case-state note short."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        b = [round(float(x), 4) for x in bbox]
    except (TypeError, ValueError):
        return None
    return f"[{b[0]}, {b[1]}, {b[2]}, {b[3]}]"


def _format_aoi_bbox_line(case_bbox: Any) -> str | None:
    """Format the Case AOI bbox as one durable instruction line, or ``None``.
    The AOI ANCHOR that must survive history capping: a long Case drops the head
    turn that named the place, and a follow-up fetch would then re-geocode."""
    if not isinstance(case_bbox, (list, tuple)) or len(case_bbox) != 4:
        return None
    try:
        b = [float(x) for x in case_bbox]
    except (TypeError, ValueError):
        return None
    return (
        f"Case AOI bbox [lon_min, lat_min, lon_max, lat_max] = "
        f"[{b[0]}, {b[1]}, {b[2]}, {b[3]}]. REUSE this exact extent for any "
        "follow-up data fetch or clip in this Case — do NOT re-derive or "
        "re-geocode the area."
    )


def build_layers_present_note(
    loaded_layers: list[dict] | None,
    case_bbox: Any = None,
) -> str | None:
    """Build the compact "Case state" model turn: layers plus the AOI bbox.
    ``None`` only when there is neither a layer nor a usable bbox."""
    # Each line carries enough IDENTITY for the model to recognize an existing
    # RESULT and not re-run the work that made it:
    #   - role: RESULT (a primary simulation / analysis output) vs INPUT
    #     (a fetched / context layer used as an input), and for a fetched layer
    #     the KIND it carries, so "the landcover for this AOI is already here"
    #     reads unambiguously;
    #   - name, layer_type, the reusable handle (== layer_id), and the uri.
    # local import: avoid cycle
    from trid3nt_server.server.dispatch.layer_reuse import fetched_layer_kind

    lines: list[str] = []
    for layer in loaded_layers or []:
        if not isinstance(layer, dict):
            continue
        layer_id = layer.get("layer_id") or "?"
        name = layer.get("name") or layer_id
        layer_type = layer.get("layer_type") or "?"
        # The layer_id IS the reusable handle; it is surfaced explicitly as
        # ``handle=`` alongside the underlying ``uri`` so the model can hand an
        # existing artifact straight to a tool instead of recomputing it.
        uri = layer.get("uri")
        role_raw = layer.get("role")
        # A ``role="primary"`` layer is a RESULT, which is what stops the re-run;
        # everything else is an INPUT / context layer. A recognized FETCHED layer
        # (buildings / landcover / dem / roads / ...) is an INPUT tagged with its
        # KIND so a fit / resize / re-show follow-up reuses it
        # (compute_layer_bounds on its handle) instead of re-fetching a duplicate.
        if role_raw == "primary":
            role_label = "RESULT"
        else:
            fetched_kind = fetched_layer_kind(layer_id, name)
            role_label = f"INPUT[{fetched_kind}]" if fetched_kind else "INPUT"
        parts = [f"id={layer_id}", role_label, layer_type, f"handle={layer_id}"]
        bbox = layer.get("bbox")
        bbox_str = _format_layer_bbox(bbox)
        if bbox_str:
            parts.append(f"bbox={bbox_str}")
        if isinstance(uri, str) and uri:
            parts.append(f"uri={uri}")
        lines.append(f"- {name} (" + ", ".join(parts) + ")")
    bbox_line = _format_aoi_bbox_line(case_bbox)
    if not lines and not bbox_line:
        return None
    segments: list[str] = []
    if lines:
        segments.append(
            "These layers are ALREADY produced and on the map for this Case. "
            "Lines tagged RESULT are finished simulation / analysis OUTPUTS for "
            "this AOI - the work that made them is DONE. "
            "Lines tagged INPUT (or INPUT[<kind>], e.g. "
            "INPUT[buildings], INPUT[landcover], INPUT[dem]) are fetched / context "
            "layers ALREADY on the map. "
            "REUSE these (pass their handle/uri DIRECTLY to the next tool) — do "
            "NOT re-run, re-fetch, or recompute them:\n"
            + "\n".join(lines)
            + "\nIf a RESULT already answers the user's request for this AOI and "
            "parameters, narrate from it and pass its handle onward (e.g. an "
            "existing flood-depth RESULT feeds compute_flood_depth_damage "
            "directly). Re-running the expensive simulation that produced an "
            "existing RESULT is FORBIDDEN unless the user changes the area / "
            "parameters or explicitly asks to re-run. Do NOT re-fetch or "
            "recompute a layer already listed here unless it is genuinely absent."
            "\nFETCHED LAYER REUSE (HARD RULE): a fetched layer "
            "(INPUT[<kind>]) for this AOI is ALREADY on the map. A follow-up to "
            "FIT, ZOOM, RESIZE the box, or 'encompass all the <features>' for "
            "that SAME data (e.g. 'resize the bbox to encompass all the "
            "buildings' when an INPUT[buildings] layer is already listed) is NOT a fetch — "
            "call compute_layer_bounds on the EXISTING layer's handle to fit the "
            "view. Re-calling the fetch_* tool produces a SECOND identical layer "
            "(a real duplicate on the map), which is FORBIDDEN. Only re-fetch when "
            "the user names a DIFFERENT area that pokes OUTSIDE the existing "
            "extent, a different data source / kind, or explicitly asks to "
            "refresh the data."
        )
    if bbox_line:
        segments.append(bbox_line)
    return "[Case state] " + "\n".join(segments)


def rehydrate_history_from_case(
    chat_messages: list[Any] | None,
    loaded_layers: list[dict] | None = None,
    *,
    cap: int = REHYDRATE_HISTORY_CAP,
    case_bbox: Any = None,
) -> tuple[list[dict], int]:
    """Convert ONE Case's persisted chat into the ``chat_history`` dict shape.
    Returns ``(history, dropped)``: only the ``cap`` most recent rows are
    replayed, and the layers-present note is appended as the last model turn."""
    rows = list(chat_messages or [])
    dropped = 0
    if cap >= 0 and len(rows) > cap:
        dropped = len(rows) - cap
        rows = rows[-cap:]

    history: list[dict] = []
    for msg in rows:
        # NEVER-REHYDRATE rule: read ONLY role / content / tool_card off the
        # persisted row. The ``thinking`` field is reasoning-channel text for
        # display replay and is deliberately never read here.
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)
        tool_card = getattr(msg, "tool_card", None)
        if role is None and isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
            tool_card = msg.get("tool_card")
        content = content or ""
        if role == "tool":
            line = _summarize_tool_row_for_history(content, tool_card)
            if line:
                # Tool transcript reads as model-side narration of what ran.
                history.append({"role": "model", "text": line})
            continue
        if role == "user":
            if content.strip():
                history.append({"role": "user", "text": content})
            continue
        if role in ("agent", "assistant", "model", "system"):
            if content.strip():
                # ``agent`` collapses to ``model`` in the contents builder;
                # ``system`` has no native role there, so it folds to
                # model-side context text -- safer for routing than
                # re-injecting it as a fresh ``user`` instruction.
                history.append({"role": "agent", "text": content})
            continue
        # Unknown role: skip rather than guess.

    note = build_layers_present_note(loaded_layers, case_bbox=case_bbox)
    if note:
        history.append({"role": "model", "text": note})

    return history, dropped


def encode_parts_blob(parts: list[Part]) -> bytes:
    """Encode a list of ``Part`` to the ``parts_blob`` wire shape.
    A JSON byte string, so it round-trips through JSON persistence."""
    import json as _json

    out: list[dict[str, Any]] = []
    for part in parts:
        entry: dict[str, Any] = {}
        if part.text:
            entry["text"] = part.text
        if part.call is not None and part.call.name:
            entry["function_call"] = {
                "name": part.call.name,
                "id": part.call.id,
                "args": dict(part.call.args or {}),
            }
        if part.response is not None and part.response.name:
            entry["function_response"] = {
                "name": part.response.name,
                "id": part.response.id,
                "response": dict(part.response.result or {}),
            }
        if entry:
            out.append(entry)
    return _json.dumps(out).encode("utf-8")


def _coerce_to_summary_value(value: Any, depth: int = 0) -> Any:
    """Recursive helper for ``summarize_tool_result``.
    Non-JSON-native types become strings, long lists and strings truncate, and
    a nested dict past depth 2 collapses: signal, never fidelity."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        # Long strings (HTML bodies, base64 payloads) get clipped.
        if len(value) > 500:
            return value[:500] + "…[truncated]"
        return value
    if isinstance(value, (list, tuple)):
        if depth >= 2:
            return f"[list len={len(value)}]"
        # Keep up to 5 items; summarize the rest by count.
        items = [_coerce_to_summary_value(v, depth + 1) for v in list(value)[:5]]
        if len(value) > 5:
            items.append(f"…[+{len(value) - 5} more items]")
        return items
    if isinstance(value, dict):
        if depth >= 2:
            return f"{{dict keys={list(value.keys())[:8]}}}"
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                k = str(k)
            # Filter obviously huge / opaque fields the LLM doesn't need.
            if k in {"raw_bytes", "raw_body", "binary", "geometry_wkb", "pixels"}:
                out[k] = f"[{k} suppressed]"
                continue
            out[k] = _coerce_to_summary_value(v, depth + 1)
        return out
    # Pydantic models / dataclasses / arbitrary objects -- repr-coerce, clip.
    s = repr(value)
    if len(s) > 200:
        s = s[:200] + "…"
    return s


def _classify_error(error: BaseException) -> tuple[str, bool]:
    """Derive ``(error_code, retryable)`` for a tool-dispatch exception.
    A typed tool exception's own ``error_code`` / ``retryable`` attributes win;
    this NEVER raises, so the multi-turn loop always gets a stable pair."""
    # 1. Honour typed-tool exception class attributes when present.
    code_attr = getattr(error, "error_code", None)
    retry_attr = getattr(error, "retryable", None)
    if isinstance(code_attr, str) and code_attr:
        code = code_attr
    else:
        code = type(error).__name__.upper()
    if isinstance(retry_attr, bool):
        return code, retry_attr

    # 2. Heuristic fallback for an untyped exception: a timeout or a
    # network-ish OSError is retryable; a ValueError / TypeError / KeyError /
    # AttributeError is an argument-shape or programmer error and is NOT;
    # everything else is retryable, capped either way by MAX_TURN_ITERATIONS.
    import asyncio as _asyncio

    if isinstance(error, (_asyncio.TimeoutError, TimeoutError)):
        return code, True
    if isinstance(error, (ConnectionError, OSError)):
        return code, True
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        return code, False
    return code, True


def _user_narration_message(tool_name: str, fallback: str) -> str:
    """Concise user-actionable text for a credential or auth-config failure.
    Reuses the credential registry's own copy when the tool has a provider;
    NEVER raises, degrading to ``fallback`` with an honest pointer."""
    try:
        from trid3nt_server.credentials.credential_registry import (
            generic_provider_for_tool,
            provider_for_tool,
        )

        provider = provider_for_tool(tool_name)
        if provider is not None:
            return provider.default_message
        generic = generic_provider_for_tool(tool_name)
        return generic.default_message
    except Exception:  # noqa: BLE001 -- narration must never break dispatch
        return (
            f"{fallback} This looks like a missing or invalid credential; "
            "provide a valid API key/token and retry."
        )


def _summarize_chart_emission(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Compact summary for a chart-emission tool result.
    The full ``vega_lite_spec`` is DROPPED -- it already reached the client on
    its own envelope -- so narration must source its numbers from the caption."""
    spec = result.get("vega_lite_spec")
    spec = spec if isinstance(spec, dict) else {}
    mark = spec.get("mark")
    if isinstance(mark, dict):
        chart_type = mark.get("type")
    elif isinstance(mark, str):
        chart_type = mark
    else:
        chart_type = None
    data = spec.get("data")
    n_rows = (
        len(data["values"])
        if isinstance(data, dict) and isinstance(data.get("values"), list)
        else None
    )
    return {
        "tool": tool_name,
        "status": "ok",
        "result": {
            "chart_emitted": True,
            "chart_id": result.get("chart_id"),
            "title": result.get("title"),
            "caption": result.get("caption"),
            "chart_type": chart_type,
            "n_data_rows": n_rows,
            "source_layer_uri": result.get("source_layer_uri"),
            # Explicit guidance for the LLM: the chart is now on the user's
            # screen; narrate the caption's numbers and what the chart shows.
            "note": (
                "A chart has been rendered for the user. Narrate what it shows "
                "using the numbers in 'caption'; do NOT restate the raw data rows."
            ),
        },
    }


def _failed_modeled_envelope_error_code(result: dict[str, Any]) -> str:
    """Extract the threaded failure code from a failed "modeled" envelope.
    Prefers the depth-0 ``workflow_name`` seam, the one that survives the
    summary coercion; else the buried ``metrics.solver_version`` seam."""
    # A failed envelope threads its error code into TWO seams so it survives
    # ``_coerce_to_summary_value``'s depth>=2 dict collapse: the top-level
    # ``workflow_name == "<name>:FAILED:<CODE>"``, and the buried
    # ``<hazard>.metrics.solver_version == "failed:<CODE>"``.
    wf = result.get("workflow_name")
    if isinstance(wf, str) and ":FAILED:" in wf:
        code = wf.split(":FAILED:", 1)[1].strip()
        if code:
            return code
    # Scan any hazard payload's metrics.solver_version for "failed:<CODE>".
    # Flood is the headline path; be generic so a future "modeled" composer
    # (seismic, plume, ...) with the same threading also resolves.
    for payload in result.values():
        if not isinstance(payload, dict):
            continue
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            continue
        sv = metrics.get("solver_version")
        if isinstance(sv, str) and sv.startswith("failed:"):
            code = sv.split("failed:", 1)[1].strip()
            if code:
                return code
    return "MODEL_RUN_PRODUCED_NO_LAYERS"


def _modeled_envelope_is_failure_tagged(result: dict[str, Any]) -> bool:
    """True when a "modeled" envelope carries an explicit failure marker.
    A run can append its ``solver_run_id`` BEFORE failing, so the marker, not
    the presence of a run id, is what decides."""
    # Two seams carry it: the depth-0 ``workflow_name`` containing ":FAILED:",
    # which survives ``_coerce_to_summary_value``, and any hazard payload's
    # depth-2 ``metrics.solver_version`` starting with "failed:".
    wf = result.get("workflow_name")
    if isinstance(wf, str) and ":FAILED:" in wf:
        return True
    for payload in result.values():
        if not isinstance(payload, dict):
            continue
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            continue
        sv = metrics.get("solver_version")
        if isinstance(sv, str) and sv.startswith("failed:"):
            return True
    return False


def _extract_flood_metrics_phrase(result: dict[str, Any]) -> str:
    """Render whatever flood metrics exist into an honest narration fragment.
    A solve that succeeded but never reached the map still produced real
    numbers; only present fields are emitted, and ``""`` when none are."""
    metrics: dict[str, Any] | None = None
    flood = result.get("flood")
    if isinstance(flood, dict):
        m = flood.get("metrics")
        if isinstance(m, dict):
            metrics = m
    if metrics is None:
        # Fall back to any hazard payload carrying a metrics dict.
        for payload in result.values():
            if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict):
                metrics = payload["metrics"]
                break
    if not metrics:
        return ""

    parts: list[str] = []

    def _num(key: str) -> float | None:
        val = metrics.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
        return None

    area = _num("flooded_area_km2")
    if area is not None:
        parts.append(f"flooded area {area:g} km^2")
    max_d = _num("max_depth_m")
    if max_d is not None:
        parts.append(f"max depth {max_d:g} m")
    mean_d = _num("mean_depth_m")
    if mean_d is not None:
        parts.append(f"mean depth {mean_d:g} m")
    p95 = _num("p95_depth_m")
    if p95 is not None:
        parts.append(f"p95 depth {p95:g} m")

    return ", ".join(parts)


def _extract_synthetic_inputs(result: Any) -> list[dict[str, Any]]:
    """Pull the structured ``synthetic_inputs`` provenance list off a tool result.
    Returns plain dicts, or ``[]`` when none is declared; NEVER raises, so a
    missing field on any result shape degrades to ``[]``."""

    def _as_dicts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, (list, tuple)) or not value:
            return []
        out: list[dict[str, Any]] = []
        for entry in value:
            if isinstance(entry, dict):
                out.append(entry)
            elif hasattr(entry, "model_dump"):
                try:
                    out.append(entry.model_dump(mode="json"))
                except Exception:  # noqa: BLE001
                    continue
        return out

    # 1. top-level attribute (LayerURI subclass / result model)
    direct = _as_dicts(getattr(result, "synthetic_inputs", None))
    if direct:
        return direct
    # 2. dict key
    if isinstance(result, dict):
        found = _as_dicts(result.get("synthetic_inputs"))
        if found:
            return found
        # nested summary / derived_params dicts
        for key in ("summary", "derived_params"):
            sub = result.get(key)
            if isinstance(sub, dict):
                found = _as_dicts(sub.get("synthetic_inputs"))
                if found:
                    return found
        # a layers list of LayerURIs
        for layer in result.get("layers") or []:
            found = _as_dicts(getattr(layer, "synthetic_inputs", None))
            if found:
                return found
        return []
    # 3. result models that wrap a single primary layer
    for attr in ("layers",):
        seq = getattr(result, attr, None)
        if isinstance(seq, (list, tuple)):
            for layer in seq:
                found = _as_dicts(getattr(layer, "synthetic_inputs", None))
                if found:
                    return found
    for attr in ("asr_layer", "primary", "layer", "peak"):
        layer = getattr(result, attr, None)
        if layer is not None:
            found = _as_dicts(getattr(layer, "synthetic_inputs", None))
            if found:
                return found
    return []


def _hoist_synthetic_inputs(payload: dict[str, Any], result: Any) -> None:
    """Hoist a one-line ``assumptions_summary`` and its structured list to the
    TOP of the function_response, so which inputs are defaults versus
    site-derived is narrated as ONE line and never a table. A no-op when none."""
    from trid3nt_contracts.common import render_assumptions_line

    entries = _extract_synthetic_inputs(result)
    if not entries:
        return
    line = render_assumptions_line(entries)
    if line:
        payload["assumptions_summary"] = line
    # keep the structured list too (clipped) so a consumer can enumerate it.
    payload["synthetic_inputs"] = entries[:12]


def summarize_tool_result(
    tool_name: str,
    result: Any,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Compact a tool result into the ``function_response.response`` payload.
    A SUMMARY, never the raw result: metadata, key metrics, error codes and
    counts. The dict always carries ``"tool"`` and ``"status"``."""
    import json as _json

    # An error becomes {status: "error", error_code, message, retryable,
    # error_type}. The code and retryability are harvested from the tool's own
    # typed exception when it declares them, so the model can retry with
    # corrected args, pick another tool, or narrate the failure honestly;
    # ``MAX_TURN_ITERATIONS`` caps a runaway retry either way.
    if error is not None:
        from trid3nt_server.gates.actionability import classify_actionability

        code, retryable = _classify_error(error)
        message = str(error)[:500]
        actionability = classify_actionability(tool_name, error)
        if actionability == "operator":
            # Contract violation or internal exception: the model gets a
            # terse, honest acknowledgment ONLY -- nothing here for it to act
            # on. The FULL exception already reached the log at the dispatch
            # site, and error_code / actionability still ride the telemetry
            # record.
            message = "internal error, logged"
        elif actionability == "user":
            # Missing-credential/auth-config: a concise narration directive
            # (what happened + what the user can do) replaces the raw
            # exception text so a small model relays it rather than
            # paraphrasing internals.
            message = _user_narration_message(tool_name, message)
        envelope = {
            "tool": tool_name,
            "status": "error",
            "error_code": code,
            "message": message,
            "retryable": retryable,
            # Legacy alias -- preserved so existing tests / callers that
            # read ``error`` continue to work.  ``message`` is the new
            # canonical field; both carry the same string.
            "error": message,
            "error_type": type(error).__name__,
            "actionability": actionability,
        }
        # Typed no-data / recovery contract: a tool exception may carry a
        # ``suggestions`` sequence of short recovery options. It is surfaced as
        # a STRUCTURED list so a small model relays the options rather than
        # inventing a next step.
        raw_suggestions = getattr(error, "suggestions", None)
        if isinstance(raw_suggestions, (list, tuple)):
            suggestions = [str(s) for s in raw_suggestions if str(s).strip()]
            if suggestions:
                envelope["suggestions"] = suggestions[:8]
        return envelope

    if result is None:
        return {"tool": tool_name, "status": "no_result"}

    # chart-emission results carry a full
    # Vega-Lite spec with INLINE data rows (up to ~2000). The model must
    # narrate from the chart's numbers, not re-read the inline rows -- and the
    # spec could blow the char budget. Strip ``vega_lite_spec`` and surface a
    # COMPACT summary (chart_id / title / caption / chart type / data-shape) so
    # the function_response stays small and narration-focused. The FULL spec
    # already reached the client on the ``chart-emission`` envelope.
    if (
        isinstance(result, dict)
        and result.get("envelope_type") == "chart-emission"
        and isinstance(result.get("vega_lite_spec"), dict)
    ):
        return _summarize_chart_emission(tool_name, result)

    # code_exec_request returns a COMPACT summary
    # (status / result descriptor / stdout tail / truncated / duration) PLUS the
    # full ``code-exec-result`` wire payload under ``_code_exec_result`` (which
    # carries the larger 16-KiB stdout/stderr fields). The full payload already
    # already reached the client on the ``code-exec-result`` envelope; it is
    # stripped from the function_response so narration runs off the compact
    # summary and the structured ``result``, not the raw logs.
    if isinstance(result, dict) and "_code_exec_result" in result:
        compact = {k: v for k, v in result.items() if k != "_code_exec_result"}
        return {
            "tool": tool_name,
            "status": "ok",
            "result": _coerce_to_summary_value(compact),
        }

    # NET GUARANTEE: envelope_type=="modeled" AND an EMPTY ``layers`` list is
    # NEVER stamped status="ok". Two sub-cases:
    #
    #   (a) FAILURE-TAGGED -- the depth-0 ``workflow_name`` carries ":FAILED:",
    #       or a payload's ``metrics.solver_version`` starts with "failed:".
    #       Surface status="error" with the parsed code, REGARDLESS of any
    #       solver_run_id: a run can append one before failing.
    #   (b) NOT FAILURE-TAGGED -- the solve COMPLETED (metrics present, no
    #       ":FAILED:" tag) but the result layer was dropped at publish or
    #       render. Surface status="error", error_code="NO_RENDERABLE_LAYER",
    #       and INCLUDE the available metrics so the numbers can still be
    #       narrated honestly even though nothing reached the map.
    #
    # ``_coerce_to_summary_value`` collapses the depth-2 metrics dict to bare
    # key names, so without this detector the model sees {status:ok, layers:[],
    # metrics:{dict keys=...}} and honestly narrates "done". The check keys off
    # the STRUCTURE of the result, not on whether an exception was raised, so
    # it is root-cause-agnostic. Do NOT broaden it to "observed"/"fetched"
    # tools: those legitimately return non-layer data. A modeled envelope WITH
    # a non-empty layers list still reads as status="ok".
    if (
        isinstance(result, dict)
        and result.get("envelope_type") == "modeled"
        and not result.get("layers")
    ):
        if _modeled_envelope_is_failure_tagged(result):
            # Sub-case (a): an explicitly failure-tagged run (covers both the
            # never-dispatched non-runs AND the dispatched-then-failed exits
            # that already appended a solver_run_id).
            code = _failed_modeled_envelope_error_code(result)
            message = (
                f"{tool_name} produced no layers and the model did NOT run "
                f"successfully ({code})."
            )
            return {
                "tool": tool_name,
                "status": "error",
                "error_code": code,
                "message": message,
                "retryable": False,
                # Legacy alias -- same string as ``message`` (matches the raised-
                # exception error path above so downstream consumers are uniform).
                "error": message,
                "error_type": "FailedModelEnvelope",
            }
        # Sub-case (b): the solve completed (or at least produced metrics) but
        # no renderable layer survived publish/render. Surface the numbers so
        # the agent narrates honestly: real flood, just not on the map.
        metrics_phrase = _extract_flood_metrics_phrase(result)
        if metrics_phrase:
            message = (
                f"The simulation completed ({metrics_phrase}) but the result "
                f"layer could not be published/rendered — it is not on the map."
            )
        else:
            message = (
                f"{tool_name} completed but produced no renderable layer — "
                f"the result is not on the map."
            )
        return {
            "tool": tool_name,
            "status": "error",
            "error_code": "NO_RENDERABLE_LAYER",
            "message": message,
            "retryable": False,
            "error": message,
            "error_type": "NoRenderableLayer",
        }

    if isinstance(result, dict):
        summary = _coerce_to_summary_value(result)
        payload: dict[str, Any] = {
            "tool": tool_name,
            "status": "ok",
            "result": summary,
        }
    else:
        payload = {
            "tool": tool_name,
            "status": "ok",
            "result": _coerce_to_summary_value(result),
        }
        # HONESTY FLOOR: a bare LayerURI result repr-coerces clipped to 200
        # chars, which would drop the trailing ``fallback_note`` field. When a
        # cross-source fallback happened, the note is hoisted to a top-level key
        # so the model ALWAYS sees that the delivered data is the fallback
        # source, never the primary. Scoped to fallback layers only.
        _fb_note = getattr(result, "fallback_note", None)
        if isinstance(_fb_note, str) and _fb_note:
            payload["fallback_note"] = _fb_note
        # Same clipping hazard, second channel: ``fallback_warning`` is the
        # LABELED degrade a fetcher writes about its own composite (which source
        # painted what share). It is the ONLY loudness a request that bypassed a
        # coverage gate gets, so it must not die in the 200-char repr either.
        _fb_warning = getattr(result, "fallback_warning", None)
        if isinstance(_fb_warning, str) and _fb_warning:
            payload["fallback_warning"] = _fb_warning

    # provenance-chain wave: whichever branch built the payload, hoist any
    # structured input provenance the result carries (a demo default is buried in
    # the coerced/repr'd result otherwise). Renders one compact assumptions line
    # so the LLM narrates demo-vs-site-derived inputs. No-op when none declared.
    _hoist_synthetic_inputs(payload, result)

    # Final char-budget clip: serialize, if oversized clip and re-wrap.
    try:
        encoded = _json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001 -- pathological non-serializable
        return {
            "tool": tool_name,
            "status": "ok",
            "result_repr": repr(result)[:1000],
            "note": "result not JSON-serializable; coerced via repr",
        }
    if len(encoded) > _FUNCTION_RESPONSE_CHAR_BUDGET:
        return {
            "tool": tool_name,
            "status": "ok",
            "result_summary": encoded[:_FUNCTION_RESPONSE_CHAR_BUDGET] + "…[clipped]",
            "note": "full result exceeded char budget; clipped for LLM context",
        }
    return payload


#
# ``success`` (did the tool return without raising / without a failure-tagged
# envelope) is NOT the same question as ``was the result USABLE`` -- the headline
# bug: a layer-producing tool
# can return status="ok" while carrying an EMPTY layers list (a modeled run that
# produced no renderable layer, or a publish/render drop). That reads as a
# SUCCESS in the per-tool count but is NOT a usable result. ``result_usable``
# captures exactly that distinction so the tool-accuracy dashboard can separate
# "the call worked" from "the call produced something the user can use".
#
# Returns:
#   - ``False`` -- a layer-producing tool whose result has NO renderable layer
#     (or a modeled envelope with empty layers), EVEN when success=True. This is
#     keyed off the SAME honesty-floor classifier ``summarize_tool_result`` uses
#     (NO_RENDERABLE_LAYER / failure-tagged modeled envelope), so the two stay
#     in lockstep at the single dispatch chokepoint.
#   - ``True`` -- a real renderable result (a LayerURI / non-empty layers list)
#     OR a non-empty data payload from a layer/data tool.
#   - ``None`` -- the notion does not apply (meta / control-plane tools that never
#     produce a layer or a data payload, e.g. confirmation / discovery / cancel
#     helpers; also when the call itself errored -- usability is undefined for a
#     call that did not complete).
#
# Conservative by construction: anything we cannot positively classify as a
# layer- or data-producing result returns ``None`` rather than guessing True.

#: Result keys whose presence marks a layer-producing return. A non-empty value
#: under any of these is a renderable artifact (LayerURI dict, gs://"/s3:// COG,
#: or a layers list). Mirrors the ``*_uri`` vocabulary the adapter already
#: tracks for handle-passing.
_LAYER_RESULT_KEYS = frozenset(
    {
        "layers",
        "layer_uri",
        "layer",
        "published_layers",
        "result_layers",
    }
)


def _result_has_renderable_layer(result: Any) -> bool | None:
    """Whether ``result`` carries a renderable layer artifact.
    ``True`` a layer survived; ``False`` a layer-producer whose slot is empty;
    ``None`` not layer-shaped at all, leaving usability to the caller."""
    # LayerURI / pydantic-or-dataclass return with the two defining attributes.
    if not isinstance(result, (dict, str, bytes)) and result is not None:
        if hasattr(result, "layer_id") and hasattr(result, "uri"):
            return bool(getattr(result, "uri", None))
    if not isinstance(result, dict):
        return None
    # A modeled assessment envelope: the layers list is the renderable slot.
    if result.get("envelope_type") is not None:
        return bool(result.get("layers"))
    # Generic layer keys.
    saw_layer_key = False
    for key in _LAYER_RESULT_KEYS:
        if key in result:
            saw_layer_key = True
            val = result.get(key)
            if isinstance(val, (list, tuple, str)):
                if val:
                    return True
            elif val:
                return True
    if saw_layer_key:
        # A layer key was present but every one was empty/falsy.
        return False
    return None


def classify_result_usable(
    tool_name: str,
    result: Any,
    summary: dict[str, Any] | None,
) -> bool | None:
    """Classify whether a completed tool call produced a USABLE result.
    Keyed off the honesty-floor signal ``summarize_tool_result`` already
    stamped, so the two never diverge; NEVER raises, degrading to ``None``."""
    try:
        # 1. The honesty floor already decided this is an empty-layer modeled
        #    run (status=error + NO_RENDERABLE_LAYER). That is the canonical
        #    "succeeded but unusable" case.
        if isinstance(summary, dict):
            if summary.get("error_code") == "NO_RENDERABLE_LAYER":
                return False
        # 2. A modeled envelope that was failure-tagged also has no usable
        #    layer. summarize_tool_result surfaces these as status=error with a
        #    parsed code, but key off the result STRUCTURE directly so this is
        #    robust even if the summary shape changes.
        if isinstance(result, dict) and result.get("envelope_type") == "modeled":
            if not result.get("layers"):
                return False
            return True
        # 3. Layer-shaped results: True iff a renderable layer survived.
        layer_state = _result_has_renderable_layer(result)
        if layer_state is not None:
            return layer_state
        # 4. None / no_result: the call produced nothing usable, but the notion
        #    of "renderable layer" doesn't apply -- treat as N/A (meta path).
        if result is None:
            return None
        # 5. A non-empty data payload from a non-layer tool (point query,
        #    table, scalar, count) IS a usable result. An empty dict / empty
        #    string / empty collection is not. Anything else (a populated dict,
        #    a number, a non-empty string) counts.
        if isinstance(result, dict):
            # Drop bookkeeping-only keys before judging emptiness so a dict that
            # carries ONLY a status/tool marker is not mistaken for data.
            data_keys = [
                k
                for k in result
                if k not in {"status", "tool", "envelope_type"}
            ]
            # Real data -> usable (True). A dict carrying ONLY bookkeeping
            # markers (a bare {"status":"ok"} control-plane return) has no data
            # AND no notion of a "usable result" -> N/A (None), NOT unusable
            # (False), so meta/confirm/discovery helpers don't drag the
            # result_usability_rate down.
            return bool(data_keys) or None
        if isinstance(result, (list, tuple, set, str, bytes)):
            return bool(result)
        # A bare scalar (int/float/bool) return is a usable data result.
        return True
    except Exception:  # noqa: BLE001 -- classification must never break dispatch
        return None


def build_function_call_content(
    name: str,
    args: dict[str, Any],
    call_id: str | None = None,
) -> Message:
    """Build the ``model``-role Message wrapping the tool call.
    Appended to ``contents`` after a dispatch so the next model round sees its
    own prior tool-call decision."""
    return Message(
        role="model",
        parts=[Part(call=ToolCall(name=name, args=args or {}, id=call_id))],
    )


def build_function_response_content(
    name: str,
    response: dict[str, Any],
    call_id: str | None = None,
) -> Message:
    """Build the ``user``-role Message wrapping the tool response.
    Appended right after the matching call message, so the model has the
    (call, response) pair before deciding its next turn."""
    return Message(
        role="user",
        parts=[Part(response=ToolResponse(name=name, result=response, id=call_id))],
    )


def build_user_text_content(text: str) -> Message:
    """Build a plain ``user``-role text Message.
    The one-Part shape the contents builder uses for a live user message, so a
    loop driver can append a corrective turn without hand-rolling the IR."""
    return Message(role="user", parts=[Part(text=text)])



async def stream_events(
    client: Any,
    model: str,
    user_text: str,
    tool_declarations: list[ToolDeclaration] | None = None,
    system_prompt: str | None = None,
    chat_history: list[dict] | None = None,
    model_cache_ref: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream the model's reply to ``user_text`` as typed ``StreamEvent``s.
    ``client`` is accepted for signature parity and IGNORED -- each provider
    adapter opens its own; an empty ``tool_declarations`` means text-only."""
    contents = build_contents_from_history(user_text, chat_history)
    async for event in stream_events_with_contents(
        client,
        model,
        contents,
        tool_declarations=tool_declarations,
        system_prompt=system_prompt,
        model_cache_ref=model_cache_ref,
    ):
        yield event




async def stream_events_with_contents(
    client: Any,
    model: str,
    contents: list[Message],
    tool_declarations: list[ToolDeclaration] | None = None,
    system_prompt: str | None = None,
    model_cache_ref: str | None = None,
    model_id: str | None = None,
    show_thinking: bool = False,
) -> AsyncIterator[StreamEvent]:
    """Stream ONE model round from a fully-built ``contents`` list.
    ``show_thinking`` reaches the OpenAI adapter only; the dispatch is an
    EXPLICIT provider switch, and any other provider raises."""
    # ``model_cache_ref``, when set, means the request carries NO ``tools[]``
    # and no ``tool_config``: the provider-side cache already holds the catalog
    # and the system instruction, and sending either field alongside a cached
    # reference is a 400. ``system_prompt`` and ``tool_declarations`` are
    # ignored on that path.
    #
    # Every adapter converts the IR contents and tool declarations at its own
    # boundary and yields the SAME StreamEvent union, so the dispatch loop, the
    # validator, the emitter and the UI are untouched.
    from .model_selection import model_provider
    from .scripted_adapter import model_provider_is_scripted, stream_scripted

    # MODEL_PROVIDER=scripted (aliases replay/fake): replay a canned transcript of
    # tool calls with NO model call -- the zero-cost deterministic test/dev
    # sandbox. Intercepted FIRST so it never reaches a provider client.
    if model_provider_is_scripted():
        async for _ev in stream_scripted(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=model_id,
        ):
            yield _ev
        return

    # MODEL_PROVIDER=anthropic: delegate to the Anthropic Messages API adapter
    # (official ``anthropic`` SDK). Dormant unless selected.
    if model_provider() == "anthropic":
        from .anthropic_adapter import stream_anthropic

        async for _ev in stream_anthropic(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=model_id,
        ):
            yield _ev
        return

    # MODEL_PROVIDER=openai: delegate to the OpenAI-compatible adapter. Covers
    # Ollama, vLLM, llama.cpp server, LM Studio, OpenAI, Groq, DeepSeek,
    # OpenRouter -- any endpoint that speaks the chat.completions streaming API.
    # Dormant unless selected; zero cloud impact when MODEL_PROVIDER != "openai".
    if model_provider() == "openai":
        from .openai_adapter import stream_openai
        async for _ev in stream_openai(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=model_id,
            show_thinking=show_thinking,
        ):
            yield _ev
        return

    # Provider dispatch is EXPLICIT -- scripted/replay/fake, anthropic and
    # openai each returned above. Anything else is unsupported: a TYPED error,
    # never a silent empty turn: there is no client path to fall through to.
    raise UnsupportedModelProviderError(
        f"MODEL_PROVIDER={model_provider()!r} is not supported. Valid providers: "
        "scripted/replay/fake, anthropic, openai."
    )


__all__ = [
    "DEFAULT_VERTEX_MODEL",
    "MAX_TURN_ITERATIONS",
    "ModelSettings",
    "StreamEvent",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "FunctionCallEvent",
    "UsageMetadataEvent",
    "CompactionStartEvent",
    "CompactionCompleteEvent",
    # Upstream-provider discipline
    "UpstreamProviderError",
    "classify_provider_error_class",
    "provider_retries",
    "provider_backoff_s",
    "provider_backoff_wait",
    # Never-rehydrate guard (thinking persistence)
    "NEVER_REHYDRATE_FIELDS",
    "SYSTEM_PROMPT",
    "build_contents_from_history",
    "build_layers_present_note",
    "build_function_call_content",
    "build_function_response_content",
    "build_tool_declarations",
    "encode_parts_blob",
    "load_settings",
    "stream_events",
    "stream_events_with_contents",
    "summarize_tool_result",
    "classify_result_usable",
]
