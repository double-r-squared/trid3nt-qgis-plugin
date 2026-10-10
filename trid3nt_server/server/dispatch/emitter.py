"""Tool execution through the pipeline emitter: sync-offload safety + the invoke driver."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._fetch_common import UpstreamAPIError
from trid3nt_server.tools.tool_arg_normalizer import autofill_missing_bbox, normalize_args
from trid3nt_server.render.pipeline_emitter import PipelineEmitter, bind_turn_case
from trid3nt_server.render.uri_registry import activate_registry, deactivate_registry, get_uri_registry
# The gate engine (trid3nt_server.inputs.gate.confirm) is imported function-locally in
# _invoke_tool_via_emitter -- deferred to break the server<->gates load cycle.
from trid3nt_server.server.config import _env_flag
from trid3nt_server.server.dispatch.aoi import pin_case_aoi_from_solve
from trid3nt_server.server.dispatch.persist import _VALID_ERROR_CODES, _persist_chart_record, _persist_chat_turn, _persist_tool_card
from trid3nt_server.server.dispatch.results import _run_to_completion_shielded
from trid3nt_server.inputs.gate.cards.run_inputs import review_run_inputs
from trid3nt_server.inputs.gate.errors import CodeExecConfirmationCancelledError, GateConfirmationTimeoutError, PayloadWarningCancelledError, SolverConfirmationCancelledError, UserDeclinedError
from trid3nt_server.server.errors import ToolNotFoundError
from trid3nt_server.server.session.case_state import _persist_case_layer_handles, _persist_case_loaded_layers, _turn_case_bbox, _turn_case_id
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.inputs.extent import as_bbox
from trid3nt_server.server.spatial import _last_zoom_to_bbox
from trid3nt_server.server.turn.wire import _emit_turn_complete, _send_error
from typing import Any, Awaitable, Callable
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

def _ensure_emitter(websocket: ServerConnection, state: SessionState) -> None:
    """Bind a ``PipelineEmitter`` to this session if one is not already bound;
    its sink is the WebSocket send, one envelope per transition."""
    if state.emitter is not None:
        return

    async def _sink(text: str) -> None:
        # The socket may be mid-close when a terminal pipeline-state frame is emitted on the cancel
        # path, and the send would then raise straight out of the emitter, swallowing that frame and
        # escaping the cancel chain.
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
        # A terminal compute card persists through the same writer the atomic tool cards use, so it
        # replays on a reconnect or Case reopen. The Case is pinned from the live turn context, so a
        # cancel-and-redispatch race cannot re-aim the write. Best-effort.
        await _persist_tool_card(state, **kwargs)

    state.emitter = PipelineEmitter(
        session_id=state.session_id,
        sink=_sink,
        chat_history=state.chat_history,
        chart_persist=_chart_persist,
        tool_card_persist=_tool_card_persist,
    )

# Arg keys whose VALUES are credentials and must NEVER appear in an emitted envelope. The early
# input-only frame snapshots the ORIGINAL call args, which on the dev resolution path can carry a
# raw key.
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
    """Return the step_id of the emitter's currently running step for ``tool_name``, or ``None``
    when there is none. Best-effort: the early input-only frame it feeds is never a gate."""
    # The step id is only published at the terminal transition, so the in-flight id is derived the
    # way the emitter's own progress update does: the most-recently-added step still running. The
    # tool_name guard keeps a stale running step from a sibling dispatch from mis-keying the frame.
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

# A synchronous atomic tool runs its whole body on the agent's asyncio loop inside the
# ``entry.fn(**params)`` branch below, so a slow one (boto3, requests, heavy GDAL or numpy compute)
# stalls the WS keepalive past the pong deadline and the client reconnect-cycles or the socket dies.
# Off-loading to a worker thread is safe only because tool bodies are EMIT-FREE: every loop-bound
# emitter call lives in the wrapper. to_thread propagates contextvars, so a stray emit would still
# resolve, hence ``_assert_sync_offload_safe`` refuses to start when a body references the emitter.

#: Loop-bound emitter API names. A sync tool whose CODE - comments and string
#: literals excluded - references any of these, or any ``emit_*`` attribute, is
#: NOT safe to off-load, and ``_assert_sync_offload_safe`` refuses to arm.
_EMITTER_API_NAMES = frozenset(
    {
        "current_emitter",
        "add_loaded_layer",
        "update_progress",
        "start_pipeline",
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

def _assert_sync_offload_safe() -> None:
    """Startup gate: refuse to start when a sync tool, which runs in a worker
    thread, references the loop-bound emitter API."""
    import inspect

    offenders: list[str] = []
    uninspectable: list[str] = []
    n_candidates = 0
    for name, reg in TOOL_REGISTRY.items():
        fn = getattr(reg, "fn", None)
        if fn is None or asyncio.iscoroutinefunction(fn):
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
            "these sync tools reference the loop-bound emitter API and are "
            "UNSAFE to run off-loop: %s. Refusing to start."
            % ", ".join(sorted(offenders))
        )
    if uninspectable:
        logger.warning(
            "sync-tool off-load: %d tool(s) could not be source-inspected for "
            "the emit-free check: %s",
            len(uninspectable),
            ", ".join(sorted(uninspectable)),
        )
    logger.info(
        "sync-tool off-load: %d sync tool(s) verified emit-free", n_candidates,
    )

@dataclass
class _ReuseEntry:
    """A ``RegisteredTool``-shaped stand-in for the reuse short-circuit: the real
    tool's ``metadata``, so the card and the telemetry label are unchanged, over an
    ``fn`` that returns the EXISTING layer instead of producing it again."""

    metadata: Any
    layer: LayerURI

    @property
    def fn(self) -> Any:
        layer = self.layer

        def _return_existing(**_ignored: Any) -> LayerURI:
            return layer

        return _return_existing


def _reusable_fetch_layer(
    state: SessionState, tool_name: str, params: dict
) -> LayerURI | None:
    """The layer already on this Case whose bytes THIS fetch would re-produce, by the cache key
    both address. ``None`` for an undeclared, uncacheable or unmatched source."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec
    from trid3nt_server.tools.fetchers._router.router import prospective_cache_key

    spec = get_spec(tool_name)
    if spec is None or state.emitter is None:
        return None
    handle = get_uri_registry(state.session_id).handle_for_cache_key(
        prospective_cache_key(spec, params)
    )
    if handle is None:
        return None
    for row in reversed(list(state.emitter.loaded_layers or [])):
        if getattr(row, "layer_id", None) != handle:
            continue
        # The case row is what survives a reopen, so the layer is rebuilt from
        # it; its legend rides along so the re-emit paints what it painted.
        return LayerURI(
            layer_id=row.layer_id,
            name=row.name,
            layer_type=row.layer_type,
            uri=row.uri,
            role=row.role,
            legend=row.legend,
        )
    return None


