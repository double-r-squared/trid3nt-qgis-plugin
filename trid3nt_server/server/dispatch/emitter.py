"""Tool execution through the pipeline emitter: sync-offload safety + the invoke driver."""

from __future__ import annotations

import asyncio
import os
import logging
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.tool_arg_normalizer import autofill_missing_bbox, normalize_args
from trid3nt_server.emission.pipeline_emitter import PipelineEmitter, bind_turn_case, bind_turn_drawn_geometry
from trid3nt_server.emission.uri_registry import activate_registry, deactivate_registry, get_uri_registry
# The gate engine (trid3nt_server.gates.confirm) is imported function-locally in
# _invoke_tool_via_emitter -- deferred to break the server<->gates load cycle.
from trid3nt_server.gates.tool_gating import BenchBlockedError
from trid3nt_server.scenario_reuse import fetched_kind_for_tool, find_reusable_fetched_layer, get_scenario_index, scenario_signature, scenario_type_for_tool
from trid3nt_server.server.config import _env_flag
from trid3nt_server.server.dispatch.aoi import _maybe_default_fetch_bbox_to_pinned_aoi, _maybe_default_solver_bbox_to_pinned_aoi, _pin_case_aoi_from_solve, _pin_case_aoi_from_tool_bbox, _scenario_produces_domain
from trid3nt_server.server.dispatch.persist import _VALID_ERROR_CODES, _persist_chart_record, _persist_chat_turn, _persist_tool_card
from trid3nt_server.server.dispatch.results import _run_to_completion_shielded
from trid3nt_server.server.dispatch.reuse import _ReuseEntry
from trid3nt_server.server.errors import CodeExecConfirmationCancelledError, PayloadWarningCancelledError, SolverConfirmationCancelledError, ToolNotFoundError
from trid3nt_server.server.session.case_state import _persist_case_layer_handles, _persist_case_loaded_layers, _turn_case_bbox, _turn_case_id
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _is_finite_bbox4, _last_zoom_to_bbox
from trid3nt_server.server.turn.wire import _emit_turn_complete, _send_error
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

def _ensure_emitter(websocket: ServerConnection, state: SessionState) -> None:
    """Bind a ``PipelineEmitter`` to this session if one isn't already.

    The emitter's sink is the WebSocket ``send`` -- every transition method
    writes one envelope on the wire (replace-not-reconcile)."""
    if state.emitter is not None:
        return

    async def _sink(text: str) -> None:
        # The WS may be mid-close when a terminal pipeline-state frame
        # (mark_cancelled / mark_failed) is emitted on the cancel path --
        # ``websocket.send`` then raises ConnectionClosed straight out of the
        # emitter, swallowing the terminal frame AND letting the exception
        # escape the cancel chain. Best-effort: swallow send failures so the
        # card-state transition is always recorded server-side and the
        # CancelledError propagates cleanly for any clients still attached.
        try:
            await websocket.send(text)
        except Exception:  # noqa: BLE001 -- socket may be closing on cancel/fail
            logger.debug(
                "emitter sink: websocket.send failed (socket closing?); "
                "frame dropped best-effort (session=%s)",
                state.session_id,
            )

    async def _chart_persist(payload: dict) -> None:
        # task-198: composer-side chart persistence goes through the SAME
        # _persist_chart_record the tool-result chart path uses, so a
        # composer-emitted chart replays on Case rehydration exactly like a
        # generate_chart chart. Best-effort inside _persist_chart_record.
        await _persist_chart_record(state, payload)

    async def _tool_card_persist(**kwargs: Any) -> None:
        # A terminal SIM compute card persists through the same
        # ``_persist_tool_card`` used by on-box atomic tool cards, so it
        # replays on a WS reconnect / Case reopen. Case is pinned via the
        # live turn context so a cancel-and-redispatch race cannot re-aim
        # the write. Best-effort.
        await _persist_tool_card(state, **kwargs)

    state.emitter = PipelineEmitter(
        session_id=state.session_id,
        sink=_sink,
        chat_history=state.chat_history,
        chart_persist=_chart_persist,
        tool_card_persist=_tool_card_persist,
    )

# Arg keys whose VALUES are credentials/secrets and must NEVER appear in an
# emitted envelope. The early input-only tool-io frame snapshots the ORIGINAL
# call args, which on the dev/test resolution path can carry a raw key (see
# test_credential_request_envelope_never_carries_raw_key). Mirrors + extends the
# secret keys the credential pipeline strips at ``_inject_secret_ref`` (~4586).
_SECRET_ARG_KEYS: frozenset[str] = frozenset({
    "secret_ref", "map_key", "api_key", "apikey", "token", "access_token",
    "password", "passwd", "secret", "secret_key", "access_key", "private_key",
    "credentials", "credential", "auth", "authorization",
})

def _redact_secret_args(args: Any) -> Any:
    """Copy ``args`` with any secret-bearing VALUE masked (key kept visible).

    Defense-in-depth for the early input-only tool-io frame: the visible input
    (bbox, place, …) is preserved so the card shows the real request, but a raw
    credential value is never echoed into a wire/persisted envelope.
    """
    if not isinstance(args, dict):
        return args
    return {
        k: ("***redacted***" if str(k).lower() in _SECRET_ARG_KEYS else v)
        for k, v in args.items()
    }

def _running_emitter_step_id(emitter: Any, tool_name: str) -> str | None:
    """Return the step_id of the emitter's CURRENTLY-running step for ``tool_name``.

    FIX B (early input-only tool-io frame): ``emit_tool_call`` mints the card's
    step INSIDE itself (``add_step`` + ``mark_running``) and only publishes the
    id on ``last_tool_step`` at the TERMINAL transition. To emit an early
    input-only ``tool-io`` frame at dispatch START -- so the client shows the
    input args immediately + a "Running…" output placeholder before the tool
    body returns -- we need the in-flight step's id from INSIDE the invoke
    callable (which runs after ``mark_running``). We derive it the SAME way
    ``PipelineEmitter.update_current_progress`` does: the most-recently-added
    step still in ``running`` state. Best-effort + defensive: any missing
    pipeline internals (or no running step) returns ``None`` so the caller skips
    the early emit -- it is a UX nicety, never a correctness gate. We also guard
    on ``tool_name`` so a stale running step from a sibling dispatch never
    mis-keys the frame.
    """
    if emitter is None:
        return None
    try:
        order = emitter._step_order  # type: ignore[attr-defined]
        steps = emitter._steps  # type: ignore[attr-defined]
        for step_id in reversed(order):
            s = steps.get(step_id)
            if s is not None and getattr(s, "state", None) == "running":
                if getattr(s, "tool_name", None) != tool_name:
                    return None
                return step_id
    except Exception:  # noqa: BLE001 -- never break the dispatch on an emit nicety
        return None
    return None

# ---------------------------------------------------------------------------
# #6 STAGED SYNC-TOOL DISPATCH OFF-LOAD (loop-safety, ships DARK)
# ---------------------------------------------------------------------------
# Every synchronous atomic tool currently runs its WHOLE body on the agent
# asyncio event loop inside ``_invoke_with_unique_layer_id`` below (the
# ``out = entry.fn(**params)`` branch). A slow sync tool (boto3 / requests /
# heavy GDAL/numpy compute) therefore stalls the WS keepalive past the pong
# deadline -> client reconnect-cycle (layer flicker) or WS death. See
# feedback_no_sync_blocking_on_asyncio_loop. The fix is to off-load the sync
# tool body to a worker thread via ``asyncio.to_thread``. This is SAFE because
# tool bodies are EMIT-FREE: all loop-bound PipelineEmitter use (``emit_*`` /
# ``add_loaded_layer`` / ``update_progress``) lives in the SURROUNDING
# ``emit_tool_call`` wrapper + ``_restamp`` + early-input-frame machinery, which
# stay on the loop; only the pure ``entry.fn(**params)`` call moves to the
# thread. ``asyncio.to_thread`` propagates the contextvars Context, so a stray
# emit WOULD still resolve the ContextVar -- hence the armed-only
# ``_assert_sync_offload_safe`` startup guard below refuses to arm if any
# candidate sync tool's source even references the emitter API.
#
# Rolled out in STAGES via the ``TRID3NT_SYNC_TOOL_OFFLOAD`` env var (NO code
# change between stages):
#   ""/"off"  (DEFAULT, Stage 0)  -> disabled; sync tools stay on the loop.
#   "subset"  (Stage 1)           -> off-load only the pure compute_*/clip_*
#                                    family (smallest provably emit-free set),
#                                    live-verify, then advance.
#   "global"/"all"/"on" (Stage 2) -> off-load every sync tool body.
# Stage 3 (bake "global" as the in-code default) is a later commit once global
# mode is live-proven.
_SYNC_OFFLOAD_MODE = os.environ.get("TRID3NT_SYNC_TOOL_OFFLOAD", "off").strip().lower()

