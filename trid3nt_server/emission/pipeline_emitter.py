"""PipelineEmitter - pipeline-state and session-state emission.

Owns one session's ``PipelineSnapshot`` and its ``loaded_layers`` accumulator.
REPLACE, never reconcile: every emission carries the full current state, and
there is no merge, update-partial or apply-delta helper here to break that.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.case import PersistedSubStepRecord
from trid3nt_contracts.collections import (
    PipelineSnapshot,
    PipelineStepSummary,
    ProjectLayerSummary,
)
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.ws import (
    Envelope,
    MapCommandPayload,
    PipelineStatePayload,
    PipelineStep,
    SessionStatePayload,
    SolveProgressPayload,
    ToolIoPayload,
)

from trid3nt_server.gates.context_budget import COMPACTING_LABEL, compaction_complete_label
from .layer_uri_emit import emit_layer_uri, publish_for_emission

__all__ = [
    "ErrorCodeRegistry",
    "EMITTER_ERROR_CODES",
    "EmitterError",
    "StepNotFoundError",
    "PipelineEmitter",
    "EmissionSink",
    "current_emitter",
    "dispatched_tool_name",
    "substep",
    "begin_substeps",
    "emit_chart_payloads",
    "ChartPersistHook",
    "ToolCardPersistHook",
    "bind_turn_case",
    "current_turn_case",
    "mint_dispatch_and_sim_cards",
    "route_sim_terminal",
    "mint_compaction_card",
    "complete_compaction_card",
]


#
# The turn's pinned Case is bound here at task entry, and EVERY envelope
# constructed inside the turn stamps ``Envelope.case_id`` from it, so the client
# routes live envelopes to the OWNING Case even when the user has since switched
# Cases. A ContextVar is per-task: concurrent turns cannot cross-tag.

_TURN_CASE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trid3nt_turn_case", default=None
)


def bind_turn_case(case_id: str | None) -> contextvars.Token:
    """Bind the turn's owning Case for envelope tagging; returns the token."""
    return _TURN_CASE.set(case_id)


def current_turn_case() -> str | None:
    """The Case bound to the current task's turn, or None outside a turn."""
    return _TURN_CASE.get()


#
# The user's rubber-band rectangle rides ``user-message`` as ``drawn_geometry``
# and is bound here per task, so a gate reads it without a new kwarg threaded
# down every dispatch path. Where one is present it is a ``basis="user"`` spatial
# knob and overrides the model's prompt-interpreted proposal. ``None`` means
# nothing was drawn this turn, which is the common case.

_TURN_DRAWN_GEOMETRY: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "trid3nt_turn_drawn_geometry", default=None
)


def bind_turn_drawn_geometry(geometry: dict | None) -> contextvars.Token:
    """Bind the turn's user-drawn geometry (dict) for gate consumption; returns
    the token."""
    return _TURN_DRAWN_GEOMETRY.set(geometry)


def current_turn_drawn_geometry() -> dict | None:
    """The drawn geometry bound to the current task's turn, or None."""
    return _TURN_DRAWN_GEOMETRY.get()


#
# ``emit_tool_call`` binds the active ``PipelineEmitter`` here for the lifetime of
# one tool or workflow invocation, so a workflow body can fire a transient map
# verb - a zoom-to the moment a geocode resolves, long before the solve returns.
# A ContextVar, because several sessions service tool calls concurrently in one
# process and a per-task binding never leaks across asyncio tasks.

_CURRENT_EMITTER: contextvars.ContextVar["PipelineEmitter | None"] = (
    contextvars.ContextVar("trid3nt_current_emitter", default=None)
)

#: The name of the TOP-LEVEL tool ``emit_tool_call`` is currently dispatching.
#: Bound alongside ``_CURRENT_EMITTER`` for the lifetime of one invocation so the
#: emit-on-fetch router seam can tell its two calling modes apart: a
#: DIRECT chat fetch is dispatched AS the tool (``dispatched_tool_name()`` == the
#: fetcher's own name -> the tool-wrapper already emits the returned LayerURI, so
#: the seam stays silent), whereas an IN-COMPOSER bare fetch runs nested under a
#: COMPOSER dispatch (``dispatched_tool_name()`` is the composer's name != the
#: fetcher -> the seam surfaces the fetched data as a role="context" input). A
#: bare/CI/direct call outside ``emit_tool_call`` leaves this ``None``.
_DISPATCHED_TOOL: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("trid3nt_dispatched_tool", default=None)
)


def current_emitter() -> "PipelineEmitter | None":
    """Return the ``PipelineEmitter`` bracketing the current tool/workflow call.
    ``None`` outside an ``emit_tool_call`` scope, and every caller must handle it:
    a transient verb is never a correctness gate.
    """
    return _CURRENT_EMITTER.get()


def dispatched_tool_name() -> str | None:
    """The top-level tool name ``emit_tool_call`` is dispatching (or ``None``).

    A nested call reads the name of its DISPATCHER here, not its own.
    """
    return _DISPATCHED_TOOL.get()


@asynccontextmanager
async def substep(emitter: "PipelineEmitter | None", raw_name: str):
    """No-op-safe wrapper over ``PipelineEmitter.substep``.
    Yields ``None`` and mints nothing when no emitter is bound, so a body reads
    the same whether or not the timeline is being surfaced.
    """
    if emitter is None:
        yield None
        return
    async with emitter.substep(raw_name) as child_id:
        yield child_id


def begin_substeps(emitter: "PipelineEmitter | None", total: int | None) -> None:
    """No-op-safe wrapper over ``PipelineEmitter.begin_substeps``.

    Declares the planned child count without a None-check at the call site.
    """
    if emitter is None:
        return
    emitter.begin_substeps(total)


async def emit_chart_payloads(payloads: Any) -> None:
    """Side-emit one or more chart-emission payloads via the current emitter.
    Takes one payload, a sequence, or ``None``; a ``None`` entry is SKIPPED,
    because a builder returns one when the series it needs is absent.
    """
    emitter = current_emitter()
    if emitter is None:
        return
    if isinstance(payloads, (list, tuple)):
        items = list(payloads)
    else:
        items = [payloads]
    for payload in items:
        if isinstance(payload, dict) and payload:
            await emitter.emit_chart(payload)


logger = logging.getLogger("trid3nt_server.emission.pipeline_emitter")


#
# The TERMINAL pipeline-state send can raise ConnectionClosed* on a dead or
# mid-cycling socket, which would abort the transition and LOSE the red/green
# card. ONLY the connection-closed class is swallowed on that path, never a real
# logic error, so the state transition itself always completes. The import is
# defensive (an empty tuple) so this module stays importable without websockets.
try:  # pragma: no cover -- import shape, not behavior
    from websockets.exceptions import (
        ConnectionClosedError,
        ConnectionClosedOK,
    )

    _CONNECTION_CLOSED_EXC: tuple[type[BaseException], ...] = (
        ConnectionClosedError,
        ConnectionClosedOK,
    )
except Exception:  # pragma: no cover -- websockets absent in a minimal env
    _CONNECTION_CLOSED_EXC = ()


#
# A tool or workflow can FAIL or be CANCELLED and still RETURN a value rather
# than raise - a killed solver run comes back as a terminal RunResult, and a
# composer that saw one returns a typed failed envelope. Without inspecting the
# RETURN value the wrapper falls through to mark_complete and paints a GREEN card
# on a dead solve, so the shapes below are recognised before that happens.

_FAILED_DICT_STATUSES = frozenset({"error", "failed", "cancelled"})


def _classify_tool_return(result: Any) -> tuple[str, str, str] | None:
    """Inspect a tool RETURN value for a non-success terminal outcome.
    ``(terminal_state, error_code, error_message)``, or ``None`` for a healthy
    shape. Deliberately conservative: anything ambiguous reads as success.
    """
    # Every shape below keys off STRUCTURE, never off a raised exception.

    def _from_workflow_name(wf: Any) -> tuple[str, str, str] | None:
        if isinstance(wf, str) and ":FAILED:" in wf:
            code = wf.split(":FAILED:", 1)[1].strip() or "MODEL_RUN_FAILED"
            state = "cancelled" if code.upper() == "CANCELLED" else "failed"
            return (state, code, f"workflow reported {code}")
        return None

    # --- Shape 1: a RunResult, or any object with the same terminal fields.
    # A non-"complete" status is the solver poll returning a killed or timed-out
    # run; "cancelled" maps to the cancelled card rather than the failed one.
    status = getattr(result, "status", None)
    if (
        isinstance(status, str)
        and not isinstance(result, dict)
        and hasattr(result, "run_id")
        and hasattr(result, "handle_id")
    ):
        if status == "complete":
            return None
        code = (
            getattr(result, "error_code", None)
            or (status.upper() if status else "SOLVER_FAILED")
        )
        message = (
            getattr(result, "error_message", None)
            or getattr(result, "cancellation_reason", None)
            or f"solver run {status}"
        )
        terminal = "cancelled" if status == "cancelled" else "failed"
        return (terminal, str(code), str(message))

    # --- Shape 2: an envelope whose ``workflow_name`` carries the ``:FAILED:``
    # infix. Checked BEFORE the generic dict branch so that infix is the
    # authoritative signal: such an envelope's own ``status`` field, where it has
    # one, is unrelated to the run outcome.
    if not isinstance(result, dict):
        wf_hit = _from_workflow_name(getattr(result, "workflow_name", None))
        if wf_hit is not None:
            return wf_hit

    # --- Shape 3: a dict whose ``status`` is one of the failed statuses.
    if isinstance(result, dict):
        wf_hit = _from_workflow_name(result.get("workflow_name"))
        if wf_hit is not None:
            return wf_hit
        dstatus = result.get("status")
        if isinstance(dstatus, str) and dstatus.lower() in _FAILED_DICT_STATUSES:
            code = result.get("error_code") or dstatus.upper()
            message = (
                result.get("error_message")
                or result.get("message")
                or result.get("error")
                or f"tool reported {dstatus}"
            )
            terminal = "cancelled" if dstatus.lower() == "cancelled" else "failed"
            return (terminal, str(code), str(message))

    return None




