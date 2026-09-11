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
from trid3nt_server.server.config import _env_flag
from trid3nt_server.server.dispatch.aoi import _maybe_default_fetch_bbox_to_pinned_aoi, _pin_case_aoi_from_tool_bbox
from trid3nt_server.server.dispatch.persist import _VALID_ERROR_CODES, _persist_chart_record, _persist_chat_turn, _persist_tool_card
from trid3nt_server.server.dispatch.results import _run_to_completion_shielded
from trid3nt_server.server.dispatch.layer_reuse import _ReuseEntry, fetched_kind_for_tool, find_reusable_fetched_layer
from trid3nt_server.server.errors import CodeExecConfirmationCancelledError, PayloadWarningCancelledError, SolverConfirmationCancelledError, ToolNotFoundError
from trid3nt_server.server.session.case_state import _persist_case_layer_handles, _persist_case_loaded_layers, _turn_case_bbox, _turn_case_id
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _is_finite_bbox4, _last_zoom_to_bbox
from trid3nt_server.server.turn.wire import _emit_turn_complete, _send_error
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

def _ensure_emitter(websocket: ServerConnection, state: SessionState) -> None:
    """Bind a ``PipelineEmitter`` to this session if one is not already bound;
    its sink is the WebSocket send, one envelope per transition."""
    if state.emitter is not None:
        return

    async def _sink(text: str) -> None:
        # The socket may be mid-close when a terminal pipeline-state frame is
        # emitted on the cancel path, and the send would then raise straight out
        # of the emitter, swallowing that frame and escaping the cancel chain.
        # Swallowing send failures keeps the card-state transition recorded
        # server-side and lets the cancel propagate cleanly.
        try:
            await websocket.send(text)
        except Exception:  # noqa: BLE001 -- socket may be closing on cancel/fail
            logger.debug(
                "emitter sink: websocket.send failed (socket closing?); "
                "frame dropped best-effort (session=%s)",
                state.session_id,
            )

    async def _chart_persist(payload: dict) -> None:
        # A composer-side chart persists through the SAME record writer the
        # tool-result chart path uses, so it replays on Case rehydration
        # identically. Best-effort inside that writer.
        await _persist_chart_record(state, payload)

    async def _tool_card_persist(**kwargs: Any) -> None:
        # A terminal compute card persists through the same writer the atomic
        # tool cards use, so it replays on a reconnect or Case reopen. The Case
        # is pinned from the live turn context, so a cancel-and-redispatch race
        # cannot re-aim the write. Best-effort.
        await _persist_tool_card(state, **kwargs)

    state.emitter = PipelineEmitter(
        session_id=state.session_id,
        sink=_sink,
        chat_history=state.chat_history,
        chart_persist=_chart_persist,
        tool_card_persist=_tool_card_persist,
    )

# Arg keys whose VALUES are credentials and must NEVER appear in an emitted
# envelope. The early input-only frame snapshots the ORIGINAL call args, which on
# the dev resolution path can carry a raw key. Mirrors and extends the keys the
# credential pipeline strips when it injects a secret reference.
_SECRET_ARG_KEYS: frozenset[str] = frozenset({
    "secret_ref", "map_key", "api_key", "apikey", "token", "access_token",
    "password", "passwd", "secret", "secret_key", "access_key", "private_key",
    "credentials", "credential", "auth", "authorization",
})

def _redact_secret_args(args: Any) -> Any:
    """Copy ``args`` with any secret-bearing VALUE masked and the key left
    visible, so the card still shows the real request while a raw credential is
    never echoed into a wire or persisted envelope."""
    if not isinstance(args, dict):
        return args
    return {
        k: ("***redacted***" if str(k).lower() in _SECRET_ARG_KEYS else v)
        for k, v in args.items()
    }