async def _invoke_tool_via_emitter(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
) -> Any:
    """The tool-call site: every registry ``fn(...)`` runs through this wrapper,
    so each transition emits a step, a ``LayerURI`` return re-emits session
    state, and a cancel propagates while any other exception becomes a wire code."""
    from trid3nt_server.inputs.gate.confirm import (
        _gate_on_code_exec,
        _gate_spec_for,
        _gate_with_turn_memory,
        _inject_secret_ref,
        _maybe_gate_on_payload_warning,
    )

    _ensure_emitter(websocket, state)
    if tool_name not in TOOL_REGISTRY:
        raise ToolNotFoundError(tool_name, list(TOOL_REGISTRY))
    entry = TOOL_REGISTRY[tool_name]

    # Snapshot the ORIGINAL call args NOW, before normalization, gating, URI-resolve and
    # secret-inject rewrite ``params``: the early input-only frame's args must equal the completion
    # frame's, so the card shows the same input the model sent both live and at completion.
    _original_tool_args = dict(params)

    async def _through_gate(
        gate: Awaitable[tuple[bool, dict]],
        decline: Callable[[dict], BaseException],
    ) -> dict:
        # Returns the approved params. Either refusal still gets a CARD: the step is minted and
        # ended through the emitter (DECLINED -> cancelled, unanswered -> failed). The trailing
        # raise keeps the gate FAIL-CLOSED even if the emitter returned instead of re-raising.
        try:
            should_run, effective = await gate
        except GateConfirmationTimeoutError as expired:
            refusal: BaseException = expired
        else:
            if should_run:
                return effective
            refusal = decline(effective)

        async def _refuse() -> Any:
            raise refusal

        await state.emitter.emit_tool_call(
            name=entry.metadata.name, tool_name=tool_name, invoke=_refuse
        )
        raise refusal

    # Bind this dispatch to the turn's Case ONCE, up front. The .qgs routing, tool-card persist, and
    # layer attribution below all use this capture -- a mid-dispatch ``case-command(select)`` must
    # not re-aim them at the newly visible Case (verified contamination).
    turn_case_id = _turn_case_id(state)

    # Drop ``case_id`` for tools that don't declare it -- defense in depth.
    # No registered tool declares one: the Case scoping a publish needs travels
    # to the emission seam through ``current_turn_case()``, not through params.
    if "case_id" in params:
        params = {k: v for k, v in params.items() if k != "case_id"}

    # Payload-warning gate. When the tool declares a ``payload_mb_estimator_name`` and the estimate
    # exceeds the warning threshold, emit ``tool-payload-warning`` and await
    # ``tool-payload-confirmation``. A cancel raises PayloadWarningCancelledError (non-retryable) so the model
    # reads a DECLINED result naming the card instead of an uninterpretable no_result.
    params = await _through_gate(
        _maybe_gate_on_payload_warning(websocket, state, tool_name, params),
        lambda _approved: PayloadWarningCancelledError(tool_name),
    )

    # run_pyqgis confirm gate: running arbitrary Python in the user's session is a consequential
    # action -- the user MUST approve the exact code first. A model-supplied confirmed/code_exec_id
    # is STRIPPED before gating (the gate is server-owned); approval re-injects them. Fail-closed:
    # a cancel raises a typed non-retryable decline, an unanswered card its own timeout.
    if tool_name == "run_pyqgis":
        params.pop("confirmed", None)
        params.pop("code_exec_id", None)
        params = await _through_gate(
            _gate_on_code_exec(websocket, state, params),
            lambda refused: CodeExecConfirmationCancelledError(
                refused.get("code_exec_id", "unknown")
            ),
        )

    # Centralized kwarg sweep: the model routinely invents kwargs that don't exist on our tools
    # (``run_name``, ``scenario_id``, ``return_period_years`` when the tool accepts
    # ``return_period_yr``, etc.). normalize_args rewrites aliases and drops the rest, never
    # raises, and runs BEFORE the solver-confirm gate and the reuse guard so both see canonical names.
    params = normalize_args(tool_name, params, entry.fn)

    # bbox AUTO-FILL. A tool whose signature REQUIRES a bbox-like param ('bbox' / 'aoi_bbox') that
    # the model OMITTED gets it injected here -- precedence: explicit arg > active canvas AOI > Case
    # bbox. Explicit model args are NEVER overridden; runs AFTER normalize_args and BEFORE the
    # reuse guards and AOI snaps.
    params = autofill_missing_bbox(
        tool_name,
        params,
        entry.fn,
        active_aoi=state.active_aoi_bbox,
        case_bbox=_turn_case_bbox(state),
    )

    # A repeat fetch that would land on a cache key a layer of this Case ALREADY carries is the same
    # bytes, so it hands back that layer instead of minting a second identical one on the map.
    # The key is exact (same source, same question, one TTL window); anything else fetches. A
    # truthy force_refetch/refetch/force is the escape hatch and is stripped before dispatch.
    _reuse_note: str | None = None
    _force_refetch = any(
        bool(params.get(k)) for k in ("force_refetch", "refetch", "force")
    )
    for _k in ("force_refetch", "refetch", "force"):
        params.pop(_k, None)
    # ``TRID3NT_FETCH_REUSE=0`` disables the short-circuit; the guard-control
    # strip above stays unconditional either way.
    if not _force_refetch and _env_flag("TRID3NT_FETCH_REUSE", True):
        # Off the loop: predicting the key runs the source's own pre-cache-key
        # resolve, which may reach the network.
        _existing = await asyncio.to_thread(
            _reusable_fetch_layer, state, tool_name, params
        )
        if _existing is not None:
            logger.info(
                "fetch-reuse[%s]: %s -> the layer already on the case "
                "(layer_id=%s); not re-fetching",
                state.session_id, tool_name, _existing.layer_id,
            )
            _reuse_note = (
                f"Reusing the layer already on the map for this request "
                f"(layer '{_existing.name}', handle={_existing.layer_id}) - the "
                "data was NOT re-fetched. A fit / zoom / resize reads this "
                "layer's own bbox off the case note; re-fetch only for a "
                "different area or an explicit refresh."
            )
            entry = _ReuseEntry(entry.metadata, _existing)

    # Confirmation-before-consequence, driven by the tool's declared gate spec; membership IS that
    # spec's presence, never a name set. A model-supplied ``confirmed`` is STRIPPED for a solver
    # gate (only an explicit user proceed injects it); the turn memory replays an earlier decision
    # for a same-tool, same-bbox retry rather than hanging on a second gate.
    _gate_spec = _gate_spec_for(tool_name)
    if _gate_spec is not None and not isinstance(entry, _ReuseEntry):
        if _gate_spec.kind == "solver":
            params.pop("confirmed", None)
        params = await _through_gate(
            _gate_with_turn_memory(websocket, state, tool_name, params),
            lambda _approved: SolverConfirmationCancelledError(tool_name),
        )

    # Layer-handle indirection kills the URI-mangling class: every URI-consuming param resolves
    # through the session registry - a known handle to its registered URI, an exact known URI
    # passes, a close mangle is substituted with a warning, and an unknown managed-bucket path
    # becomes a typed retryable error listing the real handles.
    uri_registry = get_uri_registry(state.session_id)
    params = uri_registry.resolve_params(tool_name, params)

    # Thread the session's key into a keyed tool's ``secret_ref``. A no-op for a
    # public tool; a keyed tool with no key anywhere refuses here by name rather
    # than dispatching a call that cannot succeed.
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
    # Capture the tool IO for the persisted tool-card row, since the live ``tool-io`` sidecar is
    # wire-only and is lost on reopen.
    _card_raw_args: Any = None
    _card_response: Any = None
    _card_io_error: bool = False

    # Mint a UNIQUE layer_id for every FRESHLY fetched layer: two layers from the same source would
    # otherwise share a source-derived id, and a client keying by layer_id skips the second add and
    # tears down the shared source on delete, so both vanish. Only a LIVE fetch mints (a Case
    # reopen keeps ids); a reuse short-circuit must not re-mint, or it orphans the live layer.
    _mint_unique_layer_id = not isinstance(entry, _ReuseEntry)

    def _restamp(value: Any) -> Any:
        if not _mint_unique_layer_id:
            return value
        if isinstance(value, LayerURI):
            return value.model_copy(update={"layer_id": new_ulid()})
        # Some tools return a list of layers, and the loaded-layer set dedups by source identity
        # rather than by layer_id, so two layers sharing a source-derived id would both persist and
        # then collide on delete. Every element is re-stamped and the sequence type preserved.
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
        # The completion-time ``tool-io`` frame carries both args and response, so without an early
        # frame the card shows nothing until the tool returns. The early frame carries only raw_args
        # on the same step_id, so the two merge on one card; best-effort.
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
        # A SYNCHRONOUS body runs in a worker thread so a slow tool cannot stall the WS keepalive;
        # the emit machinery stays on the loop. A reuse short-circuit returns an already-produced
        # layer synchronously and is not covered by the startup emit-free scan, so it is excluded.
        # A tool mis-classified as sync returns a coroutine from the thread, awaited on the loop.
        if (
            not isinstance(entry, _ReuseEntry)
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
        # Persist the replayable tool-card row so a reopen re-renders the inline card. Fires for
        # complete AND failed terminal states, before the narration row that closes the turn,
        # because created_at order IS the replay order.
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
        # Persist the Case layer accumulator in the FINALLY block: the layer is appended to the
        # accumulator BEFORE the emit, so persisting here captures it even when the post-invoke
        # emission raises on a dying socket. Never raises and never masks the original exception.
        if turn_case_id and state.emitter is not None:
            # Run the layer persist UNDER A SHIELD so a cancellation of the (possibly detached) turn
            # cannot interrupt the write of a fully computed layer: a bare ``await`` here re-raises
            # the pending cancel at the persist's first suspension point and SKIPS the write (a
            # solve could write every COG and persist no layers after a WS drop).
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

    # Persist the freshly minted short-handle map with the Case, so a reconnect or reopen resolves
    # the SAME handles the model already saw.
    await _persist_case_layer_handles(state, case_id=turn_case_id)

    # A composer's layer carries the FINAL floored AOI bbox, but the live zoom-to it fires never
    # lands in the turn's map commands on its own. Appended after any geocode snap so it is the LAST
    # zoom-to a re-entry replays; deduped against the last accumulated one.
    if isinstance(result, LayerURI) and as_bbox(result.bbox) is not None:
        _floored_bbox = list(result.bbox)
        if _last_zoom_to_bbox(state.current_turn_map_commands) != _floored_bbox:
            state.current_turn_map_commands.append(
                {"command": "zoom-to", "args": {"bbox": _floored_bbox}}
            )

    # PIN the Case AOI to the extent the run solved over: a completed workflow returns its own
    # record at the floored domain, which is the run's own extent, so every later fetch that states
    # no area fills from the ground the run covered. A reuse short-circuit pinned at first production.
    if (
        not isinstance(entry, _ReuseEntry)
        and hasattr(entry.fn, "workflow")
        and isinstance(result, LayerURI)
        and as_bbox(result.bbox) is not None
    ):
        try:
            await pin_case_aoi_from_solve(
                state, case_id=turn_case_id, bbox=result.bbox
            )
        except Exception:  # noqa: BLE001 -- the pin is a side effect, never break
            logger.debug("aoi-pin failed", exc_info=True)

    # On a reuse short-circuit the emitter has ALREADY re-loaded the existing layer onto the map, so
    # what remains is a function response naming the EXISTING result, so the model narrates
    # honestly instead of retrying.
    if _reuse_note is not None and isinstance(result, LayerURI):
        logger.info("fetch-reuse note=%s", _reuse_note)
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

def _is_layer_result(result: Any) -> bool:
    if isinstance(result, LayerURI):
        return True
    if isinstance(result, (list, tuple)):
        return any(isinstance(el, LayerURI) for el in result)
    return isinstance(result, dict) and result.get("status") == "reused_existing"

async def _emit_value_on_card(
    state: SessionState, tool_name: str, params: dict, result: Any
) -> None:
    """Put a returned VALUE on the dispatch card's ``tool-io`` frame, the frame a model-issued call
    fills from its summary. A layer reaches the map on its own, so a layer result emits nothing."""
    # The !run path has no model loop to fill the completion frame, so
    # without this a ``!run`` of a value-returning tool finishes its card with
    # the value nowhere on the wire.
    if result is None or _is_layer_result(result) or state.emitter is None:
        return
    step = state.emitter.last_tool_step
    if step is None or step.tool_name != tool_name:
        return
    await state.emitter.emit_tool_io(
        step_id=step.step_id,
        tool_name=tool_name,
        raw_args=_redact_secret_args(params),
        function_response=result,
        is_error=False,
    )

async def _dispatch_tool_and_persist(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    raw_user_text: str,
) -> None:
    """Invoke a tool, then persist the agent's reply to the active Case; the persisted content is a
    readable summary of the result. NOTHING MAY ESCAPE THIS FRAME."""
    # The !run path is a bare task with no awaiter: anything that escapes becomes an
    # unretrieved-task log line and the client gets nothing, so every failure leaves as an error envelope.
    # Entry-time Case capture keeps a mid-turn switch from re-aiming the writes.
    turn_case_id = _turn_case_id(state)
    bind_turn_case(turn_case_id)  # envelope tagging
    try:
        try:
            if tool_name in TOOL_REGISTRY:
                # A direct run opens the one gate's card for the inputs its
                # call left out or that refused; the card's proceed launches.
                _ensure_emitter(websocket, state)
                params = await review_run_inputs(
                    tool_name, TOOL_REGISTRY[tool_name].fn, params,
                    layers=[layer.model_dump(mode="json")
                            for layer in state.emitter.loaded_layers],
                    emitter=state.emitter)
            result = await _invoke_tool_via_emitter(
                websocket, state, tool_name, params
            )
            await _emit_value_on_card(state, tool_name, params, result)
        except asyncio.CancelledError:
            raise
        except ToolNotFoundError as exc:
            logger.info(
                "!run references unregistered tool "
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
        except UserDeclinedError as exc:
            # The user answered a gate card with cancel. Its own code is on the
            # wire's error list, so it goes out verbatim and is never logged as
            # a fault.
            logger.info(
                "!run declined at a gate card session=%s tool=%s "
                "code=%s",
                state.session_id,
                tool_name,
                exc.error_code,
            )
            await _send_error(
                websocket,
                state.session_id,
                exc.error_code,
                str(exc),
                retryable=exc.retryable,
            )
        except Exception as exc:  # noqa: BLE001 -- honesty-floor catch-all
            # Any OTHER tool exception on this no-awaiter path must still reach the client as a
            # structured envelope rather than silence. The wire error_code is a CLOSED set: an
            # invalid tool code leads the message as a [MARKER] under INTERNAL_ERROR; an upstream
            # provider failure goes out under UPSTREAM_API_ERROR with its own code leading.
            tool_code = getattr(exc, "error_code", None) or "TOOL_EXECUTION_FAILED"
            retryable = bool(getattr(exc, "retryable", False))
            if tool_code in _VALID_ERROR_CODES:
                wire_code, message = tool_code, str(exc)
            elif isinstance(exc, UpstreamAPIError):
                wire_code, message = "UPSTREAM_API_ERROR", f"[{tool_code}] {exc}"
            else:
                wire_code, message = "INTERNAL_ERROR", f"[{tool_code}] {exc}"
            logger.exception(
                "!run tool raised session=%s tool=%s code=%s",
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
        # End-of-turn idle signal on the !run path too. Best-effort.
        await _emit_turn_complete(
            websocket, state, pipeline_id=state.current_turn_pipeline_id
        )