class ErrorCodeRegistry:
    """The SCREAMING_SNAKE_CASE error codes the emitter knows about.
    An OPEN set: a code can be registered at runtime, and the enumeration exists
    so a typo at a call site surfaces here rather than inventing a new code.
    """

    def __init__(self, initial: list[str] | None = None) -> None:
        self._codes: set[str] = set(initial or [])

    def register(self, code: str) -> str:
        """Register ``code`` if not present and return it. Idempotent.
        No shape validation here - the registry stays a passive set, and a
        malformed code raises later, where the summary is constructed.
        """
        self._codes.add(code)
        return code

    def known(self, code: str) -> bool:
        return code in self._codes

    def snapshot(self) -> list[str]:
        return sorted(self._codes)


#: Seed set of error codes the atomic tools + the cancel chain may emit.
#: Add new codes here (and at the call site) when a new failure mode lands.
EMITTER_ERROR_CODES = ErrorCodeRegistry(
    initial=[
        "UPSTREAM_API_ERROR",  # external HTTP API returned non-2xx / network failure
        "BBOX_INVALID",  # caller passed an unparseable / empty bbox
        "GEOCODE_NO_MATCH",  # geocode returned zero candidates
        "TOOL_NOT_FOUND",  # registry miss at the tool-call site
        "TOOL_PARAMS_INVALID",  # tool args failed validation
        "CANCELLED",  # the tool-call wrapper caught asyncio.CancelledError
        "INTERNAL_ERROR",  # uncategorized exception in the tool body
    ]
)


class EmitterError(RuntimeError):
    """Base class for emitter-internal errors. Distinct from tool errors."""


class StepNotFoundError(EmitterError):
    """``mark_*`` called with a step_id the emitter does not own."""




#: Type of the per-session sink the emitter pushes frames to. The sink is
#: ``async`` so the emitter can await ``websocket.send``; tests pass a sync
#: capture closure wrapped in an async lambda.
EmissionSink = Callable[[str], Awaitable[None]]

#: type of the optional chart-persistence hook ``server`` wires into
#: the emitter so a composer-side ``emit_chart`` persists a SessionChartRecord
#: through the SAME ``server._persist_chart_record(state, payload)`` the tool
#: path uses (the hook closes over ``state``; the emitter does not hold it).
ChartPersistHook = Callable[[dict], Awaitable[None]]

#: type of the optional sim/compute-card
#: Persistence hook for a terminal ``compute`` card, so the solve card persists
#: as a ``role="tool"`` chat row through the SAME path a plain tool card takes.
#: Without it a reconnect or Case reopen replays an empty pipeline and the
#: green/red solve card vanishes. ``None`` on a send-only path.
ToolCardPersistHook = Callable[..., Awaitable[None]]




def _now() -> datetime:
    """UTC ``datetime`` factory. Tests can patch via ``PipelineEmitter._now_fn``."""
    return datetime.now(timezone.utc)


def _elapsed_ms(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    """Wall-clock elapsed time in whole milliseconds.
    ``None`` without both endpoints, and clamped at 0 so clock skew never puts a
    negative duration on the wire.
    """
    if started_at is None or completed_at is None:
        return None
    delta = (completed_at - started_at).total_seconds() * 1000.0
    if delta < 0:
        return 0
    return int(round(delta))




def _json_for_tool_io(value: Any) -> tuple[str, bool, int]:
    """Serialize a tool-io field to ``(json_string, truncated, orig_bytes)``.
    Never raises: an unserializable value degrades to ``str()``. ``orig_bytes``
    is the ORIGINAL UTF-8 length, so a truncation can be reported honestly.
    """
    import json

    try:
        text = json.dumps(value, indent=2, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 -- last-resort: never raise on serialization
        text = str(value)
    orig_bytes = len(text.encode("utf-8"))
    cap = ToolIoPayload.MAX_FIELD_BYTES
    if orig_bytes <= cap:
        return text, False, orig_bytes
    # Truncate on the UTF-8 byte boundary, then decode back ignoring a split
    # multibyte tail so the JSON-ish prefix stays valid text (the UI shows it
    # as raw text + a truncation note, so it need not remain valid JSON).
    truncated = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
    return truncated, True, orig_bytes




def _fgb_bytes_to_geojson(fgb_bytes: bytes) -> dict[str, Any] | None:
    """Convert FlatGeobuf bytes to a GeoJSON FeatureCollection dict.

    ``None`` when the read fails; the output is always EPSG:4326.
    """
    import os
    import tempfile
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.warning("_fgb_bytes_to_geojson: geopandas missing: %s", exc)
        return None
    tmp_path: str | None = None
    try:
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".fgb", delete=False, prefix="trid3nt_inline_"
            ) as f:
                f.write(fgb_bytes)
                tmp_path = f.name
            gdf = gpd.read_file(tmp_path, engine="pyogrio")
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fgb_bytes_to_geojson: read failed: %s", exc)
        return None
    if gdf is None or len(gdf) == 0:
        return {"type": "FeatureCollection", "features": []}
    try:
        gdf = gdf[gdf.geometry.notna()]
    except Exception:  # noqa: BLE001
        pass
    try:
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif str(gdf.crs).upper() not in {"EPSG:4326", "WGS84"}:
            gdf = gdf.to_crs("EPSG:4326")
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fgb_bytes_to_geojson: CRS reproj failed: %s", exc)
    try:
        import json
        return json.loads(gdf.to_json())
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fgb_bytes_to_geojson: GeoJSON dump failed: %s", exc)
        return None


async def _read_vector_uri_as_geojson(uri: str) -> dict[str, Any] | None:
    """Read a vector layer uri and return it as a GeoJSON dict.
    ``.fgb``, ``.json`` and ``.geojson`` only; ``None`` on any failure. The read
    and the densify both run in a worker thread, never on the asyncio loop.
    """
    # s3:// reads go through the ONE object-store seam;
    # everything else is a local path read via fsspec.
    if "://" in uri:
        key = uri.split("://", 1)[1].split("/", 1)[-1]
    else:
        key = uri
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""

    # A session resume re-reads and re-densifies the SAME content-addressed
    # artifact per active-case vector layer, and even off-loop that repeated
    # simplify of tens of thousands of features saturates the box. The cached
    # value IS what the off-loop path would recompute, so a repeat read is an
    # O(1) hit with no repeat GET and no repeat simplify.
    cache_key = _densified_cache_key(uri)
    cached = _DENSIFIED_FC_CACHE_BY_URI.get(cache_key)
    if cached is not None:
        return cached

    def _read_and_parse() -> dict[str, Any] | None:
        try:
            if uri.startswith("s3://"):
                from trid3nt_server.workflows.solver.solver import _read_object_bytes

                data = _read_object_bytes(uri)
            else:
                # Local path (test / dev convenience).
                import fsspec  # type: ignore[import-not-found]
                with fsspec.open(uri, "rb") as f:
                    data = f.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_read_vector_uri_as_geojson: object read failed uri=%s: %s", uri, exc,
            )
            return None
        if ext == "fgb":
            obj = _fgb_bytes_to_geojson(data)
        elif ext in {"json", "geojson"}:
            try:
                import json
                obj = json.loads(data)
                if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
                    logger.warning(
                        "_read_vector_uri_as_geojson: not a FeatureCollection uri=%s", uri,
                    )
                    return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_read_vector_uri_as_geojson: JSON parse failed uri=%s: %s", uri, exc,
                )
                return None
        else:
            logger.warning(
                "_read_vector_uri_as_geojson: unsupported extension '%s' for uri=%s",
                ext, uri,
            )
            return None
        # The densify is CPU-heavy - a topology-preserving simplify plus a
        # feature cap over thousands of footprints - and MUST run here in the
        # executor thread, never back on the asyncio loop after the executor
        # returns: on the loop it blocks the WS keepalive.
        return _densify_off_loop(obj, uri)

    result = await asyncio.to_thread(_read_and_parse)
    if result is not None:
        # ONLY a successful read is cached: a transient object-store failure must
        # be retried, never pinned.
        _store_densified_fc(cache_key, result)
    return result


def _densify_off_loop(geojson_obj: Any, uri: str) -> Any:
    """Densify a just-read FeatureCollection and stamp the URI-keyed side-table.
    Runs INSIDE the worker thread, never on the asyncio loop. A densify failure
    falls through to the undensified FC: a render always beats a tag.
    """
    if not (isinstance(geojson_obj, dict)
            and geojson_obj.get("type") == "FeatureCollection"):
        return geojson_obj
    try:
        from trid3nt_server.tools.vector_tiles import densify_if_needed

        geojson_obj, _density_meta = densify_if_needed(geojson_obj, layer_id=uri)
        if _density_meta is not None:
            # FIFO-evict past the cap so this module-global side-table cannot grow
            # without limit in an always-on process; a dict preserves insertion
            # order, so the head is the oldest entry.
            if uri in _LAST_DENSITY_META_BY_URI:
                del _LAST_DENSITY_META_BY_URI[uri]
            _LAST_DENSITY_META_BY_URI[uri] = _density_meta
            while len(_LAST_DENSITY_META_BY_URI) > _MAX_DENSITY_META_ENTRIES:
                _LAST_DENSITY_META_BY_URI.pop(
                    next(iter(_LAST_DENSITY_META_BY_URI))
                )
    except Exception as exc:  # noqa: BLE001 -- never block a vector render
        logger.warning(
            "_read_vector_uri_as_geojson: densify failed uri=%s: %s", uri, exc,
        )
    return geojson_obj


#: The most-recent dense-vector ``DensifyMeta`` keyed by the vector artifact URI,
#: stashed here because the reader is a module function and the per-emitter table
#: is keyed by layer_id. Module scope is safe: the uri is content-addressed, so
#: two sessions reading the same artifact compute identical meta. FIFO-bounded at
#: the write site.
_MAX_DENSITY_META_ENTRIES: int = 256
_LAST_DENSITY_META_BY_URI: dict[str, Any] = {}


#: Cache of the DENSIFIED FeatureCollection, keyed by the content-addressed uri
#: folded with the densify params. The cached value is already capped to
#: MAX_INLINE_FEATURES, so an entry is bounded in size, and the map is FIFO-
#: bounded like the meta table above. Only the event loop reads or writes it -
#: the worker thread never touches it - so no cross-thread locking is needed.
_MAX_DENSIFIED_FC_CACHE_ENTRIES: int = 32
_DENSIFIED_FC_CACHE_BY_URI: dict[str, dict[str, Any]] = {}