def _running_emitter_step_id(emitter: Any, tool_name: str) -> str | None:
    """Return the step_id of the emitter's currently running step for
    ``tool_name``, or ``None`` when there is none. Best-effort: the early
    input-only frame it feeds is a nicety, never a correctness gate."""
    # The step id is only published at the terminal transition, so the in-flight
    # id is derived the way the emitter's own progress update does: the
    # most-recently-added step still running. The tool_name guard keeps a stale
    # running step from a sibling dispatch from mis-keying the frame.
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

# A synchronous atomic tool runs its whole body on the agent's asyncio loop
# inside the ``entry.fn(**params)`` branch below, so a slow one (boto3,
# requests, heavy GDAL or numpy compute) stalls the WS keepalive past the pong
# deadline and the client reconnect-cycles or the socket dies. Off-loading that
# call to a worker thread is SAFE because tool bodies are EMIT-FREE: every
# loop-bound emitter call lives in the surrounding wrapper, which stays on the
# loop. ``asyncio.to_thread`` propagates the contextvars Context, so a stray
# emit WOULD still resolve its ContextVar - hence the armed-only
# ``_assert_sync_offload_safe`` guard refuses to arm when a candidate tool's
# source so much as references the emitter API.
#
# ``TRID3NT_SYNC_TOOL_OFFLOAD`` selects the mode with no code change:
#   ""/"off"             -> disabled; sync tools stay on the loop.
#   "subset"             -> off-load only the pure compute_*/clip_* family.
#   "global"/"all"/"on"  -> off-load every sync tool body.
_SYNC_OFFLOAD_MODE = os.environ.get("TRID3NT_SYNC_TOOL_OFFLOAD", "off").strip().lower()

_SYNC_OFFLOAD_GLOBAL_VALUES = frozenset({"global", "all", "on", "1", "true", "yes"})

#: The subset mode's cohort: the pure-compute and pure-clip families, which take
#: no emitter and do CPU-bound GDAL or numpy work.
_SYNC_OFFLOAD_SUBSET_PREFIXES = ("compute_", "clip_")

#: ALWAYS off-loaded whatever the env mode says: a hand-audited, TIGHT set of
#: sync tools whose bodies do multi-second synchronous work - tile merge,
#: reproject, WarpedVRT or COG materialize, a large download plus xarray or
#: netCDF compute, a dense-index build, a staged container run - on the asyncio
#: loop, stalling the WS data-heartbeat past the client's reconnect deadline.
#: Every entry was confirmed EMIT-FREE, and ``_assert_sync_offload_safe``
#: re-validates that for this set even when the env mode is off, so a future
#: emitting tool can never be added silently. This is NOT "off-load
#: everything": the light vector and scalar fetchers, and every non-fetch sync
#: tool, stay on the loop.
_ALWAYS_OFFLOAD_SYNC_TOOLS = frozenset(
    {
        # dense-index build on first call (sentence-transformers encode)
        "search_living_atlas",
        "fetch_living_atlas_layer",
        # tile mosaic / windowed warp-read plus COG materialize
        "fetch_topobathy",
        "fetch_dem",
        "fetch_3dep_extra",
        "fetch_landcover",
        "extract_landcover_class",
        "fetch_population",
        "fetch_hrsl_population",
        "fetch_gcn250_curve_numbers",
        "fetch_statsgo_soils",
        # blocking retrieve plus xarray open, compute and COG write
        "fetch_era5_reanalysis",
        "fetch_gridmet",
        "fetch_hrrr_forecast",
        "fetch_hrrr_smoke",
        "fetch_mrms_qpe",
        "fetch_goes_satellite",
        # per-frame stitch, reproject and COG-write loop, one chain per timestamp
        "fetch_goes_animation",
        "fetch_goes_blend_animation",
        "fetch_viirs_day_fire",
        # up to 144 archive frames in ONE sync call, each a ~54 MB netCDF
        # download plus reproject and COG write
        "fetch_goes_archive_animation",
        "fetch_goes_active_fire",
        "fetch_gtsm_tide_surge",
        # STAC raster readers: sign, windowed /vsicurl warp-read, COG write
        "compute_ndvi",
        "fetch_naip",
        # multi-granule netCDF download plus in-AOI group filter and raster write
        "fetch_glm_lightning",
        # record fetchers: a windowed Zarr stream, and a multi-MB entity download
        "fetch_aorc_precip",
        "fetch_lter_records",
        # stages every layer_ref into the run dir, then blocks on a container run
        "code_exec_request",
        # reads the run's outputs listing over the network
        "list_run_frames",
        # STAC sign plus windowed warp-read and COG / FlatGeobuf write
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
        # two Sentinel-2 scenes read per band, vectorized, written as one FGB
        "compute_change_detection",
        # stages an s3 COG, fetches an inventory, samples, writes an FGB
        "compute_flood_depth_damage",
        "compute_model_residuals",
    }
)