_SYNC_OFFLOAD_GLOBAL_VALUES = frozenset({"global", "all", "on", "1", "true", "yes"})

#: Stage-1 subset: the hand-audited pure-compute / pure-clip tool families that
#: take no emitter and do CPU-bound GDAL/numpy work -- the safest first cohort.
_SYNC_OFFLOAD_SUBSET_PREFIXES = ("compute_", "clip_")

#: ALWAYS off-load (regardless of TRID3NT_SYNC_TOOL_OFFLOAD mode). A hand-audited,
#: TIGHT set of PROVEN-PATHOLOGICAL sync tools whose bodies do multi-second
#: synchronous work (rasterio.merge / reproject / WarpedVRT / COG materialize, or
#: large network download + xarray/netCDF compute) ON the asyncio loop, stalling
#: the 12s WS data-heartbeat past the browser's reconnect deadline (code 1005)
#: BEFORE any solve dispatches. See feedback_no_sync_blocking_on_asyncio_loop.
#: Each entry was confirmed EMIT-FREE (its registered fn source does not reference
#: the loop-bound emitter API per _source_references_emitter) and the startup
#: guard _assert_sync_offload_safe re-validates that invariant for this set even
#: when the env mode is "off" (so a future emitting tool can never be silently
#: added here). This is NOT "off-load everything": ~8 light vector/scalar fetchers
#: (fetch_buildings, fetch_river_geometry, lookup_precip_return_period,
#: fetch_landfire_fuels, fetch_usfs_canopy_fuels, fetch_mtbs_burn_severity,
#: show_nexrad_radar, fetch_field_boundaries) and all non-fetch sync tools
#: stay on the loop. Justification per tool:
#:   fetch_topobathy        -> CUDEM+3DEP tile merge + reproject + 189 MB COG (~61 s; ROOT-CAUSE of the 1005 turn-death)
#:   fetch_dem              -> py3dep 3DEP tile mosaic + COG materialize
#:   fetch_3dep_extra       -> pfdf TNM DEM tile mosaic + COG materialize
#:   fetch_landcover        -> NLCD/ESA window clip + COG translate (rasterio + GDAL CLI)
#:   extract_landcover_class-> windowed read of source COG + tiled LZW GeoTIFF write
#:   fetch_population       -> WorldPop ~50 MB stream + windowed rasterio read + COG write
#:   fetch_hrsl_population  -> /vsicurl/ VRT windowed read + COG write
#:   fetch_gcn250_curve_numbers -> /vsicurl/ ~640 MB COG windowed read + tiled GeoTIFF write
#:   fetch_statsgo_soils    -> STATSGO COG-tile mosaic + COG materialize
#:   fetch_era5_reanalysis  -> blocking cdsapi retrieve + xarray open + compute + COG write
#:   fetch_gridmet          -> OPeNDAP xarray open + time-mean compute + COG write
#:   fetch_hrrr_forecast    -> xr.open_zarr + merge + rio.reproject + compute + COG write
#:   fetch_hrrr_smoke       -> xr.open_zarr + merge + rio.reproject + compute + COG write
#:   fetch_mrms_qpe         -> S3 grib2 download + rasterio GRIB read + warp.reproject + GeoTIFF write
#:   fetch_goes_satellite   -> ~50 MB netCDF stream + warp.reproject + COG write
#:   fetch_gtsm_tide_surge  -> blocking CDS ZIP download + xr.open_mfdataset + per-gauge compute
_ALWAYS_OFFLOAD_SYNC_TOOLS = frozenset(
    {
        # first call builds the ~6.7k-doc dense index synchronously
        # (sentence-transformers encode) - must never run on the WS loop
        "search_living_atlas",
        "fetch_living_atlas_layer",
        "fetch_topobathy",
        "fetch_dem",
        "fetch_3dep_extra",
        "fetch_landcover",
        "extract_landcover_class",
        "fetch_population",
        "fetch_hrsl_population",
        "fetch_gcn250_curve_numbers",
        "fetch_statsgo_soils",
        "fetch_era5_reanalysis",
        "fetch_gridmet",
        "fetch_hrrr_forecast",
        "fetch_hrrr_smoke",
        "fetch_mrms_qpe",
        "fetch_goes_satellite",
        # fire-animation demos S3/J3: the per-frame SLIDER stitch + reproject +
        # COG-write loop is heavy multi-second sync work (one frame chain per
        # timestamp); off-load so it never stalls the WS heartbeat. The bodies
        # are emit-free (the surrounding emit_tool_call wrapper does the emit).
        # fetch_goes_blend_animation is heavier still (two product fetches + a
        # per-frame RGB blend per timestamp) -- same off-load rationale.
        "fetch_goes_animation",
        "fetch_goes_blend_animation",
        "fetch_viirs_day_fire",
        # satellite-animation loop-block: both of these read the
        # RAW noaa-goesNN MCMIPC S3 archive and loop over UP TO 144 frames in ONE
        # sync call, each frame = a ~54 MB netCDF download + rasterio reproject +
        # COG write (logged as "fetch_goes_satellite: downloaded ~54MB" +
        # "fetch_goes_archive_animation" cache writes every ~2-3 s, sequentially,
        # for 78+ frames). When the LLM calls either DIRECTLY (the "historical
        # fire animation" / "active fire over the past hours" path -- no composer
        # in between to to_thread it), the whole multi-frame loop ran ON the
        # asyncio loop and starved the 12 s WS data-heartbeat past the browser
        # reconnect deadline -> the agent health endpoint timed out + clients hung
        # in a "connecting..." reconnect loop for the entire build. Off-load so
        # the per-frame loop runs in a worker thread and the loop/heartbeat stay
        # live. Bodies are emit-free (the surrounding emit_tool_call wrapper does
        # the emit). fetch_goes_active_fire reuses the same per-frame archive
        # download + reproject core (_fetch_archive_frame_cog_bytes).
        "fetch_goes_archive_animation",
        "fetch_goes_active_fire",
        "fetch_gtsm_tide_surge",
        # conservation reference scenario: PC STAC raster fetchers that do
        # multi-second sync work (SAS sign + windowed /vsicurl warp-read +
        # COG-write). Bodies are emit-free (the surrounding emit_tool_call
        # wrapper does the emit), so off-load so they never stall the WS
        # heartbeat (feedback_no_sync_blocking_on_asyncio_loop).
        "compute_ndvi",
        "fetch_naip",
        "fetch_mobi",
        # fetch_glm_lightning (GOES GLM optical-lightning): heavy SYNC fetcher
        # now LIVE on the box (multi-granule netCDF download + per-granule
        # in-AOI group filter + raster/COG write). Emit-free body (the
        # surrounding emit_tool_call wrapper does the emit), so off-load so it
        # never blocks the asyncio loop / starves the WS heartbeat
        # (feedback_no_sync_blocking_on_asyncio_loop). Escalated by the
        # tools-session (tool-retrieval kickoff #6).
        "fetch_glm_lightning",
        # Record fetchers: heavy SYNC I/O in the record hook. fetch_aorc_precip
        # opens a public AORC Zarr year store (anonymous s3fs) and streams the windowed
        # AOI-mean over multi-second network reads; fetch_lter_records downloads and parses
        # a multi-MB EDI data entity through the DataONE mirror. Emit-free bodies (the
        # surrounding emit_tool_call wrapper emits), so off-load so they never stall the WS
        # heartbeat (feedback_no_sync_blocking_on_asyncio_loop).
        "fetch_aorc_precip",
        "fetch_lter_records",
        # sandbox-staging: code_exec_request now PRE-FETCHES each layer_ref URI
        # (single OR a list of animation frames) from S3 into the per-run sandbox
        # workdir before the jailed executor opens them as local files, then runs
        # the executor subprocess synchronously -- multi-second sync network +
        # subprocess work. Off-load so it never stalls the WS heartbeat
        # (feedback_no_sync_blocking_on_asyncio_loop). The body is emit-free (the
        # confirm card is emitted on the loop by _gate_on_code_exec; server.py
        # emits the result envelope), so the off-load is safe.
        "code_exec_request",
        # list_run_frames reads the run's outputs.json from S3 (with a legacy
        # publish_manifest fallback) -- sync network I/O. Emit-free (returns the
        # listing dict), so off-load it for the same reason.
        "list_run_frames",
        # These heavy raster/vector
        # fetchers do multi-second sync work (STAC sign + windowed /vsicurl warp
        # read + COG/FlatGeobuf write), the SAME shape as compute_ndvi/fetch_naip
        # above. Their bodies are emit-free (the emit_tool_call wrapper emits), so
        # off-load them so they never stall the WS heartbeat
        # (feedback_no_sync_blocking_on_asyncio_loop). digitize_water_body was
        # flagged heavy by its building agent (Sentinel-2 NDWI raster + vectorize).
        # _assert_sync_offload_safe still gates each at arm time.
        "digitize_water_body",
        "fetch_sentinel2_truecolor",
        "fetch_sentinel1_sar",
        "fetch_landsat_imagery",
        "fetch_modis_lst",
        "fetch_copernicus_dem",
        "fetch_chirps_precipitation",
        "fetch_ghsl_population",
        "fetch_jrc_global_surface_water",
        "fetch_soilgrids",
        "fetch_esri_landcover_10m",
        "fetch_noaa_sst",
        # compute_change_detection reads TWO
        # Sentinel-2 scenes (SAS sign + windowed /vsicurl warp-read per band)
        # + vectorizes + writes an FGB in ONE sync call -- the same shape as
        # compute_ndvi/digitize_water_body above. Emit-free body (the
        # emit_tool_call wrapper does the emit), so off-load so it never
        # stalls the WS heartbeat (feedback_no_sync_blocking_on_asyncio_loop).
        "compute_change_detection",
        # compute_flood_depth_damage stages an s3 depth COG + fetches the NSI
        # inventory + samples + writes an FGB in one sync call -- same off-load
        # rationale; emit-free body.
        "compute_flood_depth_damage",
        # compute_model_residuals stages an s3 model COG + (optionally) fetches
        # USGS groundwater observations over HTTP + bilinear-samples + writes
        # an FGB in one sync call -- same off-load rationale; emit-free body.
        "compute_model_residuals",
    }
)