def _densified_cache_key(uri: str) -> str:
    """Cache key for the densified output of one vector uri.
    The densify params are folded in, so a config change invalidates stale
    entries instead of serving a differently-simplified FeatureCollection.
    """
    try:
        from trid3nt_server.tools.vector_tiles import (
            DENSE_VECTOR_THRESHOLD,
            MAX_INLINE_FEATURES,
        )

        return f"{uri}|t={DENSE_VECTOR_THRESHOLD}|c={MAX_INLINE_FEATURES}"
    except Exception:  # noqa: BLE001 -- never let key-building block a read
        return uri


def _store_densified_fc(key: str, fc: dict[str, Any]) -> None:
    """Store a densified FC in the bounded FIFO cache; the oldest is evicted.

    A refreshed key is re-inserted at the tail, so recency survives the eviction.
    """
    if key in _DENSIFIED_FC_CACHE_BY_URI:
        del _DENSIFIED_FC_CACHE_BY_URI[key]
    _DENSIFIED_FC_CACHE_BY_URI[key] = fc
    while len(_DENSIFIED_FC_CACHE_BY_URI) > _MAX_DENSIFIED_FC_CACHE_ENTRIES:
        _DENSIFIED_FC_CACHE_BY_URI.pop(next(iter(_DENSIFIED_FC_CACHE_BY_URI)))


def _legend_for_layer_uri(uri: str | None) -> Any:
    """Lift the stashed ``LegendKey`` for a layer's uri, or ``None``.
    A publish returns a bare uri, so the legend travels beside it and is lifted
    back here. Fail-open: any error leaves the layer unstyled rather than absent.
    """
    if not uri:
        return None
    try:
        from trid3nt_server.emission.publish import pop_legend_for_uri

        return pop_legend_for_uri(uri)
    except Exception:  # noqa: BLE001 - legend lift is best-effort, never fatal
        return None


@dataclass
class _StepState:
    """Internal mutable record for one step.
    Materialized into the wire and persistence shapes on demand, and kept private
    so the public API exposes only the immutable snapshot models.
    """

    step_id: str
    name: str
    tool_name: str
    state: str = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    progress_percent: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: Authoritative wall-clock elapsed time in milliseconds.
    #: Stamped on the terminal transition from ``started_at``→``completed_at``;
    #: ``None`` while pending/running. Deterministic -- never an LLM estimate.
    duration_ms: int | None = None
    #: Two-card sim observability: card-kind discriminator + solver-run
    #: binding. ``role`` defaults to ``"tool"`` (the plain atomic-tool card --
    #: every existing step); ``"compute"`` is the solver card bound to a
    #: dispatched run. ``batch_job_id`` is the solver-dispatch backend's run id
    #: the compute card tracks; ``batch_status`` mirrors that backend's
    #: last-polled run status verbatim - never an LLM estimate. Both ids are
    #: ``None`` for a plain tool card, so the wire shape is unchanged.
    role: str = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    #: WHICH engine and which of its modules this compute card is a run of. Both
    #: ``None`` on a plain tool card, which is a run of nothing.
    engine: str | None = None
    module: str | None = None
    #: Durable-card lifecycle: the STABLE persisted
    #: ``message_id`` (a ULID) of this step's tool-card row. Set on a compute
    #: SOLVE step at mint so the card persisted ``running`` and the
    #: later terminal write target the SAME row (upsert in place -- no duplicate).
    #: ``None`` for a plain tool card (which persists once, at terminal, with a
    #: fresh appended id).
    card_message_id: str | None = None
    #: Nested sub-step timeline. ``parent_step_id`` is set on a CHILD
    #: step (a composer's internal atomic-tool call surfaced as a nested row);
    #: when set, the client nests this step under the parent and never renders it
    #: as a top-level card. ``substep_label`` / ``substep_index`` /
    #: ``substep_total`` are set on the PARENT and drive the live breadcrumb
    #: ("fetching topobathy 2/7"); they are CLEARED on the parent's terminal
    #: transition. All default None so a plain step is byte-identical on the wire.
    parent_step_id: str | None = None
    substep_label: str | None = None
    substep_index: int | None = None
    substep_total: int | None = None
    #: PARENT-only running tally of how many substeps have STARTED. Internal
    #: bookkeeping that drives ``substep_index``; never serialized directly.
    substep_started_count: int = 0