#: Loop-bound emitter API names. A sync tool whose CODE - comments and string
#: literals excluded - references any of these, or any ``emit_*`` attribute, is
#: NOT safe to off-load, and ``_assert_sync_offload_safe`` refuses to arm.
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
    """True if ``src`` contains a real CODE reference to the loop-bound emitter
    API; comments and string literals are ignored, so a mention in a docstring is
    not a false positive."""
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
    """Return True when ``tool_name``'s sync body should run through
    ``asyncio.to_thread``: the always-offload set unconditionally, then whatever
    the env mode selects."""
    if tool_name in _ALWAYS_OFFLOAD_SYNC_TOOLS:
        return True
    mode = _SYNC_OFFLOAD_MODE
    if mode in _SYNC_OFFLOAD_GLOBAL_VALUES:
        return True
    if mode == "subset":
        return tool_name.startswith(_SYNC_OFFLOAD_SUBSET_PREFIXES)
    return False

def _assert_sync_offload_safe() -> None:
    """ARMED-ONLY startup gate: refuse to start when a sync tool that would be
    off-loaded references the loop-bound emitter API, since a worker thread must
    never touch the event loop."""
    # The always-offload set runs off-loop even in ``off`` mode, so its emit-free
    # invariant is validated whenever that set is non-empty; with an empty set and
    # a disabled mode there is nothing to scan and the source sweep is skipped.
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
            "%s. Refusing to start."
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
    """The tool-call site: every registry ``fn(...)`` runs through this wrapper,
    so each transition emits a step, a ``LayerURI`` return re-emits session
    state, and a cancel propagates while any other exception becomes a wire code."""
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

    # BENCH PRE-DISPATCH BLOCK HOOK, armed only by the bench harness. When
    # armed it decides the tool's fate BEFORE any gate or fetch runs: a
    # non-member pick is blocked outright, while a member pick in the block tier
    # runs the same arg normalizer a real dispatch would, so the block is graded
    # on canonicalized args. Both raise THROUGH the emitter, so the tool still
    # surfaces as a failed step and ``entry.fn`` is never reached.
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

    # Snapshot the ORIGINAL call args NOW, before normalization, gating,
    # URI-resolve and secret-inject rewrite ``params``: the early input-only
    # frame's args must equal the completion frame's, so the card shows the same
    # input the model sent both live and at completion.
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
    # STRIP a model-supplied confirmed/code_exec_id BEFORE gating: the gate is
    # server-owned, so user confirmation is mandatory on every model-issued
    # code_exec call, and only an explicit approval inside the gate re-injects
    # them. A trusted programmatic caller invokes the tool function directly.
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

    # Default a bbox-taking FETCH to the pinned Case AOI: a same-area follow-up
    # is forced onto the pinned extent so all layers cover the SAME AOI by
    # construction; a genuinely DIFFERENT place (disjoint) or an explicit WIDEN
    # (encloses the pin) is honored. Runs BEFORE the reuse guard so the reuse
    # comparison sees the snapped bbox. No-op when no AOI is pinned.
    params = _maybe_default_fetch_bbox_to_pinned_aoi(
        tool_name, params, _turn_case_bbox(state)
    )

    # Deterministic reuse backstop for FETCHERS: a fit, resize or re-show
    # follow-up would otherwise re-fetch and mint a SECOND identical layer, so a
    # same-kind loaded layer that already ENCLOSES the requested AOI
    # short-circuits to that handle. Any ambiguity falls through to a fetch, and
    # a truthy force_refetch/refetch/force kwarg is the explicit escape hatch,
    # stripped before the real dispatch.
    _reuse_note: str | None = None
    if fetched_kind_for_tool(tool_name) is not None:
        _force_refetch = any(
            bool(params.get(k)) for k in ("force_refetch", "refetch", "force")
        )
        for _k in ("force_refetch", "refetch", "force"):
            params.pop(_k, None)
        # ``TRID3NT_FETCH_REUSE=0`` disables the short-circuit; the guard-control
        # strip above stays unconditional either way.
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
                    "layer_reuse[%s]: FETCH SHORT-CIRCUIT %s -> reusing "
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
                    bbox=fmatch.bbox,
                )
                entry = _ReuseEntry(entry.metadata, _reused_fetch_layer)

    # Anchor the Case AOI from THIS bbox-carrying fetch's final params, after
    # the reuse guard, so it never perturbs its read of the prior pin.
    await _pin_case_aoi_from_tool_bbox(
        state, case_id=turn_case_id, tool_name=tool_name, params=params
    )

    # Confirmation-before-consequence, driven by the tool's declared gate spec;
    # membership IS that spec's presence, never a name set. A model-supplied
    # ``confirmed`` is STRIPPED for a solver gate, because the gate is
    # server-owned and only an explicit user proceed injects it; a fetch gate
    # ignores the flag. A reuse short-circuit has nothing to confirm. The turn
    # memory replays an earlier decision for a same-tool, same-bbox retry rather
    # than hanging on a second unanswered gate.
    _gate_spec = _gate_spec_for(tool_name)
    if _gate_spec is not None and not isinstance(entry, _ReuseEntry):
        if _gate_spec.kind == "solver":
            params.pop("confirmed", None)
        should_run, params = await _gate_with_turn_memory(
            websocket, state, tool_name, params
        )
        if not should_run:
            raise SolverConfirmationCancelledError(tool_name)

    # Layer-handle indirection kills the URI-mangling class: every URI-consuming
    # param resolves through the session registry - a known handle to its
    # registered URI, an exact known URI passes, a close mangle is substituted
    # with a warning, and an unknown managed-bucket path becomes a typed
    # retryable error listing the real handles, so the model self-corrects
    # instead of inventing.
    uri_registry = get_uri_registry(state.session_id)
    params = uri_registry.resolve_params(tool_name, params)

    # Thread the user's per-Case ``secret_ref`` into a keyed tool so its key
    # resolution reads the vault first and env second. No-op for a non-keyed
    # tool and when no active secret exists, in which case the tool falls back to
    # env or a typed auth error the credential flow below acts on.
    params = await _inject_secret_ref(state, tool_name, params, turn_case_id)

    state.current_pipeline_id = state.emitter.start_pipeline()
    state.current_turn_pipeline_id = state.current_pipeline_id
    # Bind the registry as the ambient observation sink for the lifetime of the
    # invoke, so a composer-internal publish registers its object-store URI even
    # though the composer's envelope carries only the service URL.
    _uri_reg_token = activate_registry(uri_registry)
    # Tool-card persistence bookkeeping. ``_card_state`` stays None on
    # cancellation - there is no replayable outcome - and the wall-clock pair is
    # only the FALLBACK timing, since the persist prefers the emitter's stamps.
    _card_state: str | None = None
    _card_started_at = now_utc()
    _card_t0 = asyncio.get_running_loop().time()
    # Capture the tool IO for the persisted tool-card row, since the live
    # ``tool-io`` sidecar is wire-only and is lost on reopen. ``_card_raw_args``
    # is the post-resolution params the tool ran with and ``_card_response`` the
    # raw result, both serialized with the same helper and field names the live
    # sidecar uses, so the persisted shape matches the wire shape.
    _card_raw_args: Any = None
    _card_response: Any = None
    _card_io_error: bool = False

    # Mint a UNIQUE layer_id for every FRESHLY fetched layer: two layers from
    # the same source would otherwise share a source-derived id, and a client
    # keying by layer_id skips the second add and tears down the shared source
    # on delete, so both vanish. The mint happens at the dispatch seam, before
    # the layer is handed to the loaded-layer set and before the URI registry
    # and reuse index read it back.
    #
    # Only a LIVE fetch mints: a Case reopen rehydrates persisted dicts without
    # re-running a tool, so an instance keeps its id. A reuse short-circuit is
    # the deliberate exception - it hands back an ALREADY-loaded layer, and
    # re-minting would orphan the live map layer and duplicate it.
    _mint_unique_layer_id = not isinstance(entry, _ReuseEntry)

    def _restamp(value: Any) -> Any:
        if not _mint_unique_layer_id:
            return value
        if isinstance(value, LayerURI):
            return value.model_copy(update={"layer_id": new_ulid()})
        # Some tools return a list of layers, and the loaded-layer set dedups by
        # source identity rather than by layer_id, so two layers sharing a
        # source-derived id would both persist and then collide on delete. Every
        # element is re-stamped, non-layer elements pass through, and the
        # sequence type is preserved so downstream list checks are unaffected.
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
        # The completion-time ``tool-io`` frame carries both args and response,
        # so without an early frame the card shows nothing until the tool
        # returns. This emits the SAME wire shape with only raw_args populated,
        # keyed on THIS dispatch's running step, so the two frames merge on one
        # card by step_id rather than duplicating. Runs inside the invoke
        # callable, after the step is marked running, and is best-effort so an
        # emit hiccup never blocks the tool body.
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
        # Emit the input-only frame BEFORE the tool body runs, so the input and
        # its running placeholder land while the tool is still executing.
        await _emit_early_input_frame()
        # When the off-load is armed for this tool, run the SYNCHRONOUS body in
        # a worker thread so a slow tool cannot stall the WS keepalive; the emit
        # machinery stays on the loop. A reuse short-circuit returns an
        # already-produced layer synchronously and is not covered by the startup
        # emit-free scan, so it is excluded. A tool mis-classified as sync
        # returns a coroutine from the thread, awaited back on the loop.
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
        # Dispatch with a credential-request retry: a missing or invalid
        # credential for a keyed provider PAUSES the dispatch, emits a
        # credential request, and retries ONCE with the freshly pushed key. One
        # prompt per tool per turn, so a still-bad key fails through the normal
        # typed-error surface instead of re-prompting forever.
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
        # Stamp the IO for the persisted tool-card row: ``params`` is the
        # post-resolution arg dict the tool ran with and ``result`` the raw
        # return, serialized with ``default=str`` so a model never breaks it.
        _card_raw_args = params
        _card_response = result
    except asyncio.CancelledError:
        raise
    except BaseException as _exc:
        _card_state = "failed"
        _card_raw_args = params
        # On failure there is no result, so the exception text is persisted as
        # the response and the reopened expander shows WHY it failed.
        _card_response = {"error": str(_exc) or _exc.__class__.__name__}
        _card_io_error = True
        raise
    finally:
        deactivate_registry(_uri_reg_token)
        state.emitter.close_pipeline()
        state.current_pipeline_id = None
        # Persist the replayable tool-card row so a reopen re-renders the inline
        # card. Fires for complete AND failed terminal states, before the
        # narration row that closes the turn, because created_at order IS the
        # replay order. Best-effort; never masks the original exception.
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
                # Persist the tool IO on the row so a reopen rehydrates the
                # expander, reusing the live payload's field names.
                raw_args=_card_raw_args,
                function_response=_card_response,
                io_is_error=_card_io_error,
            )
        # Persist the Case layer accumulator in the FINALLY block: the layer is
        # appended to the accumulator BEFORE the emit, so persisting here
        # captures it even when the post-invoke emission raises on a dying
        # socket. Never raises and never masks the original exception.
        if turn_case_id and state.emitter is not None:
            # Run the layer persist UNDER A SHIELD so a cancellation of the
            # (possibly detached) turn cannot interrupt the write of a fully
            # computed layer: a bare ``await`` here re-raises the pending cancel
            # at the persist's first suspension point and SKIPS the write, which
            # is how a solve can write every COG and still persist no layers
            # after a transient WS drop. The shield keeps the write running and
            # then re-raises the cancel. The persist swallows its own errors, so
            # the parent cancel is the only interruption this guard absorbs.
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

    # Register every URI the result carries - layer id to uri pairs and bare
    # object-store strings - so the NEXT call can resolve handles and detect
    # mangles. Best-effort: registration never breaks the dispatch.
    uri_registry.register_tool_result(tool_name, result)

    # Persist the freshly minted short-handle map with the Case, so a reconnect
    # or reopen resolves the SAME handles the model already saw. No-op when
    # nothing new was minted; best-effort.
    await _persist_case_layer_handles(state, case_id=turn_case_id)

    # A composer's layer carries the FINAL floored AOI bbox, but the live
    # zoom-to it fires never lands in the turn's map commands on its own.
    # Appending it here, after any earlier geocode snap, makes it the LAST
    # zoom-to, so a re-entry that replays the newest one snaps to the floored
    # AOI. Guarded on a finite extent and deduped against the last accumulated
    # zoom-to, so a repeat dispatch does not double-append.
    if isinstance(result, LayerURI) and _is_finite_bbox4(result.bbox):
        _floored_bbox = list(result.bbox)
        if _last_zoom_to_bbox(state.current_turn_map_commands) != _floored_bbox:
            state.current_turn_map_commands.append(
                {"command": "zoom-to", "args": {"bbox": _floored_bbox}}
            )

    # On a reuse short-circuit the emitter has ALREADY re-loaded the existing
    # layer onto the map, so what remains is an unambiguous function response
    # saying this is the EXISTING result, letting the model narrate honestly
    # instead of retrying. The compact dict replaces the bare layer return;
    # nothing renderable is lost, because the map update already happened.
    if _reuse_note is not None and isinstance(result, LayerURI):
        logger.info("layer_reuse note=%s", _reuse_note)
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

    # Per-Case layer persistence happens in the ``finally`` above, so it also
    # fires when the tool or its post-invoke emission raised: the accumulator
    # already holds the layer at that point.
    return result