#: Loop-bound emitter API names. A sync tool whose CODE (comments + string /
#: docstring literals EXCLUDED) references any of these -- or any ``emit_*``
#: attribute -- is NOT safe to off-load (it would touch the loop from a worker
#: thread); ``_assert_sync_offload_safe`` refuses to arm in that case.
_EMITTER_API_NAMES = frozenset(
    {
        "current_emitter",
        "add_loaded_layer",
        "update_progress",
        "start_pipeline",
        "reinline_vector_layers",
    }
)

def _source_references_emitter(src: str) -> bool:
    """True if ``src`` (a tool's source) contains a real CODE reference to the
    loop-bound emitter API.

    Comments and string/docstring literals are ignored (tokenize drops them)
    so a mention in a comment is NOT a false positive -- only an actual
    identifier in code counts. (publish_layer and fetch_river_geometry both
    only MENTION add_loaded_layer in docstrings; their bodies are emit-free,
    the surrounding emit_tool_call wrapper does the emit.)
    """
    import io
    import textwrap
    import tokenize

    try:
        tokens = tokenize.generate_tokens(
            io.StringIO(textwrap.dedent(src)).readline
        )
        for tok in tokens:
            if tok.type != tokenize.NAME:
                continue
            name = tok.string
            if name in _EMITTER_API_NAMES or name.startswith("emit_"):
                return True
        return False
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Un-tokenizable (odd indent/decorator/partial): be CONSERVATIVE -- fall
        # back to a line scan that skips obvious comment lines and flag on any
        # surviving emitter token (better to refuse-arm than silently break).
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if (
                "current_emitter" in line
                or "add_loaded_layer" in line
                or "emit_" in line
            ):
                return True
        return False

def _should_offload_sync_tool(tool_name: str) -> bool:
    """Return True when ``tool_name``'s sync body should run via
    ``asyncio.to_thread``.

    The hand-audited, proven-pathological ``_ALWAYS_OFFLOAD_SYNC_TOOLS`` set is
    off-loaded UNCONDITIONALLY (even when TRID3NT_SYNC_TOOL_OFFLOAD=off) -- these
    tools do multi-second sync raster/COG/download work that stalls the WS
    heartbeat (see feedback_no_sync_blocking_on_asyncio_loop). On top of that the
    env-driven staged mode applies: ``off`` (the dark default) and any unknown
    value -> False for everything else."""
    if tool_name in _ALWAYS_OFFLOAD_SYNC_TOOLS:
        return True
    mode = _SYNC_OFFLOAD_MODE
    if mode in _SYNC_OFFLOAD_GLOBAL_VALUES:
        return True
    if mode == "subset":
        return tool_name.startswith(_SYNC_OFFLOAD_SUBSET_PREFIXES)
    return False

def _assert_sync_offload_safe() -> None:
    """ARMED-ONLY startup safety gate for the #6 sync-tool off-load.

    Dark default (mode ``off``) WITH an empty always-set returns immediately and
    pays nothing. When the off-load is ARMED (``subset``/``global``) OR the
    in-code ``_ALWAYS_OFFLOAD_SYNC_TOOLS`` set is non-empty (which off-loads even
    in ``off`` mode), scan the SOURCE of every candidate sync tool that
    ``_should_offload_sync_tool`` would off-load and RAISE if any one references
    the loop-bound emitter API -- off-loading such a tool would let a worker
    thread touch the event loop. This enforces the headline #6 invariant ("sync
    tool bodies are emit-free") at startup, so a future emitting sync tool can
    never be silently off-loaded (including via the always-set). The cost (an
    ``inspect.getsource`` sweep) is paid once, only when something will off-load.
    """
    armed = (
        _SYNC_OFFLOAD_MODE in _SYNC_OFFLOAD_GLOBAL_VALUES
        or _SYNC_OFFLOAD_MODE == "subset"
    )
    # The always-offload set off-loads regardless of the env mode, so its
    # emit-free invariant must be validated even when the env mode is "off".
    if not armed and not _ALWAYS_OFFLOAD_SYNC_TOOLS:
        logger.info(
            "sync-tool off-load DISABLED (TRID3NT_SYNC_TOOL_OFFLOAD=%r)",
            _SYNC_OFFLOAD_MODE,
        )
        return
    import inspect  # local: only imported when the off-load is armed

    offenders: list[str] = []
    uninspectable: list[str] = []
    n_candidates = 0
    for name, reg in TOOL_REGISTRY.items():
        fn = getattr(reg, "fn", None)
        if fn is None or asyncio.iscoroutinefunction(fn):
            continue
        if not _should_offload_sync_tool(name):
            continue
        n_candidates += 1
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            uninspectable.append(name)
            continue
        if _source_references_emitter(src):
            offenders.append(name)
    if offenders:
        raise RuntimeError(
            "TRID3NT_SYNC_TOOL_OFFLOAD is armed (mode=%r) but these sync tools "
            "reference the loop-bound emitter API and are UNSAFE to off-load: "
            "%s. Refusing to start. (See "
            "feedback_no_sync_blocking_on_asyncio_loop.)"
            % (_SYNC_OFFLOAD_MODE, ", ".join(sorted(offenders)))
        )
    if uninspectable:
        logger.warning(
            "sync-tool off-load armed (mode=%r): %d candidate tool(s) could not "
            "be source-inspected for the emit-free check: %s",
            _SYNC_OFFLOAD_MODE,
            len(uninspectable),
            ", ".join(sorted(uninspectable)),
        )
    logger.info(
        "sync-tool off-load ARMED (mode=%r): %d candidate sync tool(s) "
        "verified emit-free",
        _SYNC_OFFLOAD_MODE,
        n_candidates,
    )