class PipelineEmitter:
    """Owns one session's pipeline snapshot and loaded_layers accumulator.
    Replace-not-reconcile, structurally: every ``_emit_*`` call serializes the
    FULL current snapshot, and there is no partial-update method to call instead.
    """

    #: Maximum length of an error_message. The schema enforces it; the emitter
    #: truncates defensively so a call site does not have to.
    ERROR_MESSAGE_MAX_LEN = 512

    #: Time factory; patched by tests for deterministic timestamps.
    _now_fn: Callable[[], datetime] = staticmethod(_now)

    def __init__(
        self,
        session_id: str,
        sink: EmissionSink,
        *,
        chat_history: list[dict] | None = None,
        pipeline_history: list[dict] | None = None,
        map_view: dict | None = None,
        chart_persist: "ChartPersistHook | None" = None,
        tool_card_persist: "ToolCardPersistHook | None" = None,
    ) -> None:
        self.session_id = session_id
        self._sink = sink

        #: Optional async hook so a composer-side ``emit_chart`` persists its
        #: record through the SAME path a tool-result chart takes, keeping the
        #: persist logic in one place. ``None`` on a send-only path.
        self._chart_persist: "ChartPersistHook | None" = chart_persist

        #: Optional async hook so a terminal compute card persists as a
        #: ``role="tool"`` chat row, matching a tool card's shape exactly, and
        #: therefore round-trips through the existing chat-history replay on both
        #: a case reopen and a bare reconnect. ``None`` on a send-only path.
        self._tool_card_persist: "ToolCardPersistHook | None" = tool_card_persist

        #: Current pipeline id; ``None`` when no pipeline is running.
        self._pipeline_id: str | None = None
        self._pipeline_started_at: datetime | None = None

        #: Internal ordered store of steps, keyed by step_id for fast updates.
        self._steps: dict[str, _StepState] = {}
        self._step_order: list[str] = []

        #: Session-state mirror fields (passed-through from the session record).
        self._chat_history: list[dict] = list(chat_history or [])
        self._pipeline_history: list[dict] = list(pipeline_history or [])
        self._map_view: dict | None = map_view

        #: Accumulated layers -- appended each time a tool returns a ``LayerURI``.
        self._loaded_layers: list[ProjectLayerSummary] = []

        #: The asyncio loop this emitter is bracketed on, captured at dispatch so
        #: an async surfacing coroutine can be driven from the worker thread an
        #: off-loaded sync fetcher runs in. ``None`` until the first dispatch.
        self._bound_loop: "asyncio.AbstractEventLoop | None" = None

        #: Session-level dedup of already-surfaced input uris: a fetched
        #: input is surfaced ONCE per session even if several composers re-fetch it.
        self._emitted_input_uris: set[str] = set()

        #: Monotonic stacking-order counter: every NEW layer is stamped with it,
        #: so layers carry a stable top-of-stack-is-highest order on the wire
        #: rather than an order the client has to invent. An in-place REPLACE
        #: reuses the superseded layer's z_index, so a re-publish keeps its slot
        #: instead of jumping to the top, and a reseed advances this counter past
        #: every seeded z_index so a later append cannot collide with one.
        self._next_z: int = 0

        #: Inline GeoJSON side-table for vector layers.
        #: Keyed by ``layer_id``; merged into ``loaded_layers`` wire payload
        #: in ``emit_session_state`` as additive ``inline_geojson`` field.
        #: Preserves ``ProjectLayerSummary`` extra="forbid" strictness.
        self._inline_geojson_by_layer_id: dict[str, dict[str, Any]] = {}

        #: Dense-vector density tags keyed by ``layer_id``. A vector layer that
        #: crossed the threshold and was simplified rides its meta out on the
        #: wire, so the client can state the degradation rather than hide it.
        #: Same lifecycle as the inline side-table above.
        self._density_meta_by_layer_id: dict[str, Any] = {}

        #: Terminal summary of the most recent dispatched step. Carries the
        #: AUTHORITATIVE ``started_at``/``duration_ms``, so a persisted card
        #: records exactly the duration the live card displayed and no second
        #: clock is ever consulted. Read-only outside the terminal transitions.
        self.last_tool_step: PipelineStepSummary | None = None

        #: The ordered CHILD substeps of the most-recent parent step, captured at
        #: the same terminal points as ``last_tool_step`` and while the children
        #: still exist: the pipeline is cleared BEFORE the persist hook runs, so
        #: this snapshot is the only durable copy of the nested timeline. ``[]``
        #: for a top-level dispatch with no children.
        self.last_tool_children: list[PersistedSubStepRecord] = []

        #: The step_id of the top-level step currently bracketing a dispatch. A
        #: substep mints its child against THIS id and stamps the parent's live
        #: breadcrumb. ``None`` outside a dispatched body.
        self._current_parent_step_id: str | None = None

        #: The most recent TERMINAL pipeline-state payload, replayed onto a
        #: reconnected socket so a rendered card survives a WS blip. ``None``
        #: until the first terminal transition.
        self._last_terminal_pipeline_payload: PipelineStatePayload | None = None


    def seed_chat_history(self, history: list[dict]) -> None:
        """Replace the chat-history mirror this emitter ships in session-state.
        A defensive COPY: the caller's list cannot later mutate the mirror. An
        emitter that is never seeded keeps the history it was constructed with.
        """
        self._chat_history = list(history or [])


    def rebind_sink(self, sink: EmissionSink) -> None:
        """Swap the wire sink this emitter pushes frames to, and replay onto it.
        Never raises out of the rebind: the replay is best-effort, because the new
        socket may itself have just cycled.
        """
        # One emitter drives a long-running turn, and its sink closes over the
        # socket that LAUNCHED it; when that socket dies the sink silently drops
        # every later frame. Rebinding puts the still-running turn's progress and
        # its terminal frame back on the user's live connection.
        #
        # A replay is needed as well as the swap: a terminal frame already emitted
        # onto the dead socket is never repainted by a turn that emits nothing
        # further. An OPEN pipeline replays a FULL snapshot of every step in its
        # current state, because the single terminal stash carries only the last
        # terminal card and would leave a dropped running card unpainted; the
        # stash is the fallback when no pipeline is open.
        self._sink = sink
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, so nothing to schedule. The snapshot stays stashed;
            # a later emit still carries the full view, and a loop-bound rebind
            # replays it.
            return
        if self._pipeline_id is not None and self._step_order:
            # An OPEN pipeline: replay the FULL live snapshot so any dropped
            # SETUP/dispatch running cards (the non-terminal frame a dead sink
            # would swallow in ``_emit_pipeline_state``) repaint on the new
            # socket. Built the
            # SAME way the terminal payload is built -- _to_wire_step over the
            # whole _step_order -- so every step ships in its CURRENT state
            # (pending / running / complete / failed).
            snapshot = PipelineStatePayload(
                pipeline_id=self._pipeline_id,
                steps=[self._to_wire_step(sid) for sid in self._step_order],
            )
            loop.create_task(self._replay_pipeline_snapshot(snapshot))
            return
        # No open pipeline: fall back to the last terminal stash
        # so a RENDERED/terminal card still survives a WS blip.
        terminal = self._last_terminal_pipeline_payload
        if terminal is None:
            return
        loop.create_task(self._replay_terminal_pipeline_state(terminal))

    async def _replay_pipeline_snapshot(
        self, payload: PipelineStatePayload
    ) -> None:
        """Replay a FULL live pipeline-state snapshot onto the rebound sink.
        Idempotent with any later terminal replay: the client replaces a live
        pipeline wholesale by ``pipeline_id``. Only a closed socket is swallowed.
        """
        try:
            await self._send("pipeline-state", payload)
        except _CONNECTION_CLOSED_EXC:  # type: ignore[misc]
            logger.debug(
                "emitter: live pipeline-state snapshot replay failed on the "
                "rebound socket (best-effort drop) session=%s pipeline_id=%s",
                self.session_id,
                self._pipeline_id,
            )

    async def _replay_terminal_pipeline_state(
        self, payload: PipelineStatePayload
    ) -> None:
        """Replay a stashed terminal pipeline-state onto the rebound sink.
        Only a closed socket is swallowed - the new sink may also be mid-cycle,
        and the card then replays on the NEXT rebind.
        """
        try:
            await self._send("pipeline-state", payload)
        except _CONNECTION_CLOSED_EXC:  # type: ignore[misc]
            logger.debug(
                "emitter: terminal pipeline-state replay failed on the rebound "
                "socket (best-effort drop) session=%s pipeline_id=%s",
                self.session_id,
                self._pipeline_id,
            )

    # Snapshot accessors (read-only views; tests + integrations introspect)

    @property
    def pipeline_id(self) -> str | None:
        return self._pipeline_id

    @property
    def loaded_layers(self) -> list[ProjectLayerSummary]:
        """Return a defensive shallow copy of the current loaded_layers list."""
        return list(self._loaded_layers)

    async def set_layer_visible(self, layer_id: str, visible: bool) -> bool:
        """Take a published layer off the canvas, or put it back.
        False when this session never loaded that layer - hiding what nobody
        published is a refusal rather than a no-op.
        """
        for summary in self._loaded_layers:
            if summary.layer_id == layer_id:
                if summary.visible != visible:
                    summary.visible = visible
                    await self.emit_session_state()
                return True
        return False

    def reset_loaded_layers(self, layers: list[dict] | None) -> None:
        """Replace the in-memory loaded layers from a persisted snapshot.
        ``None`` or ``[]`` FLUSHES. A malformed entry is skipped rather than
        rolled back, and nothing is emitted: the caller chooses when to send.
        """
        if not layers:
            self._loaded_layers = []
            # A flush (new Case) restarts the stacking counter.
            self._next_z = 0
            # flush inline side-table alongside loaded_layers.
            self._inline_geojson_by_layer_id.clear()
            # flush the dense-vector density tags too.
            self._density_meta_by_layer_id.clear()
            return
        seeded: list[ProjectLayerSummary] = []
        for layer_dict in layers:
            if not isinstance(layer_dict, dict):
                continue
            try:
                seeded.append(ProjectLayerSummary.model_validate(layer_dict))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "reset_loaded_layers: skipping malformed layer dict"
                )
                continue
        self._loaded_layers = seeded
        # Resume the monotonic counter PAST any seeded slot so a later append
        # cannot collide with a persisted layer's z_index. A snapshot carrying no
        # z_index at all leaves the counter at 0.
        _seeded_z = [s.z_index for s in seeded if s.z_index is not None]
        self._next_z = (max(_seeded_z) + 1) if _seeded_z else 0
        # keep only inline entries that match a still-loaded layer.
        active_ids = {layer.layer_id for layer in seeded}
        self._inline_geojson_by_layer_id = {
            k: v for k, v in self._inline_geojson_by_layer_id.items() if k in active_ids
        }
        # prune density tags to the still-loaded layers too.
        self._density_meta_by_layer_id = {
            k: v for k, v in self._density_meta_by_layer_id.items() if k in active_ids
        }

    def merge_loaded_layers_from(self, other: "PipelineEmitter") -> int:
        """Union ``other``'s in-memory loaded layers into THIS emitter.
        Union by uri and ``layer_id``, so a layer this emitter already holds is
        never duplicated. Returns the count of newly-merged layers.
        """
        # A rebind replays the pipeline CARDS but not the loaded-layers frame, so
        # a layer published after the launch socket died and before the reconnect
        # went out onto a dead sink and was dropped, while the new connection's
        # emitter is fresh and empty. Seeding from the live turn's emitter makes
        # the reconnect's own session-state carry the full live snapshot, so the
        # terminal layer survives the blip whatever the persist timing.
        if other is self or not other._loaded_layers:
            return 0
        existing_keys = {l.uri for l in self._loaded_layers}
        existing_ids = {l.layer_id for l in self._loaded_layers}
        merged = 0
        for layer in other._loaded_layers:
            key = layer.uri
            if key in existing_keys or layer.layer_id in existing_ids:
                continue
            new_layer = layer.model_copy(deep=True)
            if new_layer.z_index is None:
                new_layer.z_index = self._alloc_z()
            else:
                self._next_z = max(self._next_z, new_layer.z_index + 1)
            self._loaded_layers.append(new_layer)
            existing_keys.add(key)
            existing_ids.add(new_layer.layer_id)
            ig = other._inline_geojson_by_layer_id.get(layer.layer_id)
            if ig is not None:
                self._inline_geojson_by_layer_id[new_layer.layer_id] = ig
            dm = other._density_meta_by_layer_id.get(layer.layer_id)
            if dm is not None:
                self._density_meta_by_layer_id[new_layer.layer_id] = dm
            merged += 1
        return merged

    async def reinline_vector_layers(self) -> int:
        """Rebuild the inline-GeoJSON side-table for persisted vector layers.
        The side-table is in-memory only, so a reopened Case seeds its layers
        without payloads. Best-effort per layer; returns how many were re-inlined.
        """
        count = 0
        for layer in self._loaded_layers:
            if layer.layer_type != "vector":
                continue
            if layer.layer_id in self._inline_geojson_by_layer_id:
                continue
            uri = layer.uri or ""
            if not uri:
                continue
            try:
                geojson_obj = await _read_vector_uri_as_geojson(uri)
            except Exception:  # noqa: BLE001 -- per-layer best-effort
                logger.warning(
                    "reinline_vector_layers: read failed layer_id=%s uri=%s",
                    layer.layer_id,
                    uri,
                )
                continue
            if geojson_obj is not None:
                self._inline_geojson_by_layer_id[layer.layer_id] = geojson_obj
                # Lift any density tag from the uri-keyed stash into the
                # layer_id-keyed map, so a re-inlined layer is stamped too.
                _meta = _LAST_DENSITY_META_BY_URI.get(uri)
                if _meta is not None:
                    self._density_meta_by_layer_id[layer.layer_id] = _meta
                count += 1
        return count

    def current_snapshot(self) -> PipelineSnapshot | None:
        """The current ``PipelineSnapshot``, or ``None`` when none is running.

        A whole snapshot every time - there is no partial view of a pipeline.
        """
        if self._pipeline_id is None or not self._step_order:
            return None
        final_state: str | None = None
        if all(self._steps[sid].state == "complete" for sid in self._step_order):
            final_state = "complete"
        elif any(self._steps[sid].state == "failed" for sid in self._step_order):
            final_state = "failed"
        elif any(self._steps[sid].state == "cancelled" for sid in self._step_order):
            final_state = "cancelled"
        completed_at = (
            self._now_fn() if final_state is not None else None
        )
        return PipelineSnapshot(
            pipeline_id=self._pipeline_id,
            started_at=self._pipeline_started_at or self._now_fn(),
            completed_at=completed_at,
            final_state=final_state,  # type: ignore[arg-type]
            steps=[self._to_summary(sid) for sid in self._step_order],
        )


    def start_pipeline(self) -> str:
        """Open a fresh pipeline. Returns the new ``pipeline_id``.
        Optional: ``add_step`` auto-opens one. Calling it explicitly stamps
        ``current_pipeline`` at a moment the caller chooses.
        """
        self._pipeline_id = new_ulid()
        self._pipeline_started_at = self._now_fn()
        self._steps.clear()
        self._step_order.clear()
        return self._pipeline_id

    def close_pipeline(self) -> None:
        """Archive the current snapshot into history and clear the pipeline.
        Idempotent. The closed snapshot lives on as a history entry, so a resume
        can replay it.
        """
        if self._pipeline_id is None:
            return
        snap = self.current_snapshot()
        if snap is not None:
            self._pipeline_history.append(snap.model_dump(mode="json"))
        self._pipeline_id = None
        self._pipeline_started_at = None
        self._steps.clear()
        self._step_order.clear()

    async def add_step(self, name: str, tool_name: str) -> str:
        """Append a new ``pending`` step and emit a fresh pipeline-state.

        Auto-opens a pipeline when none is open. Returns the new ``step_id``.
        """
        if self._pipeline_id is None:
            self.start_pipeline()
        step_id = new_ulid()
        self._steps[step_id] = _StepState(
            step_id=step_id, name=name, tool_name=tool_name
        )
        self._step_order.append(step_id)
        await self._emit_pipeline_state()
        return step_id

    async def mark_running(
        self, step_id: str, *, progress_percent: int | None = None
    ) -> None:
        """Flip ``step_id`` to ``running``, stamp ``started_at``, emit."""
        step = self._require_step(step_id)
        step.state = "running"
        step.started_at = self._now_fn()
        if progress_percent is not None:
            step.progress_percent = self._coerce_progress(progress_percent)
        await self._emit_pipeline_state()

    async def update_progress(self, step_id: str, progress_percent: int) -> None:
        """Bump ``progress_percent`` on a running step; emit.
        The value is workflow-attributed, passed in by the caller, and is never
        an estimate the model produced.
        """
        step = self._require_step(step_id)
        step.progress_percent = self._coerce_progress(progress_percent)
        await self._emit_pipeline_state()

    async def update_current_progress(self, progress_percent: int) -> None:
        """Bump ``progress_percent`` on the CURRENTLY-running step; emit.
        For a body that holds the emitter but not the step_id. Targets the most
        recently added running step, and is a no-op when none is running.
        """
        running = [
            sid for sid in self._step_order if self._steps[sid].state == "running"
        ]
        if not running:
            return
        step = self._steps[running[-1]]
        step.progress_percent = self._coerce_progress(progress_percent)
        await self._emit_pipeline_state()

    # Nested sub-step timeline -- composer-internal atomic-tool
    # calls surfaced as CHILD rows nested under the parent workflow card.

    def begin_substeps(self, total: int | None) -> None:
        """Declare the planned child count for the live breadcrumb.
        ``None``, or a non-positive count, degrades the breadcrumb to label plus
        index. Emits nothing itself: the plan rides the next transition.
        """
        parent_id = self._current_parent_step_id
        if parent_id is None:
            return
        parent = self._steps.get(parent_id)
        if parent is None:
            return
        if total is not None and isinstance(total, int) and total >= 1:
            parent.substep_total = int(total)
        else:
            parent.substep_total = None

    @asynccontextmanager
    async def substep(self, raw_name: str):
        """Surface ONE internal call as a CHILD step under the current parent.
        Yields the child ``step_id``, or ``None`` when no parent is bound, in
        which case nothing is minted. Any exception is re-raised unchanged.
        """
        parent_id = self._current_parent_step_id
        if parent_id is None:
            # No parent bound -> no-op: yield None, mint nothing.
            yield None
            return
        parent = self._steps.get(parent_id)
        if parent is None:
            yield None
            return

        # Mint the child against the parent and stamp the parent breadcrumb.
        child_id = await self.add_step(name=raw_name, tool_name=raw_name)
        child = self._steps[child_id]
        child.parent_step_id = parent_id
        parent.substep_started_count += 1
        parent.substep_label = raw_name
        parent.substep_index = parent.substep_started_count
        # This emits a fresh pipeline-state carrying BOTH the running child and
        # the parent's updated breadcrumb - the frame is never split in two.
        await self.mark_running(child_id)

        try:
            yield child_id
        except asyncio.CancelledError:
            # Cancelled is distinct from failed. The CHILD is marked cancelled
            # and the error re-raised; the parent's breadcrumb stands while the
            # control flow unwinds to the parent's own terminal transition.
            await self.mark_cancelled(child_id)
            raise
        except Exception as exc:  # noqa: BLE001 -- classify-and-re-raise
            code, message = self._classify_exception(exc)
            # A failed child is RED and the parent is NOT touched: only the
            # parent's own terminal transition may colour the parent.
            await self.mark_failed(child_id, error_code=code, error_message=message)
            raise
        else:
            await self.mark_complete(child_id)


    async def add_compute_step(
        self,
        *,
        name: str,
        tool_name: str,
        batch_job_id: str,
        batch_status: str | None = None,
        engine: str | None = None,
        module: str | None = None,
    ) -> str:
        """Append a ``role="compute"`` step bound to a dispatched solver run; emit.
        Lands RUNNING immediately, so the card shows motion while the solver runs
        unheard from. ``batch_status`` mirrors the backend verbatim, never an estimate.
        """
        step_id = await self.add_step(name=name, tool_name=tool_name)
        step = self._require_step(step_id)
        step.role = "compute"
        step.batch_job_id = batch_job_id
        step.batch_status = batch_status
        step.engine = engine
        step.module = module
        # Pin a STABLE persisted row id NOW, so the row written at mint and the
        # later terminal write upsert the SAME row - running to terminal in place,
        # never as two cards.
        step.card_message_id = new_ulid()
        await self.mark_running(step_id)
        return step_id

    async def add_durable_step(self, *, name: str, tool_name: str) -> str:
        """Append a ``role="tool"`` step that starts RUNNING and is durable.
        For a server-internal action with NO dispatched run to bind to, so it
        carries none of the compute card's run fields.
        """
        step_id = await self.add_step(name=name, tool_name=tool_name)
        step = self._require_step(step_id)
        # Pin a STABLE persisted row id NOW so the running write and the later
        # terminal write upsert the SAME row.
        step.card_message_id = new_ulid()
        await self.mark_running(step_id)
        return step_id

    def rename_step(self, step_id: str, *, name: str) -> None:
        """Patch a step's display ``name`` in place; a no-op on an unknown id.
        For a card whose terminal label depends on data known only once the work
        completes; the client renders ``name`` verbatim and never rephrases it.
        """
        step = self._steps.get(step_id)
        if step is None:
            return
        step.name = name

    async def update_compute_status(
        self, step_id: str, batch_status: str
    ) -> None:
        """Patch a compute step's ``batch_status`` and re-emit; best-effort.
        Mirrors the backend verbatim, never an estimate. A no-op on an unknown id
        or an unchanged status, and it never alters the step's own ``state``.
        """
        step = self._steps.get(step_id)
        if step is None:
            return
        if step.batch_status == batch_status:
            return
        step.batch_status = batch_status
        await self._emit_pipeline_state()

    def _clear_parent_breadcrumb(self, step: _StepState) -> None:
        """Clear the live-breadcrumb fields on a PARENT's terminal transition.
        Only the parent's own breadcrumb line clears: its child rows keep their
        state, and a step that ran no substeps has nothing set to clear.
        """
        step.substep_label = None
        step.substep_index = None
        step.substep_total = None

    async def mark_complete(self, step_id: str) -> None:
        """Flip ``step_id`` to ``complete``, stamp ``completed_at``, emit."""
        step = self._require_step(step_id)
        step.state = "complete"
        step.completed_at = self._now_fn()
        self._clear_parent_breadcrumb(step)
        # The AUTHORITATIVE wall-clock duration, stamped on the terminal
        # transition. The client locks its cosmetic ticker to this number once it
        # arrives, so nothing downstream measures the step a second time.
        step.duration_ms = _elapsed_ms(step.started_at, step.completed_at)
        # The terminal emit is best-effort on a dead socket and snapshots itself
        # for replay, so the green card survives a WS cycle.
        await self._emit_terminal_pipeline_state()

    async def mark_failed(
        self, step_id: str, error_code: str, error_message: str
    ) -> None:
        """Flip ``step_id`` to ``failed``; record error_code and error_message.
        An unseen ``error_code`` is registered here, and the message is truncated;
        the code's shape is enforced where the summary is built, not again here.
        """
        step = self._require_step(step_id)
        EMITTER_ERROR_CODES.register(error_code)
        step.state = "failed"
        step.completed_at = self._now_fn()
        self._clear_parent_breadcrumb(step)
        # A failed card shows its final duration too. ``started_at`` may be None
        # when the step failed before it ever ran, and the duration is then None.
        step.duration_ms = _elapsed_ms(step.started_at, step.completed_at)
        step.error_code = error_code
        step.error_message = self._truncate_message(error_message)
        # The terminal emit is best-effort on a dead socket and snapshots itself
        # for replay, so the red card survives a WS cycle.
        await self._emit_terminal_pipeline_state()

    async def mark_cancelled(self, step_id: str) -> None:
        """Flip ``step_id`` to ``cancelled``; emit.
        A cancelled step is DISTINCT from a failed one, and the cancel chain
        calls this before the ``asyncio.CancelledError`` is re-raised.
        """
        step = self._require_step(step_id)
        step.state = "cancelled"
        step.completed_at = self._now_fn()
        self._clear_parent_breadcrumb(step)
        # Cancelled is terminal, so the duration is stamped and the yellow card
        # locks to the elapsed-before-cancel time rather than ticking forever.
        step.duration_ms = _elapsed_ms(step.started_at, step.completed_at)
        # The terminal emit is best-effort on a dead socket and snapshots itself
        # for replay, so the yellow card survives a WS cycle.
        await self._emit_terminal_pipeline_state()

    async def _persist_step_card(
        self, step_id: str, *, states: tuple[str, ...]
    ) -> None:
        """Persist ``step_id``'s tool-card row IFF its state is in ``states``.
        A no-op when no persist hook is bound, the step is unknown, or its state
        is not listed; a hook failure is swallowed and never raises.
        """
        # ``card_message_id``, where a step carries one, is the STABLE row id, so
        # the running write and the later terminal write UPSERT the SAME row -
        # running to terminal in place, never two cards. A step without one is
        # appended fresh, which is the persist-once-at-terminal shape.
        if self._tool_card_persist is None:
            return
        step = self._steps.get(step_id)
        if step is None:
            return
        if step.state not in states:
            return
        started_at = step.started_at or self._now_fn()
        duration_ms = step.duration_ms if step.duration_ms is not None else 0
        try:
            await self._tool_card_persist(
                tool_name=step.tool_name,
                label=step.name,
                card_state=step.state,
                started_at_fallback=started_at,
                duration_ms_fallback=duration_ms,
                message_id=step.card_message_id,
            )
        except Exception as exc:  # noqa: BLE001 -- persistence, never break solve
            logger.warning(
                "_persist_step_card failed (non-fatal) step=%s: %s",
                step_id,
                exc,
            )

    async def persist_running_compute_card(self, step_id: str) -> None:
        """Persist the compute card the MOMENT it is minted, still running.
        Without a running row, a reconnect mid-solve replays an empty pipeline and
        the spinning card vanishes. A no-op outside the ``running`` state.
        """
        await self._persist_step_card(step_id, states=("running",))

    async def persist_terminal_compute_card(self, step_id: str) -> None:
        """Drive the compute card's persisted row to its TERMINAL state.
        UPSERTS the row minted at running, ``cancelled`` included: a stopped solve
        is a finished solve, and leaving an orphaned running row would deny it.
        """
        await self._persist_step_card(
            step_id, states=("complete", "failed", "cancelled")
        )

    async def persist_terminal_dispatch_card(self, step_id: str) -> None:
        """Persist the DISPATCH card: terminal, appended once.
        It carries no stable row id - it completes instantly at mint - so this
        appends one durable row, pairing it with the compute card on a replay.
        """
        await self._persist_step_card(step_id, states=("complete", "failed"))


    def _alloc_z(self) -> int:
        """Return the next monotonic ``z_index`` and advance the counter.
        The single source of new stacking slots, so no in-use slot is reissued.
        """
        z = self._next_z
        self._next_z += 1
        return z

    async def add_loaded_layer(self, layer: LayerURI) -> None:
        """Append a ``LayerURI`` to ``loaded_layers`` and emit a fresh frame.
        DEDUP BY URI: one store and one scheme mean two publishes of the same COG
        name the same uri, so the fresher row REPLACES the older in place.
        """
        # RESOLVED STYLE carry-over, so the range, the ramp and the .qml reach the
        # client: a layer may carry its legend directly, and where a publish
        # returned a bare uri the legend is lifted out of the stash by that uri.
        # ``None`` means the layer reaches the map unstyled.
        _legend = getattr(layer, "legend", None) or _legend_for_layer_uri(layer.uri)
        summary = ProjectLayerSummary(
            layer_id=layer.layer_id,
            name=layer.name,
            layer_type=layer.layer_type,
            uri=layer.uri,
            visible=True,
            role=layer.role,
            temporal=layer.valid_from is not None,
            legend=_legend,
            # The quantity travels with the layer: a title is prose a producer
            # may rewrite, and matching one field's still to its animation by
            # prose is how two scales for one quantity get shipped.
            quantity=getattr(layer, "quantity", None),
            # Mesh CRS: carry the LayerURI's crs_authid onto the WS
            # row so the plugin's _add_mesh can setCrs() an MDAL mesh whose
            # native crs() is empty. None for raster/vector (byte-for-byte
            # unchanged).
            crs_authid=getattr(layer, "crs_authid", None),
            # Mesh reference time: the instant the mesh's seconds are counted
            # from, so the plugin's temporal stamp reads the run's own clock.
            reference_time=getattr(layer, "reference_time", None),
            # One frame of a sequence states its own validity window, so the map
            # stamps a fixed temporal range without reading it back out of a name.
            valid_from=getattr(layer, "valid_from", None),
            valid_to=getattr(layer, "valid_to", None),
        )
        # Dedup by uri -- in-place replace if present, else append.
        for i, existing in enumerate(self._loaded_layers):
            if existing.uri == summary.uri:
                # Drop the SUPERSEDED layer_id's side tables (inline GeoJSON /
                # density meta) so a merge cannot leave an orphan keyed on the
                # old id. No-op for raster flood layers (no inline GeoJSON).
                if existing.layer_id != summary.layer_id:
                    self._inline_geojson_by_layer_id.pop(existing.layer_id, None)
                    self._density_meta_by_layer_id.pop(existing.layer_id, None)
                # REUSE the superseded layer's slot so a re-publish (a styled
                # row superseding a styleless one) keeps its stacking position
                # instead of jumping to the top. Falls back to a fresh slot only
                # if the old row never carried one (a persisted layer seeded
                # without a z_index).
                summary.z_index = (
                    existing.z_index
                    if existing.z_index is not None
                    else self._alloc_z()
                )
                self._loaded_layers[i] = summary
                break
        else:
            # A brand-new layer takes the next monotonic slot --
            # top of the stack (highest z_index) is the most-recently-added.
            summary.z_index = self._alloc_z()
            self._loaded_layers.append(summary)
        # Vector inline-GeoJSON. Best-effort; failure is non-fatal.
        # Logs loudly so the audit can grep for "inlined GeoJSON layer_id=...".
        if layer.layer_type == "vector":
            try:
                geojson_obj = await _read_vector_uri_as_geojson(layer.uri)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "add_loaded_layer: inline GeoJSON conversion failed for "
                    "layer_id=%s uri=%s; falling back to URI-only delivery: %s",
                    layer.layer_id, layer.uri, exc,
                )
                self._inline_geojson_by_layer_id.pop(layer.layer_id, None)
            else:
                if geojson_obj is not None:
                    self._inline_geojson_by_layer_id[layer.layer_id] = geojson_obj
                    feat_count = len(geojson_obj.get("features") or [])
                    logger.info(
                        "add_loaded_layer: inlined GeoJSON layer_id=%s features=%d",
                        layer.layer_id, feat_count,
                    )
                    # lift any dense-vector density tag (keyed by uri in the
                    # module stash) into the per-emitter map (keyed by layer_id)
                    # so the wire layer carries the honest simplified/capped tag.
                    _meta = _LAST_DENSITY_META_BY_URI.get(layer.uri)
                    if _meta is not None:
                        self._density_meta_by_layer_id[layer.layer_id] = _meta
                    else:
                        self._density_meta_by_layer_id.pop(layer.layer_id, None)
        await self.emit_session_state()
        # Emit zoom-to map-command when the LayerURI carries a bbox.
        if layer.bbox is not None:
            await self.emit_map_command(
                "zoom-to",
                {"bbox": list(layer.bbox)},
            )

    async def emit_session_state(self) -> None:
        """Emit a full ``session-state`` envelope.
        A vector layer holding an inline GeoJSON entry carries it out on the wire
        as an additive field over the strict schema.
        """
        snap = self.current_snapshot()
        # Build loaded_layers dump with inline_geojson merged in.
        loaded_dump_with_inline: list[dict[str, Any]] = []
        for _layer in self._loaded_layers:
            _d = _layer.model_dump(mode="json")
            _inline = self._inline_geojson_by_layer_id.get(_layer.layer_id)
            if _inline is not None:
                _d["inline_geojson"] = _inline
            # stamp the dense-vector density tag (additive, like
            # inline_geojson) so the client can surface "simplified for
            # performance" honestly. Best-effort; a malformed meta is skipped.
            _meta = self._density_meta_by_layer_id.get(_layer.layer_id)
            if _meta is not None:
                try:
                    _d.update(_meta.as_wire_tag())
                except Exception:  # noqa: BLE001
                    pass
            loaded_dump_with_inline.append(_d)
        payload = SessionStatePayload(
            chat_history=list(self._chat_history),
            loaded_layers=loaded_dump_with_inline,
            pipeline_history=list(self._pipeline_history),
            current_pipeline=(snap.model_dump(mode="json") if snap is not None else None),
            map_view=self._map_view,
        )
        await self._send("session-state", payload)

    async def emit_map_command(self, command: str, args: dict) -> None:
        """Emit a ``map-command`` envelope: a TRANSIENT verb, never state.
        Anything that changes the layer set rides ``session-state`` instead, so a
        replayed command can never resurrect a layer.
        """
        payload = MapCommandPayload(command=command, args=args)  # type: ignore[arg-type]
        await self._send("map-command", payload)

    async def emit_solve_progress(self, progress: dict) -> None:
        """Emit a ``solve-progress`` envelope: live telemetry off a running solve.
        Best-effort - a malformed dict is logged and dropped, because telemetry is
        a hint on a card and never a correctness gate.
        """
        try:
            payload = SolveProgressPayload(**progress)
        except Exception as exc:  # noqa: BLE001 -- never break the solve loop
            logger.warning("emit_solve_progress: bad payload dropped: %s", exc)
            return
        await self._send("solve-progress", payload)

    async def emit_chart(self, chart_payload: dict) -> None:
        """Emit a ``chart-emission`` envelope from a workflow body, and persist it.
        An unset ``created_turn_id`` is stamped from the turn, so charts from one
        turn group together. Best-effort: a failure is logged and dropped.
        """
        if not isinstance(chart_payload, dict) or not chart_payload:
            return
        payload = dict(chart_payload)
        if not payload.get("created_turn_id"):
            payload["created_turn_id"] = self._pipeline_id or self.session_id
        # A hand-built dict, not the typed envelope: that envelope's payload is a
        # model with extra="forbid", which rejects a raw chart payload. Byte-
        # identical to the tool path's send, plus the owning-Case tag.
        frame = {
            "type": "chart-emission",
            "session_id": self.session_id,
            "case_id": current_turn_case(),
            "payload": payload,
        }
        try:
            await self._sink(json.dumps(frame))
            logger.info(
                "chart-emission emitted (composer) session=%s chart_id=%s title=%r",
                self.session_id,
                payload.get("chart_id"),
                payload.get("title"),
            )
        except Exception:  # noqa: BLE001 - side effect, never bubble up
            logger.warning(
                "composer chart-emission send failed session=%s chart_id=%s",
                self.session_id,
                payload.get("chart_id"),
                exc_info=True,
            )
        # Persist best-effort so the chart replays on rehydration, through the
        # SAME record the tool path writes.
        if self._chart_persist is not None:
            try:
                await self._chart_persist(payload)
            except Exception:  # noqa: BLE001 - persistence must not break the loop
                logger.warning(
                    "composer chart persistence failed session=%s chart_id=%s",
                    self.session_id,
                    payload.get("chart_id"),
                    exc_info=True,
                )

    async def emit_tool_io(
        self,
        *,
        step_id: str,
        tool_name: str,
        raw_args: Any,
        function_response: Any,
        is_error: bool = False,
    ) -> None:
        """Emit a ``tool-io`` envelope: the RAW args and response for one dispatch.
        Both are stringified and TRUNCATED, with the original byte length riding
        along. Best-effort: a failure is logged and dropped.
        """
        try:
            args_str, args_trunc, args_bytes = _json_for_tool_io(raw_args)
            resp_str, resp_trunc, resp_bytes = _json_for_tool_io(function_response)
            payload = ToolIoPayload(
                step_id=step_id,
                tool_name=tool_name,
                raw_args=args_str,
                function_response=resp_str,
                is_error=bool(is_error),
                args_truncated=args_trunc,
                response_truncated=resp_trunc,
                args_bytes=args_bytes,
                response_bytes=resp_bytes,
            )
        except Exception as exc:  # noqa: BLE001 -- never break the dispatch loop
            logger.warning("emit_tool_io: bad payload dropped: %s", exc)
            return
        await self._send("tool-io", payload)


    @contextmanager
    def tool_call(self, *, name: str, tool_name: str):
        """Sync context-manager form for non-async tool calls.
        Unimplemented: a sync context cannot await an emission, so every caller
        takes the async form instead.
        """
        raise NotImplementedError(
            "use async_emit_tool_call from the WS handler; the sync context "
            "is reserved for a future non-WS integration"
        )

    async def _emit_one_layer(self, layer: LayerURI) -> None:
        """Publish, guard, then track one client-bound layer. Never raises."""
        try:
            layer = await publish_for_emission(layer)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - enrichment is never fatal
            logger.exception(
                "emit_tool_call: auto-publish raised for layer_id=%s; emitting "
                "the layer as the tool returned it.", layer.layer_id,
            )
        emit_layer = emit_layer_uri(layer)
        if emit_layer is not None:
            await self.add_loaded_layer(emit_layer)

    async def emit_tool_call(
        self,
        *,
        name: str,
        tool_name: str,
        invoke: Callable[[], Any] | Callable[[], Awaitable[Any]],
    ) -> Any:
        """Wrap a single tool invocation with pipeline-state emission.
        Returns the tool's own result untouched, and re-raises every exception
        after marking the card - cancelled and failed stay distinct.
        """
        step_id = await self.add_step(name=name, tool_name=tool_name)
        await self.mark_running(step_id)
        # This dispatch's children accumulate fresh, so a prior dispatch's
        # substeps cannot leak onto this card. Set to the real snapshot at each
        # terminal point below, while the children still exist.
        self.last_tool_children = []
        # Bind self as the active emitter for the lifetime of the invoke, so a
        # workflow body can fire a transient map verb. The token unwinds the
        # binding exactly once, on the cancellation and exception paths too.
        token = _CURRENT_EMITTER.set(self)
        # Bind the dispatched tool name and capture the running loop, so a nested
        # seam can tell a direct dispatch from an in-composer one and can drive an
        # async coroutine back onto THIS loop from an off-loaded worker thread.
        _disp_token = _DISPATCHED_TOOL.set(tool_name)
        self._bound_loop = asyncio.get_running_loop()
        # Remember the previous parent so a nested invocation restores it; a
        # substep mints its children against this id.
        _prev_parent = self._current_parent_step_id
        self._current_parent_step_id = step_id
        try:
            try:
                result = invoke()
                if asyncio.iscoroutine(result):
                    result = await result
            except asyncio.CancelledError:
                await self.mark_cancelled(step_id)
                # Record the terminal step even on a cancel: the accessor must
                # never carry a STALE prior step past this dispatch.
                self.last_tool_step = self._to_summary(step_id)
                # And its children, for the same reason.
                self.last_tool_children = self._collect_children(step_id)
                raise
            except Exception as exc:  # noqa: BLE001 -- classify-and-re-raise
                code, message = self._classify_exception(exc)
                await self.mark_failed(step_id, error_code=code, error_message=message)
                self.last_tool_step = self._to_summary(step_id)
                # A FAILED parent card IS persisted, so its children are
                # snapshotted and the replayed card still nests its timeline.
                self.last_tool_children = self._collect_children(step_id)
                raise
            # The terminal pipeline-state frame goes out BEFORE the layer's
            # session-state emission: the classification depends only on the tool
            # RESULT, so the card flips first and the layer frame then carries the
            # terminal state rather than a card still marked running.
            #
            # A tool can FAIL or be CANCELLED and still RETURN, so the return
            # value decides: a non-success terminal outcome flips the card to
            # cancelled or failed instead of green.
            terminal = _classify_tool_return(result)
            if terminal is not None:
                state, error_code, error_message = terminal
                if state == "cancelled":
                    await self.mark_cancelled(step_id)
                else:
                    await self.mark_failed(
                        step_id,
                        error_code=error_code,
                        error_message=error_message,
                    )
                logger.info(
                    "emit_tool_call: tool %r RETURNED a non-success terminal "
                    "outcome state=%s code=%s; card marked %s (not complete)",
                    tool_name, state, error_code, state,
                )
            else:
                await self.mark_complete(step_id)
            self.last_tool_step = self._to_summary(step_id)
            # Snapshot the ordered children of this top-level card BEFORE the
            # pipeline is closed and the steps cleared; the persisted card carries
            # them, so a reopened Case rebuilds the nested timeline read-only.
            self.last_tool_children = self._collect_children(step_id)
            # Honor LayerURI return shape -- append to loaded_layers + emit
            # session-state. This runs AFTER the terminal frame above so the
            # session-state snapshot captures the step as complete/failed, never
            # "running" (the stuck-card bug). route through the single
            # emission seam first. The seam drops (returns None) a renderable
            # raster carrying a raw gs:// uri (the publish-failure degraded path)
            # so it never paints a broken layer row; vector inline-GeoJSON
            # LayerURIs and WMS-URL rasters pass untouched. The tool
            # result is unaffected -- a dropped layer is still narrated honestly
            # and the retry loop can act.
            #
            # AUTO-EMIT: a raster the tool returned as a raw
            # s3:// COG is PUBLISHED here - overviews, style params, legend -
            # before the guardrail sees it. Every raster-producing tool gets
            # that by returning a LayerURI; there is no publish call site per
            # tool and no opt-out, intermediates included. A list of layers
            # (frame series) is each layer's own trip through the same seam.
            if isinstance(result, LayerURI):
                await self._emit_one_layer(result)
            elif isinstance(result, list) and any(
                isinstance(item, LayerURI) for item in result
            ):
                for item in result:
                    if isinstance(item, LayerURI):
                        await self._emit_one_layer(item)
            return result
        finally:
            _CURRENT_EMITTER.reset(token)
            _DISPATCHED_TOOL.reset(_disp_token)
            # restore the previous parent pointer. On the normal
            # single-top-level-step path this returns it to None.
            self._current_parent_step_id = _prev_parent


    def _classify_exception(self, exc: Exception) -> tuple[str, str]:
        """Map a tool exception to an ``(error_code, error_message)`` pair.
        Deliberately conservative: an ambiguous shape buckets into
        ``INTERNAL_ERROR`` rather than fabricate a more specific code.
        """
        message = str(exc) or exc.__class__.__name__
        # Subclass-aware bucketing. Order matters -- most specific first.
        if isinstance(exc, ValueError) and "bbox" in message.lower():
            return ("BBOX_INVALID", message)
        if isinstance(exc, TimeoutError) or isinstance(
            exc, asyncio.TimeoutError
        ):  # pragma: no cover -- Py3.11+ aliases
            return ("UPSTREAM_API_ERROR", f"upstream timeout: {message}")
        if isinstance(exc, ConnectionError):
            return ("UPSTREAM_API_ERROR", message)
        if isinstance(exc, LookupError) and "geocode" in message.lower():
            return ("GEOCODE_NO_MATCH", message)
        if isinstance(exc, KeyError) and "tool" in message.lower():
            return ("TOOL_NOT_FOUND", message)
        if isinstance(exc, TypeError) or isinstance(exc, ValueError):
            return ("TOOL_PARAMS_INVALID", message)
        return ("INTERNAL_ERROR", message)

    def _require_step(self, step_id: str) -> _StepState:
        step = self._steps.get(step_id)
        if step is None:
            raise StepNotFoundError(
                f"step_id {step_id!r} not registered with this emitter"
            )
        return step

    @staticmethod
    def _coerce_progress(value: int) -> int:
        if value < 0 or value > 100:
            raise ValueError(
                f"progress_percent must be in [0,100]; got {value!r}"
            )
        return int(value)

    @classmethod
    def _truncate_message(cls, message: str) -> str:
        if len(message) <= cls.ERROR_MESSAGE_MAX_LEN:
            return message
        return message[: cls.ERROR_MESSAGE_MAX_LEN]

    def _to_wire_step(self, step_id: str) -> PipelineStep:
        s = self._steps[step_id]
        return PipelineStep(
            step_id=s.step_id,
            name=s.name,
            tool_name=s.tool_name,
            state=s.state,  # type: ignore[arg-type]
            started_at=s.started_at,
            completed_at=s.completed_at,
            progress_percent=s.progress_percent,
            duration_ms=s.duration_ms,
            # The card-kind discriminator and the solver-run binding; the
            # defaults leave a plain tool card unchanged on the wire.
            role=s.role,  # type: ignore[arg-type]
            batch_job_id=s.batch_job_id,
            batch_status=s.batch_status,
            engine=s.engine,
            module=s.module,
            # ``parent_step_id`` rides a CHILD; the live-breadcrumb trio rides
            # the PARENT, and is None while it is idle.
            parent_step_id=s.parent_step_id,
            substep_label=s.substep_label,
            substep_index=s.substep_index,
            substep_total=s.substep_total,
        )

    def _to_summary(self, step_id: str) -> PipelineStepSummary:
        s = self._steps[step_id]
        return PipelineStepSummary(
            step_id=s.step_id,
            name=s.name,
            tool_name=s.tool_name,
            state=s.state,  # type: ignore[arg-type]
            started_at=s.started_at,
            completed_at=s.completed_at,
            progress_percent=s.progress_percent,
            error_code=s.error_code,
            error_message=s.error_message,
            duration_ms=s.duration_ms,
            # The card-kind fields ride the persisted summary too, so a compute
            # card survives a reconnect and a cold-case view.
            role=s.role,  # type: ignore[arg-type]
            batch_job_id=s.batch_job_id,
            batch_status=s.batch_status,
            # And the nested sub-step fields, so a replayed snapshot carries the
            # nested timeline rather than a flat list.
            parent_step_id=s.parent_step_id,
            substep_label=s.substep_label,
            substep_index=s.substep_index,
            substep_total=s.substep_total,
        )

    def _collect_children(self, parent_step_id: str) -> list[PersistedSubStepRecord]:
        """Snapshot the TERMINAL child substeps of ``parent_step_id``.
        Complete and failed children only, in start order; ``[]`` when the parent
        had none. MUST be called while the children still exist.
        """
        # A cancelled or still-running child persists NOTHING, which is the same
        # contract the parent card follows. A failed child carries its code and
        # message, so a replayed child reads red WITH its reason. Child tool-io is
        # not captured, so those fields stay None rather than being invented.
        out: list[PersistedSubStepRecord] = []
        for sid in self._step_order:
            child = self._steps.get(sid)
            if child is None or child.parent_step_id != parent_step_id:
                continue
            if child.state not in ("complete", "failed"):
                continue
            out.append(
                PersistedSubStepRecord(
                    step_id=child.step_id,
                    parent_step_id=child.parent_step_id,
                    name=child.name,
                    tool_name=child.tool_name,
                    state=child.state,  # type: ignore[arg-type]
                    duration_ms=child.duration_ms,
                    error_code=child.error_code,
                    error_message=child.error_message,
                )
            )
        return out

    async def _emit_pipeline_state(self) -> None:
        if self._pipeline_id is None:
            # An emit with no pipeline is a programming error at the call site,
            # and is not papered over with an empty snapshot.
            raise EmitterError(
                "_emit_pipeline_state called with no open pipeline; "
                "call start_pipeline / add_step first"
            )
        payload = PipelineStatePayload(
            pipeline_id=self._pipeline_id,
            steps=[self._to_wire_step(sid) for sid in self._step_order],
        )
        # A non-terminal running transition is surfaced by this single frame, so
        # a closed socket would otherwise ABORT the transition and lose the card.
        # ONLY the connection-closed class is swallowed, symmetric with the
        # terminal path: the step state is already recorded, and a sink rebind
        # replays the full snapshot. Any other exception propagates loudly.
        try:
            await self._send("pipeline-state", payload)
        except _CONNECTION_CLOSED_EXC:  # type: ignore[misc]
            logger.debug(
                "emitter: running pipeline-state send failed on a closed "
                "socket (best-effort drop; will replay on rebind) session=%s "
                "pipeline_id=%s",
                self.session_id,
                self._pipeline_id,
            )

    async def _emit_terminal_pipeline_state(self) -> None:
        """Emit the pipeline-state for a TERMINAL transition, best-effort.
        The payload is snapshotted for replay and only a closed socket is
        swallowed, so the state transition itself always completes.
        """
        if self._pipeline_id is None:
            # A terminal emit with no open pipeline is a programming error at the
            # call site, on the same contract as the non-terminal emit above.
            raise EmitterError(
                "_emit_terminal_pipeline_state called with no open pipeline; "
                "call start_pipeline / add_step first"
            )
        payload = PipelineStatePayload(
            pipeline_id=self._pipeline_id,
            steps=[self._to_wire_step(sid) for sid in self._step_order],
        )
        # Stash the LAST terminal snapshot so a sink rebind can replay it and a
        # rendered card stays surfaced across a socket blip.
        self._last_terminal_pipeline_payload = payload
        try:
            await self._send("pipeline-state", payload)
        except _CONNECTION_CLOSED_EXC:  # type: ignore[misc]
            # A dead or cycling socket is a best-effort drop: the terminal state
            # is already on the step, and the snapshot above replays on the next
            # rebind, so the card is not lost.
            logger.debug(
                "emitter: terminal pipeline-state send failed on a closed "
                "socket (best-effort drop; will replay on rebind) session=%s "
                "pipeline_id=%s",
                self.session_id,
                self._pipeline_id,
            )

    async def send_envelope(self, message_type: str, payload: Any) -> None:
        """Emit ONE arbitrary typed envelope on this session's sink.
        For a sender whose message is outside the pipeline-step vocabulary; it is
        framed identically, stamped with the session and the owning Case.
        """
        await self._send(message_type, payload)

    async def _send(self, message_type: str, payload: Any) -> None:
        env = Envelope(
            type=message_type,
            session_id=self.session_id,
            # Stamp the owning Case, so the client routes this to the right
            # stream even after a mid-turn Case switch.
            case_id=current_turn_case(),
            payload=payload,
        )
        await self._sink(env.model_dump_json())
        logger.debug(
            "emitter session=%s type=%s pipeline_id=%s steps=%d layers=%d",
            self.session_id,
            message_type,
            self._pipeline_id,
            len(self._step_order),
            len(self._loaded_layers),
        )