async def _dispatch_tool_and_persist(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    raw_user_text: str,
) -> None:
    """Invoke a tool, then persist the agent's reply to the active Case; the
    persisted content is a readable summary of the result. NOTHING MAY ESCAPE
    THIS FRAME: every failure leaves as a structured error envelope."""
    # This is the directive path, dispatched as a bare task with no awaiter, so
    # anything that escapes becomes an unretrieved-task log line while the client
    # receives nothing at all. The named catches cover the routing failures; the
    # broad catch routes every other typed tool exception by the tool's OWN code
    # and retryable flag, falling back to a generic non-retryable failure only
    # when the exception is untyped. An upstream-provider code goes out VERBATIM
    # and is never relabelled as an internal failure.
    # Entry-time Case capture, so a mid-turn switch cannot re-aim the writes.
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
            # Any OTHER tool exception on this no-awaiter path must still reach
            # the client as a structured envelope rather than silence. The wire
            # ``error_code`` is a CLOSED set, so a tool's own code is passed
            # through only when it is already valid; otherwise the code LEADS
            # the message as a ``[MARKER]`` under INTERNAL_ERROR - honest and
            # greppable, with no enum widening. A valid upstream-provider code
            # passes through un-internalized.
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
        # End-of-turn idle signal on the directive path too. Best-effort.
        await _emit_turn_complete(
            websocket, state, pipeline_id=state.current_turn_pipeline_id
        )