async def _invoke_tool_via_emitter(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
) -> Any:
    """Tool-call site: every ``TOOL_REGISTRY[name].fn(...)`` invocation goes
    through this wrapper so that:

    - the per-session ``PipelineEmitter`` auto-creates a step,
    - emits ``pipeline-state`` on every state transition (replace-not-reconcile),
    - re-emits ``session-state`` whenever the tool returns a ``LayerURI``,
    - propagates ``asyncio.CancelledError`` (Invariant 8) and classifies
      arbitrary exceptions into the open-set A.6 error-code registry.

    Solver dispatch keeps the same shape, yielding ``progress_percent``
    updates through ``emitter.update_progress`` between solver chunks.
    """
    from trid3nt_server.gates.confirm import (
        _gate_on_code_exec,
        _gate_spec_for,
        _gate_with_turn_memory,
        _inject_secret_ref,
        _maybe_gate_on_payload_warning,
        _maybe_handle_credential_error,
    )

    _ensure_emitter(websocket, state)
    if tool_name not in TOOL_REGISTRY:
        # Raises ToolNotFoundError so the existing exception handler routes
        # through summarize_tool_result(error=...), which emits the full
        # structured envelope (error_code + retryable + message) so the model
        # can distinguish "tool ran and returned nothing" from "tool name was
        # never registered". function_response IS the signal the model reads
        # between turns -- the _send_error side-channel is not needed here.
        raise ToolNotFoundError(tool_name, list(TOOL_REGISTRY))
    entry = TOOL_REGISTRY[tool_name]

    # BENCH PRE-DISPATCH BLOCK HOOK. Armed ONLY by the bench harness via
    # session-config (``state.bench_block_config``); ``None`` (the common
    # path) is a single is-not-None check with ZERO overhead. When armed,
    # decides the tool's fate BEFORE any gate/fetch runs:
    #   * wrong_pick      -- a non-member pick: block outright (no arg work).
    #   * correct_blocked -- a member pick in the block tier: run the SAME
    #       arg normalizer a real dispatch would, then block, so the block is
    #       graded on the canonicalized args, but the fn never runs.
    # Both raise ``BenchBlockedError`` THROUGH ``emit_tool_call`` so the tool
    # still surfaces as a (failed) pipeline step while ``entry.fn`` is never
    # reached -- airtight before any fetch.
    if state.bench_block_config is not None:
        from trid3nt_server.gates.tool_gating import BenchBlockedError, bench_block_decision

        _bench_class = bench_block_decision(state.bench_block_config, tool_name)
        if _bench_class is not None:
            if _bench_class == "correct_blocked":
                # Arg validation before the block (the fn is still NOT invoked).
                normalize_args(tool_name, params, entry.fn)

            async def _bench_blocked_invoke() -> Any:
                raise BenchBlockedError(_bench_class, tool_name)

            # Mint the pipeline step (tool shows as 'fired'), then fail it via
            # the raise -- which propagates out to the dispatch loop's typed-
            # error path exactly like any tool exception.
            return await state.emitter.emit_tool_call(
                name=entry.metadata.name,
                tool_name=tool_name,
                invoke=_bench_blocked_invoke,
            )

    # FIX B (#7 early input-only frame): snapshot the ORIGINAL call args NOW,
    # before the normalize_args / gating / URI-resolve / secret-inject pipeline
    # below rewrites ``params`` (normalize_args empties args that don't match the
    # fn signature; secret-inject/URI-resolve add resolved values we must NOT
    # surface). The early frame's ``raw_args`` must equal the LIVE completion
    # frame's ``raw_args=call.args`` (server.py ~2087) so the tool card shows the
    # SAME input the LLM sent, both live and at completion.
    _original_tool_args = dict(params)

    # Bind this dispatch to the turn's Case ONCE, up front. The
    # .qgs routing, tool-card persist, and layer attribution below all use
    # this capture -- a mid-dispatch ``case-command(select)`` must not re-aim
    # them at the newly visible Case (verified contamination).
    turn_case_id = _turn_case_id(state)

    # Drop ``case_id`` for tools that don't declare it -- defense in depth.
    # No registered tool declares one: the Case scoping a publish needs travels
    # to the emission seam through ``current_turn_case()``, not through params.
    if "case_id" in params:
        params = {k: v for k, v in params.items() if k != "case_id"}

    # Payload-warning gate. When the tool declares a
    # ``payload_mb_estimator_name`` and the estimate exceeds the warning
    # threshold, emit ``tool-payload-warning`` and await
    # ``tool-payload-confirmation``. Skip / revise dispatch per the user's
    # decision. No-op when the tool didn't declare an estimator.
    should_dispatch, params = await _maybe_gate_on_payload_warning(
        websocket, state, tool_name, params
    )
    if not should_dispatch:
        # Raises PayloadWarningCancelledError so the model sees a structured
        # envelope ({status: "error", error_code:
        # "PAYLOAD_WARNING_CANCELLED", retryable: False}) instead of
        # {"status": "no_result"}, which it cannot interpret. retryable=False
        # because the user explicitly cancelled.
        raise PayloadWarningCancelledError(tool_name)

    # code_exec_request confirm gate: running arbitrary Python is a
    # consequential action -- the user MUST approve the exact code first. The
    # gate emits a ``code-exec-request`` card, blocks on the SAME
    # ``pending_payload_warnings`` future seam (code_exec_id == warning_id),
    # and on approval injects ``confirmed=True`` + the minted ``code_exec_id``
    # into params so the tool body dispatches the sandbox. A direct
    # programmatic call that already carries ``confirmed=True`` (a trusted
    # composer/test) is NOT re-gated, but an LLM-issued call never carries it,
    # so the gate is mandatory on the LLM path. Fail-closed: cancel/timeout
    # raises a typed, non-retryable error so the model narrates the decline and
    # does not re-run the same snippet.
    #
    # Invariant 9: STRIP the model-supplied confirmed/code_exec_id BEFORE
    # gating -- the gate is server-owned, exactly like the solver gate below,
    # making the user-confirmation gate MANDATORY on every model-issued
    # code_exec call; only an explicit user "proceed" inside
    # _gate_on_code_exec re-injects confirmed + the minted code_exec_id.
    # (Trusted programmatic callers/tests that must bypass invoke the tool
    # function directly, not via this server gate.)
    if tool_name == "code_exec_request":
        params.pop("confirmed", None)
        params.pop("code_exec_id", None)
        should_run, params = await _gate_on_code_exec(websocket, state, params)
        if not should_run:
            raise CodeExecConfirmationCancelledError(
                params.get("code_exec_id", "unknown")
            )

    # Centralized kwarg sweep: the model routinely invents kwargs that don't
    # exist on our tools (``run_name``, ``scenario_id``,
    # ``return_period_years`` when the tool accepts ``return_period_yr``,
    # etc.). ``normalize_args`` inspects ``entry.fn``'s signature and rewrites
    # bidirectional aliases (``_yr`` <-> ``_years``, ``_hr`` <-> ``_hours``,
    # ``durationHours`` <-> ``duration_hours``), parses string-form forcing
    # specs (``forcing="atlas14_100yr"`` -> ``return_period_years=100``),
    # absorbs silent-drop convenience kwargs, and logs+drops the rest -- never
    # raises. See ``tool_arg_normalizer.py``. Runs BEFORE the solver-confirm
    # gate AND the reuse guard so both see canonicalized param names.
    params = normalize_args(tool_name, params, entry.fn)

    # bbox AUTO-FILL. A tool whose signature REQUIRES a bbox-like
    # param ('bbox' / 'aoi_bbox') that the model OMITTED gets it injected
    # here -- precedence: explicit arg > active canvas AOI > Case bbox.
    # Explicit model args are NEVER overridden (the pinned-AOI snap below
    # owns the provided-bbox case). Runs AFTER normalize_args so bbox
    # aliases have landed on the canonical name, and BEFORE the reuse
    # guards/AOI snaps so they all see the filled value.
    params = autofill_missing_bbox(
        tool_name,
        params,
        entry.fn,
        active_aoi=state.active_aoi_bbox,
        case_bbox=_turn_case_bbox(state),
    )

    # Default a bbox-taking FETCH to the pinned Case AOI: after a solve pins
    # the domain, force a same-area follow-up fetch onto the pinned extent so
    # all layers cover the SAME AOI by construction; a genuinely DIFFERENT
    # place (disjoint) or an explicit WIDEN (encloses the pin) is honored.
    # Runs BEFORE the fetcher reuse guard so the reuse comparison sees the
    # snapped bbox. No-op when no AOI is pinned.
    params = _maybe_default_fetch_bbox_to_pinned_aoi(
        tool_name, params, _turn_case_bbox(state)
    )

    # Pin an expensive SOLVER's bbox to the active Case AOI too: the SFINCS
    # grid is built directly from this bbox via setup_grid_from_region (no
    # padding), so a follow-up/re-entry solve handed a drifted/wider
    # same-area box would compute OUTSIDE the displayed AOI. Mirrors the
    # fetch rule: solve ONLY within the active AOI, honoring an explicit
    # WIDEN (encloses the pin) or a DIFFERENT place (disjoint). No-op on the
    # first solve (no AOI pinned yet) and on archetypes/coastal (selected by
    # forcing flags, never an enclosing-wider bbox). Runs BEFORE the
    # scenario reuse guard so the reuse comparison sees the snap.
    params = _maybe_default_solver_bbox_to_pinned_aoi(
        tool_name, params, _turn_case_bbox(state)
    )

    # DETERMINISTIC expensive-simulation reuse guard: a HARD backstop before
    # launching an expensive solver composer -- checks the session's
    # already-produced results (the per-Case loaded_layers + the in-session
    # scenario index) for a CLEAR match (same scenario family + same AOI +
    # same key params). On a clear match it SHORT-CIRCUITS, returning the
    # EXISTING layer instead of launching the solver, and tags a "reusing
    # existing result (not re-running)" note for the model. CONSERVATIVE by
    # construction: any ambiguity falls through to RUN (see
    # scenario_reuse.py). ``force_rerun``/``rerun``/``force`` truthy kwargs
    # are the explicit-re-run escape hatch, stripped before the real
    # dispatch.
    _reuse_note: str | None = None
    if scenario_type_for_tool(tool_name) is not None:
        _force_rerun = any(
            bool(params.get(k))
            for k in ("force_rerun", "rerun", "re_run", "force")
        )
        # These are guard-control kwargs, never real tool params -- strip them so
        # the downstream tool body never sees an unexpected kwarg.
        for _k in ("force_rerun", "rerun", "re_run", "force"):
            params.pop(_k, None)
        # Stage 3: env kill-switch (TRID3NT_SCENARIO_REUSE=0 disables the
        # short-circuit; the guard-control strip above stays unconditional so
        # the kwargs never leak to the tool body either way).
        if not _force_rerun and _env_flag("TRID3NT_SCENARIO_REUSE", True):
            scenario_index = get_scenario_index(state.session_id)
            # Seed the index from this Case's durable loaded_layers so reuse
            # survives a reconnect / sibling connection (the in-memory index may
            # be cold while the layer persists on the Case).
            try:
                if state.emitter is not None:
                    scenario_index.seed_from_loaded_layers(
                        state.emitter.loaded_layers
                    )
            except Exception:  # noqa: BLE001 -- seeding is best-effort
                logger.debug("scenario_reuse seed failed", exc_info=True)
            request_sig = scenario_signature(tool_name, params)
            case_bbox = _turn_case_bbox(state)
            reuse = scenario_index.find_reuse(request_sig, case_bbox=case_bbox)
            if reuse is not None:
                logger.info(
                    "scenario_reuse[%s]: SHORT-CIRCUIT %s -> reusing layer_id=%s "
                    "(not re-running solver)",
                    state.session_id, tool_name, reuse.layer_id,
                )
                _reuse_note = (
                    f"Reusing the existing {reuse.scenario_type} result already "
                    f"on the map (layer '{reuse.name}', handle={reuse.layer_id}) "
                    "for this AOI and parameters — the simulation was NOT re-run. "
                    "Narrate from this existing layer; do not launch the solver "
                    "again unless the user changes the area or parameters or "
                    "explicitly asks to re-run."
                )
                _reused_layer = LayerURI(
                    layer_id=reuse.layer_id,
                    name=reuse.name,
                    layer_type=reuse.layer_type,  # type: ignore[arg-type]
                    uri=reuse.uri,
                    style_preset="",
                    bbox=reuse.bbox,
                )
                # Replace the dispatch with a synchronous return of the existing
                # layer so the SAME emission / card / persistence machinery
                # (emit_tool_call's LayerURI gate) fires with the reused layer.
                entry = _ReuseEntry(entry.metadata, _reused_layer)

    # Deterministic reuse backstop for FETCHERS (mirrors the scenario reuse
    # above, which only guards expensive SIMULATIONS): a fit/resize/re-show
    # follow-up for an already-loaded FETCHED layer would otherwise re-fetch
    # and mint a SECOND identical layer. When a same-kind loaded layer
    # already ENCLOSES the requested AOI, short-circuit to it so the agent
    # fits/narrates from the existing handle instead of re-fetching.
    # ``find_reusable_fetched_layer`` is pure/conservative: any ambiguity
    # (different kind, larger/unresolvable AOI) falls through to FETCH.
    # ``force_refetch``/``refetch``/``force`` truthy kwargs are the explicit
    # re-fetch escape hatch, stripped before the real dispatch.
    if (
        _reuse_note is None
        and not isinstance(entry, _ReuseEntry)
        and fetched_kind_for_tool(tool_name) is not None
    ):
        _force_refetch = any(
            bool(params.get(k)) for k in ("force_refetch", "refetch", "force")
        )
        for _k in ("force_refetch", "refetch", "force"):
            params.pop(_k, None)
        # Stage 3: env kill-switch (TRID3NT_FETCH_REUSE=0 disables the fetch
        # short-circuit; the guard-control strip stays unconditional).
        if (
            not _force_refetch
            and state.emitter is not None
            and _env_flag("TRID3NT_FETCH_REUSE", True)
        ):
            fetch_case_bbox = _turn_case_bbox(state)
            fmatch = find_reusable_fetched_layer(
                tool_name,
                params,
                state.emitter.loaded_layers,
                case_bbox=fetch_case_bbox,
            )
            if fmatch is not None:
                logger.info(
                    "scenario_reuse[%s]: FETCH SHORT-CIRCUIT %s -> reusing "
                    "layer_id=%s (not re-fetching)",
                    state.session_id, tool_name, fmatch.layer_id,
                )
                _reuse_note = (
                    f"Reusing the existing {fmatch.kind} layer already on the map "
                    f"(layer '{fmatch.name}', handle={fmatch.layer_id}) for this "
                    "AOI — the data was NOT re-fetched. For a fit / zoom / resize, "
                    "call compute_layer_bounds on this handle; do not re-fetch "
                    "unless the user asks for a different/larger area or an "
                    "explicit refresh."
                )
                _reused_fetch_layer = LayerURI(
                    layer_id=fmatch.layer_id,
                    name=fmatch.name,
                    layer_type=fmatch.layer_type,  # type: ignore[arg-type]
                    uri=fmatch.uri,
                    style_preset="",
                    bbox=fmatch.bbox,
                )
                entry = _ReuseEntry(entry.metadata, _reused_fetch_layer)

    # bbox-durability: anchor the Case AOI from
    # THIS bbox-carrying fetch's final (already reuse-guard-consulted /
    # AOI-defaulted) params. Runs AFTER both reuse guards above so it never
    # perturbs their read of the PRIOR pin; see _pin_case_aoi_from_tool_bbox
    # for the full root-cause + latest-wins-but-never-shrinks contract.
    await _pin_case_aoi_from_tool_bbox(
        state, case_id=turn_case_id, tool_name=tool_name, params=params
    )

    # Confirmation-before-consequence, driven by the tool's declared GateSpec
    # (ADR 0273). Membership is the ``gate_spec`` presence check -- no name set.
    # The LLM-supplied ``confirmed`` is STRIPPED first for a SOLVER gate -- the
    # gate is server-owned; only an explicit user "proceed" (the pin provider)
    # injects it. A FETCH gate does not strip it (fetchers ignore ``confirmed``).
    # SKIPPED on a reuse short-circuit (``_ReuseEntry``) -- nothing to confirm.
    # Routed through ``_gate_with_turn_memory`` so a same-tool/same-bbox retry
    # later in this SAME turn replays the earlier proceed/narrow_scope decision
    # instead of hanging on an unanswered second gate.
    _gate_spec = _gate_spec_for(tool_name)
    if _gate_spec is not None and not isinstance(entry, _ReuseEntry):
        if _gate_spec.kind == "solver":
            params.pop("confirmed", None)
        should_run, params = await _gate_with_turn_memory(
            websocket, state, tool_name, params
        )
        if not should_run:
            raise SolverConfirmationCancelledError(tool_name)

    # Layer-handle indirection: kills the LLM-URI-mangling class (invented
    # cache paths, WMS-URL-as-hazard, hash-tail hallucination, NSI
    # layer_id-as-basename, runs/ prefix mangle). Every URI-consuming param
    # resolves through the session-scoped registry: known handle -> registered
    # URI; exact known URI -> pass; close mangle -> substitute + WARNING;
    # unknown managed-bucket path -> typed retryable URI_HANDLE_UNRESOLVED
    # listing the real handles so the model self-corrects without inventing. See
    # uri_registry.py.
    uri_registry = get_uri_registry(state.session_id)
    params = uri_registry.resolve_params(tool_name, params)

    # job VAULT-READ: thread the user's per-Case ``secret_ref`` into a keyed
    # tool so its ``_resolve_*_key`` reads the VAULT key first (then env). This
    # mirrors the eBird secret_ref convention. No-op for non-keyed tools and
    # when no active secret exists (the tool falls back to env / typed
    # auth-error, which the credential-request flow below acts on).
    params = await _inject_secret_ref(state, tool_name, params, turn_case_id)

    state.current_pipeline_id = state.emitter.start_pipeline()
    state.current_turn_pipeline_id = state.current_pipeline_id
    # Bind the registry as the ambient observation sink for the
    # lifetime of the invoke so composer-internal publishes (publish_layer
    # called inside sfincs_flood) register the gs:// COG ↔ WMS
    # association even though the composer's envelope only carries the WMS URL.
    _uri_reg_token = activate_registry(uri_registry)
    # Tool-card persistence bookkeeping. ``_card_state`` stays None
    # on cancellation (Invariant 8 -- no replayable outcome); the wall-clock
    # pair is only the FALLBACK timing -- ``_persist_tool_card`` prefers the
    # emitter's authoritative ``last_tool_step`` stamps.
    _card_state: str | None = None
    _card_started_at = now_utc()
    _card_t0 = asyncio.get_running_loop().time()
    # C1: capture the tool IO for the persisted tool-card row so a Case reopen
    # rehydrates the expander (the live ``tool-io`` sidecar is wire-only and
    # was LOST on reopen). ``_card_raw_args`` is the post-resolution params the
    # tool actually ran with; ``_card_response`` is the raw tool RESULT (the
    # closest in-wrapper analogue of the live sidecar's ``function_response``
    # summary -- the summary itself is built downstream in _stream_model_reply,
    # which we don't reach from here). ``_persist_tool_card`` serializes both
    # with the SAME ``_json_for_tool_io`` helper + field names the live sidecar
    # uses, so the persisted shape matches the wire shape.
    _card_raw_args: Any = None
    _card_response: Any = None
    _card_io_error: bool = False

    # F97: mint a UNIQUE layer_id for every FRESHLY-fetched layer so two
    # layers from the SAME source (e.g. two `fetch_wdpa_protected_areas`
    # calls for the same bbox -> identical source-derived `wdpa-<lon>-<lat>`
    # id) never collide. A collision made Map.tsx (which keys MapLibre
    # sources by layer_id) skip the second add AND, on delete-by-id, tear
    # down the shared source so BOTH layers vanished. We replace the tool's
    # source-derived layer_id with a fresh ULID at the dispatch seam, BEFORE
    # ``emit_tool_call`` hands the LayerURI to ``add_loaded_layer`` (so the
    # emitted + persisted layer carries the unique id) and BEFORE the URI
    # registry / scenario-reuse index record it (they read ``result.layer_id``
    # AFTER this wrapper, so they pick up the minted id).
    #
    # Stability across reconnect/replay: minting happens only on a LIVE fetch.
    # A Case reopen rehydrates persisted dicts via ``reset_loaded_layers`` --
    # no tool re-runs, so the SAME instance keeps its minted id (per-Case
    # durability holds). The scenario-reuse short-circuit (``_ReuseEntry``)
    # is the deliberate exception: it hands back an ALREADY-loaded layer, so
    # it must keep that layer's existing id (re-minting would orphan the live
    # map layer + duplicate it). Hence we skip minting when ``entry`` is a
    # ``_ReuseEntry``.
    _mint_unique_layer_id = not isinstance(entry, _ReuseEntry)

    def _restamp(value: Any) -> Any:
        if not _mint_unique_layer_id:
            return value
        if isinstance(value, LayerURI):
            return value.model_copy(update={"layer_id": new_ulid()})
        # True-color / satellite tools return list[LayerURI] (fetch_goes_
        # animation, fetch_goes_archive_animation, fetch_goes_active_fire,
        # fetch_glm_lightning, fetch_viirs_day_fire). add_loaded_layer dedups
        # by COG-identity (the COG source uri), NOT by layer_id, so two
        # layers sharing a source-derived id both persist and collide on
        # delete-by-id. Re-stamp every LayerURI element with a fresh ULID,
        # passing non-LayerURI elements through, and PRESERVE the sequence
        # type (list stays list, tuple stays tuple) so downstream
        # isinstance(result, list) checks are unaffected.
        if isinstance(value, (list, tuple)):
            restamped = [
                el.model_copy(update={"layer_id": new_ulid()})
                if isinstance(el, LayerURI)
                else el
                for el in value
            ]
            return type(value)(restamped)
        return value

    async def _emit_early_input_frame() -> None:
        # FIX B (#7 -- input immediately + 'Running…'): the live ``tool-io``
        # sidecar was emitted ONLY at tool COMPLETION (a single frame carrying
        # BOTH raw_args AND function_response), so the chat card showed no input
        # and no output placeholder until the tool returned. Emit an EARLY
        # input-only frame at dispatch START -- SAME ``ToolIoPayload`` wire shape,
        # raw_args populated, function_response EMPTY (None -> "null"),
        # is_error False -- keyed on THIS dispatch's running step so the client
        # paints the input + a "Running…" output placeholder immediately. The
        # completion-time emit (server.py ~2090) re-keys the SAME step_id and
        # fills in function_response, so the two frames are idempotent on one
        # card (last-write-wins per step_id; merge, not duplicate). We run inside
        # the invoke callable (after emit_tool_call's mark_running) so the step
        # exists; best-effort so an emit hiccup never blocks the tool body.
        try:
            step_id = _running_emitter_step_id(state.emitter, tool_name)
            if step_id is not None:
                await state.emitter.emit_tool_io(
                    step_id=step_id,
                    tool_name=tool_name,
                    raw_args=_redact_secret_args(_original_tool_args),
                    function_response=None,
                    is_error=False,
                )
        except Exception:  # noqa: BLE001 -- early frame is a UX nicety
            logger.debug(
                "early tool-io emit failed session=%s tool=%s",
                state.session_id,
                tool_name,
                exc_info=True,
            )

    async def _invoke_with_unique_layer_id() -> Any:
        # Emit the input-only frame BEFORE the tool body runs so the input +
        # 'Running…' placeholder land while the tool is still executing.
        await _emit_early_input_frame()
        # #6 (loop-safety, ships dark): when the staged off-load is armed for
        # this tool (TRID3NT_SYNC_TOOL_OFFLOAD), run the SYNCHRONOUS body in a
        # worker thread so a slow tool cannot stall the WS keepalive. The emit
        # machinery stays on the loop (see _should_offload_sync_tool /
        # _assert_sync_offload_safe). Reuse short-circuits return a trivial
        # already-produced layer synchronously -- never worth a thread, and they
        # are not covered by the startup emit-free scan -- so they are excluded.
        # A tool mis-classified as sync (e.g. an async-callable object that
        # iscoroutinefunction missed) returns a coroutine from the thread; we
        # await it back on the loop so semantics are preserved.
        if (
            not isinstance(entry, _ReuseEntry)
            and _should_offload_sync_tool(tool_name)
            and not asyncio.iscoroutinefunction(entry.fn)
        ):
            out = await asyncio.to_thread(entry.fn, **params)
            if asyncio.iscoroutine(out):
                return _restamp(await out)
            return _restamp(out)
        out = entry.fn(**params)
        if asyncio.iscoroutine(out):
            return _restamp(await out)
        return _restamp(out)

    try:
        # Dispatch with a credential-request retry: the first attempt runs
        # the tool; if it raises a missing/invalid-credential error for a
        # keyed provider (e.g. FIRMS_AUTH_ERROR) we PAUSE, emit a
        # ``credential-request`` envelope, and await
        # ``credential-provided``. On provided=True we re-resolve the
        # freshly-pushed session-cache key and retry ONCE. One prompt per tool
        # per turn
        # (``credential_prompted_tools``) so a still-bad key fails through the
        # normal typed-error surface instead of re-prompting forever.
        try:
            result = await state.emitter.emit_tool_call(
                name=entry.metadata.name,
                tool_name=tool_name,
                invoke=_invoke_with_unique_layer_id,
            )
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except BaseException as exc:  # noqa: BLE001 -- classify below
            retry_params = await _maybe_handle_credential_error(
                websocket, state, tool_name, params, exc, turn_case_id
            )
            if retry_params is None:
                raise
            # Key provided + session-cache re-resolved: retry the tool ONCE.
            params = retry_params
            result = await state.emitter.emit_tool_call(
                name=entry.metadata.name,
                tool_name=tool_name,
                invoke=_invoke_with_unique_layer_id,
            )
        _card_state = "complete"
        # C1: stamp the IO for the persisted tool-card row. ``params`` is the
        # post-resolution arg dict the tool ran with; ``result`` is the raw
        # return. A LayerURI / pydantic model is dumped via ``default=str`` in
        # ``_json_for_tool_io`` so it never breaks serialization.
        _card_raw_args = params
        _card_response = result
    except asyncio.CancelledError:
        raise
    except BaseException as _exc:
        _card_state = "failed"
        _card_raw_args = params
        # On failure there is no result; persist the exception text as the
        # response so the reopened expander shows WHY it failed (mirrors the
        # live sidecar's is_error path).
        _card_response = {"error": str(_exc) or _exc.__class__.__name__}
        _card_io_error = True
        raise
    finally:
        deactivate_registry(_uri_reg_token)
        state.emitter.close_pipeline()
        state.current_pipeline_id = None
        # Persist the replayable tool-card row so a Case reopen re-renders
        # the inline tool card. Fires for complete AND failed terminal
        # states, BEFORE the narration row that closes the turn -- the chat
        # collection's ``created_at`` order IS the replay order. Best-effort,
        # never raises, never masks the original exception.
        if _card_state is not None and turn_case_id:
            await _persist_tool_card(
                state,
                tool_name=tool_name,
                label=entry.metadata.name,
                card_state=_card_state,
                started_at_fallback=_card_started_at,
                duration_ms_fallback=int(
                    (asyncio.get_running_loop().time() - _card_t0) * 1000.0
                ),
                case_id=turn_case_id,
                # C1: persist the tool IO on the row so a Case reopen rehydrates
                # the expander (reuses the live ToolIoPayload field names).
                raw_args=_card_raw_args,
                function_response=_card_response,
                io_is_error=_card_io_error,
            )
        # Persist the Case layer accumulator in the FINALLY block:
        # ``add_loaded_layer`` appends to ``_loaded_layers`` BEFORE it emits,
        # so persisting here captures the layer even when the post-invoke
        # ``session-state`` emission raises on a dying WebSocket. Never
        # raises (and never masks the original exception) -- persistence is
        # a side-effect, not the happy path.
        if turn_case_id and state.emitter is not None:
            # DURABILITY (layer-publish-survives-disconnect): run the
            # layer persist UNDER A SHIELD so a cancellation of the (possibly
            # detached) turn cannot interrupt the persistence write of a fully-
            # computed layer. A bare ``await`` here is cancel-fragile: a
            # same-stream re-prompt supersede / stop / any cancel re-raises the
            # pending CancelledError at the persist's first suspension point and
            # SKIPS the write -- the exact mechanism by which SFINCS run
            # 01KVSTC80F wrote 100+ COGs to S3 yet the Case persisted 0 layers
            # after a transient WS drop. ``_run_to_completion_shielded`` keeps the
            # write running to completion and THEN re-raises the cancel (Invariant
            # 8 preserved). The persist swallows its own errors (never raises), so
            # the only interruption this guard absorbs is the parent cancel.
            try:
                await _run_to_completion_shielded(
                    _persist_case_loaded_layers(state, case_id=turn_case_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - best-effort, never mask
                logger.exception(
                    "case-layer-persist (finally) failed case=%s",
                    turn_case_id,
                )

    # Register every URI the result carries (LayerURI layer_id↔uri
    # pairs + bare object-store strings) so the NEXT tool call can resolve
    # handles / detect mangles. Best-effort -- registration never breaks the
    # dispatch.
    uri_registry.register_tool_result(tool_name, result)

    # Persist the freshly-minted short-handle map (L<n> -> uri) WITH
    # the Case so a reconnect / Case reopen resolves the SAME handles the LLM
    # already saw. No-op when nothing new was minted; best-effort (never
    # breaks the dispatch).
    await _persist_case_layer_handles(state, case_id=turn_case_id)

    # A composer's LayerURI carries the FINAL floored AOI bbox and
    # ``emit_tool_call``'s LayerURI gate fires the live zoom-to via
    # ``add_loaded_layer`` -- but that never lands in
    # ``current_turn_map_commands`` on its own. Append the floored bbox HERE,
    # after any earlier geocode snap this turn, so it is the LAST zoom-to and
    # re-entry (``extractLastZoomTo``, newest-first) snaps to the floored AOI.
    # GUARDS: only a finite 4-number tuple; dedupe against the last
    # accumulated zoom-to bbox so a repeat dispatch does not double-append.
    #
    # For a DOMAIN-producing solver, emit ONLY the pinned domain bbox --
    # PURGE any earlier zoom-to entries so ``map_command_emissions`` carries
    # a single authoritative domain extent (otherwise the camera flashes the
    # geocode box then the domain box). Plain fetches keep the append-only
    # behavior so unrelated multi-layer flows are unaffected.
    if isinstance(result, LayerURI) and _is_finite_bbox4(result.bbox):
        _floored_bbox = list(result.bbox)
        if not isinstance(entry, _ReuseEntry) and _scenario_produces_domain(
            tool_name
        ):
            state.current_turn_map_commands = [
                cmd
                for cmd in state.current_turn_map_commands
                if not (isinstance(cmd, dict) and cmd.get("command") == "zoom-to")
            ]
            state.current_turn_map_commands.append(
                {"command": "zoom-to", "args": {"bbox": _floored_bbox}}
            )
        elif _last_zoom_to_bbox(state.current_turn_map_commands) != _floored_bbox:
            state.current_turn_map_commands.append(
                {"command": "zoom-to", "args": {"bbox": _floored_bbox}}
            )

    # Record a FRESHLY-PRODUCED expensive-scenario result into the
    # session reuse index so a later identical request short-circuits instead of
    # re-running the solver. Skip when this dispatch WAS the short-circuit (the
    # _ReuseEntry path) -- the layer is already indexed. Only index a real
    # success (a LayerURI return), never a failure dict. Best-effort.
    if (
        not isinstance(entry, _ReuseEntry)
        and scenario_type_for_tool(tool_name) is not None
        and isinstance(result, LayerURI)
    ):
        try:
            get_scenario_index(state.session_id).record_result(
                scenario_signature(tool_name, params),
                layer_id=result.layer_id,
                name=result.name,
                layer_type=result.layer_type,
                uri=result.uri,
                bbox=result.bbox,
            )
        except Exception:  # noqa: BLE001 -- indexing must never break dispatch
            logger.debug("scenario_reuse record failed", exc_info=True)

    # PIN the solve domain as the Case AOI: a freshly-completed expensive
    # solver (SWMM/SFINCS/MODFLOW) mints a LayerURI whose ``bbox`` IS the
    # authoritative floored solve domain. Persist it as ``CaseSummary.bbox``
    # + cache onto ``state.case_bbox`` so every subsequent fetch defaults to
    # this extent and a Case reopen rehydrates the SAME AOI. Skip on a reuse
    # short-circuit (already pinned when first produced). Best-effort.
    if (
        not isinstance(entry, _ReuseEntry)
        and _scenario_produces_domain(tool_name)
        and isinstance(result, LayerURI)
        and _is_finite_bbox4(result.bbox)
    ):
        try:
            await _pin_case_aoi_from_solve(
                state, case_id=turn_case_id, bbox=result.bbox
            )
        except Exception:  # noqa: BLE001 -- pin is a side-effect, never break
            logger.debug("aoi-pin failed", exc_info=True)

    # When this dispatch was a reuse short-circuit, the emitter has ALREADY
    # re-loaded the existing layer onto the map. What's left is to give
    # the model an UNAMBIGUOUS function_response -- "this is the EXISTING
    # result, not re-run" -- so it narrates honestly and does not retry.
    # Returns a compact dict carrying the reuse flag/note + the reused
    # layer's identity, replacing the bare LayerURI return; the map update
    # already happened, so nothing renderable is lost.
    if _reuse_note is not None and isinstance(result, LayerURI):
        logger.info("scenario_reuse note=%s", _reuse_note)
        return {
            "status": "reused_existing",
            "reused": True,
            "note": _reuse_note,
            "layer_id": result.layer_id,
            "name": result.name,
            "layer_type": result.layer_type,
            "uri": result.uri,
            "handle": result.layer_id,
        }

    # Per-Case layer persistence now happens in
    # the ``finally`` block above so it ALSO fires when the tool (or its
    # post-invoke envelope emission on a dying WebSocket) raised -- the
    # emitter's accumulator already contains the layer at that point.
    return result

async def _dispatch_tool_and_persist(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    raw_user_text: str,
) -> None:
    """Invoke a tool, then persist the agent's reply (tool result) to the
    active Case.

    Wraps ``_invoke_tool_via_emitter`` so the Case chat-history append
    happens after the tool returns. The persisted ``content`` is a
    user-readable summary of the tool result (the stringified result for
    primitive returns, or a marker for complex returns).

    NOTHING MAY ESCAPE THIS FRAME. It is the ``/invoke`` directive path, a manual
    operator-debug surface dispatched via ``asyncio.create_task``, so there is no
    awaiter to catch a propagated exception: anything that escapes becomes an
    "asyncio Task exception was never retrieved" log line while the CLIENT
    receives no error envelope at all and the plugin shows nothing.

    So every failure leaves through ``_send_error`` as a structured ``error``
    envelope, the same shape the model's multi-turn loop produces via
    ``summarize_tool_result``. The named catches cover the routing failures
    (``ToolNotFoundError`` -> ``TOOL_NOT_FOUND`` / ``retryable=False``;
    ``PayloadWarningCancelledError`` -> the cancellation reason stated rather
    than disappearing). The broad ``except Exception`` below covers every OTHER
    typed tool exception (``MeshGenerationError``, ``TelemacRainOnGridError``,
    ``HydrologyAoiTooLargeError``, ...), routing the tool's OWN ``error_code`` /
    ``retryable`` and falling back to ``TOOL_EXECUTION_FAILED`` / non-retryable
    only when the exception is untyped. The exception's real code goes out
    VERBATIM: an upstream-provider error carries its own code and must never be
    relabelled as an internal failure.
    """
    # Entry-time Case capture -- see _dispatch_model_turn_and_persist.
    turn_case_id = _turn_case_id(state)
    bind_turn_case(turn_case_id)  # envelope tagging
    bind_turn_drawn_geometry(state.drawn_geometry)
    try:
        try:
            await _invoke_tool_via_emitter(
                websocket, state, tool_name, params
            )
        except asyncio.CancelledError:
            raise
        except ToolNotFoundError as exc:
            logger.info(
                "/invoke directive references unregistered tool "
                "session=%s tool=%s",
                state.session_id,
                tool_name,
            )
            await _send_error(
                websocket,
                state.session_id,
                exc.error_code,
                str(exc),
                retryable=exc.retryable,
            )
        except PayloadWarningCancelledError as exc:
            logger.info(
                "/invoke directive cancelled via payload-warning gate "
                "session=%s tool=%s",
                state.session_id,
                tool_name,
            )
            await _send_error(
                websocket,
                state.session_id,
                exc.error_code,
                str(exc),
                retryable=exc.retryable,
            )
        except Exception as exc:  # noqa: BLE001 -- honesty-floor catch-all
            # Any OTHER tool exception on this no-awaiter create_task path (see
            # docstring): surface a structured envelope instead of a silent
            # no-result. The A.6 ErrorCode Literal is a CLOSED set, so a tool's
            # own code (e.g. TELEMAC_ROG_POUR_POINT_OFF_DEM) cannot be the wire
            # ``error_code`` -- constructing ErrorPayload with it raises inside
            # _send_error and (per the ws.py:CONTEXT_WINDOW bug) skips the send
            # entirely, the very silence this fix closes. So: pass a typed tool
            # code through only when it is already a valid ErrorCode, else fall
            # back to INTERNAL_ERROR with the specific code LEADING the message
            # as a ``[MARKER]`` (house convention, see
            # _notify_layer_auto_publish_failed) -- honest + greppable, no enum
            # widening. Upstream-provider codes that ARE valid (LLM_UNAVAILABLE)
            # pass through un-internalized.
            tool_code = getattr(exc, "error_code", None) or "TOOL_EXECUTION_FAILED"
            retryable = bool(getattr(exc, "retryable", False))
            if tool_code in _VALID_ERROR_CODES:
                wire_code, message = tool_code, str(exc)
            else:
                wire_code, message = "INTERNAL_ERROR", f"[{tool_code}] {exc}"
            logger.exception(
                "/invoke directive tool raised session=%s tool=%s code=%s",
                state.session_id,
                tool_name,
                tool_code,
            )
            await _send_error(
                websocket,
                state.session_id,
                wire_code,
                message,
                retryable=retryable,
            )
    finally:
        if turn_case_id:
            await _persist_chat_turn(
                state,
                role="agent",
                content=f"[invoked {tool_name}]",
                pipeline_id=state.current_turn_pipeline_id,
                case_id=turn_case_id,
            )
        # C2: end-of-turn idle signal for the /invoke directive path too -- same
        # rationale as _dispatch_model_turn_and_persist. Best-effort.
        await _emit_turn_complete(
            websocket, state, pipeline_id=state.current_turn_pipeline_id
        )