#
# Every solver-dispatch composer mints the same two cards: a Dispatch tool card
# recording the submit, which lands complete immediately, and a Sim compute card
# bound to the dispatched run whose live status the wait-loop poller feeds. Thin
# orchestration over the emitter's own transition methods, defined once here
# rather than per composer.


async def mint_dispatch_and_sim_cards(
    *,
    emitter: "PipelineEmitter | None",
    solver: str,
    handle: Any,
    compute_class: str | None = None,
    module: str | None = None,
) -> str | None:
    """Mint the Dispatch (tool) + Sim (compute) cards for a dispatched solve.
    Returns the SIM step's id, or ``None`` on any failure: the cards are an
    observability affordance and the solve proceeds either way.
    """
    if emitter is None:
        return None
    # Named for the CASE, not the solver: a family whose legs share one
    # registered solver id would otherwise label every run after one sibling.
    # Outside a dispatch there is no case, and the solver id is the only
    # identity there is.
    case = dispatched_tool_name() or solver
    job_id = str(getattr(handle, "workflows_execution_id", "") or "")
    backend = str(getattr(handle, "workflow_name", "") or "local-docker")
    try:
        # Card 1 "Dispatch": a normal tool step recording the submit.
        dispatch_label = f"Dispatch {case} solve"
        if compute_class:
            dispatch_label = f"{dispatch_label} ({compute_class})"
        dispatch_id = await emitter.add_step(
            name=dispatch_label, tool_name=f"{case}:dispatch"
        )
        await emitter.mark_running(dispatch_id)
        await emitter.mark_complete(dispatch_id)
        # Persist the terminal Dispatch card, so the full pair replays on a Case
        # reopen rather than existing only live on the wire.
        await emitter.persist_terminal_dispatch_card(dispatch_id)
        # Card 2 "Sim": the compute card bound to the dispatched run id.
        sim_id = await emitter.add_compute_step(
            name=f"{case} solve",
            tool_name=f"{case}:solve",
            batch_job_id=job_id,
            batch_status="SUBMITTED",
            engine=solver,
            module=module,
        )
        # Persist the SIM card NOW, still running, so a reconnect mid-solve
        # replays the live card instead of dropping it. The same row is upserted
        # to its terminal state when the solve finishes.
        await emitter.persist_running_compute_card(sim_id)
        logger.info(
            "two-card sim observability: minted dispatch + compute cards "
            "case=%s solver=%s backend=%s jobId=%s sim_step_id=%s",
            case,
            solver,
            backend,
            job_id,
            sim_id,
        )
        return sim_id
    except Exception as exc:  # noqa: BLE001 -- observability, never break the solve
        logger.warning("mint_dispatch_and_sim_cards failed (non-fatal): %s", exc)
        return None


