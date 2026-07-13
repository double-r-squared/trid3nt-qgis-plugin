"""Gemini-only containment layer (Domain Discipline: agent.md).

Every Gemini- / google-genai-specific construct lives here. The server.py and
mcp.py modules call into this module with Gemini-naive shapes (strings,
async-iterators of strings). This is **containment, not abstraction** — no
``LLMProvider`` protocol, no provider branches, no Bedrock/Strands shapes.
FR-AS-1: Gemini-only. The deferred multi-provider future (§5) is not
foreclosed cheaply because the seam exists, but no abstraction is paid for now.

Model selection (job-0015):
  GRACE2_GEMINI_MODEL env override, defaulting to ``GEMINI_DEFAULT_MODEL``
  below. As of 2026-06-05 Gemini 3 (``gemini-3-pro*``) is not yet GA on Vertex
  for this project (verified 404 from generate_content); ``gemini-2.5-pro`` is
  the current best stable. When Gemini 3 lands on Vertex this constant — and
  the env override path — flips with no other code change.

Auth: ADC via ``GOOGLE_GENAI_USE_VERTEXAI=True`` + ``GOOGLE_CLOUD_PROJECT`` +
``GOOGLE_CLOUD_LOCATION``. No API key path. (job-0014 substrate.)

job-0154: tool-dispatch fix.  ``stream_reply`` sent Gemini no function
declarations and no system prompt, so Gemini had no knowledge of any tool and
responded with a prose refusal.  The new ``stream_events`` replaces it: it
passes the tool catalog (``FunctionDeclaration`` from each registered tool's
callable + docstring) plus a focused system prompt to ``generate_content_stream``,
then demultiplexes each chunk into either a ``TextDeltaEvent`` or a
``FunctionCallEvent`` so the server can dispatch the tool through the registry.
``stream_reply`` is retained as a thin compatibility shim (text-only calls).

job-0169: multi-turn function_call → function_response loop.  job-0154 stopped
after the first function_call (single-shot dispatch) — every multi-tool prompt
("Show me protected areas in Fort Myers" → geocode_location → fetch_wdpa) hung
because Gemini never saw the result of its first call and so never decided to
call the next tool.  This module now exposes:

  * ``stream_events`` (single-turn primitive — unchanged contract; still
    accepts ``user_text`` for backward compatibility).  Existing tests use it.
  * ``stream_events_with_contents`` (new primitive used by the loop driver):
    accepts a fully-built ``contents: list[Content]`` and streams one turn.
  * ``build_contents_from_history`` — converts ``state.chat_history`` plus the
    current user_text into the initial ``contents`` list.
  * ``summarize_tool_result`` — compacts a tool result into the dict that
    becomes the ``function_response.response`` payload Gemini reads on the
    next turn.  Per the kickoff: SUMMARY shape (LayerURI metadata, key
    metrics, error code) — NEVER the full raw tool result (which can be MB
    of GeoJSON).
  * ``build_function_call_content`` / ``build_function_response_content`` —
    typed helpers for appending the model+function turn pair after a
    dispatch.

The loop driver itself lives in ``server.py`` (``_stream_gemini_reply``) so it
can dispatch tools through ``_invoke_tool_via_emitter`` (registry + emitter
side effects).  This file stays the Gemini-containment seam.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import types as _builtin_types
from typing import Any, get_args, get_origin, Union

from google import genai
from google.genai import types as genai_types

logger = logging.getLogger("grace2_agent.adapter")

# Default Gemini model id. See module docstring for the Gemini-3-on-Vertex
# availability note. Override at runtime via ``GRACE2_GEMINI_MODEL``.
GEMINI_DEFAULT_MODEL = "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# Typed stream events (job-0154)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TextDeltaEvent:
    """A streamed text fragment from Gemini."""
    delta: str


@dataclass(frozen=True)
class ThinkingDeltaEvent:
    """A streamed reasoning-channel fragment (local OpenAI-compatible path).

    NATE live-feedback 2026-07-08 (local build): Ollama's OpenAI-compat stream
    surfaces qwen3-family thinking as ``delta.reasoning`` chunks. The
    openai_adapter yields these as ``ThinkingDeltaEvent`` so the server can
    forward them live to the web as ``agent-thinking-chunk`` envelopes (greyed
    foldable block). Never emitted by the Bedrock / Vertex / scripted paths;
    the server loop must tolerate + may drop them (user toggle off).
    """
    delta: str


@dataclass(frozen=True)
class FunctionCallEvent:
    """Gemini decided to call a tool.

    ``name`` matches the registered tool name in ``TOOL_REGISTRY``.
    ``call_id`` is Gemini's per-call identifier (used when feeding back the
    function response in the multi-turn loop).
    ``args`` is the deserialized argument dict.

    ``thought_signature`` (job-B10) is Gemini 3's opaque per-thought signature
    surfaced on the ``Part`` that carries the function_call. Gemini 3 (Vertex)
    requires the same signature byte-blob be echoed back on the *Part wrapping
    the function_call* when that turn is replayed in the next ``contents``
    payload — otherwise the next ``generate_content_stream`` fails with a
    ``thought-signature mismatch`` error. The harvest must happen at the part
    level (not the FunctionCall level — ``FunctionCall`` has no signature
    field in google-genai types.py); see ``build_function_call_content``.
    For Gemini 2.5 (current default until Gemini 3 lands on Vertex per
    ``GEMINI_DEFAULT_MODEL``), the field is absent and harvested as ``None``,
    which is a no-op when fed back. The plumbing is forward-compat.
    """
    name: str
    call_id: str | None
    args: dict[str, Any] = field(default_factory=dict)
    thought_signature: bytes | None = None


@dataclass(frozen=True)
class UsageMetadataEvent:
    """Per-turn usage metadata harvested from Gemini's ``response.usage_metadata``.

    Job-B6 (Wave 4.10): the multi-turn driver needs ``cached_content_token_count``
    + ``total_token_count`` on every Gemini call so it can:

      1. Verify the 90% cache discount actually lands in production
         (the original pre-dispatch blocker from
         ``project_wave_4_10_research_findings.md``).
      2. Forward a ``cache-status`` envelope into the PipelineEmitter so the
         user-facing UI can render live cache hit-rate.
      3. Pipe ``cached_content_token_count`` into the existing tool-call
         telemetry record (``telemetry.emit_tool_call_event``).

    Emitted at most once per ``generate_content_stream`` call — the producer
    pulls ``usage_metadata`` off the LAST chunk (Gemini surfaces aggregate
    counts only on the terminal response). All fields may be ``None`` when
    the SDK version does not expose them or the response was cancelled.
    """

    cached_content_token_count: int | None = None
    total_token_count: int | None = None
    prompt_token_count: int | None = None
    candidates_token_count: int | None = None
    cache_hit: bool = False


@dataclass(frozen=True)
class CompactionStartEvent:
    """Context-budget compaction (``context_budget.compact_contents``) is
    about to run for this turn -- proactive (before the request) or reactive
    (after a detected clip; see ``openai_adapter.stream_openai``).

    Compaction UX (Part A): yielded by the OpenAI-compatible adapter in
    place of the old ``TextDeltaEvent(delta=PROACTIVE_COMPACTION_NOTE /
    CLIP_RETRY_NOTE)`` narration seam. ``server.py``'s dispatch loop mints a
    durable running card ("Compacting conversation...") the instant this
    arrives (``pipeline_emitter.mint_compaction_card``) instead of gluing a
    disclaimer sentence onto the model's own reply -- the F10 running-tool-
    card treatment, animated on the wire, persisted so it survives a Case
    reopen. Carries no fields: the token counts are not yet final (the
    compacted-side count is only known once ``compact_contents`` returns),
    they ride the matching ``CompactionCompleteEvent``. Never emitted by the
    Bedrock / Vertex / scripted paths (compaction is a local/OpenAI-path-only
    concern -- see ``context_budget`` module docstring); the server loop must
    tolerate it being absent.
    """


@dataclass(frozen=True)
class CompactionCompleteEvent:
    """The compaction a preceding ``CompactionStartEvent`` announced has
    finished. ``before_tokens`` / ``after_tokens`` are
    ``context_budget.CompactionResult.before_tokens`` /
    ``.after_tokens`` -- the server-side dispatch loop renames the running
    card to its terminal "Conversation compacted (Nk -> Mk tokens)" label and
    flips it to ``complete`` on receipt (``pipeline_emitter.
    complete_compaction_card``). Always paired 1:1 with a prior
    ``CompactionStartEvent`` within the same adapter call.
    """

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


# ---------------------------------------------------------------------------
# System prompt builder (job-0154)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are GRACE — a geospatial hazard-modeling assistant. You help users analyze,
visualize, and model natural hazards (flooding, fire, hurricanes, etc.) using
real data and physics-based simulation tools.

When a user asks you to model, analyze, simulate, or compute anything related
to a hazard or geographic data, call the appropriate tool. Do not say you
cannot help with modeling requests — you have tools for that.

Key behaviors:
- If the user asks to model a flood scenario, run a flood simulation, compute
  flood depth, or analyze inundation for any location, call
  run_model_flood_scenario immediately -- UNLESS the request is urban /
  street-level / storm-drain / stormwater / pipe-network / SWMM-style, in
  which case call run_swmm_urban_flood instead (see the flood-engine routing
  block below).
- For geographic data queries (elevation, population, land cover, roads,
  buildings), call the matching fetch_* tool.
- For QGIS geoprocessing (clip, slope, hillshade, zonal statistics), call the
  matching compute_* or clip_* tool.
- Never fabricate numbers. All depth, area, and count values in your replies
  must come from the tool result, not from your own generation.
- When a tool result contains a flood depth layer, describe the results from
  the returned metrics — do not invent values.
- Keep responses concise and focused on the hazard modeling context.
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
(e.g. "WDPA", "NEXRAD", "NWS alerts", "NLCD", "MRMS", "HRRR", "GBIF",
"iNaturalist", "eBird", "MTBS", "LANDFIRE", "USACE NSI", "FEMA NFHL",
"protected areas", "burn severity", "radar reflectivity"), you MUST dispatch
that tool after completing any precursor steps (geocoding, admin-boundary
lookup, etc.). DO NOT end the turn at the precursor step — the precursor only
exists to feed the named tool.

Example: user asks "show me NEXRAD radar in Florida"
  1. Call geocode_location for "Florida" (precursor) →
  2. THEN call fetch_nexrad_reflectivity with the geocoded bbox →
  3. THEN narrate the result.

Example: user asks "show me protected areas in Big Cypress"
  1. Call geocode_location for "Big Cypress" (precursor) →
  2. THEN call fetch_wdpa_protected_areas with the geocoded bbox →
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

CRITICAL — DO NOT use list_categories / list_tools_in_category / discover_dataset
when the user has already named the tool or source. The mapping above IS the
discovery layer for these endpoints. Catalog browsing wastes turns and burns
the per-anchor budget. Examples of WRONG behavior:
  WRONG: user says "HRRR forecast" → list_categories → list_tools_in_category("weather") → fetch_hrrr_forecast
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
  4. THEN call clip_raster_to_polygon (for raster outputs) OR
     clip_vector_to_polygon (for vector outputs) using the admin polygon URI →
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

REUSE BEFORE RE-RUN — HARD RULE (CRITICAL, NON-NEGOTIABLE — job-0326,
NATE 2026-06-16, supersedes every softer reuse clause below):
Before you call ANY expensive simulation (run_model_flood_scenario,
run_model_nws_flood_event_scenario, run_modflow_job,
run_model_groundwater_contamination_scenario, run_pelicun_*), ANY fetch_*,
or ANY compute_*, you MUST FIRST check the "[Case state]" note for the
layers ALREADY produced and on the map for this Case. If a layer or result
that ALREADY ANSWERS the user's request is present, you MUST REUSE it — pass
its existing handle/uri DIRECTLY to the next step and narrate from it. DO NOT
re-fetch, re-compute, or re-run.

Re-running an expensive simulation whose output layer is ALREADY loaded is
FORBIDDEN. A flood-depth RESULT already on the map for this AOI means the
flood already ran — DO NOT call run_model_flood_scenario again; reuse that
flood-depth handle (e.g. for a Pelicun damage assessment). A plume RESULT
already on the map means the MODFLOW run already completed — DO NOT call
run_modflow_job again. The same applies to fetched layers (a landcover /
water-mask / DEM for this AOI already present → reuse it, never re-fetch) and
to computed layers (a hillshade / slope / zonal-stats result already present →
reuse it, never re-compute).

The ONLY times you may re-run / re-fetch / re-compute are:
  (a) the user EXPLICITLY asks to re-run, refresh, or recompute it, OR
  (b) the user CHANGES a parameter that changes the answer — a different area
      (AOI / bbox / location), a different return period or duration for a
      flood, a different contaminant / release rate / duration for a plume.
If neither (a) nor (b) holds and a matching result is already present, REUSE
IT. When in genuine doubt about whether an existing layer answers the request,
prefer reusing what is already there over launching a multi-minute solve.

This rule exists because the live agent IGNORED the softer steer below and
re-ran ~10-20-minute SFINCS / MODFLOW solves whose output layers were already
on the map — wasting minutes and money. A server-side guard now ALSO
short-circuits an obviously-redundant expensive re-run and returns the
existing layer with a "reused_existing" / "not re-run" note: when you see that
note, narrate from the existing layer; do not attempt the run again.

Scope discipline (CRITICAL — job-0255, Stage 3 live finding):
Run consequential tools (solvers like run_model_flood_scenario /
run_modflow_job, and layer-producing workflows) ONLY in service of the
user's CURRENT request. Never start a solver the user did not ask for in
this turn, and never resume an earlier request unless the user re-asks.
NEVER re-run an expensive solver that already completed THIS turn with the
same arguments — reuse its returned result (the live agent re-ran a ~10-20
minute SFINCS solve twice after detours instead of reusing the layer it
had already produced). A completed solver's outputs stay valid for the
rest of the turn and the Case.

Groundwater spill routing (CRITICAL — parameterized vs. news-article):
When the user gives the spill parameters DIRECTLY — a location plus a
contaminant plus a release rate (or amount) plus a duration — call
run_modflow_job DIRECTLY. Pass spill_location_latlon as a 2-element
[lat, lon] array (latitude first), the contaminant name, release_rate_kg_s,
and duration_days. Do NOT use
run_model_groundwater_contamination_scenario for a parameterized spill —
that tool is the news-ARTICLE ingest path; it expects an article_text or
source_url and a release amount stated in gallons / liters / barrels / tons
that it must extract and convert. Use it ONLY when the user pastes or links a
news article about a spill. Parameterized spill → run_modflow_job; spill news
article → run_model_groundwater_contamination_scenario.

Flood-engine routing -- urban PySWMM vs SFINCS (CRITICAL, North Star B3):
GRACE has TWO flood solvers. Route to the right one from the prompt; do NOT
default every flood to SFINCS.

- run_swmm_urban_flood (quasi-2D PySWMM, the URBAN engine). Route here when the
  scenario is urban / street-level / storm-drain / stormwater / drainage /
  pipe-network / sewer / SWMM / PCSWMM-style: street flooding from a design
  storm over a city block or neighborhood, ponding around BUILDINGS in a
  developed area, flooding driven by the storm-drain / pipe network, or any
  scenario with structural flood controls the user drew or described -- a SOUND
  BARRIER / flood WALL (water dammed) or a FLAP GATE / one-way drain (water
  passes one direction only), passed as ``barriers``. Cue words: "urban",
  "street", "storm drain", "stormwater", "drainage", "sewer", "pipe network",
  "SWMM", "city block", "neighborhood", "around the buildings", "flood wall",
  "barrier", "flap gate". This is NATE's PCSWMM urban demo path.

- run_model_flood_scenario (SFINCS, the COASTAL / RIVERINE / WATERSHED engine).
  Route here for coastal / surge / storm-tide inundation, riverine / fluvial
  flooding along a river, and large pluvial-WATERSHED rainfall flooding over a
  county-or-larger AOI. Cue words: "coastal", "surge", "storm surge",
  "hurricane", "riverine", "river", "fluvial", "watershed", "basin",
  "county-wide". SFINCS has its OWN opt-in ``building_obstacles`` for a
  developed AOI (water routes around footprints on the regular/quadtree grid),
  so a developed AOI alone does NOT force SWMM -- the storm-drain / pipe-network
  / street-scale / barrier framing is what selects the urban PySWMM engine.
  COASTAL STORM SURGE WITH WAVES: for a coastal storm-surge / hurricane-inundation
  prompt (surge coming in from the sea, storm tide, wave run-up), set
  ``coastal=True`` -- this now AUTO-WIRES a time-varying sea-surge water-level
  boundary AND turns on SnapWave waves, so the animation shows water rising from
  the sea and marching inland with a wave-height field (you do NOT need to hand-build
  ``surge_forcing``). Set ``quadtree=True`` if the user explicitly wants WAVES on an
  otherwise-inland AOI. ``coastal=True`` with NO sea/surge/wave intent (a rainfall
  flood that merely happens to be near the coast) models only rainfall -- only flag
  coastal when the SEA is part of the scenario.
  WAVE ANIMATION CADENCE: a coastal/wave run automatically outputs FINE
  minute-scale animation frames (default ~5 min) so the surge+waves read as water
  rolling in, not a slowly-filling bathtub (hourly frames hide wave motion). The
  user can override the speed via ``output_interval_min`` (minutes per frame, e.g.
  5 or 10); leave it unset to use the sim-type default. The pluvial path stays
  hourly -- do NOT pass ``output_interval_min`` for a rainfall-only flood.

When the scope is ambiguous between the two (e.g. a developed AOI with no
storm-drain / barrier / street framing, where either engine could fit), ASK the
user one short clarifying question -- urban storm-drain street flooding (PySWMM)
or watershed / coastal / riverine inundation (SFINCS)? -- before launching a
multi-minute solve. This is consistent with the SFINCS ASK-WHEN-URBAN building
opt-in: when the urban intent is clear, dispatch run_swmm_urban_flood directly;
when only the AOI is developed but the driver is unstated, confirm first.

Satellite fire-animation routing (CIRA/GOES/JPSS fire timelapse):
To "recreate a CIRA / GOES / JPSS fire animation" (cue words: "recreate the
satellite animation", "GOES fire timelapse", "VIIRS Day Fire", "animate the fire
from satellite imagery", "CIRA loop", "watch the fire grow on satellite") route
to run_model_satellite_fire_animation. It resolves the named incident
(fetch_wfigs_incident, by NAME so offshore islands work), derives the AOI bbox +
time window, then fetches per-frame imagery and animates it with FIRMS hot pixels
+ the NIFC perimeter overlaid. Pick the imagery family from the timescale:
- GOES-18 (geostationary) for an INTRA-DAY loop at 5-minute cadence -- products
  "geocolor" + "fire_temperature" (the cue is a window of hours on one day).
- JPSS / VIIRS Day Fire (polar) for a MULTI-DAY series of irregular overpasses --
  product "day_fire" (the cue is a multi-day window; passes are not evenly
  spaced, so the frames carry their real UTC pass times).
ALWAYS hit the bbox/window REVIEW gate first: call with confirm=false to return
the AOI bbox + the planned frame list so the user can SEE + ADJUST the bbox and
the window, THEN call again with confirm=true (carrying any adjusted
bbox/start_utc/end_utc) to fetch all frames + publish. Do NOT fetch all frames on
the first turn.

Layer-handle indirection (CRITICAL — job-0263, supersedes the job-0252 /
job-0255 URI clauses): when a tool parameter takes a layer / raster /
vector URI (hazard_raster_uri, assets_uri, layer_uri, value_raster_uri,
zone_layer_uri, damage_layer_uri, dem_uri, raster_uri, polygon_uri, ...),
pass the layer_id HANDLE exactly as it appeared in a prior
function_response of THIS conversation — e.g. "flood-depth-peak-<run_id>"
from a flood scenario, the "usace-nsi-..." layer_id from fetch_usace_nsi,
or any handle listed under "layer_handles" in a tool result. The server
resolves handles to the exact storage URIs it recorded when the layer was
produced.

- A layer handle exists ONLY after the tool that PRODUCES it has already run
  in THIS conversation and returned it. NEVER fabricate or pattern-match a
  handle — do NOT invent a "flood-depth-peak-<id>" (or any layer_id) for a
  layer you have not actually produced; that handle does not exist and the
  call fails ("local path does not exist"). To assess flood damage you MUST:
  (1) call run_model_flood_scenario (or run_model_nws_flood_event_scenario),
  (2) WAIT for its function_response, (3) pass the EXACT flood-depth handle it
  returned to compute_impact_envelope / run_pelicun_damage_assessment /
  run_pelicun_with_buildings. Never skip step 1 and never guess step 3's value.
- If run_model_flood_scenario returns a FAILED envelope (status "failed",
  an error/error_code field, or NO flood-depth layer/handle), the flood did
  NOT run — there is NO flood layer to assess. Do NOT invent a handle and do
  NOT call any damage tool. Instead, tell the user the flood modeling failed
  (quote the reason from the response) and stop, or retry the flood ONLY if
  the error is retryable. A damage assessment with no real flood layer is a
  fabricated answer (Invariant 7) — never produce one.
- NEVER construct, guess, reconstruct, abbreviate, or pattern-match a
  gs:// path, and never re-type one from memory — hand-built URIs are
  rejected with URI_HANDLE_UNRESOLVED. When that error fires, its message
  lists the valid handles: pick the right one and retry.
- A raw gs:// URI is accepted only when copied VERBATIM (character for
  character) from a prior function_response. When in doubt, pass the
  layer_id handle instead — it is always correct.
- The published layer's WMS display URL (https://...&LAYERS=...) is for
  the map client only — do NOT pass it as a data URI. For damage
  assessment over a modeled flood, pass the flood layer's layer_id handle
  as hazard_raster_uri.
- If no prior tool produced the layer a parameter needs, run the
  producing tool first (e.g. run_model_flood_scenario yields the
  flood-depth layer to feed run_pelicun_damage_assessment) or tell the
  user what is missing.

Location fidelity (CRITICAL — job-0274, live finding):
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
"encompass all the <features>" (all protected areas, all buildings, all points)
for data that is ALREADY on the map, that is a VIEW change, NOT a data fetch.
Call compute_layer_bounds on the EXISTING layer's handle (from the [Case state]
note) — do NOT call the fetch_* tool again. Re-fetching a layer already present
mints a SECOND identical layer (e.g. two identical WDPA choropleths stacked on
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

Full-AOI extent for every overlay (CRITICAL — never shrink the area):
For ANY area or overlay layer (land cover, hillshade, colored relief, slope,
aspect, roads, rivers, flood depth, plume, etc.), use the FULL Case AOI
bounding box — the SAME bbox as the rest of the Case (the Case AOI bbox in
the [Case state] note, or the bbox derived from geocoding the user's named
location). Every layer in a Case must share one extent so the overlays line
up. When the user says "you don't need to fetch all" (or "don't fetch
everything", "skip some layers"), they mean DO NOT fetch every possible
layer / data source — pick the few that matter. It NEVER means shrink the
area or the bbox. Do not crop, shrink, or sub-window the AOI in response to
such a phrase; keep the full Case extent and just fetch fewer layer types.

Publish-to-map discipline (CRITICAL — job-0270, live finding):
A tool result that returns a layer handle or gs:// raster is data in
storage, NOT pixels on the user's map. When the user asked to SEE, show,
map, render, or visualize a layer (e.g. "compute a colored relief map for
Boulder"), the request is NOT complete after the compute/fetch tool — you
MUST finish by calling publish_layer(layer_uri=<handle>,
layer_id=<descriptive-id>) with the producing tool's handle, then narrate a
one-line summary. NEVER claim a layer is displayed, shown, or "added to the
map" unless publish_layer returned a WMS URL THIS turn. The only exception is
a tool whose own function_response signals it ALREADY PUBLISHED its layer —
recognized by ANY of: a "wms_url" field, "published": true, "on_map": true,
or a "publish_status" of "published". A simulation/scenario result
(run_model_flood_scenario, run_model_nws_flood_event_scenario,
run_model_flood_habitat_scenario, run_model_groundwater_contamination_scenario,
run_modflow_job) returns its peak-depth / plume layer ALREADY published, styled,
and on the map — its function_response carries "published": true and "on_map":
true. Do NOT call publish_layer on that scenario layer's handle or URI: it is
already rendered, and a second publish_layer would paint a redundant styleless
duplicate (live finding — two flood layers, one viridis). Just narrate from it.

publish_layer is for RASTER COGs ONLY (CRITICAL — vector render path):
NEVER call publish_layer on a VECTOR layer — roads, rivers, waterways,
streams, administrative boundaries, watershed/basin polygons, building
footprints, occurrence points, or any *.fgb / *.geojson / GeoParquet output.
Vector layers are ALREADY shown on the map by the fetch tool that produced
them (e.g. fetch_osm_roads, fetch_river_geometry,
fetch_administrative_boundaries, clip_vector_to_polygon) — that tool's own
function_response already put the vector on the map; there is nothing left to
publish. Calling publish_layer on a vector is an error (it publishes raster
COGs only) and a duplicate. publish_layer is exclusively for raster outputs
(DEM, hillshade, colored relief, slope, aspect, land cover, flood depth,
plume concentration — gs:// COGs). When in doubt: raster → publish_layer;
vector → already on the map, just narrate and stop.

Shaded / baked land cover — use the land cover AS the blend base (CRITICAL):
When the user asks to bake, shade, drape, or blend NLCD land cover with a
hillshade (a "shaded land cover"), pass the fetch_landcover layer handle
DIRECTLY as compute_blended_composite's base_layer_uri, with the hillshade as
the overlay. NLCD land cover is a paletted/categorical raster: the blend tool
reads its EMBEDDED color table and applies it, so blending the land cover
directly yields the real NLCD CLASS colors (forest green, water blue,
developed grey) shaded by terrain. Do NOT pre-colorize the land cover, and do
NOT substitute compute_colored_relief as the base — colored_relief is
ELEVATION colors (a DEM ramp), NOT land-cover classes, so using it produces a
terrain map and throws away the land cover the user asked for.

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
"Running the SFINCS flood solve, this can take a couple of minutes...". For a
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


# ---------------------------------------------------------------------------
# Tool declaration builder (job-0154)
# ---------------------------------------------------------------------------

def _is_union_type(annotation: Any) -> bool:
    """Return True if annotation is any union form (typing.Union or X | Y syntax).

    Python 3.10+ ``X | Y`` creates ``types.UnionType``; ``typing.Union[X, Y]``
    creates a ``_GenericAlias`` whose ``get_origin`` is ``Union``.  Both must be
    detected for full compatibility.
    """
    if isinstance(annotation, _builtin_types.UnionType):
        return True
    return get_origin(annotation) is Union


def _union_args(annotation: Any) -> tuple[Any, ...]:
    """Return the member types of a union annotation (any union form)."""
    if isinstance(annotation, _builtin_types.UnionType):
        return annotation.__args__
    return get_args(annotation)


def _is_tuple_annotation(annotation: Any) -> bool:
    """Return True when *annotation* is a ``tuple[...]`` type (not just bare ``tuple``).

    ``from_callable_with_api_option`` silently drops parameters whose type is a
    fixed-length tuple (e.g. ``tuple[float, float, float, float]``) and raises
    for ``tuple[float, float, float, float] | None``.  Both forms must be
    replaced before the callable reaches ``from_callable``.

    Handles both ``typing.Optional[tuple[...]]`` (``typing.Union``) and the
    Python 3.10+ ``tuple[...] | None`` (``types.UnionType``) syntax.
    """
    if _is_union_type(annotation):
        args = _union_args(annotation)
        return any(_is_tuple_annotation(a) for a in args if a is not type(None))
    # Plain tuple[...] — origin is ``tuple``
    return get_origin(annotation) is tuple


def _simplify_annotation(annotation: Any) -> Any:
    """Map a complex annotation to a Gemini-compatible equivalent.

    Gemini's OpenAPI schema subset rejects:
    * ``tuple[float, ...]`` — silently dropped; use ``list[float]`` instead.
    * ``tuple[float, ...] | None`` — raises in ``from_callable``; use
      ``list[float] | None``.
    * ``str | tuple[float, ...]`` — Union of incompatible types; use ``str``.
    * Any Pydantic model / dataclass annotation — raises in ``from_callable``;
      use ``str | None`` (the serialized form that crosses the LLM boundary).

    Parameters that are already schematizable (``str``, ``int``, ``float``,
    ``bool``, ``list[str]``, ``Literal[...]``, ``str | None``, etc.) pass
    through unchanged.

    Handles both ``typing.Union``/``Optional`` (Python 3.9) and the new
    ``X | Y`` union syntax (Python 3.10+, ``types.UnionType``).

    B11 (Wave 4.10): centralised in the adapter so no tool file needs touching.
    """
    if annotation is inspect.Parameter.empty:
        return annotation

    # --- Union forms (typing.Union and Python 3.10+ X|Y) ---
    if _is_union_type(annotation):
        args = _union_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        has_none = type(None) in args

        # ``tuple[...] | None`` → ``list[elem] | None``
        if len(non_none) == 1 and _is_tuple_annotation(non_none[0]):
            inner = non_none[0]
            inner_args = get_args(inner)
            elem_type = inner_args[0] if inner_args else float
            list_type: Any = list[elem_type]  # type: ignore[valid-type]
            return list_type | None  # type: ignore[return-value]

        # ``str | tuple[...]`` or any union containing a tuple → keep only str
        if any(_is_tuple_annotation(a) for a in non_none):
            str_args = [a for a in non_none if a is str]
            return str if str_args else str

        # ``SomePydanticModel | None`` → ``str | None``
        simplified_non_none = []
        for a in non_none:
            s = _simplify_annotation(a)
            simplified_non_none.append(s)

        if len(simplified_non_none) == 1:
            result = simplified_non_none[0]
            return (result | None) if has_none else result  # type: ignore[return-value]

        # Multi-type union (e.g. ``float | list[float] | None``):
        # Prefer the list form if one is present (a list[float] covers a single
        # float too from the LLM's perspective), otherwise prefer str as a
        # universal fallback so Gemini at least sees a typed parameter.
        list_args = [a for a in simplified_non_none if get_origin(a) is list]
        if list_args:
            result = list_args[0]
            return (result | None) if has_none else result  # type: ignore[return-value]
        str_args = [a for a in simplified_non_none if a is str]
        if str_args:
            result = str
            return (result | None) if has_none else result  # type: ignore[return-value]

        # Last resort — keep as-is; the ``from_callable`` call may still succeed
        # for simple multi-type unions like ``int | str``.
        return annotation

    origin = get_origin(annotation)
    args = get_args(annotation)

    # --- bare ``tuple[float, ...]`` → ``list[float]`` ---
    if origin is tuple and args:
        elem_type = args[0]
        return list[elem_type]  # type: ignore[valid-type]

    # --- complex Pydantic model / dataclass annotation → ``str | None`` ---
    # A class that is not a built-in and not a simple generic is a custom model.
    # We detect this by checking whether the origin is None (not a generic) and
    # whether the annotation is a class (not a primitive like ``str``).
    if (
        origin is None
        and isinstance(annotation, type)
        and annotation not in (str, int, float, bool, bytes, dict, list, type(None))
    ):
        # Custom class (Pydantic, dataclass, …) — replace with ``str | None``
        # so the LLM at least sees the parameter name and can supply a value.
        return str | None  # type: ignore[return-value]

    return annotation


def _normalize_callable_for_gemini(fn: Any) -> Any:
    """Return a thin wrapper of *fn* with annotations simplified for ``from_callable``.

    ``FunctionDeclaration.from_callable_with_api_option`` rejects callables whose
    annotations contain:
    * ``-> LayerURI`` or any other Pydantic/dataclass return type
    * ``tuple[float, float, float, float] | None`` parameter annotations
    * ``tuple[int, int] | None`` year-range annotations
    * ``str | tuple[float, ...]`` Union parameters
    * Complex Pydantic model parameters (``SecretRecord | None``)

    This helper produces a ``functools.wraps``-preserving wrapper whose
    ``__annotations__`` are identical to the original except that:
    1. The return annotation is replaced with ``dict`` (all tools return
       serialisable dicts over the LLM boundary regardless of their Python
       return type).
    2. Each non-underscore parameter annotation is passed through
       ``_simplify_annotation`` to replace unsupported types with
       schema-compatible equivalents (list[float], str | None, etc.).

    The wrapper delegates all calls to the original function unchanged —
    behaviour is unaffected; only the schema-generation surface is altered.

    B11 (Wave 4.10): centralised in the adapter so no individual tool file
    needs to be touched.  The OQ-0154-DECL-FALLBACK open question is resolved
    by this function — all 55 registered tools now pass ``from_callable``.
    """
    import typing as _typing

    @functools.wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    # Resolve forward-reference strings to real types via get_type_hints().
    # This is essential because all tool modules use ``from __future__ import
    # annotations``, which defers evaluation and stores strings in __annotations__.
    try:
        resolved: dict[str, Any] = _typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 — name resolution can fail in unusual envs
        # Fall back to the raw (possibly string) annotations.
        try:
            resolved = fn.__annotations__.copy()
        except AttributeError:
            resolved = {}

    new_annotations: dict[str, Any] = {}
    for param_name, annotation in resolved.items():
        if param_name == "return":
            # Always replace complex return types with ``dict``.  The actual
            # return value crosses the LLM boundary via ``summarize_tool_result``
            # (adapter.py), which serialises it to a JSON-safe dict anyway.
            new_annotations["return"] = dict
        elif param_name.startswith("_"):
            # Private/test-injection params: keep as-is (they'll be stripped
            # downstream by ``_strip_private_params``).
            new_annotations[param_name] = annotation
        else:
            new_annotations[param_name] = _simplify_annotation(annotation)

    _wrapper.__annotations__ = new_annotations
    return _wrapper


def _strip_private_params(decl: genai_types.FunctionDeclaration) -> genai_types.FunctionDeclaration:
    """Remove underscore-prefixed parameters from a generated FunctionDeclaration.

    job-0163 finding: 16+ atomic tools (``compute_zonal_statistics``,
    ``compute_impervious_surface``, ``extract_landcover_class``,
    ``clip_raster_to_*``, ``compute_hillshade``/``slope``/``aspect``, etc.)
    accept underscore-prefixed test-injection kwargs such as
    ``_storage_client: object | None = None`` and ``_bucket: str | None = None``.
    These are Python's standard "internal/private" naming convention and exist
    only so unit tests can pass a mock GCS client — they must NEVER be visible
    to the LLM.

    ``FunctionDeclaration.from_callable_with_api_option`` includes them in the
    generated schema; ``_storage_client: object | None`` becomes a Schema with
    only ``nullable=True`` (no ``type`` field), which Vertex Gemini rejects
    with ``400 INVALID_ARGUMENT: schema didn't specify the schema type field``,
    blocking the ENTIRE tool catalog — Gemini cannot dispatch any tool. This
    function surgically removes every underscore-prefixed property from the
    schema (and from ``required``) before the declaration is returned.

    Bug-class fix (per AGENTS.md "Bundle small fixes; scan for all instances"):
    the filter is keyed on the underscore prefix, so any future tool with a
    test-injection kwarg automatically gets the same treatment.
    """
    if decl.parameters is None or decl.parameters.properties is None:
        return decl
    cleaned_props = {
        n: s for n, s in decl.parameters.properties.items() if not n.startswith("_")
    }
    cleaned_required = (
        [r for r in (decl.parameters.required or []) if not r.startswith("_")]
        if decl.parameters.required is not None
        else decl.parameters.required
    )
    new_parameters = decl.parameters.model_copy(
        update={"properties": cleaned_props, "required": cleaned_required}
    )
    return decl.model_copy(update={"parameters": new_parameters})


def build_tool_declarations(
    tool_registry: dict[str, Any],
) -> list[genai_types.FunctionDeclaration]:
    """Build Gemini ``FunctionDeclaration`` objects from the TOOL_REGISTRY.

    Uses ``FunctionDeclaration.from_callable_with_api_option`` so the
    docstring discipline enforced at registration time (FR-AS-3 "Use this
    when:" / "Do NOT use this for:" / param/return descriptions) is the
    sole source of Gemini's tool-selection signal — the same text that a
    human reviewer sees is exactly what Gemini reasons over.

    B11 (Wave 4.10) compliance fix: before calling ``from_callable``, every
    tool's callable is passed through ``_normalize_callable_for_gemini`` which
    replaces Gemini-incompatible annotations with schematisable equivalents:

    * ``-> LayerURI`` (or any Pydantic/dataclass return type) → ``-> dict``
    * ``tuple[float, float, float, float]`` → ``list[float]`` (silently dropped
      by ``from_callable`` in all SDK versions tested)
    * ``tuple[float, ...] | None`` → ``list[float] | None``
    * ``tuple[int, int] | None`` → ``list[int] | None``
    * ``str | tuple[float, ...]`` → ``str``
    * ``SomeModel | None`` (Pydantic complex type) → ``str | None``

    Falls back to a docstring-only declaration only if ``from_callable`` still
    raises after normalisation (should not occur for any tool in the current
    registry; logged at WARNING, not DEBUG, to make regressions visible).

    Every generated declaration is post-processed through
    ``_strip_private_params`` to remove underscore-prefixed kwargs (job-0163;
    see that helper's docstring for the Vertex 400 trace).
    """
    declarations: list[genai_types.FunctionDeclaration] = []
    for name, entry in sorted(tool_registry.items()):
        normalised = _normalize_callable_for_gemini(entry.fn)
        try:
            decl = genai_types.FunctionDeclaration.from_callable_with_api_option(
                callable=normalised,
                api_option="VERTEX_AI",
            )
            declarations.append(_strip_private_params(decl))
        except Exception as exc:  # noqa: BLE001 — fallback gracefully
            logger.warning(
                "tool declaration fallback for %r (normalisation did not resolve "
                "complex signature — file a B11 follow-up): %s",
                name,
                exc,
            )
            doc = inspect.getdoc(entry.fn) or f"Tool: {name}"
            declarations.append(
                genai_types.FunctionDeclaration(
                    name=name,
                    # 1 000 chars captures "Use this when:" + "Do NOT" + "Params:"
                    # sections from well-documented tools (FR-AS-3 discipline).
                    description=doc[:1000],
                )
            )
    return declarations


# ---------------------------------------------------------------------------
# GeminiSettings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeminiSettings:
    """Resolved Gemini configuration (env-derived; no implicit fallbacks)."""

    model: str
    project: str
    location: str
    use_vertex: bool


def load_settings() -> GeminiSettings:
    """Resolve Gemini settings from the environment.

    Required env (job-0014 substrate):
    - ``GOOGLE_GENAI_USE_VERTEXAI=True``
    - ``GOOGLE_CLOUD_PROJECT`` (default: ``grace-2-hazard-prod``)
    - ``GOOGLE_CLOUD_LOCATION`` (default: ``us-central1``)

    Optional:
    - ``GRACE2_GEMINI_MODEL`` (default: ``GEMINI_DEFAULT_MODEL``)
    """
    return GeminiSettings(
        model=os.environ.get("GRACE2_GEMINI_MODEL", GEMINI_DEFAULT_MODEL),
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", "grace-2-hazard-prod"),
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        use_vertex=os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "True").lower()
        in ("true", "1", "yes"),
    )


def build_client(settings: GeminiSettings) -> genai.Client:
    """Build a google-genai Client configured for Vertex AI.

    Containment: nothing outside this module imports ``genai`` or
    ``genai_types``. The server consumes the async-iterator of deltas this
    module returns.
    """
    if not settings.use_vertex:
        # Pre-MVP: Vertex-only path. Surface the misconfiguration loudly
        # rather than silently falling back to API-key mode.
        raise RuntimeError(
            "GRACE2 agent runs Vertex-only (FR-AS-1). "
            "Set GOOGLE_GENAI_USE_VERTEXAI=True."
        )
    return genai.Client(
        vertexai=True, project=settings.project, location=settings.location
    )


# ---------------------------------------------------------------------------
# Content / function_response builders (job-0169)
# ---------------------------------------------------------------------------

# Hard upper bound on chars we send back to Gemini per function_response.
# Anything bigger gets clipped — Gemini doesn't need megabytes of GeoJSON to
# decide the next tool call; it needs the LayerURI, key metrics, error code,
# and a couple of identifying fields.
_FUNCTION_RESPONSE_CHAR_BUDGET = 4_000

# Maximum loop iterations for the multi-turn driver.  Each iteration is one
# Gemini stream + (optionally) one dispatched tool call.  Raised from 8 to 12
# for Wave 4.10 (job-B9) to accommodate the added chain depth from new
# fetchers (STAC, ERDDAP, THREDDS, gridMET, CO-OPS, etc.) plus the
# allowed-set discovery overhead (list_categories → list_tools_in_category →
# actual fetch → publish) that Wave 4.10 category routing introduces.
# Per the research survey (project_wave_4_10_research_findings.md): 10-12 is
# the validated range; 12 provides headroom for the longest realistic chains.
# If Gemini somehow loops past 12, that's a runaway and the fail-stop +
# loop_exhausted envelope (job-B9) is the correct response.
MAX_TURN_ITERATIONS = 12


def _decode_parts_blob(blob: Any) -> list[genai_types.Part] | None:
    """Decode a persisted ``parts_blob`` into a list of ``Part`` (job-B10).

    The ``parts_blob`` schema on a chat_history entry is a JSON byte string
    (or pre-decoded dict / list) carrying enough fidelity to reconstruct the
    exact ``Part`` objects from the prior turn — including ``function_call``,
    ``function_response``, and ``thought_signature`` — so a replayed turn
    survives Gemini 3's signature-mismatch check.

    Wire shape (one entry per part):
        {"text": "..."}                         # text-only part
        {"function_call": {"name": ..., "id": ..., "args": {...}},
         "thought_signature_b64": "..."}        # Gemini 3 model turn
        {"function_response": {"name": ..., "id": ..., "response": {...}}}

    ``thought_signature`` is persisted base64-encoded (JSON cannot carry raw
    bytes); decoded back to bytes here. Returns ``None`` if the blob is
    missing/empty/malformed so the caller can fall back to the text path —
    we never raise on a malformed history entry (a single bad row would
    otherwise break the whole conversation).
    """
    import base64 as _b64
    import json as _json

    if blob is None:
        return None
    raw: Any
    if isinstance(blob, (bytes, bytearray)):
        try:
            raw = _json.loads(blob.decode("utf-8"))
        except Exception:  # noqa: BLE001 — malformed → text fallback
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
        # Single-part shorthand — wrap.
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None

    parts: list[genai_types.Part] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kwargs: dict[str, Any] = {}
        if "text" in entry and entry["text"]:
            kwargs["text"] = entry["text"]
        if "function_call" in entry and isinstance(entry["function_call"], dict):
            fc = entry["function_call"]
            kwargs["function_call"] = genai_types.FunctionCall(
                name=fc.get("name"),
                args=fc.get("args") or {},
                id=fc.get("id"),
            )
        if "function_response" in entry and isinstance(entry["function_response"], dict):
            fr = entry["function_response"]
            kwargs["function_response"] = genai_types.FunctionResponse(
                name=fr.get("name"),
                response=fr.get("response") or {},
                id=fr.get("id"),
            )
        sig_b64 = entry.get("thought_signature_b64")
        if isinstance(sig_b64, str) and sig_b64:
            try:
                kwargs["thought_signature"] = _b64.b64decode(sig_b64)
            except Exception:  # noqa: BLE001
                pass
        if not kwargs:
            continue
        try:
            parts.append(genai_types.Part(**kwargs))
        except Exception:  # noqa: BLE001 — drop the bad part, keep going
            continue
    return parts or None


def build_contents_from_history(
    user_text: str,
    chat_history: list[dict] | None = None,
) -> list[genai_types.Content]:
    """Convert ``chat_history`` + a new ``user_text`` into Gemini ``Content``s.

    Chat history entries are dicts. The supported shapes are:

    * Text-only (legacy): ``{"role": ..., "text": "..."}`` — collapsed into a
      single text Part. ``role`` is one of ``user`` / ``agent`` / ``assistant``
      / ``model``; Gemini only understands ``user`` / ``model`` (agent and
      assistant collapse to ``model``).
    * Full-fidelity (job-B10): ``{"role": ..., "parts_blob": <bytes|str|list>,
      "text": "..." (optional fallback)}`` — when ``parts_blob`` decodes
      cleanly, the Content uses the reconstructed Parts (which may carry
      function_call, function_response, or thought_signature). This shape is
      what the multi-turn driver MUST emit to round-trip Gemini 3's
      thought_signature through chat history.

    The ``parts_blob`` path takes precedence: when present and decodable, it
    is used instead of reconstructing from text. Empty-text legacy entries
    are dropped (the persistence layer writes empty rows for the LLM's
    reply-turn marker; those carry no signal for Gemini). The new user_text
    is always appended as the terminal ``user`` turn.
    """
    contents: list[genai_types.Content] = []
    if chat_history:
        for entry in chat_history:
            role = entry.get("role", "user")
            gem_role = "model" if role in ("agent", "assistant", "model") else "user"
            # B10: prefer parts_blob when present — it carries function_call /
            # function_response Parts plus any thought_signature, so the
            # replayed turn survives Gemini 3's signature-mismatch check.
            blob = entry.get("parts_blob")
            decoded = _decode_parts_blob(blob) if blob is not None else None
            if decoded:
                contents.append(genai_types.Content(role=gem_role, parts=decoded))
                continue
            text = entry.get("text", "")
            if not text:
                continue
            contents.append(
                genai_types.Content(
                    role=gem_role,
                    parts=[genai_types.Part(text=text)],
                )
            )
    contents.append(
        genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=user_text)],
        )
    )
    return contents


# Default cap on the number of persisted chat rows rehydrated into the live
# Gemini context on a Case reopen (F17 / ux-batch-1 J8). A long-running Case
# can accumulate hundreds of user/agent/tool rows; replaying all of them every
# reopen turn would blow the context window (and the per-turn cost). We keep
# the MOST RECENT rows (the tail carries the relevant recent state — what the
# user just did and what is on the map now). The injected layers-present note
# (built separately) is the durable anchor for older work, so dropping the head
# of a long transcript does not lose "what layers already exist".
REHYDRATE_HISTORY_CAP = 40


def _summarize_tool_row_for_history(content: str, tool_card: Any) -> str:
    """Collapse a persisted ``role="tool"`` row into one model-side text line.

    F17: the persisted store keeps tool turns as a ``ToolCardRecord`` (typed
    ``tool_card`` + a JSON-string mirror in ``content``); the full-fidelity
    function_call / function_response Parts are NOT persisted, so we cannot
    rebuild a real tool turn. A short text transcript line is enough to stop
    recompute — the model only needs to know the tool already ran and how it
    came out. Shape: ``[tool <name> completed]`` / ``[tool <name> failed]``.

    Falls back to parsing ``content`` (the JSON mirror) when the typed
    ``tool_card`` is absent (pre-job-0267 / non-contract consumers).
    """
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
        except Exception:  # noqa: BLE001 — content may not be JSON
            pass
    if not name:
        return ""
    outcome = "failed" if state == "failed" else "completed"
    return f"[tool {name} {outcome}]"


def _format_layer_bbox(bbox: Any) -> str | None:
    """Compact ``[lon_min, lat_min, lon_max, lat_max]`` for a layer line (job-0326).

    Returns a short rounded string for a valid 4-tuple bbox, else ``None`` (most
    persisted ``ProjectLayerSummary`` rows carry no bbox, so the line simply
    omits it). The rounding keeps the [Case state] note compact.
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        b = [round(float(x), 4) for x in bbox]
    except (TypeError, ValueError):
        return None
    return f"[{b[0]}, {b[1]}, {b[2]}, {b[3]}]"


def _format_aoi_bbox_line(case_bbox: Any) -> str | None:
    """Format the Case AOI bbox as a single durable instruction line (F17/F20).

    ``case_bbox`` is the Case's persisted ``[lon_min, lat_min, lon_max,
    lat_max]`` (``CaseSummary.bbox``). Returns ``None`` for missing / malformed
    bboxes. This line is the AOI ANCHOR that must survive history capping —
    long Cases drop the head user turn that named the place, so without an
    explicit bbox a follow-up that fetches fresh data (e.g. a DEM for a
    hillshade) loses the extent and re-geocodes / mis-scopes (panel-flagged).
    """
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
    """Build the compact "Case state" model turn (F17): layers + AOI bbox.

    ``loaded_layers`` is the persisted ``CaseSessionState.loaded_layers`` —
    a list of ``ProjectLayerSummary`` ``model_dump(mode="json")`` dicts. We
    surface ``layer_id`` / ``name`` / ``layer_type`` per entry AND the
    reusable ``handle`` (== the ``layer_id`` per the layer-handle indirection
    contract) plus the underlying ``uri`` (``layer_uri``) so the model can
    pass an already-produced layer STRAIGHT into a tool param (e.g.
    ``compute_blended_composite`` base/overlay, or any ``*_uri`` input)
    instead of re-fetching or recomputing it (F54). ``case_bbox``
    (``CaseSummary.bbox``) is appended as a durable AOI anchor so the extent
    survives history capping (F20: follow-ups reuse the original AOI).
    Returns ``None`` only when there is neither a layer nor a usable bbox.
    Kept deliberately short.
    """
    # job-0326: enrich each line with enough IDENTITY that the model can
    # recognize an existing RESULT (so it never re-runs the solver that made it):
    #   - role: RESULT (a primary simulation / analysis output) vs INPUT
    #     (a fetched / context layer used as a solver input);
    #   - the producing scenario/family when recognizable from the layer_id
    #     (flood-depth, plume, ...) so "a flood-depth RESULT for this AOI is
    #     already here" and "the landcover/water-mask for this AOI is already
    #     here" read unambiguously;
    #   - name, layer_type, the reusable handle (== layer_id), and the uri.
    # local import: avoid cycle
    from .scenario_reuse import fetched_layer_kind, layer_id_scenario_type

    lines: list[str] = []
    for layer in loaded_layers or []:
        if not isinstance(layer, dict):
            continue
        layer_id = layer.get("layer_id") or "?"
        name = layer.get("name") or layer_id
        layer_type = layer.get("layer_type") or "?"
        # F54: the layer_id IS the reusable handle (layer-handle indirection
        # block in the system prompt); surface it explicitly as ``handle=``
        # and append the underlying ``uri`` when present so the model can
        # hand the existing artifact straight to a tool.
        uri = layer.get("uri")
        role_raw = layer.get("role")
        scenario_type = layer_id_scenario_type(layer_id, name)
        # An expensive-simulation output (recognized scenario family) OR a
        # ``role="primary"`` layer is a RESULT; everything else is an INPUT /
        # context layer. RESULT labelling is what stops the re-run. F96: a
        # recognized FETCHED layer (wdpa / landcover / dem / roads / ...) is an
        # INPUT tagged with its KIND so a fit / resize / re-show follow-up
        # reuses it (compute_layer_bounds on its handle) instead of re-fetching
        # a duplicate.
        if scenario_type is not None:
            role_label = f"RESULT[{scenario_type}]"
        elif role_raw == "primary":
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
            "Lines tagged RESULT[...] are finished simulation / analysis OUTPUTS "
            "(e.g. a flood-depth or plume RESULT for this AOI) — the work that "
            "made them is DONE. Lines tagged INPUT (or INPUT[<kind>], e.g. "
            "INPUT[wdpa], INPUT[landcover], INPUT[dem]) are fetched / context "
            "layers ALREADY on the map. "
            "REUSE these (pass their handle/uri DIRECTLY to the next tool) — do "
            "NOT re-run, re-fetch, or recompute them:\n"
            + "\n".join(lines)
            + "\nIf a RESULT already answers the user's request for this AOI and "
            "parameters, narrate from it and pass its handle onward (e.g. an "
            "existing flood-depth RESULT feeds a Pelicun damage assessment "
            "directly). Re-running the expensive simulation that produced an "
            "existing RESULT is FORBIDDEN unless the user changes the area / "
            "parameters or explicitly asks to re-run. Do NOT re-fetch or "
            "recompute a layer already listed here unless it is genuinely absent."
            "\nFETCHED LAYER REUSE (F96 — HARD RULE): a fetched layer "
            "(INPUT[<kind>]) for this AOI is ALREADY on the map. A follow-up to "
            "FIT, ZOOM, RESIZE the box, or 'encompass all the <features>' for "
            "that SAME data (e.g. 'resize the bbox to encompass all protected "
            "areas' when an INPUT[wdpa] layer is already listed) is NOT a fetch — "
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
    """Convert a Case's persisted chat into the ``chat_history`` dict shape (F17).

    On a Case reopen the server resets ``state.chat_history = []`` (the
    job-0245 cross-case clean-slate). Without rehydration the model has no
    memory of prior work and recomputes (e.g. a follow-up hillshade ask in the
    Fort Myers flood Case re-runs the whole flood). This converts the PERSISTED
    PER-CASE messages — the same data that drives the visible chat replay —
    into the lightweight TEXT-turn dict shape ``build_contents_from_history``
    consumes, so the live LLM regains that memory.

    Args:
        chat_messages: ordered ``CaseChatMessage`` list (oldest-first) for THIS
            Case. Each has ``role`` in {user, agent, system, tool}, a ``content``
            string, and (for tool rows) a ``tool_card``. Duck-typed so a dict
            shape also works.
        loaded_layers: the Case's persisted ``loaded_layers`` (used to build the
            layers-present note appended as the LAST history turn).
        cap: bound on the number of REPLAYED rows (tail-kept). Defaults to
            ``REHYDRATE_HISTORY_CAP``.

    Returns:
        ``(history, dropped)`` where ``history`` is the dict list ready for
        ``build_contents_from_history`` (role/text turns; tool rows collapsed
        to a model-side text line; layers-present note appended last as a
        ``model`` turn) and ``dropped`` is how many head rows were elided by
        the cap (for the caller to log).

    Guardrail (job-0245): this function ONLY ever sees ONE Case's persisted
    messages (the caller passes ``session_state.chat_history`` for the opened
    ``case_id``). The persisted store is keyed by Case, so this is inherently
    case-correct and cannot reintroduce the in-memory cross-case leak.
    """
    rows = list(chat_messages or [])
    dropped = 0
    if cap >= 0 and len(rows) > cap:
        dropped = len(rows) - cap
        rows = rows[-cap:]

    history: list[dict] = []
    for msg in rows:
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
                # ``agent`` collapses to ``model`` inside
                # build_contents_from_history; ``system`` has no native Gemini
                # role, so fold it to model-side context text (safer for
                # routing than re-injecting it as a fresh ``user`` instruction).
                history.append({"role": "agent", "text": content})
            continue
        # Unknown role: skip rather than guess.

    note = build_layers_present_note(loaded_layers, case_bbox=case_bbox)
    if note:
        history.append({"role": "model", "text": note})

    return history, dropped


def encode_parts_blob(parts: list[genai_types.Part]) -> bytes:
    """Encode a list of ``Part`` to the ``parts_blob`` wire shape (job-B10).

    The inverse of ``_decode_parts_blob``. Used by callers that want to
    persist full-fidelity Content turns into ``chat_history`` for replay
    through Gemini (preserving function_call/function_response Parts and
    Gemini 3 thought_signature bytes).

    Encoded as a JSON byte string so it round-trips through MongoDB / JSON
    persistence; ``thought_signature`` is base64-encoded since JSON cannot
    carry raw bytes.
    """
    import base64 as _b64
    import json as _json

    out: list[dict[str, Any]] = []
    for part in parts:
        entry: dict[str, Any] = {}
        text = getattr(part, "text", None)
        if text:
            entry["text"] = text
        fc = getattr(part, "function_call", None)
        if fc is not None and getattr(fc, "name", None):
            entry["function_call"] = {
                "name": fc.name,
                "id": getattr(fc, "id", None),
                "args": dict(getattr(fc, "args", None) or {}),
            }
        fr = getattr(part, "function_response", None)
        if fr is not None and getattr(fr, "name", None):
            entry["function_response"] = {
                "name": fr.name,
                "id": getattr(fr, "id", None),
                "response": dict(getattr(fr, "response", None) or {}),
            }
        sig = getattr(part, "thought_signature", None)
        if isinstance(sig, (bytes, bytearray)) and sig:
            entry["thought_signature_b64"] = _b64.b64encode(bytes(sig)).decode("ascii")
        if entry:
            out.append(entry)
    return _json.dumps(out).encode("utf-8")


def _coerce_to_summary_value(value: Any, depth: int = 0) -> Any:
    """Recursive helper for ``summarize_tool_result``.

    Walks the tool-result structure; converts non-JSON-native types to strings,
    truncates long lists and strings, drops nested dicts past depth 2.  The
    goal isn't fidelity — it's giving Gemini enough signal to decide the next
    call without sending it megabytes of GeoJSON.
    """
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
    # Pydantic models / dataclasses / arbitrary objects — repr-coerce, clip.
    s = repr(value)
    if len(s) > 200:
        s = s[:200] + "…"
    return s


def _classify_error(error: BaseException) -> tuple[str, bool]:
    """Derive ``(error_code, retryable)`` for a tool-dispatch exception.

    job-0177: typed tool exceptions across the registry already declare
    ``error_code`` (str) and ``retryable`` (bool) class attributes
    (``WDPAError``, ``HRSLError``, ``MTBSError``, ``MRMSError``,
    ``INatError``, ``IUCNError``, ``FIRMSError``, ``GTSMError``,
    ``LANDFIREError``, ``OSMRoadsError``, ``GBIFError``,
    ``CAMaFloodError``, ``GOESError``, ``CompFireError``,
    ``ColoredReliefError``, ``NIFCError``, ``NWSAlertsError``, etc.).
    Harvest those directly so the function_response the multi-turn loop
    feeds back to Gemini carries the retry signal the tool already knew.

    For untyped exceptions, fall back to a conservative heuristic:

    - ``asyncio.TimeoutError`` / ``TimeoutError``  → retryable
    - ``ConnectionError`` / ``OSError`` (network-ish) → retryable
    - ``ValueError`` / ``TypeError`` / ``KeyError`` / ``AttributeError``
      (programmer / arg shape error) → NOT retryable
    - everything else (``RuntimeError`` and friends) → retryable
      (Gemini reads ``message`` and decides; the cap is
      ``MAX_TURN_ITERATIONS`` either way).

    Never raises — even pathological exceptions yield a stable dict shape
    so the multi-turn loop keeps going.
    """
    # 1. Honour typed-tool exception class attributes when present.
    code_attr = getattr(error, "error_code", None)
    retry_attr = getattr(error, "retryable", None)
    if isinstance(code_attr, str) and code_attr:
        code = code_attr
    else:
        code = type(error).__name__.upper()
    if isinstance(retry_attr, bool):
        return code, retry_attr

    # 2. Heuristic fallback for untyped exceptions.
    import asyncio as _asyncio

    if isinstance(error, (_asyncio.TimeoutError, TimeoutError)):
        return code, True
    if isinstance(error, (ConnectionError, OSError)):
        return code, True
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        return code, False
    # Default: retryable so Gemini gets one more shot (capped by
    # MAX_TURN_ITERATIONS).
    return code, True


def _summarize_chart_emission(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Compact summary for a chart-emission tool result (job-0230).

    The full ``vega_lite_spec`` (with inline data rows) is intentionally
    DROPPED here — it already went to the client on the ``chart-emission`` WS
    envelope. Gemini receives only what it needs to narrate: the chart id, the
    title, the one-line caption (which already carries the key tool-computed
    numbers — e.g. "1,234 structures · 567 damaged"), the chart's mark type,
    and the number of data rows. This keeps the function_response small and
    pushes narration to source the numbers from the caption, not free text
    (Invariant 1 — determinism boundary).
    """
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
    """Extract the threaded failure code from a failed "modeled" envelope dict.

    job-0327 (HONESTY FLOOR). A ``_build_failed_envelope`` exit threads its
    error code into TWO seams so it survives ``_coerce_to_summary_value``'s
    depth>=2 dict-collapse:

    1. (B2, depth 0) ``workflow_name == "<name>:FAILED:<CODE>"`` — a top-level
       string field, always visible in the summary.
    2. (legacy, depth 2) ``flood.metrics.solver_version == "failed:<CODE>"`` —
       and the equivalent ``seismic``/other-hazard ``metrics.solver_version``.

    Prefer the depth-0 ``workflow_name`` seam (it is the one the LLM actually
    sees post-coercion); fall back to the buried ``solver_version`` seam; else
    ``"MODEL_RUN_PRODUCED_NO_LAYERS"``.
    """
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
    """True if a "modeled" envelope carries an explicit failure marker.

    job-0327 R2 (MUST-FIX 1/2a). Two seams mark a deterministically-failed
    composer run, regardless of whether a ``solver_run_id`` was already
    appended before the failure (SOLVER_FAILED/SOLVER_TIMEOUT append at
    model_flood_scenario.py:777 BEFORE failing; POSTPROCESS_FAILED likewise):

    1. (depth 0) ``workflow_name`` contains ``":FAILED:"`` — promoted by
       ``_build_failed_envelope`` and surviving ``_coerce_to_summary_value``.
    2. (depth 2) any hazard payload's ``metrics.solver_version`` starts with
       ``"failed:"`` — the legacy threading seam.
    """
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

    job-0327 R2 (MUST-FIX 2b). On a solve-succeeded-but-publish/render-dropped
    run the LLM gets ``status="error"`` with ``error_code=NO_RENDERABLE_LAYER``
    but the simulation DID produce real numbers — surface them so the agent can
    still narrate the flood honestly ("flooded area X, max depth Y") even though
    the result layer never reached the map. Degrade gracefully: emit only the
    fields that are present; return ``""`` when none are.
    """
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


#: Scenario/simulation composer tool names whose successful return is an
#: ALREADY-PUBLISHED, styled layer on the user's map (job duplicate-flood-layer).
#: Their thin wrapper publishes the postprocess result internally and returns the
#: published LayerURI (uri = the renderable http(s) WMS/tile URL). The LLM must
#: NOT call publish_layer on that handle again — a second publish re-styles the
#: SAME COG with TiTiler's viridis default and paints a duplicate map row. Kept
#: aligned with ``scenario_reuse.EXPENSIVE_SCENARIO_TOOLS`` (the reuse index keys
#: off the same set); a lazy import keeps the two in lockstep without a hard
#: module coupling at import time.
def _published_scenario_tool_names() -> frozenset[str]:
    try:
        from .scenario_reuse import EXPENSIVE_SCENARIO_TOOLS

        names = set(EXPENSIVE_SCENARIO_TOOLS.keys())
    except Exception:  # noqa: BLE001 — never let an import hiccup break summary
        names = set()
    # ``run_model_flood_habitat_scenario`` is a flood composer that is NOT in the
    # reuse index (it produces a habitat-paired flood layer) but ALSO returns an
    # already-published flood LayerURI, so include it explicitly.
    names.add("run_model_flood_habitat_scenario")
    return frozenset(names)


def _layer_uri_is_published(result: Any) -> bool:
    """True when ``result`` duck-types as a LayerURI whose ``uri`` is a renderable
    http(s) WMS/tile URL — i.e. it has ALREADY been published to the map. A raw
    ``gs://`` / ``s3://`` COG handle is storage-only and returns False."""
    if isinstance(result, (dict, str, bytes)) or result is None:
        return False
    uri = getattr(result, "uri", None)
    if not (hasattr(result, "layer_id") and isinstance(uri, str)):
        return False
    return uri.startswith("http://") or uri.startswith("https://")


def _summarize_published_scenario_layer(
    tool_name: str, result: Any
) -> dict[str, Any]:
    """Compact function_response for a scenario wrapper that returned an
    ALREADY-PUBLISHED, styled LayerURI (job duplicate-flood-layer, PRIMARY fix).

    Carries explicit ``published`` / ``on_map`` flags (plus a ``publish_status``
    and a ``wms_url`` alias matching the prompt's escape-clause vocabulary) so the
    LLM reliably recognizes the layer is on the map and does NOT issue a redundant
    publish_layer call on the same handle (which would paint a styleless viridis
    duplicate). The metadata the loop needs to narrate + pass the handle is kept:
    layer_id (the canonical handle), name, layer_type, uri, style_preset, bbox.
    """
    layer_id = getattr(result, "layer_id", None)
    uri = getattr(result, "uri", None)
    bbox = getattr(result, "bbox", None)
    summary: dict[str, Any] = {
        "tool": tool_name,
        "status": "ok",
        "published": True,
        "on_map": True,
        "publish_status": "published",
        # ``wms_url`` alias — the publish-discipline escape clause keys on this
        # field name; the LayerURI's ``uri`` IS the renderable WMS/tile URL here.
        "wms_url": uri,
        "layer_id": layer_id,
        # ``handle`` mirrors the layer_id so the model passes the canonical
        # handle (never a raw gs:// path) into any downstream *_uri param.
        "handle": layer_id,
        "name": getattr(result, "name", None),
        "layer_type": getattr(result, "layer_type", None),
        "uri": uri,
        "style_preset": getattr(result, "style_preset", None),
        "already_published_note": (
            "This scenario layer is ALREADY published, styled, and on the user's "
            "map. Do NOT call publish_layer on it — that would paint a redundant "
            "styleless duplicate. Narrate the result from this layer."
        ),
    }
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        summary["bbox"] = list(bbox)
    return summary


def summarize_tool_result(
    tool_name: str,
    result: Any,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Compact a tool result into the ``function_response.response`` payload.

    Per the kickoff: SUMMARY, not full result.  Gemini reads this between
    turns to decide its next move; it needs LayerURI metadata, key metrics,
    error codes, and counts — not the raw GeoJSON bytes.

    Conventions enforced:

    * Errors (job-0177) become
      ``{"status": "error", "error_code": str, "message": str, "retryable": bool, "error_type": str}``.
      ``error_code`` + ``retryable`` are harvested from the tool's typed
      exception class (FR-AS-11 surface) when present, else derived
      from the exception class name / runtime kind via ``_classify_error``.
      Gemini reads this and either retries with corrected args, calls a
      different tool, or narrates the failure honestly. The
      ``MAX_TURN_ITERATIONS`` cap protects against runaway retry.
      The legacy ``"error"`` field is retained as an alias of ``message``
      so older tests / consumers don't break.
    * ``None`` results (the ``_invoke_tool_via_emitter`` path returns ``None``
      on payload-warning skip, TOOL_NOT_FOUND, etc.) become
      ``{"status": "no_result"}``.
    * Dict results are walked through ``_coerce_to_summary_value`` and then
      JSON-clipped to ``_FUNCTION_RESPONSE_CHAR_BUDGET`` chars.
    * Primitive / string results become ``{"result": value}``.
    * The final dict always carries ``"tool"`` and ``"status"`` keys so the
      LLM has a stable shape to reason over.
    """
    import json as _json

    if error is not None:
        code, retryable = _classify_error(error)
        message = str(error)[:500]
        envelope = {
            "tool": tool_name,
            "status": "error",
            "error_code": code,
            "message": message,
            "retryable": retryable,
            # Legacy alias — preserved so existing tests / callers that
            # read ``error`` continue to work.  ``message`` is the new
            # canonical field; both carry the same string.
            "error": message,
            "error_type": type(error).__name__,
        }
        # Typed no-data / recovery contract (2026-07-13): a tool exception
        # may carry a ``suggestions`` sequence of short recovery options
        # (e.g. ``EarthquakesNoEventsError``: widen window / lower
        # min_magnitude). Surface it as a STRUCTURED list so a small model
        # relays the options to the user instead of inventing a next step
        # (live incident: 0-event fetch -> fabricated publish_layer handle).
        raw_suggestions = getattr(error, "suggestions", None)
        if isinstance(raw_suggestions, (list, tuple)):
            suggestions = [str(s) for s in raw_suggestions if str(s).strip()]
            if suggestions:
                envelope["suggestions"] = suggestions[:8]
        return envelope

    if result is None:
        return {"tool": tool_name, "status": "no_result"}

    # job duplicate-flood-layer (PRIMARY): a scenario/simulation composer
    # (run_model_flood_scenario & friends) returns its peak-depth / plume layer
    # ALREADY published, styled, and on the map (its thin wrapper publishes the
    # postprocess result internally; the returned LayerURI's ``uri`` is the
    # renderable http(s) WMS/tile URL). Without an explicit signal, this LayerURI
    # falls through to the repr-coerce branch below and the LLM, seeing only a
    # raw COG-ish repr, issues a SECOND publish_layer on the handle — TiTiler
    # then re-styles the same COG with its viridis default and a duplicate
    # styleless layer appears on the map. Stamp ``published``/``on_map`` so the
    # publish-discipline escape clause fires and the model narrates instead of
    # re-publishing. Scoped to the scenario tool set AND a genuinely-published
    # (http) LayerURI, so a FAILED scenario (no layer / honesty-floor empty
    # envelope, handled above) and every non-scenario tool are untouched.
    if tool_name in _published_scenario_tool_names() and _layer_uri_is_published(
        result
    ):
        return _summarize_published_scenario_layer(tool_name, result)

    # job-0230 (sprint-13 Stage 2): chart-emission results carry a full
    # Vega-Lite spec with INLINE data rows (up to ~2000). Gemini must narrate
    # from the chart's numbers, not re-read the inline rows — and the spec
    # could blow the char budget. Strip ``vega_lite_spec`` and surface a
    # COMPACT summary (chart_id / title / caption / chart type / data-shape) so
    # the function_response stays small and narration-focused. The FULL spec
    # already went to the client on the ``chart-emission`` WS envelope
    # (server.py ``_maybe_emit_chart``).
    if (
        isinstance(result, dict)
        and result.get("envelope_type") == "chart-emission"
        and isinstance(result.get("vega_lite_spec"), dict)
    ):
        return _summarize_chart_emission(tool_name, result)

    # job-0233 (sprint-13 Stage 2): code_exec_request returns a COMPACT summary
    # (status / result descriptor / stdout tail / truncated / duration) PLUS the
    # full ``code-exec-result`` wire payload under ``_code_exec_result`` (which
    # carries the larger 16-KiB stdout/stderr fields). The full payload already
    # went to the client on the ``code-exec-result`` WS envelope
    # (server.py ``_maybe_emit_code_exec_result``); strip it from the
    # function_response so Gemini narrates from the compact summary + structured
    # ``result``, not the raw logs.
    if isinstance(result, dict) and "_code_exec_result" in result:
        compact = {k: v for k, v in result.items() if k != "_code_exec_result"}
        return {
            "tool": tool_name,
            "status": "ok",
            "result": _coerce_to_summary_value(compact),
        }

    # job-0327 (HONESTY FLOOR): a "modeled" composer result MUST NOT be stamped
    # status="ok" while carrying an EMPTY ``layers`` list — a modeled run with
    # no renderable layer is exactly NATE's "no flood layer but said ok"
    # symptom. This classifier lives at the single chokepoint every tool result
    # passes through and keys off the STRUCTURE of the result (not on whether an
    # exception was raised), so it is root-cause-agnostic.
    #
    # NET GUARANTEE (job-0327 R2): envelope_type=="modeled" AND empty layers ->
    # NEVER status="ok". Two sub-cases:
    #
    #   (a) FAILURE-TAGGED — the depth-0 ``workflow_name`` carries ":FAILED:" OR
    #       any payload's ``metrics.solver_version`` starts with "failed:". This
    #       covers the _build_failed_envelope non-runs (precip-fetcher die,
    #       SFINCS build gate, solver-dispatch failure) AND the dispatched-then-
    #       failed exits (SOLVER_FAILED / SOLVER_TIMEOUT / POSTPROCESS_FAILED)
    #       which append a solver_run_id BEFORE failing — so the R1 "no
    #       solver_run_ids" gate let them slip through as ok. Surface
    #       status="error" with the parsed code, REGARDLESS of solver_run_ids.
    #
    #   (b) NOT FAILURE-TAGGED — the solve COMPLETED (metrics present, no
    #       ":FAILED:" tag) but the result layer was dropped at publish/render
    #       (model_flood_scenario.py:864 AWS publish-drop path). Surface
    #       status="error", error_code="NO_RENDERABLE_LAYER", and INCLUDE the
    #       available flood metrics in the message so the agent can still
    #       narrate the numbers honestly even though nothing reached the map.
    #
    # _coerce_to_summary_value collapses the depth-2 metrics dict to bare key
    # names, so without this detector the LLM received {status:ok, layers:[],
    # metrics:{dict keys=...}} and honestly narrated "done". Do NOT broaden to
    # "observed"/"fetched" tools: those legitimately return non-layer data
    # (scalars, tables, point queries). A modeled envelope WITH a non-empty
    # layers list reads as status="ok" (success path unchanged).
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
                # Legacy alias — same string as ``message`` (matches the raised-
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

    # Final char-budget clip: serialize, if oversized clip and re-wrap.
    try:
        encoded = _json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001 — pathological non-serializable
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


# --------------------------------------------------------------------------- #
# result_usability classifier (tool-accuracy panel — NATE 2026-06-17)
# --------------------------------------------------------------------------- #
#
# ``success`` (did the tool return without raising / without a failure-tagged
# envelope) is NOT the same question as ``was the result USABLE`` — the headline
# bug ([[project-render-chokepoint-and-honesty-floor]]): a layer-producing tool
# can return status="ok" while carrying an EMPTY layers list (a modeled run that
# produced no renderable layer, or a publish/render drop). That reads as a
# SUCCESS in the per-tool count but is NOT a usable result. ``result_usable``
# captures exactly that distinction so the tool-accuracy dashboard can separate
# "the call worked" from "the call produced something the user can use".
#
# Returns:
#   - ``False`` — a layer-producing tool whose result has NO renderable layer
#     (or a modeled envelope with empty layers), EVEN when success=True. This is
#     keyed off the SAME honesty-floor classifier ``summarize_tool_result`` uses
#     (NO_RENDERABLE_LAYER / failure-tagged modeled envelope), so the two stay
#     in lockstep at the single dispatch chokepoint.
#   - ``True`` — a real renderable result (a LayerURI / non-empty layers list /
#     a published WMS layer) OR a non-empty data payload from a layer/data tool.
#   - ``None`` — the notion does not apply (meta / control-plane tools that never
#     produce a layer or a data payload, e.g. confirmation / discovery / cancel
#     helpers; also when the call itself errored — usability is undefined for a
#     call that did not complete).
#
# Conservative by construction: anything we cannot positively classify as a
# layer- or data-producing result returns ``None`` rather than guessing True.

#: Result keys whose presence marks a layer-producing return. A non-empty value
#: under any of these is a renderable artifact (LayerURI dict, gs://"/s3:// COG,
#: WMS URL, or a layers list). Mirrors the *_uri vocabulary the adapter already
#: tracks for handle-passing (see the module-level docstring).
_LAYER_RESULT_KEYS = frozenset(
    {
        "layers",
        "layer_uri",
        "layer",
        "published_layers",
        "wms_url",
        "result_layers",
    }
)


def _result_has_renderable_layer(result: Any) -> bool | None:
    """Return whether ``result`` carries a renderable layer artifact.

    ``True`` — a LayerURI (duck-typed via ``layer_id`` + ``uri``) OR a dict with
    a non-empty layer key (``layers`` list, ``layer_uri`` string, ...).
    ``False`` — a dict that LOOKS like a layer-producer (``envelope_type`` set,
    or a layer key present) but the layer slot is empty.
    ``None`` — the result is not layer-shaped at all (the caller then decides
    whether a data payload makes it usable, or whether usability is N/A).
    """
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

    Reuses the honesty-floor signal already stamped on ``summary`` by
    ``summarize_tool_result`` so the two never diverge: a layer-producing tool
    that summarised to ``status="error"`` with ``error_code="NO_RENDERABLE_LAYER"``
    (or a failure-tagged modeled envelope) is NOT usable, regardless of the
    raised-exception ``success`` flag.

    See the module section comment above for the full True / False / None
    contract. Never raises — a classification failure degrades to ``None``.
    """
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
        #    of "renderable layer" doesn't apply — treat as N/A (meta path).
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
    except Exception:  # noqa: BLE001 — classification must never break dispatch
        return None


def build_function_call_content(
    name: str,
    args: dict[str, Any],
    call_id: str | None = None,
    thought_signature: bytes | None = None,
) -> genai_types.Content:
    """Build the ``model``-role Content wrapping the function_call.

    This is appended to ``contents`` after a dispatch so the next Gemini
    stream sees its own prior tool-call decision.

    job-B10: ``thought_signature`` (when non-None) is attached to the wrapping
    ``Part`` (not the ``FunctionCall`` — google-genai's ``FunctionCall`` has
    no signature field; only ``Part`` does, per types.py line 2044). Gemini 3
    requires the same opaque byte-blob be echoed back on the function_call
    Part for the replayed model turn or generate_content_stream raises
    ``thought-signature mismatch``. For Gemini 2.5 (current default), the
    field is None and the resulting Part carries no signature — a no-op for
    the model. The plumbing is forward-compat.
    """
    fn_call = genai_types.FunctionCall(name=name, args=args or {}, id=call_id)
    part_kwargs: dict[str, Any] = {"function_call": fn_call}
    if thought_signature is not None:
        part_kwargs["thought_signature"] = thought_signature
    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(**part_kwargs)],
    )


def build_function_response_content(
    name: str,
    response: dict[str, Any],
    call_id: str | None = None,
) -> genai_types.Content:
    """Build the ``function``-role Content wrapping the function_response.

    Appended right after the matching ``model`` function_call content so
    Gemini has the (call, response) pair before deciding its next turn.
    """
    fn_resp = genai_types.FunctionResponse(name=name, response=response, id=call_id)
    return genai_types.Content(
        role="user",
        parts=[genai_types.Part(function_response=fn_resp)],
    )


# ---------------------------------------------------------------------------
# stream_events — tool-aware streaming (job-0154, root fix)
# ---------------------------------------------------------------------------

async def stream_events(
    client: genai.Client,
    model: str,
    user_text: str,
    tool_declarations: list[genai_types.FunctionDeclaration] | None = None,
    system_prompt: str | None = None,
    chat_history: list[dict] | None = None,
    cached_content_name: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream Gemini's reply as typed ``StreamEvent`` objects.

    Replaces the text-only ``stream_reply`` path.  When ``tool_declarations``
    is supplied (non-empty list), Gemini receives the full function catalog so
    it can emit ``FunctionCallEvent`` objects instead of prose refusals.

    Each yielded item is either:
    - ``TextDeltaEvent(delta)`` — a streamed text fragment; caller wraps it
      in ``agent-message-chunk``.
    - ``FunctionCallEvent(name, call_id, args)`` — Gemini wants to call a
      tool; caller dispatches through ``_invoke_tool_via_emitter``.

    Cancellation semantics are identical to ``stream_reply``.

    Args:
        client: google-genai ``Client`` built by ``build_client``.
        model: model identifier string (e.g. ``"gemini-2.5-pro"``).
        user_text: the user's message text.
        tool_declarations: optional list of ``FunctionDeclaration`` objects
            built by ``build_tool_declarations``; pass an empty list or
            ``None`` to send no tool catalog (text-only mode).
        system_prompt: optional system instruction string; passed as
            ``GenerateContentConfig.systemInstruction``.
        chat_history: optional list of prior ``{role, text}`` dicts from
            ``SessionState.chat_history``.  Included as prior ``Content``
            turns so Gemini has conversational context.
    """
    contents = build_contents_from_history(user_text, chat_history)
    async for event in stream_events_with_contents(
        client,
        model,
        contents,
        tool_declarations=tool_declarations,
        system_prompt=system_prompt,
        cached_content_name=cached_content_name,
    ):
        yield event


# ---------------------------------------------------------------------------
# stream_events_with_contents — single-turn primitive for the multi-turn loop
# ---------------------------------------------------------------------------


def _coerce_int(v: Any) -> int | None:
    """Return ``v`` as a real ``int``, or ``None`` for anything else.

    Defends against MagicMock-on-attribute coercion in unit tests (whose
    auto-attrs implement ``__int__`` and return 1, silently fabricating
    usage counts) AND against protobuf scalars / pydantic-wrapped ints on
    the wire (the genai SDK occasionally hands these back as objects that
    coerce cleanly via ``int()`` but are not ``isinstance(int)``).
    """
    if v is None:
        return None
    if isinstance(v, bool):
        # bool is a subclass of int; reject (no real usage count is a bool).
        return None
    if isinstance(v, int):
        return v
    # Accept "looks like a real number" — int(str) / int(float) — but NOT a
    # MagicMock (whose __int__ returns 1 unconditionally and would inject
    # phantom counts into the stream).
    try:
        import unittest.mock as _mock
        if isinstance(v, _mock.NonCallableMock):
            return None
    except Exception:  # noqa: BLE001 — defensive; mock import should always work
        pass
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _usage_has_real_counts(usage: Any) -> bool:
    """Return True only if ``usage`` carries at least one real integer count.

    A MagicMock surfaces all attrs as MagicMocks — ``_coerce_int`` rejects
    those, so an all-MagicMock usage object returns False here.  A real
    SDK ``UsageMetadata`` carries at least one int (typically
    ``total_token_count`` is always populated).
    """
    for fname in (
        "total_token_count",
        "cached_content_token_count",
        "prompt_token_count",
        "candidates_token_count",
    ):
        if _coerce_int(getattr(usage, fname, None)) is not None:
            return True
    return False


async def stream_events_with_contents(
    client: genai.Client,
    model: str,
    contents: list[genai_types.Content],
    tool_declarations: list[genai_types.FunctionDeclaration] | None = None,
    system_prompt: str | None = None,
    cached_content_name: str | None = None,
    bedrock_model: str | None = None,
    show_thinking: bool = False,
) -> AsyncIterator[StreamEvent]:
    """Stream one Gemini turn from a fully-built ``contents`` list (job-0169).

    ``show_thinking`` (local build, NATE 2026-07-08): forwarded to the OpenAI
    adapter only. When True the adapter omits the ``/no_think`` system suffix
    (GRACE2_OPENAI_EXTRA_SYSTEM) for this round so the model's reasoning
    channel is generated and surfaced as ``ThinkingDeltaEvent``s. Ignored by
    the Bedrock / Vertex / scripted paths.

    This is the primitive the multi-turn loop driver in ``server.py`` uses.
    Each call corresponds to exactly one ``generate_content_stream`` round —
    the driver appends function_call + function_response Content entries to
    ``contents`` and re-calls this until Gemini emits no further function
    calls (only text → terminal turn).

    ``stream_events`` (the user-text variant) now delegates here after
    building ``contents`` via ``build_contents_from_history``.

    Cancellation: ``asyncio.CancelledError`` cancels the underlying producer
    thread and re-raises.

    Job-B6 (Wave 4.10) — CachedContent integration:
        When ``cached_content_name`` is provided, the request is built
        WITHOUT ``tools[]`` and WITHOUT ``tool_config``. The cache carries
        the full catalog + system instruction; sending either field
        alongside ``cached_content`` is a Vertex 400 (the original
        pre-dispatch blocker). ``system_prompt`` and ``tool_declarations``
        are silently ignored in this path.

        Per-turn allowed-set enforcement happens server-side via
        ``categories.validate_function_call`` (see ``server.py``); the cache
        always carries the FULL catalog.

        A ``UsageMetadataEvent`` is emitted from the final chunk's
        ``usage_metadata`` so the multi-turn driver can verify the cached
        token discount, emit the ``cache-status`` envelope into the
        PipelineEmitter, and pipe ``cached_content_token_count`` into the
        tool-call telemetry record.
    """
    # sprint-14-aws (job-0286): model-provider switch. When MODEL_PROVIDER=bedrock,
    # delegate to the Bedrock Converse adapter -- it converts the genai contents +
    # tool declarations at the boundary and yields the SAME StreamEvent union, so
    # the server.py dispatch loop, validator, emitter, and UI are untouched.
    # cached_content_name is a Gemini-only fast-path and does not apply here.
    from .bedrock_adapter import model_provider, stream_bedrock
    from .scripted_adapter import model_provider_is_scripted, stream_scripted

    # MODEL_PROVIDER=scripted (aliases replay/fake): replay a canned transcript of
    # tool calls with NO model call -- the zero-cost deterministic test/dev
    # sandbox. Intercept BEFORE the bedrock check (model_provider() would not be
    # "bedrock", so it must not fall through to the Vertex client path). Yields
    # the same StreamEvent union, so the dispatch loop is untouched.
    if model_provider_is_scripted():
        async for _ev in stream_scripted(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=bedrock_model,
        ):
            yield _ev
        return

    if model_provider() == "bedrock":
        async for _ev in stream_bedrock(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=bedrock_model,
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
            model=bedrock_model,
            show_thinking=show_thinking,
        ):
            yield _ev
        return

    loop = asyncio.get_running_loop()

    # Build the tool list for the config. SKIPPED when a cache is supplied —
    # the cache carries the catalog and Vertex 400s when both are passed.
    gem_tools: list[genai_types.Tool] | None = None
    if tool_declarations and not cached_content_name:
        gem_tools = [genai_types.Tool(function_declarations=tool_declarations)]

    def _open_stream():
        if cached_content_name:
            # Cached path: NO tools[], NO tool_config, NO system_instruction.
            # All three live in the cache. Sending them alongside
            # ``cached_content`` is a Vertex 400. The temperature / AFC fields
            # are per-request, so they stay.
            cfg = genai_types.GenerateContentConfig(
                temperature=0.7,
                cached_content=cached_content_name,
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            )
        else:
            cfg = genai_types.GenerateContentConfig(
                temperature=0.7,
                system_instruction=system_prompt or None,
                tools=gem_tools or None,
                # Disable automatic function calling — we handle dispatch ourselves.
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ) if gem_tools else None,
            )
        return client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=cfg,
        )

    # Use a typed queue: items are StreamEvent | None (sentinel) | BaseException.
    queue: asyncio.Queue[StreamEvent | None | BaseException] = asyncio.Queue()

    def _producer() -> None:
        try:
            last_usage: Any = None  # last seen ``usage_metadata`` across chunks.
            for chunk in _open_stream():
                # Walk parts: each chunk may carry text OR function_call parts.
                cands = getattr(chunk, "candidates", None) or []
                emitted_something = False
                for cand in cands:
                    content = getattr(cand, "content", None)
                    if content is None:
                        continue
                    parts = getattr(content, "parts", None) or []
                    for part in parts:
                        fn_call = getattr(part, "function_call", None)
                        if fn_call is not None and getattr(fn_call, "name", None):
                            # job-B10: harvest Gemini 3 thought_signature off
                            # the Part level. ``Part.thought_signature`` is the
                            # google-genai SDK field (types.py line 2044) — a
                            # bytes blob the model uses to re-anchor its
                            # reasoning across turns. On Gemini 2.5 the field
                            # is None (the model does not surface signatures);
                            # on Gemini 3 it must be echoed back unchanged on
                            # the function_call Part of the replayed turn or
                            # generate_content_stream fails with a
                            # ``thought-signature mismatch`` error.
                            sig = getattr(part, "thought_signature", None)
                            event = FunctionCallEvent(
                                name=fn_call.name,
                                call_id=getattr(fn_call, "id", None),
                                args=dict(fn_call.args or {}),
                                thought_signature=sig if isinstance(sig, (bytes, bytearray)) else None,
                            )
                            loop.call_soon_threadsafe(queue.put_nowait, event)
                            emitted_something = True
                        else:
                            text = getattr(part, "text", None)
                            if text:
                                loop.call_soon_threadsafe(
                                    queue.put_nowait, TextDeltaEvent(delta=text)
                                )
                                emitted_something = True
                # Fallback: some SDK versions expose chunk.text directly.
                if not emitted_something:
                    delta = getattr(chunk, "text", None)
                    if delta:
                        loop.call_soon_threadsafe(
                            queue.put_nowait, TextDeltaEvent(delta=delta)
                        )
                # Job-B6: harvest usage_metadata as it appears. Gemini surfaces
                # aggregate counts only on the terminal response chunk; we
                # capture every non-None value so a fallback path still works
                # if the SDK changes which chunk carries usage. We require at
                # least one bona-fide int field on the metadata object — this
                # avoids spurious UsageMetadataEvent emission from MagicMocks
                # in unit tests (whose auto-attrs coerce to 1 via __int__) and
                # from SDK chunks that carry a usage object with all-None
                # fields.
                usage = getattr(chunk, "usage_metadata", None)
                if usage is not None and _usage_has_real_counts(usage):
                    last_usage = usage
            # Once the stream completes, emit a single UsageMetadataEvent so
            # the caller can stash cached_content_token_count for telemetry +
            # the cache-status envelope.
            if last_usage is not None:
                cached_tokens = _coerce_int(
                    getattr(last_usage, "cached_content_token_count", None)
                )
                total_tokens = _coerce_int(
                    getattr(last_usage, "total_token_count", None)
                )
                prompt_tokens = _coerce_int(
                    getattr(last_usage, "prompt_token_count", None)
                )
                cand_tokens = _coerce_int(
                    getattr(last_usage, "candidates_token_count", None)
                )
                ev = UsageMetadataEvent(
                    cached_content_token_count=cached_tokens,
                    total_token_count=total_tokens,
                    prompt_token_count=prompt_tokens,
                    candidates_token_count=cand_tokens,
                    cache_hit=bool(cached_tokens and cached_tokens > 0),
                )
                loop.call_soon_threadsafe(queue.put_nowait, ev)
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except BaseException as exc:  # noqa: BLE001 — surface any error to caller
            loop.call_soon_threadsafe(queue.put_nowait, exc)

    producer_task = loop.run_in_executor(None, _producer)

    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    except asyncio.CancelledError:
        producer_task.cancel()
        raise


# ---------------------------------------------------------------------------
# stream_reply — text-only shim (kept for backward-compat; delegates to
# stream_events without tool declarations)
# ---------------------------------------------------------------------------

async def stream_reply(
    client: genai.Client, model: str, user_text: str
) -> AsyncIterator[str]:
    """Stream Gemini's reply as a sequence of delta strings (text-only).

    Retained for callers that only want text.  Internally delegates to
    ``stream_events`` with no tool declarations.

    Cancellation: ``asyncio.CancelledError`` is the cancel path.
    """
    async for event in stream_events(client, model, user_text):
        if isinstance(event, TextDeltaEvent):
            yield event.delta


__all__ = [
    "GEMINI_DEFAULT_MODEL",
    "MAX_TURN_ITERATIONS",
    "GeminiSettings",
    "StreamEvent",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "FunctionCallEvent",
    "UsageMetadataEvent",
    "CompactionStartEvent",
    "CompactionCompleteEvent",
    "SYSTEM_PROMPT",
    "build_client",
    "build_contents_from_history",
    "build_layers_present_note",
    "build_function_call_content",
    "build_function_response_content",
    "build_tool_declarations",
    "encode_parts_blob",
    "load_settings",
    "stream_events",
    "stream_events_with_contents",
    "stream_reply",
    "summarize_tool_result",
    "classify_result_usable",
    # B11 schema-normalisation helpers (exported for audit / test use)
    "_is_tuple_annotation",
    "_normalize_callable_for_gemini",
    "_simplify_annotation",
]