async def route_sim_terminal(
    emitter: "PipelineEmitter | None",
    sim_step_id: str | None,
    *,
    run_result: Any,
) -> None:
    """Drive the SIM compute card to its terminal state.
    A ``None`` ``run_result`` is a cancel. Every branch UPSERTS the row written
    running at mint, and an emit failure is swallowed rather than raised.
    """
    if emitter is None or not sim_step_id:
        return
    try:
        status = str(getattr(run_result, "status", "") or "") if run_result is not None else ""
        if run_result is None or status == "cancelled":
            await emitter.mark_cancelled(sim_step_id)
            # A cancel UPSERTS the row written running at mint, leaving no
            # orphaned running row: a stopped solve stays traceable.
            await emitter.persist_terminal_compute_card(sim_step_id)
        elif status == "complete":
            await emitter.mark_complete(sim_step_id)
            # The green card persists like a plain tool card, so it replays on a
            # reconnect or a reopen.
            await emitter.persist_terminal_compute_card(sim_step_id)
        else:
            error_code = (
                getattr(run_result, "error_code", None) or (status.upper() if status else "SOLVER_FAILED")
            )
            error_message = (
                getattr(run_result, "error_message", None)
                or getattr(run_result, "cancellation_reason", None)
                or f"solver run {status or 'failed'}"
            )
            await emitter.mark_failed(
                sim_step_id, error_code=str(error_code), error_message=str(error_message)
            )
            # The red card persists too: a terminal solve FAILURE must survive a
            # socket cycle rather than disappear with it.
            await emitter.persist_terminal_compute_card(sim_step_id)
    except Exception as exc:  # noqa: BLE001 -- observability, never break the solve
        logger.warning("route_sim_terminal failed (non-fatal): %s", exc)


#
# The running-then-upsert-terminal shape above, collapsed to a SINGLE card:
# compaction is one atomic local pass, not a dispatch and a solve, so it takes a
# ``role="tool"`` card and never a compute one - there is no dispatched run to
# bind. It starts running and is later renamed and completed, riding the same
# wire shape as every other tool card rather than a new envelope type.


async def mint_compaction_card(*, emitter: "PipelineEmitter | None") -> str | None:
    """Mint the durable running compaction card and return its step id.
    ``None`` on any failure: the card is an observability affordance, never a
    gate on the compaction it describes.
    """
    if emitter is None:
        return None
    try:
        step_id = await emitter.add_durable_step(
            name=COMPACTING_LABEL, tool_name="context:compact"
        )
        # Persist the running card NOW, so a reconnect mid-pass replays the
        # spinning card instead of dropping it; the terminal write upserts the
        # SAME row.
        await emitter.persist_running_compute_card(step_id)
        return step_id
    except Exception as exc:  # noqa: BLE001 -- observability, never break the turn
        logger.warning("mint_compaction_card failed (non-fatal): %s", exc)
        return None


async def complete_compaction_card(
    *,
    emitter: "PipelineEmitter | None",
    step_id: str | None,
    before_tokens: int,
    after_tokens: int,
) -> None:
    """Drive the minted compaction card to its terminal state.
    A no-op when the mint failed or was never called; an emit or persist failure
    is swallowed rather than raised into the turn.
    """
    if emitter is None or step_id is None:
        return
    try:
        # Renamed BEFORE the completion, so the live terminal emission and the
        # persisted upsert both carry the final text rather than the stale
        # running label.
        emitter.rename_step(
            step_id, name=compaction_complete_label(before_tokens, after_tokens)
        )
        await emitter.mark_complete(step_id)
        # Upserts the SAME row persisted running at mint, so the card survives a
        # reopen with its final renamed label and state.
        await emitter.persist_terminal_compute_card(step_id)
    except Exception as exc:  # noqa: BLE001 -- observability, never break the turn
        logger.warning("complete_compaction_card failed (non-fatal): %s", exc)
