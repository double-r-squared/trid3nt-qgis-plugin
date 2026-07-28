"""Appendix-A WebSocket server (FR-AS-5, Appendix A core subset for M1/job-0015).

Implements the M1 hello-world subset of Appendix A:

  client -> agent (A.3):
    - session-resume          -> session-state
    - user-message            -> agent-message-chunk* (terminal done=True)
    - cancel                  -> pipeline-state(cancelled) within NFR-R-3 30s

  agent -> client (A.4):
    - session-state           initial replay on session-resume
    - agent-message-chunk     streamed deltas + terminal frame
    - pipeline-state          for cancel; also a one-step "thinking" snapshot
    - error                   A.6 codes

Every wire envelope is validated through ``trid3nt_contracts.ws.Envelope`` --
NEVER hand-roll JSON. Per Invariant 8 cancellation is first-class: any
in-flight Gemini stream is cancelled via asyncio task cancellation; the LLM
side of the chain completes within 30s. Cloud Workflows ``terminate`` is the
v0.2/M5 side of the chain (no solver yet in M1).

FR-WC-15 ``research_mode``: pass-through pinned. For job-0015 v0.1 the field is
logged and forwarded as-is -- there is no second pipeline strategy yet.

FR-AS-8 confirmation hooks: scaffolded as ``CONFIRMATION_TRIGGERS`` (empty in
M1). Session-record writes (Appendix D.6) are explicitly carved out per FR-AS-8.

OQ-1 (Cloud Run WS vs Agent Engine) -- see report's Open Questions section.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
import weakref
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
)

from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.case import (
    CaseChatMessage,
    CaseCommandEnvelopePayload,
    CaseListEnvelopePayload,
    CaseOpenEnvelopePayload,
    CaseSessionState,
    CaseSummary,
    ToolCardRecord,
)
from trid3nt_contracts.payload_warning import (
    HARD_CAP_MB_DEFAULT,
    WARNING_THRESHOLD_MB_DEFAULT,
    PayloadConfirmationEnvelopePayload,
    PayloadWarningEnvelopePayload,
)
from trid3nt_contracts.sandbox_contracts import CodeExecRequestPayload
from trid3nt_contracts.secrets import (
    CredentialProvidedEnvelopePayload,
    CredentialRequestEnvelopePayload,
    SecretAddEnvelopePayload,
    SecretRevokeEnvelopePayload,
    SecretsListEnvelopePayload,
)
from trid3nt_contracts.region_choice import (
    RegionCandidate,
    RegionChoiceProvidedEnvelopePayload,
    RegionChoiceRequestEnvelopePayload,
)
from trid3nt_contracts.ws import (
    AgentMessageChunkPayload,
    CancelPayload,
    CatalogAdditionResponsePayload,
    Envelope,
    AgentThinkingChunkPayload,
    ErrorPayload,
    PipelineStatePayload,
    PipelineStep,
    SessionResumePayload,
    SessionStatePayload,
    SpatialInputRequestPayload,
    SpatialInputResponsePayload,
    UserMessagePayload,
)

from .main import MAX_TURNS_PER_SESSION

from .agent.gates.runaway_guard import (
    ABORT_LOOP_WATCHDOG,
    ABORT_STEP_CAP,
    ABORT_WALL_CLOCK,
    LoopWatchdog,
    abort_message,
    max_turn_seconds,
    step_cap_for_model,
)

from .agent.adapters.adapter import (
    CompactionCompleteEvent,
    CompactionStartEvent,
    FunctionCallEvent,
    ModelSettings,
    MAX_TURN_ITERATIONS,
    SYSTEM_PROMPT,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    UpstreamProviderError,
    UsageMetadataEvent,
    build_client,
    classify_provider_error_class,
    build_contents_from_history,
    build_layers_present_note,
    build_function_call_content,
    build_function_response_content,
    build_user_text_content,
    build_tool_declarations,
    load_settings,
    rehydrate_history_from_case,
    REHYDRATE_HISTORY_CAP,
    stream_events,  # noqa: F401 -- retained for tests / direct text-only callers
    stream_events_with_contents,
    summarize_tool_result,
    classify_result_usable,
)
from .credentials.auth_handshake import (
    AuthResult,
    authenticate_token,
    build_auth_ack,
    derive_advertised_endpoints,
    get_auth_token_timeout_s,
    verify_access_token,
)
from .case_lifecycle import CaseLifecycleError, ensure_case_qgs
from .agent.gates.context_budget import (
    FABRICATION_CAVEAT,
    ContextWindowExceededError,
    build_context_window_abort_note,
    looks_like_fabricated_action_claim,
)
from .credentials.credential_registry import (
    CredentialProvider,
    generic_provider_for_tool,
    is_credential_error,
    is_credential_shaped_error,
    provider_for_tool,
)
from .emission.layer_uri_emit import emit_layer_uri
from .agent.gates.mode2_classifier import (
    Mode2CandidateEnvelope,
    classify_for_mode2,
)
from .persistence import Persistence
from .emission.pipeline_emitter import (
    _FLOOD_FRAME_NAME_RE,
    PipelineEmitter,
    _json_for_tool_io,
    bind_turn_case,
    complete_compaction_card,
    current_turn_case,
    mint_compaction_card,
)
from .credentials.secrets_handler import (
    SecretError,
    handle_secret_add,
    handle_secret_revoke,
    handle_secrets_list,
)
from .telemetry import (
    compute_args_hash,
    emit_shadow_selection_event,
    emit_tool_call_event,
    emit_turn_telemetry,
)
from .agent.tool_arg_normalizer import (
    autofill_missing_bbox,
    coerce_bbox_value,
    normalize_args,
)
from .emission.uri_registry import (
    activate_registry,
    deactivate_registry,
    get_uri_registry,
)
from .scenario_reuse import (
    bbox_encloses,
    bbox_equivalent,
    fetched_kind_for_tool,
    find_reusable_fetched_layer,
    get_scenario_index,
    scenario_signature,
    scenario_type_for_tool,
)
from .agent.gates.spatial_input import (
    SpatialInputParseError,
    parse_spatial_input_features,
)
from .agent.gates.cards import (
    _build_credential_request_payload,
    _build_fetch_resolution_envelope,
    _build_fire_confirm_envelope,
    _build_flood_run_settings_envelope,
    _build_geoclaw_confirm_envelope,
    _build_psha_confirm_envelope,
    _build_region_choice_request_payload,
    _build_spatial_input_request_payload,
    _build_swmm_granularity_envelope,
    _build_telemac_mesh_envelope,
    _clamp_fetch_resolution,
    _clamp_swmm_resolution_to_cap,
    _gate_memory_key,
    _get_hard_cap_mb,
    _get_warning_threshold_mb,
    _local_compute_lane,
    _resolve_payload_estimator,
    _spatial_response_to_result,
    _LANDCOVER_DEFAULT_RES_M,
)
from .agent.tools import TOOL_REGISTRY
from .agent.tools.processing.charts_common import is_chart_emission_result
from .agent.tools.meta.code_exec_tool.code_exec_tool import (
    CODE_EXEC_RESULT_KEY,
    is_code_exec_result,
)
from .agent.categories import (
    AllowedToolSet,
    OutOfAllowedSetError,
    validate_function_call,
)
from .agent.gates.circuit_breaker import CircuitBreakerError, ToolCircuitBreaker
from .agent.gates.tool_gating import BenchBlockedError

# Auth-token envelope (Appendix H.5 connect handshake).
from trid3nt_contracts.auth import AuthTokenEnvelope

logger = logging.getLogger("trid3nt_server.server")

# Confirmation triggers (FR-AS-8). Empty for M1: solver runs and non-session
# Mongo writes will populate this when those code paths land. Session-record
# writes (Appendix D.6) are NOT a trigger -- that carveout is documented in
# the report, not represented as data here.
CONFIRMATION_TRIGGERS: set[str] = set()

# ---------------------------------------------------------------------------
# Tool-retrieval mode (tool-retrieval kickoff -- orchestrator half).
#
# Three modes, read once at import time following the TRID3NT_DYNAMIC_HOT_SET /
# TRID3NT_SYNC_TOOL_OFFLOAD env idiom (NO code change to flip):
#
#   off     (DEFAULT) -- the catalog is the FULL flat registry, untouched. This
#                        is BYTE-IDENTICAL to the pre-feature behavior: no
#                        retrieval is even computed, no shadow record is logged.
#   shadow            -- compute the WOULD-BE-visible set via
#                        retrieve_visible_tools and LOG it as shadow telemetry,
#                        but STILL build declarations over the FULL registry.
#                        ZERO behavior change (the model sees all tools); the
#                        log feeds the recall@k dashboard.
#   enforce           -- subset TOOL_REGISTRY to the visible set BEFORE building
#                        declarations (and UNION the visible set into the Case's
#                        monotonic AllowedToolSet so a once-visible tool never
#                        leaves within a Case). Locked OFF on cloud until recall@k
#                        proves >= 0.99/flow.
#
# K is the discover top-k for retrieve_visible_tools (default 25; the function
# clamps to [1, MAX_K]).
_TOOL_RETRIEVAL_VALID_MODES = frozenset({"off", "shadow", "enforce"})
_TOOL_RETRIEVAL_MODE = (
    os.environ.get("TRID3NT_TOOL_RETRIEVAL", "off").strip().lower()
)
if _TOOL_RETRIEVAL_MODE not in _TOOL_RETRIEVAL_VALID_MODES:
    # Unknown value -> fail-safe to the no-op default (never silently enforce).
    _TOOL_RETRIEVAL_MODE = "off"


def _tool_retrieval_k() -> int:
    """Resolve TRID3NT_TOOL_RETRIEVAL_K (default 25); fall back to the default on
    any parse error. Read per-call so a test can override via the env without a
    module reload."""
    from .agent.tools.search.tool_retrieval import DEFAULT_K

    raw = os.environ.get("TRID3NT_TOOL_RETRIEVAL_K")
    if raw is None:
        return DEFAULT_K
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_K


def _tool_retrieval_mode() -> str:
    """Current tool-retrieval mode. Reads the env LIVE (not the import-time
    snapshot) so a test / runtime flip is honored; unknown -> 'off' (fail-safe
    to the no-op default, never silently enforce)."""
    mode = os.environ.get("TRID3NT_TOOL_RETRIEVAL", "off").strip().lower()
    return mode if mode in _TOOL_RETRIEVAL_VALID_MODES else "off"

# The ``code_exec_request`` confirm gate validity window (seconds). Running
# arbitrary Python is a deliberate user decision; on expiry the gate fails
# closed (CONFIRMATION_TIMEOUT) and the sandbox does not run. The code-exec
# gate itself no longer waits on this constant (see
# ``_code_exec_approval_timeout_s``); it is retained because the credential /
# region-choice / solver-confirm gates borrow it as their default wait window.
CODE_EXEC_CONFIRM_TIMEOUT_SECONDS: int = int(
    os.environ.get("TRID3NT_CODE_EXEC_CONFIRM_TIMEOUT", "300")
)

# Honest timeout on unanswered code-exec approvals: the code-exec gate gets its
# OWN bounded approval window that applies in EVERY lane (deliberately bypassing
# the F6 24h local override). When no confirmation envelope answers the card in
# time, the gate raises the typed ``CodeExecApprovalTimeoutError`` so the LLM
# receives a structured function_response, narrates honestly, and the TURN
# COMPLETES. Read LIVE (not an import-time snapshot) so runtime flips are honored.
CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S: float = 180.0


def _code_exec_approval_timeout_s() -> float:
    """Effective approval-wait window (seconds) for the code-exec confirm gate.

    Env override ``TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S``; default 180s.
    Malformed / non-positive values fall back to the default (never an
    unbounded or zero wait).
    """
    raw = os.environ.get("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S")
    if raw is None:
        return CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S
    if value <= 0:
        return CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S
    return value


# ---------------------------------------------------------------------------
# Stage 3 (ADR 0017 mechanisms 3-5 + ADR 0018) -- harness-absorbs-prompt
# config seams. Every mechanism ships with an env kill-switch so a live
# regression can be flipped off without a code change (the TRID3NT_* idiom).
# ---------------------------------------------------------------------------


def _env_flag(name: str, default: bool = True) -> bool:
    """Boolean env flag, read LIVE: '0'/'off'/'false'/'no' -> False,
    '1'/'on'/'true'/'yes' -> True, unset/unknown -> ``default``."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    if raw in ("1", "on", "true", "yes"):
        return True
    return default


def _session_routing_mode(state: "SessionState") -> str:
    """ADR 0018 routing-visibility mode for this session: 'auto' | 'ask'.

    A per-session setting (the ``session-config`` envelope's ``mode`` field)
    wins; else the ``TRID3NT_MODE`` env default; else 'auto'. Gates are NEVER
    mode-dependent -- the mode governs tool-selection VISIBILITY only.
    """
    mode = getattr(state, "routing_mode", None)
    if isinstance(mode, str) and mode in ("auto", "ask"):
        return mode
    env = (os.environ.get("TRID3NT_MODE") or "auto").strip().lower()
    return env if env in ("auto", "ask") else "auto"


def _ambiguity_margin_threshold() -> float:
    """ADR 0018 measured-ambiguity threshold (``TRID3NT_AMBIGUITY_MARGIN``).

    RELATIVE top-1 vs top-2 retrieval-score margin under which AUTO mode still
    surfaces the tool-candidates card. Calibration: RRF fused scores are
    rank-compressed -- a tool that is rank-1 on every channel beats a
    consistent rank-2 by only ~1.6% relative, while a genuine cross-channel
    tie (each of two tools rank-1 somewhere) lands well under ~1%. The 0.01
    default therefore fires ONLY on genuine channel disagreement, not on any
    consistently-ordered ranking. ``0`` disables ambiguity asks entirely (the
    kill switch; ask mode is unaffected). Malformed -> default.
    """
    raw = os.environ.get("TRID3NT_AMBIGUITY_MARGIN")
    if raw is None:
        return 0.01
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.01
    return max(0.0, value)


def _tool_choice_timeout_s() -> float:
    """Bounded wait (seconds) for a ``tool-choice`` reply to the
    ``tool-candidates`` card (``TRID3NT_TOOL_CHOICE_TIMEOUT_S``, default 45).

    Deliberately BYPASSES the F6 24h local-lane ``_gate_wait_timeout``
    override (the code-exec-gate precedent): an unanswered picker must
    degrade to autonomous routing, never hang the turn.
    """
    raw = os.environ.get("TRID3NT_TOOL_CHOICE_TIMEOUT_S")
    if raw is None:
        return 45.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 45.0
    return value if value > 0 else 45.0


#: Max candidates surfaced on one tool-candidates card (avoid flooding).
_TOOL_CANDIDATES_MAX = 4

#: Turn-loop-invariant continuation nudge (ADR 0017 mechanism 4). ONE per
#: turn, injected as a user-role content when a turn (a) terminates with tool
#: results but zero assistant text since the last tool round, or (b) only ever
#: geocoded while the user asked for data/analysis.
_CONTINUATION_NUDGE: str = (
    "You have tool results but have not answered the user yet. Summarize "
    "the results for the user now, and if their requested data or analysis "
    "is not complete, continue with the appropriate tool calls."
)

#: Data/analysis-intent heuristic for the bare-geocode backstop. Deliberately
#: broad verbs/nouns -- a pure "where is X" locate ask matches none of these.
_DATA_INTENT_RE = re.compile(
    r"\b(show|display|map|fetch|get|load|download|overlay|plot|chart|graph|"
    r"visuali[sz]e|analy[sz]e|analysis|model|simulat\w*|comput\w*|calculat\w*|"
    r"estimat\w*|assess\w*|data|imagery|satellite|layer|flood\w*|fire|smoke|"
    r"earthquake|rainfall|precipitation|storm|surge|wind|population|"
    r"buildings?|roads?|elevation|terrain|dem|landcover|damage|risk|hazard|"
    r"depth|extent|inundat\w*)\b",
    re.IGNORECASE,
)


def _asks_for_data_or_analysis(user_text: Any) -> bool:
    """True when the user's message asks for data/analysis (not a bare locate)."""
    return bool(isinstance(user_text, str) and _DATA_INTENT_RE.search(user_text))


#: ADR 0018 analysis-flow stages, in pipeline order. The wave label derivation
#: (``_stage_label_for_candidates``) tie-breaks toward the EARLIEST stage so a
#: multi-step turn reads as a forward march acquisition -> ... -> visualization.
_STAGE_ORDER: tuple[str, ...] = (
    "acquisition",
    "preprocessing",
    "analysis",
    "visualization",
)


def _stage_label_for_tool(tool_name: str) -> str:
    """Coarse analysis-flow stage for ONE tool name (ADR 0018:
    acquisition -> preprocessing -> analysis -> visualization).

    Category definitions: acquisition = fetchers (fetch_/geocode_/discover_/
    catalog_/search_); preprocessing = processing/clip (clip_/merge_/fill_/cut_/
    import_/digitize_/extract_); analysis = spatial_query/code_exec/compute
    (compute_/run_/model_/spatial_/query_/analyze_/aggregate_/code_exec);
    visualization = publish/charts (publish_/generate_/export_/zoom/compose_/
    chart/plot). Anything else falls back to ``"tool-selection"``.
    """
    if tool_name.startswith(
        ("fetch_", "geocode_", "discover_", "catalog_", "search_")
    ):
        return "acquisition"
    if tool_name.startswith(
        ("clip_", "merge_", "fill_", "cut_", "import_", "digitize_", "extract_")
    ):
        return "preprocessing"
    if tool_name.startswith(
        ("publish_", "generate_", "export_", "zoom", "compose_", "chart", "plot")
    ):
        return "visualization"
    if tool_name.startswith(
        ("compute_", "run_", "model_", "spatial_", "query_", "analyze_", "aggregate_")
    ) or tool_name.startswith("code_exec"):
        return "analysis"
    return "tool-selection"


def _stage_label_for_candidates(ranked: list[tuple[str, float]]) -> str:
    """Derive one wave ``stage_label`` from the TOP candidates' categories.

    ADR 0018 wave completion: a single top tool is a brittle signal for a
    round's stage (the rank-1 pick may be an outlier). Instead we aggregate the
    categories of the top ``_TOOL_CANDIDATES_MAX`` candidates and pick the
    PLURALITY stage, tie-broken toward the earliest pipeline stage so a
    multi-step turn surfaces a forward-marching sequence of labels. A candidate
    set that is all fallbacks (``"tool-selection"``) yields ``"tool-selection"``.
    """
    counts: dict[str, int] = {}
    for name, _score in ranked[:_TOOL_CANDIDATES_MAX]:
        stage = _stage_label_for_tool(name)
        if stage == "tool-selection":
            continue
        counts[stage] = counts.get(stage, 0) + 1
    if not counts:
        return "tool-selection"
    # Plurality; ties broken toward the earliest pipeline stage (_STAGE_ORDER).
    best = max(
        counts.items(),
        key=lambda kv: (kv[1], -_STAGE_ORDER.index(kv[0])),
    )
    return best[0]


def _tool_summary_line(entry: Any) -> str:
    """First docstring line of a registered tool, for the candidates card."""
    doc = getattr(getattr(entry, "fn", None), "__doc__", None) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first[:140]


def _geocode_drift_note(
    args: Any, geocode_bbox: Any, active_aoi: Any
) -> str | None:
    """Stage 3 guard (d): WARNING text when a call's bbox intersects NEITHER
    the turn's geocoded bbox NOR the active AOI; ``None`` = no drift.

    Advisory only -- the dispatch is never blocked. Calls without a coercible
    bbox arg are skipped (nothing to compare).
    """
    if not isinstance(args, dict):
        return None
    for key in ("bbox", "aoi_bbox"):
        cand = _coerce_bbox4(args.get(key))
        if cand is None:
            continue
        if _bbox_overlaps(cand, geocode_bbox):
            return None
        if active_aoi is not None and _bbox_overlaps(cand, active_aoi):
            return None
        gc = _coerce_bbox4(geocode_bbox)
        return (
            f"WARNING: this call's {key} {[round(v, 4) for v in cand]} does "
            f"not intersect the geocoded location bbox "
            f"{[round(v, 4) for v in gc] if gc else geocode_bbox}"
            + (" or the active AOI" if active_aoi is not None else "")
            + ". The area of interest may have drifted -- verify the "
            "coordinates before relying on this result."
        )
    return None


# --------------------------------------------------------------------------- #
# ADR 0018 -- pending tool-choice registry: module-level, keyed by the
# unguessable ULID request_id + owning session_id, so a reply arriving on a
# sibling WebSocket connection of the same session still resolves the paused
# turn.
# --------------------------------------------------------------------------- #

_PENDING_TOOL_CHOICES: dict[str, tuple[str, "asyncio.Future"]] = {}


def _register_pending_tool_choice(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_TOOL_CHOICES[request_id] = (session_id, fut)


def _pop_pending_tool_choice(request_id: str) -> None:
    _PENDING_TOOL_CHOICES.pop(request_id, None)


def _resolve_pending_tool_choice(session_id: str, payload: Any) -> bool:
    """Complete the pending tool-candidates gate for ``payload['request_id']``.

    The payload is a LOOSE dict on purpose -- the contracts lane declares the
    ``tool-choice`` model; until integration we parse defensively. Returns
    True when a live future was resolved.
    """
    if not isinstance(payload, dict):
        return False
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return False
    entry = _PENDING_TOOL_CHOICES.get(request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "tool-choice request_id=%s owned by session=%s but resolved-by=%s; "
            "ignoring",
            request_id,
            owner_session,
            session_id,
        )
        return False
    if fut.done():
        return False
    fut.set_result(dict(payload))
    return True


def _union_pinned_tool(
    pinned: str | None,
    retrieval_registry: dict,
    state: SessionState,
) -> dict:
    """Union a user-pinned tool into the allowed set + visible registry.

    Shared by the pre-turn tool-candidates gate and the per-round ASK-mode
    waves (ADR 0018): a pinned tool must be BOTH in the allowed set (post-hoc
    validation) AND in the retrieval-visible registry (so the declaration is
    built and the model can actually call it). Returns the registry to use --
    a NEW dict when the pin widened it, else the same object (so callers can
    skip a needless ``build_tool_declarations`` rebuild via identity check).
    """
    if pinned and pinned in TOOL_REGISTRY:
        state.allowed_tool_set.add_tools({pinned})
        if pinned not in retrieval_registry:
            retrieval_registry = dict(retrieval_registry)
            retrieval_registry[pinned] = TOOL_REGISTRY[pinned]
    return retrieval_registry


# ---------------------------------------------------------------------------
# Mode 2 offer-to-add -- pending catalog-offer registry (FR-DS-* Mode 2).
#
# The ``mode2-candidate`` emission is FIRE-AND-FORGET: it never blocks the turn
# (unlike the tool-choice gate, so there is no future to await). Instead we
# stash the candidate keyed by its ``candidate_id`` -- which the plugin card
# echoes back as the ``catalog-addition-response.request_id`` -- so a later
# positive reply can draft the full catalog entry from the original candidate.
#
# BOUNDED, like every card: offers expire after a TTL and the registry is
# capped, so an offer the user never answers cannot leak (proceeds, never
# hangs). Session-scoped + unguessable-ULID keyed, mirroring the tool-choice
# registry so a reply on a sibling connection of the same session still
# resolves. Overlay write + probe run OFF the loop (asyncio.to_thread).
# ---------------------------------------------------------------------------

#: request_id -> (owner_session_id, candidate wire-dict, monotonic_expiry).
_PENDING_CATALOG_OFFERS: dict[str, tuple[str, dict, float]] = {}

#: Cap on outstanding offers; the oldest is dropped when the bound is hit.
_CATALOG_OFFER_MAX = 64


def _catalog_offer_ttl_s() -> float:
    """Bounded offer validity (``TRID3NT_CATALOG_OFFER_TTL_S``, default 600s).

    Mirrors the ``offer-catalog-addition`` contract's 10-minute default -- the
    user is reading + sanity-checking provenance. Malformed / non-positive ->
    default.
    """
    raw = os.environ.get("TRID3NT_CATALOG_OFFER_TTL_S")
    if raw is None:
        return 600.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 600.0
    return value if value > 0 else 600.0


def _prune_catalog_offers(now: float | None = None) -> None:
    """Drop expired pending offers (called on every register / pop)."""
    now = time.monotonic() if now is None else now
    expired = [
        rid for rid, (_s, _c, exp) in _PENDING_CATALOG_OFFERS.items() if exp <= now
    ]
    for rid in expired:
        _PENDING_CATALOG_OFFERS.pop(rid, None)


def _register_pending_catalog_offer(
    session_id: str, request_id: str, candidate: dict
) -> None:
    """Stash a mode2 candidate so a later positive reply can draft its entry."""
    _prune_catalog_offers()
    # Insertion-ordered dict -> the first key is the oldest offer.
    while len(_PENDING_CATALOG_OFFERS) >= _CATALOG_OFFER_MAX:
        oldest = next(iter(_PENDING_CATALOG_OFFERS), None)
        if oldest is None:
            break
        _PENDING_CATALOG_OFFERS.pop(oldest, None)
    _PENDING_CATALOG_OFFERS[request_id] = (
        session_id,
        dict(candidate),
        time.monotonic() + _catalog_offer_ttl_s(),
    )


def _pop_pending_catalog_offer(session_id: str, request_id: str) -> dict | None:
    """Remove + return the candidate for ``request_id`` (owner-checked).

    Returns ``None`` when the offer is unknown / expired, or when a DIFFERENT
    session claims it (refused loudly -- mirrors ``_resolve_pending_tool_choice``).
    A None here is NOT fatal: a self-contained ``edited_catalog_entry`` on the
    reply can still be completed without the original candidate.
    """
    _prune_catalog_offers()
    entry = _PENDING_CATALOG_OFFERS.get(request_id)
    if entry is None:
        return None
    owner, candidate, _exp = entry
    if owner != session_id:
        logger.warning(
            "catalog-addition-response request_id=%s owned by session=%s but "
            "resolved-by=%s; ignoring",
            request_id,
            owner,
            session_id,
        )
        return None
    _PENDING_CATALOG_OFFERS.pop(request_id, None)
    return candidate


def _slug(text: str | None) -> str:
    """Lowercase kebab slug for a derived catalog id (``weather.gov`` -> ``weather-gov``)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "source"


def _probe_catalog_endpoint_sync(url: str) -> "ProbeFindings":
    """Cheap best-effort conformity probe for a Mode 2 candidate URL.

    Fills the ``ProbeFindings`` axes we can observe from a single bounded HEAD
    (with a ranged-GET fallback for servers that reject HEAD): content type,
    last-modified, range support. DEGRADES HONESTLY -- any failure returns an
    empty ``ProbeFindings`` (all None) rather than fabricating a finding. Runs
    OFF the event loop (called via ``asyncio.to_thread``).
    """
    from trid3nt_contracts.ws import ProbeFindings

    try:
        import requests

        resp = requests.head(url, timeout=5.0, allow_redirects=True)
        if resp.status_code >= 400 or not (resp.headers or {}):
            resp = requests.get(
                url, timeout=5.0, stream=True, headers={"Range": "bytes=0-0"}
            )
        headers = resp.headers or {}
        ct = headers.get("Content-Type") or headers.get("content-type")
        lm = headers.get("Last-Modified") or headers.get("last-modified")
        ar = headers.get("Accept-Ranges") or headers.get("accept-ranges")
        supports_range = (
            isinstance(ar, str) and "bytes" in ar.lower()
        ) or resp.status_code == 206
        return ProbeFindings(
            content_type=(
                ct.split(";")[0].strip() if isinstance(ct, str) and ct else None
            ),
            last_modified_header=lm if isinstance(lm, str) and lm else None,
            supports_range_requests=supports_range or None,
        )
    except Exception:  # noqa: BLE001 -- honest degrade, never fabricate
        logger.info(
            "catalog probe failed url=%s -- degrading to empty ProbeFindings",
            url,
            exc_info=True,
        )
        return ProbeFindings()


async def _probe_catalog_endpoint(url: str | None) -> "ProbeFindings | None":
    """Bounded async wrapper for ``_probe_catalog_endpoint_sync`` (never raises)."""
    from trid3nt_contracts.ws import ProbeFindings

    if not isinstance(url, str) or not url:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_catalog_endpoint_sync, url), timeout=8.0
        )
    except Exception:  # noqa: BLE001 -- probe is best-effort
        return ProbeFindings()


def _complete_catalog_entry(
    base: Any, candidate: dict | None, findings: "ProbeFindings | None"
) -> Any:
    """Draft + complete a full ``CatalogEntry`` for the user-overlay catalog.

    Sources, in precedence order: the user's ``edited_catalog_entry`` (a
    ``SuggestedCatalogEntry``), then the originating mode2 ``candidate`` dict,
    then the conformity ``findings``, then honest defaults. Returns a validated
    ``CatalogEntry`` (status ``"active"`` -- in the single-user local build the
    user's acceptance IS the curator approval), or ``None`` when no endpoint
    URL is derivable (the one field we refuse to fabricate).
    """
    from trid3nt_contracts.catalog import CatalogEntry
    from urllib.parse import urlparse

    cand = candidate or {}

    def _b(attr: str) -> Any:
        return getattr(base, attr, None) if base is not None else None

    urls: list[str] = []
    if base is not None and _b("urls"):
        urls = [u for u in _b("urls") if isinstance(u, str) and u]
    if not urls and isinstance(cand.get("url"), str) and cand["url"]:
        urls = [cand["url"]]
    if not urls:
        return None  # no endpoint -> cannot honestly draft an entry.
    url = urls[0]
    host = (urlparse(url).hostname or "").lower()

    entry_id = _b("id") or f"user-{_slug(host or 'source')}-{new_ulid()[-8:].lower()}"
    name = (
        _b("name")
        or (cand.get("title") if isinstance(cand.get("title"), str) else None)
        or host
        or entry_id
    )
    description = (
        _b("description")
        or f"User-added source via Mode 2 catalog addition ({host or url})."
    )
    access_tier = (
        _b("access_tier")
        or (findings.access_tier_inferred if findings is not None else None)
        or 3
    )
    # credential_tier: draft ONLY as tier 1 (key-free). Tier >= 2 requires an
    # api_key_secret_ref we do not have at draft time; downgrading keeps the
    # entry valid against the CatalogEntry cross-field rule (honest, not a shim).
    ttl_class = _b("ttl_class") or "semi-static-7d"
    source_class = (
        _b("source_class")
        or (
            cand.get("suggested_tool_kind")
            if isinstance(cand.get("suggested_tool_kind"), str)
            else None
        )
        or "user_added"
    )
    license_txt = (
        _b("license_claim")
        or (findings.license_observed if findings is not None else None)
        or "Unknown (user-proposed, unverified)"
    )
    how_to_use = (
        _b("how_to_use")
        or (
            f"User-proposed endpoint {url}. Fetch via web_fetch or the generic "
            "OGC/HTTP adapters; verify the response shape before relying on it."
        )
    )
    citation = (
        f"{name} -- {host or url} (user-proposed via Mode 2, added "
        f"{now_utc().date().isoformat()})"
    )
    try:
        return CatalogEntry(
            id=entry_id,
            name=name,
            description=description,
            urls=urls,
            access_tier=access_tier,
            credential_tier=1,
            ttl_class=ttl_class,
            source_class=source_class,
            license=license_txt,
            citation=citation,
            vintage=None,
            last_verified=now_utc().isoformat(),
            status="active",
            how_to_use=how_to_use,
            api_key_secret_ref=None,
        )
    except Exception:  # noqa: BLE001 -- surface the honest failure, no shim
        logger.warning(
            "catalog completion: could not build a valid CatalogEntry id=%r",
            entry_id,
            exc_info=True,
        )
        return None


async def _handle_catalog_addition_response(
    websocket: ServerConnection,
    state: SessionState,
    car: "CatalogAdditionResponsePayload",
) -> None:
    """Mode 2 offer-to-add completion (the server half of the loop).

    On ACCEPT: draft + complete the entry (edited entry / candidate / probe),
    APPEND it to the user-overlay catalog, and reset the catalog cache so
    ``search_data_catalog`` finds it on the very next load. On REJECT / cancel:
    just resolve (drop) the pending offer. Best-effort -- never raises into the
    message loop; a probe / append fault degrades honestly and logs.
    """
    candidate = _pop_pending_catalog_offer(state.session_id, car.request_id)

    if car.cancelled or car.decision != "accept":
        logger.info(
            "catalog-addition-response resolved session=%s request_id=%s "
            "decision=%s cancelled=%s (offer dropped)",
            state.session_id,
            car.request_id,
            car.decision,
            car.cancelled,
        )
        return

    base = car.edited_catalog_entry
    if base is None and candidate is None:
        logger.warning(
            "catalog-addition-response accept with NO pending offer and NO "
            "edited entry session=%s request_id=%s -- cannot draft; ignored",
            state.session_id,
            car.request_id,
        )
        return

    url_for_probe: str | None = None
    if base is not None and base.urls:
        url_for_probe = base.urls[0]
    elif candidate and isinstance(candidate.get("url"), str):
        url_for_probe = candidate["url"]
    findings = await _probe_catalog_endpoint(url_for_probe)

    entry = _complete_catalog_entry(base, candidate, findings)
    if entry is None:
        logger.warning(
            "catalog-addition-response accept could not complete a valid entry "
            "session=%s request_id=%s",
            state.session_id,
            car.request_id,
        )
        return

    try:
        from .agent.tools.search.catalog_common import append_user_catalog_entry

        await asyncio.to_thread(append_user_catalog_entry, entry)
    except Exception:  # noqa: BLE001 -- append fault must not break the loop
        logger.exception(
            "catalog-addition-response overlay append failed session=%s id=%s",
            state.session_id,
            entry.id,
        )
        return

    logger.info(
        "catalog-addition-response ACCEPTED session=%s request_id=%s appended "
        "catalog id=%s url=%s",
        state.session_id,
        car.request_id,
        entry.id,
        entry.urls[0],
    )


# ---------------------------------------------------------------------------
# Routing-layer typed exceptions (FR-AS-11 surface).
#
# These live here rather than in a shared exceptions module because they are
# raised exclusively inside ``_invoke_tool_via_emitter`` -- the server-side
# routing layer. They follow the same FR-AS-11 contract as the tool-level
# typed exceptions (``WDPAError``, ``HRSLError``, etc.): ``error_code`` is a
# SCREAMING_SNAKE_CASE string and ``retryable`` is False for both (the LLM
# cannot retry its way out of a missing tool registration; it must revise its
# function-call decision).
#
# ``summarize_tool_result`` in ``adapter.py`` harvests ``error_code`` +
# ``retryable`` from any exception that carries them, so these propagate as
# a full structured error envelope to the model -- the same shape as any
# ``fetch_*`` / ``compute_*`` typed exception.
# ---------------------------------------------------------------------------


class ToolNotFoundError(RuntimeError):
    """Raised when ``_invoke_tool_via_emitter`` receives a tool name that is
    not registered in ``TOOL_REGISTRY``.

    ``retryable=False``: Gemini cannot retry its way to a registration it
    invented -- it must revise its call (use a different tool, narrate that
    it cannot help, or ask for clarification).

    The ``valid_tools`` attribute carries the first 20 registered names so
    the Gemini function-response payload gives the LLM a correction hint
    without blowing the response character budget.
    """

    error_code: str = "TOOL_NOT_FOUND"
    retryable: bool = False

    def __init__(self, tool_name: str, valid_tools: list[str]) -> None:
        # Limit to first 20 names to stay within _FUNCTION_RESPONSE_CHAR_BUDGET.
        hint = valid_tools[:20]
        super().__init__(
            f"tool {tool_name!r} not in TOOL_REGISTRY; "
            f"valid tools (first 20): {hint}"
        )
        self.tool_name = tool_name
        self.valid_tools = hint


class PayloadWarningCancelledError(RuntimeError):
    """Raised when the payload-warning gate skips dispatch because the user
    chose ``cancel`` or the gate timed out.

    ``retryable=False``: the user explicitly declined; Gemini should narrate
    the cancellation honestly and not re-issue the same call without narrower
    scope.
    """

    error_code: str = "PAYLOAD_WARNING_CANCELLED"
    retryable: bool = False

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"tool {tool_name!r} dispatch cancelled via payload-warning gate "
            "(user chose 'cancel' or gate timed out)"
        )
        self.tool_name = tool_name


class CodeExecConfirmationCancelledError(RuntimeError):
    """Raised when the ``code_exec_request`` confirm gate denies the run
    because the user chose ``cancel`` or the gate timed out.

    Running arbitrary Python is a consequential action; the gate fails closed.
    ``retryable=False``: the user explicitly declined to run THIS code -- Gemini
    should narrate the decline honestly and not re-issue the identical snippet
    without the user changing course.
    """

    error_code: str = "CODE_EXEC_CANCELLED"
    retryable: bool = False

    def __init__(self, code_exec_id: str) -> None:
        super().__init__(
            f"code_exec_request {code_exec_id!r} cancelled at the confirm gate "
            "(user chose 'cancel' or gate timed out); the sandbox did not run"
        )
        self.code_exec_id = code_exec_id


class CodeExecApprovalTimeoutError(RuntimeError):
    """Raised when the ``code-exec-request`` approval card was never answered.

    Distinct from :class:`CodeExecConfirmationCancelledError` (an explicit
    user decision): here NOBODY answered the card within the approval
    window -- e.g. a client with no handler for the envelope leaves the
    parked tool call waiting forever.

    ``retryable=False``: re-issuing the identical snippet would just park on
    another unanswered card; the LLM should narrate that the approval card was
    not answered and let the user decide how to proceed. ``summarize_tool_result``
    harvests ``error_code`` + ``retryable`` so this reaches the LLM as a
    structured function_response and the turn completes.
    """

    error_code: str = "CODE_EXEC_APPROVAL_TIMEOUT"
    retryable: bool = False

    def __init__(self, code_exec_id: str, timeout_s: float) -> None:
        super().__init__(
            f"code_exec_request {code_exec_id!r} approval card was not answered "
            f"within {timeout_s:.0f}s (no confirmation arrived from the user "
            "interface); the sandbox did not run. Tell the user their approval "
            "was required but never received, and do not re-issue the identical "
            "snippet unless they ask to retry."
        )
        self.code_exec_id = code_exec_id
        self.timeout_s = timeout_s


class SolverConfirmationCancelledError(RuntimeError):
    """Raised when a solver confirm gate denies the dispatch.

    A solver run is a consequence (FR-AS-8 / Invariant 9): the user must
    approve the derived forcing parameters before the model executes. Cancel,
    timeout, and disconnect all fail closed. ``retryable=False`` so Gemini
    narrates the decline honestly instead of re-dispatching the same run.
    """

    error_code: str = "SOLVER_CONFIRMATION_CANCELLED"
    retryable: bool = False

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"{tool_name} declined at the parameter-confirmation gate "
            "(user chose 'cancel' or the gate timed out); the solver did not run"
        )
        self.tool_name = tool_name


# Tools whose dispatch is a consequence (a solver run, FR-AS-8 / Invariant 9)
# and MUST pass a parameter-confirmation gate on the LLM path. The gate runs
# the composer's PURE extraction to build the confirm card, blocks on the
# same pending_payload_warnings future seam as payload-warning/code-exec,
# and injects confirmed=True only after the user approves.
SOLVER_CONFIRM_TOOLS: set[str] = {
    "run_model_groundwater_contamination_scenario",
    # engine-door refactor: run_model_contamination_affected_fields is CUT
    # (its zonal field-analysis half re-homed to a playground recipe). The
    # MODFLOW plume templates were NOT confirm-gated before the fold, so
    # gating parity is preserved by NOT adding them here.
    # Flood solvers are gated too: an unrequested SFINCS solve must not run
    # without confirmation. The card is built from the call args
    # (location/return period/duration).
    # engine-door refactor (SFINCS slice): run_model_flood_scenario is now
    # the sfincs_flood template (the door run_sfincs executes no solve; the
    # TEMPLATE submits the solver, so the gate keys on the template).
    "sfincs_flood",
    # The granularity gate: the SWMM urban-flood solver joins the confirm
    # set with an ENRICHED card carrying a GranularitySuggestion (the
    # autoscaler's pre-run resolution ladder + estimated cells / solve time /
    # compute class). The user can override the rung before the heavy solve
    # via the existing tool-payload-confirmation ``narrow_scope`` path.
    # engine-door refactor (SWMM slice): run_swmm_urban_flood is now the
    # swmm_urban_flood template (the door run_swmm executes no solve; the
    # TEMPLATE submits the solver, so the gate keys on the template).
    "swmm_urban_flood",
    # The OpenQuake classical-PSHA solver joins the confirm set (Invariant 9
    # -- a consequential long Batch run must be user-confirmed). It dispatches
    # an area-source PSHA over the whole bbox via run_solver ('openquake'),
    # so it is a solve like SFINCS/SWMM/MODFLOW, not a fetch. The gate emits
    # a simple proceed/cancel card (no granularity picker): the area source
    # spans the whole AOI, so no rupture/incident-area user input is needed
    # for classical PSHA (that is scenario mode, which is not built).
    # engine-door refactor (OPENQUAKE slice): re-keyed run_seismic_hazard_psha
    # -> openquake_psha (the template that submits the solver; the
    # run_openquake door runs no solve).
    "openquake_psha",
    # The TELEMAC river-dye solver joins the confirm set with the richest
    # card yet: the builder runs the fast mesh-only worker (gmsh, no DEM, no
    # solve, ~10-25 s), emits the actual triangle-wireframe mesh onto the
    # map as a role="input" vector layer, and the card carries a
    # GranularitySuggestion (mesh_resolution_m ladder + real node/element
    # counts + CFL-coupled dt + conservative solve estimate). The user sees
    # the mesh before approving the expensive solve; narrow_scope re-runs
    # with a different edge length. Keyed on the TEMPLATE that submits the
    # solver (the run_telemac name is now the read-only door, which runs no
    # solve; telemac_river_dye is the tool the gate must intercept).
    "telemac_river_dye",
    # The ELMFIRE wildfire-spread solver joins the confirm set (Invariant 9
    # -- a consequential solver run: LANDFIRE fetches + a containerized
    # level-set solve). The card is built by _build_fire_confirm_envelope
    # from the call args: approximate grid cell count + a calibrated runtime
    # estimate + the scenario weather, so the user confirms the actual run
    # about to dispatch. Simple proceed/cancel (no granularity picker at v1
    # -- cellsize_m is an explicit tool arg the LLM can restate).
    # engine-door refactor (ELMFIRE slice): re-keyed model_fire_spread ->
    # elmfire_fire_spread (the template that submits the solver; the
    # run_elmfire door runs no solve).
    "elmfire_fire_spread",
    # The GeoClaw shallow-water inundation solver joins the confirm set
    # (Invariant 9 -- a consequential solver run: DEM/topobathy fetch + a
    # containerized Clawpack solve). Mirrors the psha/fire wiring: a simple
    # proceed/cancel card built inline by _build_geoclaw_confirm_envelope
    # from the call args (AOI area + scenario + sim window + AMR levels). No
    # granularity picker at v1 (amr_levels / sim_duration_s are explicit
    # tool args the LLM can restate). Keyed on the geoclaw_inundation
    # TEMPLATE that submits the solver; the run_geoclaw door runs no solve.
    "geoclaw_inundation",
}


# The granularity gate widened to the two HEAVY raster FETCHERS (DEM +
# topobathy) so the user controls fetch resolution before a big
# download/merge -- same confirm machinery, same GranularitySuggestion card.
# Kept a SEPARATE set from SOLVER_CONFIRM_TOOLS on purpose: a fetch is NOT
# a solve. The gate-trigger below fires for the UNION; the solver-only
# confirmed/enable_autoscale injection stays guarded to SOLVER_CONFIRM_TOOLS.
FETCH_CONFIRM_TOOLS: set[str] = {
    "fetch_dem",
    "fetch_topobathy",
    "fetch_landcover",
}

#: job duplicate-flood-layer (SAFETY NET): tokens that mark a FLOOD / DEPTH COG
#: (vs terrain / land-cover / plume / generic rasters). Used at the publish_layer
#: wrap-site so a re-publish of a flood-depth COG that arrives with an EMPTY
#: style_preset is defaulted to ``continuous_flood_depth`` (white->blue->green)
#: instead of "" -- an empty preset makes TiTiler fall back to viridis and paints
#: a redundant styleless flood layer (the exact duplicate-flood-layer symptom).
#: Token-boundary matched (not substring) so e.g. ``demo`` never trips ``dem``.
_FLOOD_DEPTH_STYLE_TOKENS: frozenset[str] = frozenset(
    {"flood", "depth", "inundation", "floodepth"}
)
_DEFAULT_FLOOD_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"


def _is_flood_depth_cog(layer_uri: str, layer_id: str) -> bool:
    """True when the resolved URI or layer_id tokenizes to a FLOOD/DEPTH raster.

    Token-boundary matching on non-alphanumerics so ``flood-depth-peak-<run_id>``
    and a ``.../flood_depth_peak.tif`` URI both match, while ``demo``/``dem`` do
    not. Conservative: an unrecognized raster returns False (keeps the existing
    empty-preset / QGIS-default behavior for non-flood rasters)."""
    import re as _re

    tokens = set(_re.split(r"[^a-z0-9]+", f"{layer_uri} {layer_id}".lower()))
    return bool(tokens & _FLOOD_DEPTH_STYLE_TOKENS)


def _resolve_publish_wrap_style_preset(
    *, style_preset: str | None, layer_uri: str, layer_id: str
) -> str:
    """Style preset for the publish_layer wrap-site LayerURI (job
    duplicate-flood-layer SAFETY NET).

    Honors an explicit non-empty ``style_preset`` (the LLM / tool asked for it).
    When it resolves EMPTY, default a flood/depth COG to
    ``continuous_flood_depth`` so a redundant re-publish is never styleless
    (which TiTiler renders as viridis). Non-flood rasters keep ``""`` (QGIS /
    TiTiler default) exactly as before -- terrain auto-scales, paletted COGs use
    their embedded color table."""
    preset = (style_preset or "").strip()
    if preset:
        return preset
    if _is_flood_depth_cog(layer_uri, layer_id):
        return _DEFAULT_FLOOD_DEPTH_STYLE_PRESET
    return ""


def _is_droppable_object_store_raster(value: Any) -> bool:
    """True iff ``value`` is exactly the LayerURI class ``emit_layer_uri`` DROPS.

    The deterministic auto-publish targets precisely the LayerURIs that
    ``layer_uri_emit.emit_layer_uri`` refuses to deliver: a RENDERABLE
    RASTER carrying a raw object-store uri (``s3://`` / ``gs://``), which
    MapLibre cannot fetch. Those must be converted to an http(s) tile URL
    via publish_layer before they can render. A vector (inline-GeoJSON path),
    an http(s)-uri raster (already renderable), or any non-LayerURI return
    is NOT a candidate. ``PlumeLayerURI`` / ``SeepageLayerURI`` are LayerURI
    subclasses, so ``isinstance(..., LayerURI)`` covers them.
    """
    if not isinstance(value, LayerURI):
        return False
    if value.layer_type != "raster":
        return False
    uri = value.uri or ""
    return uri.startswith("s3://") or uri.startswith("gs://")


#: Result keys that mark a dispatch as having PRODUCED a real artifact -- a
#: published / registered layer, a stored object, a feature set. Used by the
#: loop-watchdog progress witness: a round that produces one of these is
#: ADVANCING the Case (a new layer/handle appears) even if the model
#: pathologically repeats the same call, so it is allowed to run to the step
#: cap / loop-exhausted envelope rather than being watchdog-aborted. A
#: bare-ack wedge shape (``{"ok": True}`` re-issued forever) carries none of
#: these and so loads the no-progress streak.
_PROGRESS_RESULT_KEYS: tuple[str, ...] = (
    "layer_id",
    "wms_url",
    "uri",
    "layer_uri",
    "feature_count",
)


def _dispatch_made_progress(result: Any) -> bool:
    """True iff a single tool dispatch produced a real artifact.

    A ``LayerURI`` return (any subclass) is always progress -- a renderable
    layer was produced. A dict carrying a layer/handle/feature signal
    (:data:`_PROGRESS_RESULT_KEYS`) is progress. Everything else -- a bare ack
    (``{"ok": True}``), ``None``, a primitive, an empty dict -- is NOT progress:
    that is the no-op-repeat shape the watchdog must catch.
    """
    if isinstance(result, LayerURI):
        return True
    if isinstance(result, dict):
        return any(
            result.get(k) not in (None, "", [], {})
            for k in _PROGRESS_RESULT_KEYS
        )
    return False


#: How many CONSECUTIVE no-progress model rounds we tolerate AFTER a
#: terminal composer has delivered its artifact before concluding the turn
#: cleanly. Symptom without this: a SFINCS flood publishes its depth layer
#: and the model, having nothing left to do, keeps emitting unproductive
#: function calls until it trips ``MAX_TURN_ITERATIONS`` and emits a
#: (harmless but sloppy) ``loop_exhausted`` frame. Once the deliverable is
#: in hand we (a) stamp the composer's function_response with a one-time
#: wrap-up directive so a well-behaved model just summarizes and stops, and
#: (b) keep this small safety budget: if the model spins
#: ``_POST_DELIVERABLE_WRAPUP_ROUNDS`` rounds in a row without producing
#: anything new, we conclude the turn cleanly instead of letting it run to
#: the cap. A round that produces genuine follow-up work
#: (``_dispatch_made_progress``) RESETS the streak, so legitimate
#: multi-deliverable flows are never cut off. This is NOT the runaway
#: guard: a turn that never produced a terminal deliverable still runs to
#: the cap / watchdog exactly as before.
_POST_DELIVERABLE_WRAPUP_ROUNDS: int = 2

#: The one-time wrap-up directive stamped onto a terminal composer's
#: function_response the moment it delivers (see ``_is_terminal_composer``).
_DELIVERABLE_COMPLETE_DIRECTIVE: str = (
    "DELIVERABLE COMPLETE: this run produced its primary result and any "
    "layers are already published to the user's map. Unless the user "
    "explicitly asked for ADDITIONAL analysis beyond this, do NOT call more "
    "tools -- give a brief (1-3 sentence) final summary of what was produced "
    "and stop. Calling further tools now will not improve the answer."
)

#: EMPTY-COMPLETION RETRY: the local qwen3 model occasionally returns a
#: round with ZERO tool calls AND ZERO non-whitespace text. This is NOT
#: context overflow (that is the compaction/clip guard) -- the model has
#: room and simply emits nothing. The loop RETRIES the round with a
#: corrective user-role nudge appended (production tool-runner pattern:
#: OpenAI tool-runner / LangChain retry-with-nudge, not a blind resend),
#: BOUNDED by this cap so an always-empty model can never loop forever
#: (same safety discipline as the loop watchdog). Scoped to the LOCAL
#: (MODEL_PROVIDER=openai) path only -- a legitimately empty Bedrock round
#: must NOT change.
_EMPTY_COMPLETION_RETRY_CAP: int = 2

#: The corrective user-role nudge appended to ``contents`` before a retried
#: empty round (OPEN-16). Plain instruction -- either act (tool) or answer;
#: never another empty message.
_EMPTY_COMPLETION_NUDGE: str = (
    "Your previous response was empty. Either call the appropriate tool to "
    "fulfill the request, or reply with your answer. Do not return an empty "
    "message."
)

#: DISCOVERY-EXPANDS-GATE (task 2): the max number of NEW tool names the
#: tool-search tool's results may add to a turn's visible gate, summed across
#: the whole turn. Bounds the widening so a chatty search cannot re-expand the
#: gate back toward the full catalog it was meant to trim.
_DISCOVERY_EXPAND_CAP: int = 8

#: ENGINE-DOOR gate expansion: an engine door lists a CLOSED, CURATED set - its
#: own engine's registered tier=template members (11 for MODFLOW). The open-ended
#: ``_DISCOVERY_EXPAND_CAP`` was calibrated to bound an UNBOUNDED ranked tail from
#: dataset discovery; capping a door's curated listing at 8 would hide templates
#: and silently break select-then-call. Doors get this separate, larger cap
#: (>= the largest engine's template count, with headroom) applied ONLY on the
#: door branch; the open-ended search_tools branch keeps _DISCOVERY_EXPAND_CAP.
_DOOR_EXPAND_CAP: int = 24


def _tool_search_tool_names() -> frozenset[str]:
    """The registered name(s) of the tool-search (data-discovery) tool.

    Resolved by REGISTRY LOOKUP off the discovery module's own registration
    metadata (``search_tools``, formerly ``discover_dataset``) rather than a
    hardcoded literal, so the parallel rename lands transparently. Any legacy
    alias still present in the live registry is also honored. Never raises: a
    resolution fault yields the empty set (the expand simply no-ops).
    """
    names: set[str] = set()
    try:
        from .agent.tools.search.search_tools.search_tools import _SEARCH_TOOLS_METADATA

        if getattr(_SEARCH_TOOLS_METADATA, "name", None):
            names.add(_SEARCH_TOOLS_METADATA.name)
    except Exception:  # noqa: BLE001 -- module shape drift must not break dispatch
        logger.debug("discovery-expand: search_tools metadata lookup failed",
                     exc_info=True)
    for _legacy in ("discover_dataset",):
        if _legacy in TOOL_REGISTRY:
            names.add(_legacy)
    return frozenset(names)


def _engine_door_tool_names() -> frozenset[str]:
    """Every registered engine-door name (``metadata.tier == "door"``).

    Resolved by REGISTRY LOOKUP (never a hardcoded literal) so a new engine
    door is picked up automatically the moment it registers. A door is a
    gate-expander by construction: its result carries the ``templates`` list the
    server unions into the turn's visible set. Never raises: a lookup fault
    yields the empty set (the door simply does not expand).
    """
    names: set[str] = set()
    try:
        for _name, _entry in TOOL_REGISTRY.items():
            if getattr(_entry.metadata, "tier", "general") == "door":
                names.add(_name)
    except Exception:  # noqa: BLE001 -- registry shape drift must not break dispatch
        logger.debug("engine-door: registry lookup failed", exc_info=True)
    return frozenset(names)


def _default_declarable_registry() -> dict[str, Any]:
    """The DEFAULT per-turn declarable tool set: the full ``TOOL_REGISTRY``
    MINUS every ``tier="template"`` engine template.

    ENGINE-DOOR invariant: engine templates (``sfincs_flood``, ``modflow_*``,
    ``openquake_psha``, ...) surface to the model ONLY via their door's gate
    expansion (see ``_gate_expander_tool_names`` and the door-expand block in
    ``_stream_model_reply``). Declaring them by default -- which the raw
    ``TOOL_REGISTRY`` default did in the gating-off / retrieval-off / fail-open
    paths -- defeats the door architecture. This is the SINGLE default-registry
    seam: the door expand re-adds the specific templates it lists back into the
    per-turn ``_retrieval_registry`` (and the Case allowed-set), so an expanded
    template stays declarable while the un-expanded rest never leak.

    Resolved by REGISTRY LOOKUP (never a literal) so a new template is excluded
    the moment it registers with ``tier="template"``. Mirrors the pool-side
    filter in ``tools.discovery.tool_retrieval`` (fail-open dump) so the
    default-declaration path and the retrieval pool exclude templates
    identically.
    """
    return {
        name: entry
        for name, entry in TOOL_REGISTRY.items()
        if getattr(entry.metadata, "tier", "general") != "template"
    }


def _gate_expander_tool_names() -> frozenset[str]:
    """The union of gate-expanders: the tool-search tool(s) AND every engine door.

    A call to any of these expands the turn's visible gate with the tool names
    its result names (search_tools -> ``results[].tool_name``; an engine door ->
    ``templates[].tool_name``). See ``_tool_names_from_search_result`` for the
    extraction and the dispatch post-processing for the union + cap.
    """
    return _tool_search_tool_names() | _engine_door_tool_names()


def _tool_names_from_search_result(result: Any) -> list[str]:
    """Extract the ranked tool names from a gate-expander result payload.

    ``search_tools`` returns ``{"results": [{"tool_name": <name>, ...}, ...]}``;
    an engine door returns ``{"templates": [{"tool_name": <name>, ...}, ...]}``.
    Reads ``results`` first, falling back to ``templates`` when ``results`` is
    absent/empty. Returns the names in listing order (best first), de-duplicated.
    Tolerant of a malformed / partial shape -- a non-conforming entry is
    skipped, never raised on.
    """
    if not isinstance(result, dict):
        return []
    rows = result.get("results") or result.get("templates")
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("tool_name")
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _is_terminal_composer(tool_name: str) -> bool:
    """True iff ``tool_name`` is a top-level run-a-model composer.

    A terminal composer is a ``run_*`` workflow-dispatch tool (the
    ``run_model_*`` / ``run_*_job`` / ``swmm_urban_flood`` /
    ``openquake_psha`` family) -- the deliverable-producing entry
    points whose successful return IS the answer the user asked for. Helper
    workflow-dispatch tools that merely compute an intermediate
    (``compute_cross_section``, ``request_spatial_input``, ...) are
    deliberately EXCLUDED by the ``run_`` prefix: drawing geometry or computing
    a profile is mid-pipeline, not a turn-ending deliverable.
    """
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return False
    # Engine TEMPLATES carry deliverable-producing
    # names that do NOT start with ``run_`` (``modflow_contaminant_plume`` et al.).
    # A completed template IS a turn-ending deliverable, so ALSO latch any
    # tier="template" workflow-dispatch tool - otherwise the crisp-end wrap-up +
    # post-deliverable idle reset never fire and the turn spins to the loop cap.
    is_workflow_dispatch = (
        getattr(entry.metadata, "source_class", None) == "workflow_dispatch"
    )
    is_template = getattr(entry.metadata, "tier", "general") == "template"
    return is_workflow_dispatch and (tool_name.startswith("run_") or is_template)


# --------------------------------------------------------------------------- #
# Session-scoped confirmation registry
# --------------------------------------------------------------------------- #
#
# One module-level registry keyed on the (globally unique, unguessable ULID)
# warning_id, tagged with the owning session_id. Any connection's inbound
# ``tool-payload-confirmation`` handler can resolve a pending gate as long as
# the session matches, since the client can open multiple WebSocket
# connections per browser session. Shared by every confirmation gate --
# payload warning, code-exec, solver.

_PENDING_CONFIRMATIONS: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_confirmation(
    session_id: str, warning_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_CONFIRMATIONS[warning_id] = (session_id, fut)


def _pop_pending_confirmation(warning_id: str) -> None:
    _PENDING_CONFIRMATIONS.pop(warning_id, None)


def _resolve_pending_confirmation(
    session_id: str, conf: "PayloadConfirmationEnvelopePayload"
) -> bool:
    """Complete the pending gate future for ``conf.warning_id``.

    Returns True when a live future was resolved. False when the warning_id is
    unknown/already-resolved, or when the confirming session is not the owner
    (cross-session confirmation is refused loudly -- the warning_id is an
    unguessable ULID, but defense-in-depth costs one string compare).
    """
    entry = _PENDING_CONFIRMATIONS.get(conf.warning_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "tool-payload-confirmation REFUSED: session=%s is not the owner "
            "(owner=%s) for warning_id=%s",
            session_id,
            owner_session,
            conf.warning_id,
        )
        return False
    if fut.done():
        _PENDING_CONFIRMATIONS.pop(conf.warning_id, None)
        return False
    fut.set_result(conf)
    _PENDING_CONFIRMATIONS.pop(conf.warning_id, None)
    return True


# --------------------------------------------------------------------------- #
# Session-scoped pending-CREDENTIAL registry
# --------------------------------------------------------------------------- #
#
# Mirrors ``_PENDING_CONFIRMATIONS`` (the payload-warning / code-exec / solver
# gate registry) but for the credential-request flow: when a keyed tool
# dispatch hits a missing/invalid credential the dispatch coroutine pauses on
# a future keyed by the credential ``request_id``, having emitted a
# ``credential-request`` envelope. The inbound ``credential-provided``
# handler (which may arrive on a different WebSocket connection of the same
# session) resolves the future, and the paused dispatch retries the tool
# (which now reads the user's freshly-saved vault key). Tagged with the
# owning session_id so a cross-session credential-provided is refused.
_PENDING_CREDENTIALS: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_credential(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_CREDENTIALS[request_id] = (session_id, fut)


def _pop_pending_credential(request_id: str) -> None:
    _PENDING_CREDENTIALS.pop(request_id, None)


def _resolve_pending_credential(
    session_id: str, provided: "CredentialProvidedEnvelopePayload"
) -> bool:
    """Complete the pending credential future for ``provided.request_id``.

    Returns True when a live future was resolved. False when the request_id is
    unknown/already-resolved, or when the answering session is not the owner
    (refused loudly -- the request_id is an unguessable ULID, but the string
    compare is cheap defense-in-depth, matching ``_resolve_pending_confirmation``).
    """
    entry = _PENDING_CREDENTIALS.get(provided.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "credential-provided REFUSED: session=%s is not the owner "
            "(owner=%s) for request_id=%s",
            session_id,
            owner_session,
            provided.request_id,
        )
        return False
    if fut.done():
        _PENDING_CREDENTIALS.pop(provided.request_id, None)
        return False
    fut.set_result(provided)
    _PENDING_CREDENTIALS.pop(provided.request_id, None)
    return True


# --------------------------------------------------------------------------- #
# job AGENT-AOI-RESIDUAL (#159): turn zoom-to accumulator helpers
# --------------------------------------------------------------------------- #
def _is_finite_bbox4(bbox: Any) -> bool:
    """True iff ``bbox`` is a 4-tuple/list of finite real numbers.

    Guards the LayerURI floored-bbox append so a None / wrong-length /
    NaN / inf bbox never lands a bad zoom-to in ``current_turn_map_commands``.
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return False
    for v in bbox:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if not math.isfinite(float(v)):
            return False
    return True


def _coerce_bbox4(value: Any) -> tuple[float, float, float, float] | None:
    """Coerce ``value`` into a finite 4-float bbox tuple, else ``None``.

    Shared by the LANE-C AOI-pin + fetch-default helpers. Tolerates list/tuple of
    4 numbers; rejects strings, wrong lengths, and non-finite values (so a bad
    extent never becomes a pinned AOI or a forced fetch bbox).
    """
    if not _is_finite_bbox4(value):
        return None
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _aoi_zoom_to_bbox(
    result: Any, current_turn_map_commands: list[dict]
) -> tuple[float, float, float, float] | None:
    """Return the bbox the camera should snap to for a tool ``result``.

    Fires whenever an AOI/bbox is established, not only on a
    ``geocode_location`` result -- the user giving coordinates directly skips
    geocode, so without this the map never moved to "where we are" until a
    downstream layer with a bbox landed.

    Prefers a top-level ``bbox``, falling back to ``aoi_bbox`` (the
    request_spatial_input / draw result shape). Returns ``None`` when:
      - ``result`` is not a dict, or carries no finite 4-number extent, OR
      - the extent equals the turn's LAST zoom-to bbox (dedupe: a chain of
        bbox-bearing tools over the SAME AOI must not re-snap repeatedly).
    Pure + side-effect-free so the caller owns the emit + accumulator append.
    """
    if not isinstance(result, dict):
        return None
    raw = result.get("bbox")
    if not _is_finite_bbox4(raw):
        raw = result.get("aoi_bbox")
    aoi = _coerce_bbox4(raw)
    if aoi is None:
        return None
    last = _last_zoom_to_bbox(current_turn_map_commands)
    if last is not None and list(aoi) == list(last):
        return None  # already snapped to this exact AOI this turn.
    return aoi


def _last_zoom_to_bbox(commands: list[dict]) -> list | None:
    """Return the bbox of the most-recent ``zoom-to`` entry, else None.

    Mirrors the web ``extractLastZoomTo`` newest-first walk so the dedupe
    here compares against the SAME bbox the client would replay.
    """
    for cmd in reversed(commands):
        if isinstance(cmd, dict) and cmd.get("command") == "zoom-to":
            args = cmd.get("args")
            if isinstance(args, dict):
                bbox = args.get("bbox")
                if isinstance(bbox, (tuple, list)):
                    return list(bbox)
            return None
    return None


# --------------------------------------------------------------------------- #
# Session-scoped pending-REGION-CHOICE registry (region-disambiguation picker)
# --------------------------------------------------------------------------- #
#
# Mirrors ``_PENDING_CREDENTIALS`` exactly, but for the region-choice flow: when
# a ``geocode_location`` result comes back as a state-bbox-fallback snap, the
# dispatch coroutine emits a ``region-choice-request`` envelope (whole-state
# default + candidate counties) and pauses on a future keyed by the choice
# ``request_id``. The inbound ``region-choice-provided`` handler (which may
# arrive on a DIFFERENT WebSocket connection of the same session -- StrictMode
# double-mount / reconnect) resolves the future, and the paused dispatch either
# narrows the geocode bbox to the picked region or keeps the whole-state bbox.
# Fail-open: on timeout / no client the whole-state bbox (already the geocode
# result) is used unchanged so the automated path never blocks. Tagged with the
# owning session_id so a cross-session region-choice-provided is refused.
_PENDING_REGION_CHOICES: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_region_choice(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_REGION_CHOICES[request_id] = (session_id, fut)


def _pop_pending_region_choice(request_id: str) -> None:
    _PENDING_REGION_CHOICES.pop(request_id, None)


def _resolve_pending_region_choice(
    session_id: str, provided: "RegionChoiceProvidedEnvelopePayload"
) -> bool:
    """Complete the pending region-choice future for ``provided.request_id``.

    Returns True when a live future was resolved. False when the request_id is
    unknown/already-resolved, or when the answering session is not the owner
    (refused loudly -- mirrors ``_resolve_pending_credential``).
    """
    entry = _PENDING_REGION_CHOICES.get(provided.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "region-choice-provided REFUSED: session=%s is not the owner "
            "(owner=%s) for request_id=%s",
            session_id,
            owner_session,
            provided.request_id,
        )
        return False
    if fut.done():
        _PENDING_REGION_CHOICES.pop(provided.request_id, None)
        return False
    fut.set_result(provided)
    _PENDING_REGION_CHOICES.pop(provided.request_id, None)
    return True


# --------------------------------------------------------------------------- #
# Session-scoped pending-SPATIAL-INPUT registry (FR-AS-10 request_spatial_input)
# --------------------------------------------------------------------------- #
#
# Mirrors ``_PENDING_REGION_CHOICES`` exactly, but for the FR-WC-16 urban
# vector-draw flow: when the LLM (or the urban-flood flow) calls
# ``request_spatial_input``, the dispatch coroutine emits a
# ``spatial-input-request`` envelope (point / bbox / vector_draw) and pauses on a
# future keyed by the request ``request_id``. The inbound
# ``spatial-input-response`` handler (which may arrive on a DIFFERENT WebSocket
# connection of the same session -- StrictMode double-mount / reconnect) resolves
# the future with the drawn ``FeatureCollection`` (or a cancellation). Tagged
# with the owning session_id so a cross-session response is refused. Fail-open:
# on timeout / no client the gate resolves to ``None`` and the caller surfaces a
# typed "no geometry drawn" result (honest -- never a fabricated AOI/barriers).
_PENDING_SPATIAL_INPUTS: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_spatial_input(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_SPATIAL_INPUTS[request_id] = (session_id, fut)


def _pop_pending_spatial_input(request_id: str) -> None:
    _PENDING_SPATIAL_INPUTS.pop(request_id, None)


def _resolve_pending_spatial_input(
    session_id: str, response: "SpatialInputResponsePayload"
) -> bool:
    """Complete the pending spatial-input future for ``response.request_id``.

    Returns True when a live future was resolved. False when the request_id is
    unknown/already-resolved, or when the answering session is not the owner
    (refused loudly -- mirrors ``_resolve_pending_region_choice``).
    """
    entry = _PENDING_SPATIAL_INPUTS.get(response.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "spatial-input-response REFUSED: session=%s is not the owner "
            "(owner=%s) for request_id=%s",
            session_id,
            owner_session,
            response.request_id,
        )
        return False
    if fut.done():
        _PENDING_SPATIAL_INPUTS.pop(response.request_id, None)
        return False
    fut.set_result(response)
    _PENDING_SPATIAL_INPUTS.pop(response.request_id, None)
    return True


class SpatialInputInvalidResponseError(Exception):
    """A spatial-input-response arrived but failed structural validation.

    Carries the typed error the paused ``request_spatial_input`` turn surfaces
    to the LLM (honesty floor: a malformed reply degrades to a typed error, NOT
    a silent success and NOT a hung turn that drains the read TTL). Raised into
    the pending future by ``_fail_pending_spatial_input`` so the awaiting
    dispatch coroutine returns IN-BAND immediately instead of blocking until
    ``default_timeout_seconds`` then degrading to ``SPATIAL_INPUT_TIMEOUT``
    (the FR-WC-16 untagged-barrier mismatch).
    """

    def __init__(self, error_code: str, error_message: str) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message


def _fail_pending_spatial_input(
    session_id: str,
    request_id: str,
    error_code: str,
    error_message: str,
) -> bool:
    """Fail the pending spatial-input future for ``request_id`` with a typed error.

    Used when an inbound ``spatial-input-response`` cannot be parsed/validated
    (e.g. a barrier feature missing ``barrier_type``). Resolves the future
    EAGERLY via ``set_exception`` so the awaiting ``request_spatial_input`` turn
    wakes immediately with a typed error result rather than hanging until the
    read TTL expires. Returns True when a live future was failed; False when the
    request_id is unknown/already-resolved, or the answering session is not the
    owner (refused loudly -- mirrors ``_resolve_pending_spatial_input``).
    """
    entry = _PENDING_SPATIAL_INPUTS.get(request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "spatial-input-response (invalid) REFUSED: session=%s is not the "
            "owner (owner=%s) for request_id=%s",
            session_id,
            owner_session,
            request_id,
        )
        return False
    if fut.done():
        _PENDING_SPATIAL_INPUTS.pop(request_id, None)
        return False
    fut.set_exception(
        SpatialInputInvalidResponseError(error_code, error_message)
    )
    _PENDING_SPATIAL_INPUTS.pop(request_id, None)
    return True


# App-level Persistence singleton. The MongoDB Atlas MCP server is the
# LLM-facing DB path (FR-AS-4, Decision F); ``Persistence`` wraps it with a
# typed surface (CaseSummary / User / SecretRecord / CaseChatMessage). Bound
# at startup if ``TRID3NT_MONGO_MCP_URL`` is set or a stdio MCP config
# resolves; otherwise stays ``None`` and callers fall back to in-memory
# state (the M1 path). Module-level (not per-connection): the MCP client is
# expensive to start, per-session writes only need a typed wrapper not
# connection isolation, and it resets on process restart for tests.
_PERSISTENCE: Persistence | None = None


def get_persistence() -> Persistence | None:
    """Return the app-level ``Persistence`` singleton, or ``None`` if unbound.

    Callers (chiefly the message-dispatch path in this module) MUST handle
    the ``None`` case gracefully -- the M1 in-memory path is still supported
    when the MCP environment is not provisioned (e.g. CI without Atlas).
    """
    return _PERSISTENCE


def set_persistence(p: Persistence | None) -> None:
    """Bind or clear the app-level ``Persistence`` singleton.

    The agent service startup path calls this once after launching the MCP
    client; tests call it directly with a mock-backed ``Persistence`` to
    exercise the wired-in code paths.

    job credential-pipeline-generic: also binds the SAME ``Persistence`` into
    EVERY keyed-tool secret-resolution seam (FIRMS / eBird / ERA5 / GTSM /
    IUCN -- each exposes ``set_persistence_for_secrets``) so a tool dispatched
    with a per-Case ``secret_ref`` can materialize the user's vault key without
    importing the MCP client. (Movebank constructs its own MCP-less Persistence
    inline, so it needs no seam.) Binding here keeps every persistence-set path
    (production MCP, dev file-backed, test mocks) in sync without editing each
    call site.
    """
    global _PERSISTENCE
    _PERSISTENCE = p
    _bind_secret_seams(p)


# Keyed tools that expose a ``set_persistence_for_secrets(p)`` seam. The server
# binds the live Persistence into all of them so any keyed tool can resolve a
# per-Case ``secret_ref`` (vault -> env). Movebank is intentionally absent: it
# builds its own MCP-less Persistence inline for credential resolution.
_SECRET_SEAM_TOOL_MODULES: tuple[str, ...] = (
    "fetchers.hazard.fetch_firms_active_fire",
    "fetchers.biodiversity.fetch_ebird_observations",
    "fetchers.climate.fetch_era5_reanalysis",
    "fetchers.ocean.fetch_gtsm_tide_surge",
    "fetchers.biodiversity.fetch_iucn_red_list_range",
)


def _bind_secret_seams(p: "Persistence | None") -> None:
    """Bind ``p`` into every keyed tool's ``set_persistence_for_secrets`` seam.

    Best-effort per tool: a missing module / seam logs at debug and does not
    abort binding the rest (one tool's import hiccup must not starve the others
    of their vault resolver).
    """
    import importlib

    for mod_name in _SECRET_SEAM_TOOL_MODULES:
        try:
            mod = importlib.import_module(f".tools.{mod_name}", __package__)
            mod.set_persistence_for_secrets(p)
        except Exception:  # noqa: BLE001 -- secret-seam binding is best-effort
            logger.debug(
                "set_persistence: could not bind secret seam for %s",
                mod_name,
                exc_info=True,
            )


async def init_persistence_from_env() -> Persistence | None:
    """Resolve a ``Persistence`` instance from environment configuration.

    GCP is decommissioned: the live MongoDB-MCP (Atlas) bootstrap is GONE
    (``mcp.py`` deleted -- it depended on GCP Secret Manager for the SRV and the
    ``mongodb-mcp-server`` stdio subprocess). On AWS the prod persistence is
    DynamoDB or the file backend, bound by ``main._maybe_bind_dev_persistence``
    / ``dynamo_backend.make_persistence_for_backend`` before this runs.

    This method does NOT clear a pre-bound singleton; it preserves whatever the
    startup path already bound. Returns the ``Persistence`` instance or ``None``.
    """
    # This method does NOT clear a pre-bound singleton. The agent
    # startup path (``main._maybe_bind_dev_persistence`` / DynamoDB binding)
    # may have already bound a ``Persistence``; we preserve it.
    if get_persistence() is not None:
        logger.info("Persistence singleton already bound; retained")
        return get_persistence()
    logger.info("Persistence singleton remains unbound (no backend configured)")
    return None


#: Synthetic owner UID assigned to every pre-Auth Case (a Case written
#: before the Auth track carried no ``user_id`` field). The one-time
#: idempotent startup migration (``persistence.migrate_preauth_cases``)
#: stamps these orphan Cases with this constant so they belong to a single
#: synthetic owner instead of leaking to every user.
#:
#: Chosen as a fixed, non-ULID, obviously-synthetic sentinel so it is
#: trivially greppable in logs / the persisted store and can never collide
#: with a real ULID (26-char Crockford base32).
MIGRATION_ANON_UID = "__preauth_migration_anon__"


async def _run_preauth_case_migration() -> None:
    """One-time idempotent pre-Auth case migration.

    Calls ``Persistence.migrate_preauth_cases(MIGRATION_ANON_UID)`` if a
    Persistence singleton is bound. Cases written before the Auth track had
    no ``user_id`` field; this stamps them with the synthetic owner so each
    Case is visible only to its owner.

    Idempotent: the migration's filter is ``{"user_id": {"$exists": False}}``,
    so a second startup matches nothing. Best-effort: a failure is logged at
    WARNING and never aborts server startup (mirrors the Persistence-init and
    session-touch postures).
    """
    p = get_persistence()
    if p is None:
        logger.info(
            "pre-Auth case migration skipped: no Persistence singleton bound"
        )
        return
    try:
        n = await p.migrate_preauth_cases(MIGRATION_ANON_UID)
        logger.info("pre-Auth case migration complete: %s case(s) stamped", n)
    except Exception:  # noqa: BLE001 -- startup must not abort on migration
        logger.warning("pre-Auth case migration failed (continuing)", exc_info=True)


#: COLDVIEW FRESHNESS BACKFILL: env toggle (default ON) for the daemon-restart
#: sweep that re-materializes every live Case's cold snapshot+manifest. Set
#: TRID3NT_COLDVIEW_BACKFILL=0 to disable (ops escape hatch). Bounded per-Case
#: concurrency keeps the sweep from saturating the S3/store round-trips.
_COLDVIEW_BACKFILL_ENABLED: bool = (
    os.environ.get("TRID3NT_COLDVIEW_BACKFILL", "1").strip().lower()
    not in ("0", "false", "no", "off")
)
_COLDVIEW_BACKFILL_CONCURRENCY: int = max(
    1, int(os.environ.get("TRID3NT_COLDVIEW_BACKFILL_CONCURRENCY", "4"))
)


async def _run_coldview_backfill() -> None:
    """Daemon-restart sweep: re-materialize the cold snapshot+manifest per Case.

    CLOSES THE SNAPSHOT-FRESHNESS GAP. The case-view snapshot
    (``case-views/{id}.json``) and thin manifest (``case-manifests/{id}.json``)
    are only ever (re)written while the daemon is UP -- the 4 mutation
    triggers (create / rename / layer-publish / turn-close) plus case-open. There
    is NO daemon-down materialization path, so a Case that gained layers
    and was then left as the daemon stopped (or whose newest snapshot predates
    its current layers) shows a STALE / empty cold face indefinitely: the exact
    "can't see it until I connect" symptom. The daemon-down cold view fetches
    that presigned snapshot and cannot paint until the daemon restarts and the
    Case is re-opened once.

    This sweep runs ONCE at every daemon startup and re-materializes the
    snapshot AND manifest for every live Case -- straight off the persisted
    ``projects`` doc, no live session / emitter needed (the writers re-source
    the full doc per Case; inline-vector side-tables only exist on the live
    emitter and are absent here, which is correct -- a cold sweep carries the
    URI-only layers, and the next warm open/turn re-inlines vectors). After one
    restart every existing Case has a CURRENT cold face without a warm re-open.

    Best-effort by contract (same posture as ``_run_preauth_case_migration``):
    no Persistence binding short-circuits; the per-Case writers each swallow
    their own S3/Dynamo errors and return ``False`` (never raise), so one bad
    Case can never abort the sweep or server startup. Bounded per-Case
    concurrency via a semaphore so the sweep does not burst the S3/Dynamo
    round-trips. Toggle off via ``TRID3NT_COLDVIEW_BACKFILL=0``.
    """
    if not _COLDVIEW_BACKFILL_ENABLED:
        logger.info("coldview backfill disabled (TRID3NT_COLDVIEW_BACKFILL=0)")
        return
    p = get_persistence()
    if p is None:
        logger.info("coldview backfill skipped: no Persistence singleton bound")
        return
    try:
        case_ids = await p.list_all_active_case_ids()
    except Exception:  # noqa: BLE001 -- startup must not abort on enumeration
        logger.warning("coldview backfill: case enumeration failed", exc_info=True)
        return
    if not case_ids:
        logger.info("coldview backfill: no live Cases to refresh")
        return

    sem = asyncio.Semaphore(_COLDVIEW_BACKFILL_CONCURRENCY)
    refreshed = 0

    async def _refresh_one(cid: str) -> bool:
        nonlocal refreshed
        async with sem:
            # Each writer swallows its own errors + returns a bool; gate the
            # snapshot+manifest INDIVIDUALLY so a manifest hiccup never voids a
            # good snapshot (and vice versa). No emitter -> URI-only snapshot.
            ok_snap = False
            try:
                ok_snap = await p.write_case_view_snapshot(cid)
            except Exception:  # noqa: BLE001 -- defensive: writer is best-effort
                logger.warning("coldview backfill: snapshot failed case=%s", cid)
            try:
                await p.write_case_manifest(cid)
            except Exception:  # noqa: BLE001 -- defensive: writer is best-effort
                logger.warning("coldview backfill: manifest failed case=%s", cid)
            if ok_snap:
                refreshed += 1
            return ok_snap

    await asyncio.gather(
        *(_refresh_one(cid) for cid in case_ids), return_exceptions=True
    )
    logger.info(
        "coldview backfill complete: %d/%d live Case(s) re-materialized",
        refreshed,
        len(case_ids),
    )


# Session-scoped active-Case registry. The client mounts two WebSocket
# connections per tab (Chat.tsx + App.tsx) sharing one session_id; the
# server builds a fresh ``SessionState`` per connection, so this registry
# keys the active Case by ``session_id`` instead, keeping every connection
# of a session -- including post-reconnect replacements -- on the same Case
# context. Bounded: oldest entries evicted past the cap (a stale session's
# next case-command re-establishes context).
_SESSION_ACTIVE_CASE: dict[str, str | None] = {}
_SESSION_ACTIVE_CASE_CAP = 4096

#: A1 FIX 5: strong references to the fire-and-forget S3 case-view snapshot
#: tasks. ``asyncio.create_task`` only holds a weak reference, so an
#: unreferenced task can be garbage-collected mid-flight (the snapshot's S3 PUT
#: silently dropped before it completes). Each detached snapshot task is added
#: here and self-discards via an ``add_done_callback`` once it finishes.
_BG_SNAPSHOT_TASKS: set[asyncio.Task] = set()

#: Bounded wall-clock budget for the graceful-shutdown drain of
#: ``_BG_SNAPSHOT_TASKS``. A SIGTERM unwinds ``run_server`` and waits at
#: most this long for outstanding detached snapshot/manifest PUTs to flush
#: so a stale cold ``case-views/{case_id}.json`` is not left behind; if a
#: PUT is pathologically slow it is abandoned rather than hanging shutdown
#: forever. Overridable for ops via the env var (seconds).
_BG_SNAPSHOT_DRAIN_TIMEOUT_S: float = float(
    os.environ.get("TRID3NT_BG_SNAPSHOT_DRAIN_TIMEOUT_S", "10")
)


async def _drain_bg_snapshot_tasks(
    timeout: float | None = None,
) -> None:
    """Flush any outstanding detached case-view snapshot / manifest writes.

    Called from ``run_server``'s shutdown ``finally`` so a graceful stop
    (SIGTERM) lands the per-turn / turn-close fire-and-forget snapshot PUTs
    still pending in ``_BG_SNAPSHOT_TASKS`` before the process exits -- closing
    the box-stop write race for the sites that (unlike the publish site) stay
    detached. Bounded by ``timeout`` (defaults to
    ``_BG_SNAPSHOT_DRAIN_TIMEOUT_S``) so a pathologically slow PUT cannot hang
    shutdown. Best-effort: each snapshot task swallows its own errors (returns
    False / never raises); ``return_exceptions=True`` plus the timeout guard
    keep a slow/failed PUT from breaking teardown. A no-op when nothing is
    pending."""
    pending = [t for t in _BG_SNAPSHOT_TASKS if not t.done()]
    if not pending:
        return
    budget = timeout if timeout is not None else _BG_SNAPSHOT_DRAIN_TIMEOUT_S
    logger.info("bg-snapshot drain: flushing %d pending write(s)", len(pending))
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "bg-snapshot drain: timed out after %.1fs with %d write(s) "
            "still pending",
            budget,
            sum(1 for t in pending if not t.done()),
        )
    except Exception:  # noqa: BLE001 - drain is best-effort, never blocks exit
        logger.exception("bg-snapshot drain: unexpected error")


#: Sentinel for ``SessionState.case_context_synced_to`` -- distinct from None
#: because ``None`` is a legitimate "no active Case" binding.
_CASE_SYNC_NEVER = "__case-context-never-synced__"

#: Stream key for turns dispatched with no active Case (mirrors the
#: client's ROOT_STREAM_KEY in Chat.tsx).
_ROOT_STREAM_KEY = "__root__"

#: Per-task narration-list registry. ``_stream_model_reply`` registers its
#: turn's narration list under the running asyncio task (in the synchronous
#: prefix, so crash/cancel still leaves the entry) and
#: ``_dispatch_gemini_and_persist`` pops it in its finally -- the wrapper then
#: joins THIS turn's list even when a concurrent turn has re-pointed
#: ``state.current_turn_narration``. Weak keys: an entry whose task was
#: never popped (direct stream callers) vanishes with the task, no leak.
_TURN_NARRATION_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, list[str]]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task OPEN-segment registry. ``_stream_model_reply`` registers the
#: list backing the currently open narration segment (received text not yet
#: finalized). On each finalize the in-loop code ``.clear()``s this same
#: list object (never rebinds it) so the wrapper always reads the live open
#: buffer. ``_dispatch_gemini_and_persist`` pops it in its finally and
#: persists the un-finalized remainder as the tail row, so no narration is
#: lost and finalized segments are never double-persisted.
_TURN_OPEN_SEGMENT_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, list[str]]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task count of narration SEGMENTS finalized+persisted this turn.
#: ``_finalize_segment`` increments it only when it actually writes a
#: non-empty ``role="agent"`` row. The wrapper's finally reads it to decide
#: whether the legacy single marker row (narration-less completed turn)
#: still needs writing (segments_done == 0) or whether the per-segment rows
#: already carried the narration (segments_done > 0 -> skip the marker).
_TURN_SEGMENTS_PERSISTED_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, int]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task flag set True ONLY when a row that snapshotted the turn's
#: zoom-to/layer accumulator was actually persisted -- i.e. the in-loop
#: TERMINAL ``_finalize_segment`` wrote a non-empty ``role="agent"`` row
#: (``is_terminal=True`` -> ``layer_emissions=None`` -> ``_persist_chat_turn``
#: snapshots ``current_turn_layer_ids`` + ``current_turn_map_commands``).
#: The wrapper's finally reads it to decide whether a tool-terminal turn
#: (final round ended in tool calls with no trailing narration -> no
#: terminal finalize fired -> accumulator orphaned) still needs a closing
#: accumulator-bearing marker row so the Case-reopen zoom-snap
#: (``extractLastZoomTo``) + layer attribution survive. Not set when the
#: terminal segment was empty/whitespace -- that turn's accumulator is
#: likewise unwritten and the marker is needed.
_TURN_TERMINAL_ACC_PERSISTED_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, bool]" = (
    weakref.WeakKeyDictionary()
)


def _set_session_active_case(session_id: str, case_id: str | None) -> None:
    """Bind ``case_id`` as the active Case for every connection of ``session_id``."""
    if (
        session_id not in _SESSION_ACTIVE_CASE
        and len(_SESSION_ACTIVE_CASE) >= _SESSION_ACTIVE_CASE_CAP
    ):
        # Evict oldest (insertion order) -- bounded memory, see note above.
        _SESSION_ACTIVE_CASE.pop(next(iter(_SESSION_ACTIVE_CASE)))
    _SESSION_ACTIVE_CASE[session_id] = case_id


# Session-scoped ANON-ID registry: belt-and-suspenders mirror of
# ``_SESSION_ACTIVE_CASE`` for the dual-socket anon-identity race. The web
# mounts two WebSocket connections per tab sharing one session_id, each
# running its own auth handshake; in the rare first-connect window before a
# client hint is persisted, each connection would otherwise mint a
# different random anon ULID and fork the owner-scoped case-list. This
# registry records ``session_id -> anon_user_id`` on first mint/bind so a
# sibling connection of the same session reuses it instead. Bounded like
# ``_SESSION_ACTIVE_CASE``; only anonymous ids are recorded here.
_SESSION_ANON_ID: dict[str, str] = {}
_SESSION_ANON_ID_CAP = 4096


def _get_session_anon_id(session_id: str) -> str | None:
    """Return the anon ``user_id`` bound to ``session_id`` this process, if any."""
    return _SESSION_ANON_ID.get(session_id)


def _set_session_anon_id(session_id: str, anon_user_id: str) -> None:
    """Record ``anon_user_id`` as the session's anon identity (idempotent).

    Bounded + insertion-order eviction, mirroring ``_set_session_active_case``.
    No-op when ``anon_user_id`` is falsy (defensive -- never record an empty id).
    """
    if not session_id or not anon_user_id:
        return
    if (
        session_id not in _SESSION_ANON_ID
        and len(_SESSION_ANON_ID) >= _SESSION_ANON_ID_CAP
    ):
        # Evict oldest (insertion order) -- bounded memory, see note above.
        _SESSION_ANON_ID.pop(next(iter(_SESSION_ANON_ID)))
    _SESSION_ANON_ID[session_id] = anon_user_id


def _apply_session_anon_hint(
    session_id: str, tok: "AuthTokenEnvelope | None"
) -> "AuthTokenEnvelope | None":
    """Fill a MISSING anon hint from the session-scoped registry.

    cases-vanish fix (belt-and-suspenders). When a connection of ``session_id``
    presents no token AND no ``anonymous_user_id`` hint, but a sibling
    connection of the same session already bound an anon identity this process,
    return a copy of the envelope carrying that recorded id as the hint -- so
    ``authenticate_token`` reuses the SAME anon user instead of minting a fresh
    random ULID. This collapses the (now rare) first-connect no-hint window
    where the App + Chat sockets would otherwise fork the owner-scoped
    case-list.

    Strictly additive / non-clobbering:
    - A client-supplied hint always wins (it is the durable, cross-refresh id) --
      we only fill when the hint is absent.
    - A non-empty ``token`` is left untouched: a presented token resolves via
      ``authenticate_token``'s own fallback, never diverted to an anon id.
    - No registry entry → the envelope is returned unchanged.
    """
    recorded = _get_session_anon_id(session_id)
    if not recorded:
        return tok
    # Only fill the anonymous path: a present token means the verify path owns
    # this connect (authed path unaffected).
    if tok is not None and (tok.token or "").strip():
        return tok
    # A client-supplied hint is the durable id -- never override it.
    if tok is not None and tok.anonymous_user_id:
        return tok
    if tok is None:
        return AuthTokenEnvelope(token="", anonymous_user_id=recorded)
    return tok.model_copy(update={"anonymous_user_id": recorded})


# Module-level live-turn registry keyed by ``(session_id, turn_key)`` --
# mirrors ``_SESSION_ACTIVE_CASE``'s session-scoped discipline so an
# in-flight turn OUTLIVES the per-connection ``SessionState``. Keying the
# running task by ``session_id`` lets it survive the death of any one
# socket; a closing connection's handler ``finally`` only drops that
# connection's references (letting cheap turns finish) instead of
# cancelling. ``wait_for_completion``'s own 1800s budget bounds a truly
# stuck solve.
#
# Each entry carries the running ``asyncio.Task`` AND the ``PipelineEmitter``
# the task is driving (so a reconnecting socket can rebind the emitter's
# sink and receive the live solve's progress + terminal frames -- see
# ``_rebind_live_turns``). A done-callback removes the entry on
# completion/cancellation (no leak). Bounded by session-count; the value is
# one task+emitter pair per live turn.
@dataclass
class _LiveTurn:
    """An in-flight turn that has been detached from its launching connection.

    ``task`` is the running ``asyncio.Task``; ``emitter`` is the
    ``PipelineEmitter`` it drives (its ``_sink`` may point at a now-dead socket
    until a reconnecting socket rebinds it via ``_rebind_live_turns``)."""

    task: "asyncio.Task"
    emitter: "PipelineEmitter | None"


#: session_id -> {turn_key -> _LiveTurn}. Populated when a connection closes with
#: a still-running turn (handler ``finally``); consulted by the cancel envelope
#: (so the stop button still kills a detached solve) and by a reconnecting
#: connection (so its emitter sink is rebound to the live turn).
_SESSION_LIVE_TURNS: dict[str, dict[str, _LiveTurn]] = {}
_SESSION_LIVE_TURNS_CAP = 4096


def _register_live_turn(
    session_id: str, turn_key: str, task: "asyncio.Task", emitter: "PipelineEmitter | None"
) -> None:
    """Detach ``task`` into the module-level live-turn registry.

    Installs a done-callback that removes the entry on completion/cancellation
    so a completed/cancelled task never lingers (Requirement 4: NO leak). Safe
    to call more than once for the same task (the callback de-dups on identity).
    """
    if (
        session_id not in _SESSION_LIVE_TURNS
        and len(_SESSION_LIVE_TURNS) >= _SESSION_LIVE_TURNS_CAP
    ):
        # Evict the oldest session bucket whose turns are ALL done; if none are
        # fully-done, evict the oldest regardless (bounded memory -- a live solve
        # is never silently dropped under normal session counts).
        for sid in list(_SESSION_LIVE_TURNS):
            if all(lt.task.done() for lt in _SESSION_LIVE_TURNS[sid].values()):
                _SESSION_LIVE_TURNS.pop(sid, None)
                break
        else:
            _SESSION_LIVE_TURNS.pop(next(iter(_SESSION_LIVE_TURNS)), None)
    bucket = _SESSION_LIVE_TURNS.setdefault(session_id, {})
    bucket[turn_key] = _LiveTurn(task=task, emitter=emitter)

    def _drop(_t: "asyncio.Task") -> None:
        b = _SESSION_LIVE_TURNS.get(session_id)
        if b is None:
            return
        lt = b.get(turn_key)
        # Only drop if THIS task still owns the slot (a same-stream supersede may
        # have replaced it with a fresh task -- don't evict the newer turn).
        if lt is not None and lt.task is _t:
            b.pop(turn_key, None)
        if not b:
            _SESSION_LIVE_TURNS.pop(session_id, None)

    task.add_done_callback(_drop)


def _rebind_live_turns(
    session_id: str,
    emitter: "PipelineEmitter | None",
    *,
    only_turn_key: str | None = None,
) -> int:
    """Rebind live turn(s) of ``session_id`` onto ``emitter``'s sink.

    When a new socket for the same session connects, point the
    still-running turn's emitter at the new socket so its progress +
    terminal frames reach the live connection. Returns the number of turns
    rebound. No-op when no live turns exist or ``emitter`` is None.

    The new connection's emitter IS the wire face (its ``_sink`` closes over
    the live socket's ``send``). We swap the LIVE turn's emitter sink to
    that same sink. Done/cancelled turns are skipped + pruned.

    ``only_turn_key`` restricts the rebind to a single stream -- used by the
    case-open path so opening Case A only rebinds Case A's live solve onto
    the new socket (a concurrent Case B solve keeps emitting through its
    own -- soon its OWN socket-resume / case-open rebinds it, or it lands
    fully-detached and its layer rehydrates on the next case-open)."""
    bucket = _SESSION_LIVE_TURNS.get(session_id)
    if not bucket or emitter is None:
        return 0
    rebound = 0
    for turn_key in list(bucket):
        if only_turn_key is not None and turn_key != only_turn_key:
            continue
        lt = bucket.get(turn_key)
        if lt is None:
            continue
        if lt.task.done():
            bucket.pop(turn_key, None)
            continue
        if lt.emitter is not None and lt.emitter is not emitter:
            lt.emitter.rebind_sink(emitter._sink)
            # Rebinding the live turn's emitter onto the new sink only
            # recovers FUTURE frames + pipeline CARDS -- not a loaded-layers
            # session-state emitted onto the now-dead launch socket before
            # this reconnect (e.g. a terminal flood-depth layer published
            # late after a multi-minute solve). Seed this reconnect's fresh
            # emitter from the live turn's accumulated layers so the
            # caller's emit_session_state carries the full snapshot to the
            # new socket. Union-by-identity: no duplicate, and the live
            # turn's later (superset) emits never regress it.
            emitter.merge_loaded_layers_from(lt.emitter)
            rebound += 1
    if not bucket:
        _SESSION_LIVE_TURNS.pop(session_id, None)
    return rebound


def _find_live_turn(session_id: str, turn_key: str) -> "asyncio.Task | None":
    """Return the live, not-done task for ``(session_id, turn_key)`` or None."""
    bucket = _SESSION_LIVE_TURNS.get(session_id)
    if not bucket:
        return None
    lt = bucket.get(turn_key)
    if lt is not None and not lt.task.done():
        return lt.task
    return None


def _any_live_turn(session_id: str) -> "asyncio.Task | None":
    """Return any live (not-done) detached turn for ``session_id`` or None.

    Cancel fallback: when the keyed lookup misses (the binding moved), the stop
    button still needs to reach a detached solver turn."""
    bucket = _SESSION_LIVE_TURNS.get(session_id)
    if not bucket:
        return None
    for lt in bucket.values():
        if not lt.task.done():
            return lt.task
    return None


@dataclass
class SessionState:
    """Per-session in-memory state. M1 keeps everything in-process; Mongo-backed
    session restore (NFR-R-2) lands when the LLM-facing DB seam is wired.

    Owns the per-session ``PipelineEmitter``, which owns the current
    ``PipelineSnapshot`` + ``loaded_layers`` accumulator and broadcasts real
    ``pipeline-state`` / ``session-state`` envelopes (Appendix A.7
    replace-not-reconcile). ``current_pipeline_id`` / ``current_pipeline_steps``
    stay as the M1 mirror for the LLM-streaming reply path (which doesn't go
    through the emitter -- there are no tool calls there)."""

    session_id: str
    chat_history: list[dict] = field(default_factory=list)
    current_pipeline_id: str | None = None
    current_pipeline_steps: list[PipelineStep] = field(default_factory=list)
    # In-flight turns keyed by STREAM (case_id, or _ROOT_STREAM_KEY for the
    # Cases root). Only a re-prompt in the SAME stream replaces (cancels)
    # that stream's turn; turns in other Cases keep running. Their
    # persistence follows the turn-Case pin and their model context is the
    # per-turn captured history list (see _stream_model_reply), so a
    # concurrent turn cannot re-aim either. Known v0.1 limit (display only):
    # the web routes live streaming envelopes to the last-submitted stream,
    # so a still-running turn's late envelopes may paint in the newer
    # stream until envelope case-tagging lands -- the persisted replay is
    # always correct.
    inflight_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    emitter: PipelineEmitter | None = None
    # FR-FR-3: per-session turn counter.  Increments on every
    # user-message dispatch (Gemini stream or /invoke directive). When
    # turn_count > MAX_TURNS_PER_SESSION the agent refuses further dispatch
    # and emits a ``session-state(status="max_turns_reached")`` envelope.
    # New WebSocket connection → new SessionState → fresh counter at 0.
    turn_count: int = 0
    # ``active_case_id`` is a PROPERTY backed by the module-level
    # ``_SESSION_ACTIVE_CASE`` registry (keyed by ``session_id``), not a
    # per-connection dataclass field, so the Case context is shared across
    # every connection of the session and survives reconnects. See
    # ``case_context_synced_to`` + ``_sync_case_context`` for the
    # per-connection in-memory catch-up (chat_history / emitter seed).
    #
    # Per-connection marker of which Case this connection's in-memory
    # context (chat_history + emitter loaded_layers) was last synced to. A
    # string sentinel (never a valid case id) means "never synced"; ``None``
    # is a legitimate value (no active Case).
    case_context_synced_to: str | None = _CASE_SYNC_NEVER
    # JOB 2 (active-AOI repair): durable cache of the active Case's persisted
    # AOI bbox (``CaseSummary.bbox`` == ``[lon_min, lat_min, lon_max,
    # lat_max]``). Set when the active Case is selected / synced (the same
    # ``session_state.case.bbox`` already read for the layers-present note) and
    # cleared on deselect. ``_turn_case_bbox`` reads THIS instead of the
    # non-existent ``state.active_case`` attribute (the pre-fix read always
    # returned None, so the agent had no active-AOI signal and re-geocoded /
    # re-fetched, starving the sim/fetch reuse short-circuits of an AOI anchor).
    # ``None`` is legitimate (no active Case, or a Case with no recorded bbox).
    case_bbox: Any = None
    # ADR 0017: the session's ACTIVE canvas AOI -- structured ``aoi_bbox``
    # ([min_lon, min_lat, max_lon, max_lat], EPSG:4326) set/cleared by
    # ``_set_active_aoi_from_payload``. Read by dispatch-time bbox auto-fill:
    # explicit arg > active AOI > case bbox. ``None`` = no drawn AOI.
    active_aoi_bbox: list[float] | None = None
    # ADR 0018 (Stage 3): per-session routing-visibility mode ('auto' | 'ask').
    # Set by the ``session-config`` envelope's ``mode`` field; ``None`` falls
    # back to the TRID3NT_MODE env default (see _session_routing_mode). Governs
    # tool-selection VISIBILITY only -- consent gates are never mode-dependent.
    routing_mode: str | None = None
    # BENCH pre-dispatch block hook: the armed, session-scoped
    # ``BenchBlockConfig`` set only by the bench harness via the
    # ``session-config`` path (``bench_tool_block`` key). ``None`` = normal
    # operation -- the dispatch guard is a single ``is not None`` check with
    # zero overhead when unarmed. When armed, the dispatch site blocks a
    # wrong / block-tier tool pick before the fn runs (see
    # tool_gating.bench_block_decision + _invoke_tool_via_emitter).
    bench_block_config: Any = None
    # Per-turn layer + map-command emission accumulators. Reset at
    # the start of every dispatch (Gemini stream or /invoke tool). The
    # CaseChatMessage write at turn close reads from these so a Case replay
    # can re-bind layers via the same emission sequence.
    current_turn_layer_ids: list[str] = field(default_factory=list)
    current_turn_pipeline_id: str | None = None
    # Per-turn zoom-to accumulator -- persisted into the closing
    # agent row's ``map_command_emissions`` so Case reopen can snap the
    # camera back (web replays the LAST persisted zoom-to).
    current_turn_map_commands: list[dict] = field(default_factory=list)
    # Per-turn narration accumulator. ``_stream_model_reply`` resets it at
    # stream start and appends every ``TextDeltaEvent`` delta (across all
    # loop iterations -- they share one ``message_id`` bubble on the wire).
    # ``_dispatch_gemini_and_persist`` joins it at turn close and persists
    # the agent's narration as a ``CaseChatMessage(role="agent")`` so a Case
    # reopen replays what the agent actually said.
    current_turn_narration: list[str] = field(default_factory=list)
    # BUG 1 (post-OPEN-14 acceptance rerun): set by the ``except
    # ContextWindowExceededError`` handler in ``_stream_model_reply`` when a
    # turn aborts on a clipped prompt. ``_dispatch_gemini_and_persist``'s
    # finally reads + clears it and appends the text to whichever partial-
    # narration row it is about to persist, so the reader sees the abort
    # verdict in the SAME chat row as the (unverified) streamed text, not
    # only in a transient error envelope a dead/detached socket may drop.
    current_turn_context_abort_note: str | None = None
    # The Case this TURN is bound to. Pinned by ``_prepare_user_turn`` at
    # dispatch time (after the auto-create-from-root hand-off, before the
    # first write). Every turn-scoped persistence write -- chat rows, tool
    # cards, layer attribution, per-Case .qgs routing, charts -- targets THIS
    # binding via ``_turn_case_id``, never the live ``active_case_id``, which
    # a mid-stream ``case-command(select)`` can re-point mid-turn.
    current_turn_case_id: str | None = None
    # Per-connection authenticated user context (SRS Appendix H.5).
    #
    # Populated by the connect-handshake (``_perform_auth_handshake``) after
    # the ``auth-token`` envelope verifies (or after the 5-second anonymous
    # fallback timeout). When set, every subsequent envelope for this
    # connection is scoped to ``authenticated_user_id`` -- Case lookups
    # (``Persistence.list_cases_for_user``) filter by it, and Case creation
    # binds it as ``owner_user_id``. ``None`` only between connect and the
    # handshake completion; never ``None`` after handshake.
    authenticated_user_id: str | None = None
    is_anonymous: bool = True
    firebase_uid: str | None = None
    tier: str = "free"
    auth_handshake_complete: bool = False
    # The web's keepalive (ws.ts) sends an empty ``session-resume`` envelope
    # every 25s on the open socket as a proof-of-life ping -- indistinguishable
    # from a genuine fresh-socket resume by the envelope alone. This flag is
    # the gate: a fresh ``SessionState`` is built per WebSocket connection, so
    # the FIRST ``session-resume`` on THIS connection replays layers, and
    # every later one is a keepalive ping (skip the layer replay; still emit
    # the ``session-state`` pong so the client's pong deadline clears). Reset
    # to False only by a brand-new connection's fresh SessionState.
    did_fresh_resume: bool = False
    # Per-connection latch for the active-Case REBIND decision - distinct
    # from ``did_fresh_resume`` (which gates the LAYER REPLAY). A session
    # mounts two sockets (App.tsx + Chat.tsx), each sending its own 25s
    # keepalive ``session-resume``. This flag flips True after the FIRST
    # resume on THIS connection, so the client-stamp rebind in
    # ``_handle_session_resume`` fires only on a genuine fresh resume, never
    # a keepalive ping. Explicit ``case-command(select)`` / ``user-message``
    # still rebind unconditionally (deliberate user intent). Reset to False
    # only by a brand-new connection's fresh SessionState.
    did_first_resume: bool = False
    # Per-session audit log of payload-warning events. Each entry is a dict
    # carrying ``warning_id``, ``tool_name``, ``estimated_mb``,
    # ``threshold_mb``, ``decision`` (set on confirmation), and the ULID
    # timestamps. Surfaces in tests + post-mortem; persisted to the active
    # Case as part of the chat turn record (best-effort).
    payload_warning_audit_log: list[dict] = field(default_factory=list)
    # Per-session post-hoc allowed-set tracker. The full tool catalog is
    # cached in the provider's ``CachedContent.tools[]`` slot at session
    # start and the ``allowed_function_names`` filter is enforced in our
    # code, not in the provider request. Every emitted ``function_call`` is
    # validated against this set via ``categories.validate_function_call``
    # before dispatch. The set is monotonically growing within a session --
    # it starts at the hot set and widens as the LLM opens categories
    # (``list_tools_in_category``) or successfully dispatches tools.
    allowed_tool_set: AllowedToolSet = field(default_factory=AllowedToolSet)
    # Per-session prompt-cache reference (legacy field name retained for the
    # ``cache-status`` envelope). GCP is decommissioned: the Vertex-only
    # CachedContent fast-path (``gemini_cache.py``) is REMOVED, so this is always
    # ``None``. Bedrock prompt caching is handled by ``bedrock_adapter`` via its
    # own ``cachePoint`` markers and reported through ``UsageMetadataEvent`` --
    # there is no per-session cache name to track here.
    gemini_cache_name: str | None = None
    # Per-session circuit breaker. Tracks consecutive failures per tool;
    # trips after TRID3NT_CIRCUIT_THRESHOLD (default 3) consecutive failures,
    # enforcing a TRID3NT_CIRCUIT_COOLDOWN_S (default 60s) cooldown.
    # ``_stream_model_reply`` checks ``is_tripped`` before every
    # ``_invoke_tool_via_emitter`` dispatch and records success/failure after
    # each attempt. A tripped breaker raises ``CircuitBreakerError``, which
    # ``summarize_tool_result`` surfaces as a structured envelope so the
    # model reads the signal and narrates the outage honestly.
    circuit_breaker: ToolCircuitBreaker = field(default_factory=ToolCircuitBreaker)
    # Per-TURN set of tools that have already surfaced a credential-request
    # this turn. The credential pipeline pauses + prompts + retries ONCE per
    # tool per turn: after the single retry the tool either succeeds (key
    # now in vault) or fails through the normal typed-error surface. Without
    # this guard a still-invalid key would re-trip the auth error and
    # re-prompt forever. Reset at the start of every turn.
    credential_prompted_tools: set[str] = field(default_factory=set)
    # fix (bbox-gate-retry-loop, 2026-07-09): per-TURN memory of solver-confirm
    # / fetch-resolution gate ("tool-payload-warning") decisions, keyed by
    # ``_gate_memory_key(tool_name, params)`` (tool name + bbox rounded to
    # ~6 decimals, or the full normalized args when there is no bbox). A
    # model that retries a gated tool with corrected NON-bbox args (e.g.
    # ``fetch_landcover(dataset='nlcd')`` -> typed error -> retried with
    # ``dataset='nlcd_'`` -> typed error -> retried with ``dataset=
    # 'nlcd_2021')``) re-emitted an IDENTICAL confirm gate on the SAME bbox
    # every retry; the user only answered the FIRST one, and local gates
    # have no timeout by design, so the second gate hung the turn forever.
    # Only "proceed" / "narrow_scope" decisions are recorded here (a
    # "cancel" raises before reaching the write site, so a corrected retry
    # still re-gates - the user might reconsider). Reset at the start of
    # every new user-message dispatch (same site as
    # ``credential_prompted_tools`` above), so it never leaks across turns;
    # it lives on the per-session ``SessionState`` so it never leaks across
    # sessions or Cases either. Values are the DELTA the gate applied to the
    # params (e.g. ``{"resolution_m": 300}``), not the whole approved dict,
    # so a later retry keeps its own corrected non-bbox args.
    gate_decisions_this_turn: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    # In-chat model selector: the Bedrock model id chosen by the user for
    # the current turn. Updated on every ``user-message`` that carries a
    # non-None ``model_id``; persists across turns so consecutive messages
    # without one inherit the last-chosen model. ``None`` means "use the
    # server default" (``bedrock_adapter.bedrock_model_id()``). Only
    # consulted when MODEL_PROVIDER=bedrock; ignored on the Vertex path.
    selected_model: str | None = None

    # ------------------------------------------------------------------ #
    # Active-Case context -- session-scoped, NOT per-connection.
    # ------------------------------------------------------------------ #

    @property
    def active_case_id(self) -> str | None:
        """The active Case for this SESSION (shared across its connections).

        ``None`` for fresh sessions (no Case selected yet -- the M1 stateless
        demo path remains supported). Updated by ``case-command(create|select)``
        on ANY connection of the session; cleared on ``delete`` of the active
        Case. When non-None, the tool-call wrapper
        (``_invoke_tool_via_emitter``) carries the case context into tools
        that opt in via ``case_id`` (currently ``publish_layer``); chat +
        layer persistence route every turn into the Case record.
        """
        return _SESSION_ACTIVE_CASE.get(self.session_id)

    @active_case_id.setter
    def active_case_id(self, value: str | None) -> None:
        _set_session_active_case(self.session_id, value)


def _new_envelope(message_type: str, session_id: str, payload: Any) -> str:
    """Construct + validate an Envelope and return its JSON wire form.

    Stamps ``case_id`` from the turn's ContextVar binding so the web routes
    live envelopes to the owning Case's stream. None outside a turn --
    lifecycle envelopes are untagged.
    """
    env = Envelope(
        type=message_type,
        session_id=session_id,
        case_id=current_turn_case(),
        payload=payload,
    )
    return env.model_dump_json()


async def _send_error(
    websocket: ServerConnection,
    session_id: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> None:
    payload = ErrorPayload(error_code=code, message=message, retryable=retryable)
    # F1 (2026-07-08): route through the session-aware safe send. An error
    # reply aimed at a just-dropped socket must reach the session's surviving
    # sibling socket when one exists, and must NEVER raise into the caller --
    # pre-fix, the turn-failure path's _send_error re-raised ConnectionClosed
    # and skipped the terminal-failure-card persist entirely.
    await _session_safe_send(
        websocket, session_id, _new_envelope("error", session_id, payload)
    )


# WS-30s STORM FIX (server data heartbeat): the browser ``WebSocket`` API
# handles server PROTOCOL-level PING control-frames transparently and NEVER
# surfaces them to ``onmessage``, so the server's ``ping_interval=20`` pings do
# NOT reset the client's inbound-frame timer (ws.ts ``noteInboundActivity``
# fires only on a DATA frame). Between turns the ONLY data frame the client sees
# is its own keepalive's ``session-state`` reply; if that reply is slow or stalls
# (a reconnect re-runs the active-case layer replay + vector densify), the
# client's pong deadline expires and it force-reconnects -> the reconnect re-runs
# the replay -> stalls again -> a self-sustaining ~30s reconnect storm in which
# the user's prompts never reach the turn handler.
#
# Fix: per WS connection, a background task sends a lightweight ``heartbeat`` DATA
# frame every ``HEARTBEAT_INTERVAL_SECONDS`` (well under the client's
# ~25s ping + 10s pong-timeout window) so the client's ``onmessage`` fires and
# its inbound-activity timer is reset on a cheap server clock that is independent
# of the (possibly-slow) resume reply. ws.ts already (a) calls
# ``noteInboundActivity()`` on EVERY inbound frame BEFORE any type parsing and
# (b) routes an unknown ``heartbeat`` type to a no-op ``default:`` (console.debug
# only) -- so NO web change is required for the client to tolerate + benefit from
# it. The interval is deliberately shorter than the client's 25s keepalive so a
# heartbeat lands inside every pong window even on a busy loop.
HEARTBEAT_INTERVAL_SECONDS: float = 12.0


async def _heartbeat_loop(
    websocket: ServerConnection,
    session_id: str,
) -> None:
    """Send a lightweight ``heartbeat`` DATA frame every interval until cancelled.

    WS-30s STORM FIX (primary): the server PING control-frames never reach the
    browser ``onmessage`` handler, so they cannot keep the client's
    inbound-activity / pong-deadline timer alive. This per-connection task sends a
    tiny ``heartbeat`` envelope on a server clock (every
    ``HEARTBEAT_INTERVAL_SECONDS``) so the client always sees a fresh DATA frame
    well inside its pong window -- breaking the reconnect storm regardless of how
    slow the session-resume reply is.

    Built as a raw-JSON envelope (the same pattern ``_emit_turn_complete`` /
    ``_send_loop_exhausted`` use) so no schema-lane payload model is required; the
    payload carries only a server timestamp. NOT stamped with a Case tag -- it is
    a pure transport-liveness frame, never routed to a Case stream.

    Cancelled cleanly by the handler's ``finally`` on EVERY disconnect path. A
    per-send wire failure (the socket may be mid-close) is swallowed so a transient
    write error never tears down the loop early; a ``ConnectionClosed`` ends the
    ``async for``-driven handler which then cancels this task.
    """
    import json as _json

    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            await websocket.send(
                _json.dumps(
                    {
                        "type": "heartbeat",
                        "id": new_ulid(),
                        "ts": now_utc().isoformat().replace("+00:00", "Z"),
                        "session_id": session_id,
                        "case_id": None,
                        "payload": {
                            "ts": now_utc().isoformat().replace("+00:00", "Z"),
                        },
                    }
                )
            )
        except asyncio.CancelledError:
            # Clean shutdown from the handler ``finally`` -- propagate so the
            # awaiting canceller observes completion (NATE: cancel cleanly).
            raise
        except Exception:  # noqa: BLE001 -- transport liveness; never tear down
            # A half-closed socket send fails; the handler loop will end on the
            # real ConnectionClosed and cancel this task. Swallow + keep ticking
            # so a single transient write hiccup does not kill the heartbeat.
            logger.debug(
                "heartbeat send failed session=%s", session_id, exc_info=True
            )


# Mid-turn sends must survive a dead captured socket: fall back across any
# other live socket for the session rather than aborting the turn.


async def _session_safe_send(
    websocket: "ServerConnection | None",
    session_id: str,
    message: str,
) -> bool:
    """Send ``message`` on the captured socket, falling back to any live
    socket of ``session_id``. Never raises; returns True when a send landed.
    """
    if websocket is not None:
        try:
            await websocket.send(message)
            return True
        except Exception:  # noqa: BLE001 -- captured socket may be dead
            pass
    for conn in list(_SESSION_WS_CONNECTIONS.get(session_id, ())):
        if conn is websocket:
            continue
        try:
            await conn.send(message)
            return True
        except Exception:  # noqa: BLE001 -- sibling may be mid-close too
            continue
    logger.debug(
        "session-safe-send: no live socket for session=%s (frame dropped; "
        "persisted rows remain the replay backstop)",
        session_id,
    )
    return False


async def _send_loop_exhausted(
    websocket: ServerConnection,
    session_id: str,
) -> None:
    """Emit the distinct ``loop_exhausted`` envelope.

    Fires when the multi-turn loop hits ``MAX_TURN_ITERATIONS`` without a
    natural termination (no tool-call-free turn). Raw-JSON envelope typed
    ``"loop_exhausted"`` -- distinct from the generic ``"error"`` type -- so
    the UI can render "Agent ran out of steps" rather than a generic
    failure indicator.

    Wire shape:
        {
          "type": "loop_exhausted",
          "session_id": str,
          "payload": {
            "status": "loop_exhausted",
            "error_code": "MAX_ITERATIONS_REACHED",
            "message": "Agent reached max iteration limit (N) before completing the request.",
            "retryable": False
          }
        }

    The ``payload.error_code`` key is SCREAMING_SNAKE_CASE and lives in the
    ``loop_exhausted`` typed envelope, not the ``error`` envelope, so clients
    can distinguish "tool chain too long" from an upstream LLM failure.
    ``retryable=False`` because the agent already consumed all its turns;
    the user should rephrase or narrow scope.

    Best-effort: a wire failure is logged but not re-raised so the terminal
    agent-message-chunk can still fire.
    """
    import json as _json

    try:
        payload = {
            "status": "loop_exhausted",
            "error_code": "MAX_ITERATIONS_REACHED",
            "message": (
                f"Agent reached max iteration limit ({MAX_TURN_ITERATIONS}) "
                "before completing the request. "
                "Try rephrasing your request with a narrower scope."
            ),
            "retryable": False,
        }
        await _session_safe_send(
            websocket,
            session_id,
            _json.dumps(
                {
                    "type": "loop_exhausted",
                    "session_id": session_id,
                    "payload": payload,
                }
            ),
        )
        logger.info(
            "loop_exhausted envelope sent session=%s max_iter=%d",
            session_id,
            MAX_TURN_ITERATIONS,
        )
    except Exception:  # noqa: BLE001 -- observability; never break the reply path
        logger.exception(
            "loop_exhausted envelope send failed session=%s", session_id
        )


async def _send_agent_abort(
    websocket: ServerConnection,
    session_id: str,
    reason_code: str,
    message: str,
) -> None:
    """Emit the runaway-agent abort envelope.

    Sent when a per-turn guard fires (step cap, wall-clock, or loop watchdog)
    to stop a runaway turn before it can wedge the shared box. Reuses the
    ``loop_exhausted`` typed wire envelope but carries the specific guard
    ``error_code`` and an honest message (honesty floor: state exactly why
    the turn stopped, never a fabricated success). Best-effort: a wire
    failure is logged, never re-raised, so the turn still terminates and
    releases busy.
    """
    import json as _json

    try:
        await _session_safe_send(
            websocket,
            session_id,
            _json.dumps(
                {
                    "type": "loop_exhausted",
                    "session_id": session_id,
                    "payload": {
                        "status": "loop_exhausted",
                        "error_code": reason_code,
                        "message": message,
                        "retryable": False,
                    },
                }
            ),
        )
        logger.warning(
            "agent-abort session=%s reason=%s", session_id, reason_code
        )
    except Exception:  # noqa: BLE001 -- observability; never break the reply path
        logger.exception(
            "agent-abort envelope send failed session=%s reason=%s",
            session_id,
            reason_code,
        )


async def _emit_turn_complete(
    websocket: ServerConnection,
    state: SessionState,
    *,
    pipeline_id: str | None = None,
    final_state: str | None = None,
) -> None:
    """Emit the end-of-turn ``turn-complete`` signal so the client
    force-completes any card still rendering ``running``.

    A tool/turn's terminal ``pipeline-state`` frame can be written onto a
    just-dropped socket and lost, leaving a card spinning after its tool
    actually finished. This idle marker is emitted at the end of every turn
    and re-emitted on session-resume so no card hangs past turn end.

    Wire shape (matches ``TurnCompletePayload`` exactly -- both fields
    optional, a bare ``{}`` is a valid whole-turn idle):
        {"type": "turn-complete", "session_id": ..., "case_id": <turn case>,
         "payload": {"envelope_type": "turn-complete",
                     "pipeline_id": <str|null>, "final_state": <str|null>}}

    Built as a raw-JSON envelope (not via ``_new_envelope``) because the
    typed ``Envelope.payload`` is a ``GraceModel`` with ``extra="forbid"``
    and this payload model is not yet in ``trid3nt_contracts``; same
    raw-JSON pattern ``_send_loop_exhausted`` uses. ``case_id`` is stamped
    from the turn's ContextVar tag so this fans out session-wide and routes
    by ``case_id`` to the owning Case's stream, exactly like ``solve-progress``.

    Best-effort: a wire failure (the socket may already be half-closed) is
    logged, never raised -- the persisted tool-card terminal state is the
    durable replay backstop, and session-resume re-emits this signal anyway.
    """
    import json as _json

    try:
        env = {
            "type": "turn-complete",
            "id": new_ulid(),
            "ts": now_utc().isoformat().replace("+00:00", "Z"),
            "session_id": state.session_id,
            "case_id": current_turn_case(),
            "payload": {
                "envelope_type": "turn-complete",
                "pipeline_id": pipeline_id,
                "final_state": final_state,
            },
        }
        await websocket.send(_json.dumps(env))
        logger.debug(
            "turn-complete emitted session=%s case=%s pipeline=%s final=%s",
            state.session_id,
            env["case_id"],
            pipeline_id,
            final_state,
        )
    except Exception:  # noqa: BLE001 -- idle signal; never break the reply path
        logger.debug(
            "turn-complete emit failed session=%s", state.session_id,
            exc_info=True,
        )


async def _handle_max_turns_reached(
    websocket: ServerConnection, state: SessionState
) -> None:
    """Emit the cap-hit envelope sequence.

    1. Emit ``session-state`` with ``status="max_turns_reached"`` so the
       client knows the session is at its turn limit.
    2. Send a closing ``agent-message-chunk`` summarising what's been done
       and directing the user to start a new session.

    Called instead of the normal dispatch when ``state.turn_count`` exceeds
    ``MAX_TURNS_PER_SESSION``. No tool calls are dispatched.
    """
    _ensure_emitter(websocket, state)
    # Re-emit session-state with the cap status so the client can render a
    # "session full" indicator.
    closing_payload = SessionStatePayload(
        chat_history=state.chat_history,
        status="max_turns_reached",
    )
    await websocket.send(
        _new_envelope("session-state", state.session_id, closing_payload)
    )
    # Send a closing agent-message-chunk so the user sees a human-readable
    # explanation in the chat panel.
    message_id = new_ulid()
    closing_text = (
        "This session has reached its turn limit "
        f"({MAX_TURNS_PER_SESSION} turns). "
        "No further tool calls will be dispatched. "
        "Start a new session to continue working."
    )
    await websocket.send(
        _new_envelope(
            "agent-message-chunk",
            state.session_id,
            AgentMessageChunkPayload(
                message_id=message_id, delta=closing_text, done=False
            ),
        )
    )
    await websocket.send(
        _new_envelope(
            "agent-message-chunk",
            state.session_id,
            AgentMessageChunkPayload(message_id=message_id, delta="", done=True),
        )
    )
    logger.info(
        "max-turns-reached session=%s turn_count=%d limit=%d",
        state.session_id,
        state.turn_count,
        MAX_TURNS_PER_SESSION,
    )


async def _emit_cache_status(
    websocket: ServerConnection,
    state: SessionState,
    usage: UsageMetadataEvent,
) -> None:
    """Emit a ``cache-status`` envelope so the UI can render live cache hit rate.

    Forwarded once per Gemini stream after the ``UsageMetadataEvent`` lands.
    Payload shape:

        {
            "cache_hit":     bool,
            "cached_tokens": int,
            "total_tokens":  int,
            "prompt_tokens": int | null,
            "candidates_tokens": int | null,
            "cache_name":    str | null   (the cached_content name in use this turn),
        }

    Intentionally raw-JSON (no contract model): observability surface, not
    a wire-API contract. A wire-side failure is logged but never raised --
    cache-status reporting must not break the agent loop.
    """
    import json as _json

    try:
        payload = {
            "cache_hit": bool(usage.cache_hit),
            "cached_tokens": int(usage.cached_content_token_count or 0),
            "total_tokens": int(usage.total_token_count or 0),
            "prompt_tokens": usage.prompt_token_count,
            "candidates_tokens": usage.candidates_token_count,
            "cache_name": state.gemini_cache_name,
        }
        await _session_safe_send(websocket, state.session_id,
            _json.dumps(
                {
                    "type": "cache-status",
                    "session_id": state.session_id,
                    "payload": payload,
                }
            )
        )
    except Exception:  # noqa: BLE001 -- observability, never bubble up
        logger.exception(
            "cache-status emission failed session=%s", state.session_id
        )


async def _maybe_emit_tool_candidates(
    websocket: ServerConnection,
    state: SessionState,
    user_text: str,
    exclude_tools: "set[str] | None" = None,
) -> tuple[str | None, list[str]]:
    """ADR 0018: surface the retrieval-ranked tool candidates BEFORE dispatch.

    Fires when the session mode is ``ask``, OR in ``auto`` when the top-1 vs
    top-2 retrieval-score margin is under the measured-ambiguity threshold
    (``_ambiguity_margin_threshold``; 0 disables). Emits the ``tool-candidates``
    envelope (raw-JSON, heartbeat-style -- the contracts lane declares the
    typed model; until integration the payload is a plain dict) and waits
    gate-style for the ``tool-choice`` reply with a BOUNDED timeout
    (``_tool_choice_timeout_s`` -- deliberately bypasses the F6 24h local-lane
    override, code-exec-gate precedent). On timeout / fault the turn proceeds
    AUTONOMOUSLY (fail-open) -- the picker is an optimization, never a wall.

    Returns ``(pinned_tool_name | None, notes)``:
      * a ``tool_name`` reply pins that tool for the next dispatch -- the
        caller unions it into the visible registry + allowed set, and a
        directive note rides into ``contents``;
      * a ``free_text`` reply becomes a user-clarification note;
      * timeout yields a proceed-autonomously note.
    """
    mode = _session_routing_mode(state)
    threshold = _ambiguity_margin_threshold()
    if mode != "ask" and threshold <= 0.0:
        return None, []

    from .agent.tools.search.tool_retrieval import retrieve_ranked_tools

    ranked = retrieve_ranked_tools(user_text, k=8)
    if exclude_tools:
        # ADR 0018 wave semantics: drop this turn's already-dispatched tools so
        # each subsequent ASK-mode round's wave advances the stage label
        # (acquisition -> preprocessing -> analysis -> visualization) instead of
        # re-offering the same acquisition picks every round.
        ranked = [(n, s) for (n, s) in ranked if n not in exclude_tools]
    if not ranked:
        # Cold index / no match / all excluded: nothing to offer -- autonomous.
        return None, []

    reason: str | None = None
    if mode == "ask":
        reason = "ask_mode"
    elif len(ranked) >= 2 and ranked[0][1] > 0.0:
        margin = (ranked[0][1] - ranked[1][1]) / ranked[0][1]
        if margin < threshold:
            reason = "ambiguity"
    if reason is None:
        return None, []

    candidates = [
        {
            "tool_name": name,
            "summary": _tool_summary_line(TOOL_REGISTRY.get(name)),
            "score": round(float(score), 6),
        }
        for name, score in ranked[:_TOOL_CANDIDATES_MAX]
    ]
    timeout_s = _tool_choice_timeout_s()
    request_id = new_ulid()
    payload = {
        "request_id": request_id,
        "stage_label": _stage_label_for_candidates(ranked),
        "candidates": candidates,
        "reason": reason,
        "timeout_s": timeout_s,
    }

    import json as _json

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _register_pending_tool_choice(state.session_id, request_id, fut)
    try:
        await _session_safe_send(
            websocket,
            state.session_id,
            _json.dumps(
                {
                    "type": "tool-candidates",
                    "id": new_ulid(),
                    "ts": now_utc().isoformat().replace("+00:00", "Z"),
                    "session_id": state.session_id,
                    "case_id": current_turn_case(),
                    "payload": payload,
                }
            ),
        )
        logger.info(
            "tool-candidates emitted session=%s request_id=%s reason=%s "
            "n=%d top=%s timeout=%.0fs",
            state.session_id,
            request_id,
            reason,
            len(candidates),
            ranked[0][0],
            timeout_s,
        )
        reply = await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.info(
            "tool-candidates TIMEOUT session=%s request_id=%s (%.0fs) -- "
            "proceeding autonomously",
            state.session_id,
            request_id,
            timeout_s,
        )
        return None, [
            "(The tool-selection card was not answered in time -- proceed "
            "autonomously with your best tool choice.)"
        ]
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- the picker must never break the turn
        logger.warning(
            "tool-candidates gate fault session=%s -- proceeding autonomously",
            state.session_id,
            exc_info=True,
        )
        return None, []
    finally:
        _pop_pending_tool_choice(request_id)

    # Defensive dict parse (contracts lane declares the typed model later).
    tool_name: str | None = None
    free_text: str | None = None
    if isinstance(reply, dict):
        tn = reply.get("tool_name")
        ft = reply.get("free_text")
        if isinstance(tn, str) and tn.strip():
            tool_name = tn.strip()
        if isinstance(ft, str) and ft.strip():
            free_text = ft.strip()

    notes: list[str] = []
    pinned: str | None = None
    if tool_name:
        if tool_name in TOOL_REGISTRY:
            pinned = tool_name
            notes.append(
                f"[User tool choice] Use the tool '{tool_name}' for this "
                "request."
            )
            logger.info(
                "tool-choice PINNED session=%s request_id=%s tool=%s",
                state.session_id,
                request_id,
                tool_name,
            )
        else:
            logger.warning(
                "tool-choice named unknown tool %r session=%s -- ignored",
                tool_name,
                state.session_id,
            )
    if free_text:
        notes.append(f"[User clarification] {free_text}")
        logger.info(
            "tool-choice free-text session=%s request_id=%s len=%d",
            state.session_id,
            request_id,
            len(free_text),
        )
    return pinned, notes


async def _stream_model_reply(
    websocket: ServerConnection,
    state: SessionState,
    settings: ModelSettings,
    user_text: str,
    research_mode: str,
    bedrock_model: str | None = None,
    show_thinking: bool = False,
) -> None:
    """Stream one user-message reply with multi-turn tool dispatch.

    The canonical agent loop:

        contents = history + user_text
        for _ in range(MAX_TURN_ITERATIONS):
            stream Gemini:
                text deltas -> forward as agent-message-chunk
                function_calls -> collect (this turn)
            if no function_calls this turn:
                break  # final narrative turn
            for each call:
                result = await _invoke_tool_via_emitter(...)
                summary = summarize_tool_result(name, result, error)
                append model Content (function_call) + function Content (response)
            # then loop: Gemini now sees the call + result and decides next
            # tool call OR narrates the answer.

    Cancellation: ``asyncio.CancelledError`` aborts the whole loop and emits a
    cancelled ``pipeline-state`` for the outer ``llm_generation`` step.
    """
    logger.info(
        "user-message session=%s research_mode=%s text=%r",
        state.session_id,
        research_mode,
        user_text[:80],
    )

    # Auto-name an Untitled Case from its FIRST user message BEFORE dispatch
    # (a failed narration must not skip it). Deterministic heuristic, no LLM
    # call; best-effort and never-raise. The end-of-turn call below stays as
    # a no-op fallback covering a mid-stream case switch.
    try:
        if await _maybe_autoname_case(state, user_text):
            await _emit_case_list(websocket, state, force=True)
    except Exception:  # noqa: BLE001 -- naming is a nicety, never break the turn
        logger.debug(
            "pre-dispatch case auto-name failed session=%s", state.session_id,
            exc_info=True,
        )

    # One bubble per CONTIGUOUS narration run: message_id is minted lazily on
    # the first text of a segment, finalized when the next function-call
    # round dispatches, and a new segment opens after that round. ``None``
    # means no open segment (no leading-text-before-first-tool-call bubble).
    current_message_id: str | None = None
    pipeline_id = new_ulid()
    step_id = new_ulid()
    state.current_pipeline_id = pipeline_id
    # Fresh per-stream narration accumulator: one stream == one message_id
    # bubble == one persisted role="agent" CaseChatMessage at turn close.
    # Captured as LOCALS in the synchronous prefix (before any await) because
    # per-Case turn concurrency re-points SessionState fields mid-stream; this
    # turn must keep appending to ITS OWN lists. Also registered under the
    # running task so the dispatch wrapper's finally joins THIS turn's list
    # (never the live field), even on crash/cancel.
    state.current_turn_narration = []
    # BUG 1: reset the per-turn context-window-abort note. A new turn is a
    # fresh request -- any prior turn's abort note must never leak forward.
    state.current_turn_context_abort_note = None
    # job VAULT-READ: reset the per-turn credential-prompt guard. A new user
    # turn is a fresh request -- a tool that prompted for a key last turn may
    # legitimately prompt again this turn (the key may still be missing).
    state.credential_prompted_tools = set()
    # fix (bbox-gate-retry-loop, 2026-07-09): reset the per-turn
    # solver-confirm/fetch-resolution gate-decision memory. A new user turn
    # is a fresh request - a tool+bbox pair confirmed last turn must gate
    # again this turn (see ``gate_decisions_this_turn`` docstring above).
    state.gate_decisions_this_turn = {}
    turn_narration = state.current_turn_narration
    turn_history = state.chat_history
    # Per-segment buffer for the CURRENTLY OPEN bubble only; reset via
    # .clear() at each boundary (same list object stays registered).
    # Captured + registered in the synchronous prefix so a crash/cancel
    # mid-segment lets the wrapper's finally persist the tail.
    _segment_buf: list[str] = []
    # Per-segment reasoning-text buffer, filled only when the per-turn
    # ``show_thinking`` toggle is ON. ``_finalize_segment`` persists it as the
    # ``thinking`` field on the SAME agent row as the segment's answer
    # (same-bubble contract; QGIS plugin reads this field) and clears it. A
    # thinking-only segment keeps its buffer so it attaches to the turn's NEXT
    # persisted row; thinking with no text anywhere persists no phantom
    # bubble. NEVER-REHYDRATE: this is display replay material only --
    # build_contents_from_history strips it BY RULE.
    _thinking_buf: list[str] = []
    _reg_task = asyncio.current_task()
    if _reg_task is not None:
        _TURN_NARRATION_BY_TASK[_reg_task] = turn_narration
        _TURN_OPEN_SEGMENT_BY_TASK[_reg_task] = _segment_buf
        _TURN_SEGMENTS_PERSISTED_BY_TASK[_reg_task] = 0
        # False until the terminal finalize actually
        # snapshots the accumulator onto a persisted row (see registry doc).
        _TURN_TERMINAL_ACC_PERSISTED_BY_TASK[_reg_task] = False

    # Emit a one-step "thinking" pipeline snapshot so the client has a
    # cancellable handle. The loop driver keeps this single outer step; each
    # dispatched tool gets its own step through the emitter.
    thinking_step = PipelineStep(
        step_id=step_id,
        name="llm_generation",
        tool_name="gemini_generate",
        state="running",
    )
    state.current_pipeline_steps = [thinking_step]
    await _session_safe_send(websocket, state.session_id,
        _new_envelope(
            "pipeline-state",
            state.session_id,
            PipelineStatePayload(pipeline_id=pipeline_id, steps=[thinking_step]),
        )
    )

    # Under Bedrock there is no Vertex client to build --
    # build_client() requires GCP ADC, which run-local and the AWS deploy do not
    # have. stream_events_with_contents' bedrock branch ignores ``client``.
    # Provider resolved once here and reused by the cache guard below.
    from .agent.adapters.bedrock_adapter import model_provider as _model_provider

    _provider = _model_provider()
    # #225 per-model telemetry: resolve the EFFECTIVE model that actually
    # serves this turn (not the possibly-None explicit selection) so telemetry
    # rows for DEFAULT-model turns are tagged with the real model instead of
    # collapsing into the "unknown" bucket in the by_model accuracy slice. On
    # the openai/OpenRouter path this applies openai_model's own precedence
    # (selection -> TRID3NT_OPENAI_MODEL); on bedrock, the selection or the
    # configured default. Best-effort -- a resolution error must never break
    # the turn, so fall back to the raw selection.
    try:
        if _provider == "openai":
            from .agent.adapters import openai_adapter as _oa  # noqa: WPS433
            _effective_model = _oa.openai_model(bedrock_model)
        elif _provider == "bedrock":
            from .agent.adapters.bedrock_adapter import bedrock_model_id as _bmid  # noqa: WPS433
            _effective_model = bedrock_model or _bmid()
        else:
            _effective_model = bedrock_model
    except Exception:  # noqa: BLE001 -- telemetry tag only, never fatal
        _effective_model = bedrock_model
    # No Vertex client under bedrock OR the scripted/replay sandbox (neither has,
    # nor needs, GCP ADC -- their stream_* branches ignore ``client``).
    client = None if _provider in ("bedrock", "scripted", "replay", "fake") else build_client(settings)
    first_token_logged = False
    started_at = asyncio.get_running_loop().time()

    # Build tool declarations + system prompt for this request.
    #
    # Tool-retrieval: default OFF = the full flat registry (no retrieval
    # computed). In ``shadow`` we compute the WOULD-BE-visible set and LOG it
    # for recall@k, but still build declarations over the FULL registry (zero
    # behavior change). In ``enforce`` we subset TOOL_REGISTRY to the visible
    # set before build_tool_declarations and UNION it into the Case's
    # monotonic AllowedToolSet so a once-visible tool never leaves within a
    # Case. Any retrieval error / empty result FAILS OPEN to the full
    # registry, logged. The cachePoint TAIL is inserted downstream by
    # bedrock_adapter (after tools), so subsetting the dict here preserves it.
    #
    # ENGINE-DOOR default: the DEFAULT declarable set is the full registry
    # MINUS tier=template engine templates -- NOT the raw TOOL_REGISTRY.
    # Templates surface to the model ONLY via their door's gate expansion
    # (which re-adds the specific templates it lists back into
    # _retrieval_registry). Using the raw registry here would re-leak every
    # template into DEFAULT declarations in the gating-off / retrieval-off /
    # fail-open paths, defeating the door architecture. See
    # _default_declarable_registry.
    _retrieval_registry = _default_declarable_registry()
    _retrieval_mode = _tool_retrieval_mode()
    if _retrieval_mode in ("shadow", "enforce"):
        try:
            from .agent.tools.search.tool_retrieval import retrieve_visible_tools

            _retrieval_k = _tool_retrieval_k()
            _visible = retrieve_visible_tools(
                user_text, state.allowed_tool_set, _retrieval_k
            )
            if not _visible:
                # FAIL-OPEN: an empty result must never trim the catalog.
                raise ValueError("retrieve_visible_tools returned empty")
            # Shadow telemetry (fire-and-forget, never-raise). Logged in BOTH
            # shadow and enforce so recall@k can be measured in either mode.
            try:
                emit_shadow_selection_event(
                    session_id=state.session_id,
                    turn_id=pipeline_id,
                    user_text=user_text,
                    visible_tools=_visible,
                    mode=_retrieval_mode,
                    k=_retrieval_k,
                    full_registry_size=len(TOOL_REGISTRY),
                    model_id=_effective_model,
                )
            except Exception:  # noqa: BLE001 -- telemetry must never break dispatch
                logger.warning(
                    "tool-retrieval: shadow emit failed", exc_info=True
                )
            if _retrieval_mode == "enforce":
                # UNION the visible set into the Case's monotonic AllowedToolSet
                # FIRST (so it never shrinks across turns), then subset the
                # registry to the resulting snapshot. as_frozenset() already
                # carries the core floor + accrued tools; intersecting with the
                # registry keeps only real, registered tools.
                try:
                    state.allowed_tool_set.add_tools(_visible)
                    _allowed_snapshot = set(
                        state.allowed_tool_set.as_frozenset()
                    )
                except Exception:  # noqa: BLE001 -- never shrink on a snapshot fault
                    logger.warning(
                        "tool-retrieval: allowed-set union failed; "
                        "FAIL-OPEN to full registry",
                        exc_info=True,
                    )
                    _allowed_snapshot = set(TOOL_REGISTRY)
                _subset = _allowed_snapshot & set(TOOL_REGISTRY)
                if _subset:
                    _retrieval_registry = {
                        name: entry
                        for name, entry in TOOL_REGISTRY.items()
                        if name in _subset
                    }
                    logger.info(
                        "tool-retrieval enforce: %d/%d tools visible "
                        "(turn=%s session=%s)",
                        len(_retrieval_registry),
                        len(TOOL_REGISTRY),
                        pipeline_id,
                        state.session_id,
                    )
                else:
                    logger.warning(
                        "tool-retrieval enforce: empty subset; "
                        "FAIL-OPEN to full registry"
                    )
        except Exception:  # noqa: BLE001 -- any fault FAILS OPEN to the full catalog
            logger.warning(
                "tool-retrieval: selection failed; FAIL-OPEN to full registry "
                "(mode=%s)",
                _retrieval_mode,
                exc_info=True,
            )
            # FAIL-OPEN to the template-filtered default (NOT raw TOOL_REGISTRY):
            # a faulted retrieval must never re-leak engine templates into the
            # default declarations. Templates still reach the turn via door
            # expansion. See _default_declarable_registry.
            _retrieval_registry = _default_declarable_registry()

    # TOP-K TOOL GATING: the openai adapter path was sending ALL ~190 tool
    # schemas every round. Gate the per-turn tool list to the retrieval
    # top-k (TRID3NT_TOOL_GATING_TOPK, default 24; 0 disables) PLUS the
    # always-include floors -- the META set (hot set + catalog pair +
    # web_fetch), every tool already used this case-session, and any tool
    # the user NAMED in the message. Scoped to MODEL_PROVIDER=openai:
    # bedrock/scripted/vertex tool lists are byte-unchanged. FAIL-OPEN on a
    # cold index / empty ranking / any fault.
    if _provider == "openai":
        try:
            from .agent.gates.tool_gating import (
                WIDEN_K,
                gate_tool_registry,
                gating_topk,
                gating_widen_threshold,
                should_widen_for_poor_fit,
            )
            from .agent.tools.search.tool_retrieval import retrieve_ranked_tools

            _gate_k = gating_topk()
            if _gate_k > 0:
                _gate_ranked = retrieve_ranked_tools(user_text, k=_gate_k)
                # POOR-FIT WIDENING: a LOW top-1 retrieval score means the ranking is
                # uncertain for this ask -- widen the gate k once (24 -> WIDEN_K) so
                # recall does not silently drop on a vague/ambiguous turn. Fires at most
                # once per turn, only when the widened k exceeds the current k.
                # Threshold is env-tunable (TRID3NT_GATING_WIDEN_THRESHOLD).
                _widen_threshold = gating_widen_threshold()
                if (
                    WIDEN_K > _gate_k
                    and should_widen_for_poor_fit(_gate_ranked, _widen_threshold)
                ):
                    _top_score = _gate_ranked[0][1] if _gate_ranked else None
                    _gate_k = WIDEN_K
                    _gate_ranked = retrieve_ranked_tools(user_text, k=_gate_k)
                    logger.info(
                        "tool-gating: POOR-FIT widen k->%d (top_score=%.5f < "
                        "threshold=%.5f) turn=%s session=%s",
                        _gate_k,
                        _top_score if _top_score is not None else -1.0,
                        _widen_threshold,
                        pipeline_id,
                        state.session_id,
                    )
                _used_tools = set(state.allowed_tool_set.dispatched_tools) | set(
                    state.allowed_tool_set.explicit_tools
                )
                _gated = gate_tool_registry(
                    user_text,
                    _retrieval_registry,
                    _gate_ranked,
                    _gate_k,
                    used_tools=_used_tools,
                )
                if _gated is not None:
                    logger.info(
                        "tool-gating: %d/%d tools visible (topk=%d used=%d "
                        "turn=%s session=%s)",
                        len(_gated),
                        len(_retrieval_registry),
                        _gate_k,
                        len(_used_tools),
                        pipeline_id,
                        state.session_id,
                    )
                    _retrieval_registry = _gated
                else:
                    logger.info(
                        "tool-gating: no-op (%d tools already visible via the "
                        "retrieval-enforce layer; topk=%d ranked=%d) turn=%s",
                        len(_retrieval_registry),
                        _gate_k,
                        len(_gate_ranked),
                        pipeline_id,
                    )
        except Exception:  # noqa: BLE001 -- gating faults FAIL OPEN (all tools)
            logger.warning(
                "tool-gating: fault; FAIL-OPEN to ungated registry",
                exc_info=True,
            )

    # ADR 0018: auto/ask tool-candidates gate. May PAUSE here (bounded --
    # see _tool_choice_timeout_s) awaiting the user's tool-choice. A pinned
    # tool is unioned into the visible registry + allowed set BEFORE
    # declarations are built so the model can actually call it. Any fault
    # proceeds autonomously.
    _pin_notes: list[str] = []
    try:
        _pinned_tool, _pin_notes = await _maybe_emit_tool_candidates(
            websocket, state, user_text
        )
        _retrieval_registry = _union_pinned_tool(
            _pinned_tool, _retrieval_registry, state
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- the picker is an optimization
        logger.warning(
            "tool-candidates gate failed; proceeding autonomously",
            exc_info=True,
        )
    tool_decls = build_tool_declarations(_retrieval_registry)

    # GCP decommissioned: the agent runs on Bedrock, whose prompt caching is
    # its own ``cachePoint`` mechanism (bedrock_adapter). The Vertex-only
    # ``CachedContent`` fast-path (``gemini_cache.py``) is REMOVED, so this is
    # always ``None``. The field is retained for the ``cache-status`` envelope
    # payload (``_emit_cache_status``) which reports cachePoint hit metrics.
    state.gemini_cache_name = None

    # Seed the multi-turn contents list with chat history + this user_text.
    # The entry-captured list -- a mid-stream case switch rebinds
    # state.chat_history, never mutates this one.
    #
    # Inject the already-loaded-layers + reuse-AOI note on EVERY live turn
    # (lists each layer already on the map plus the Case AOI bbox with a
    # REUSE instruction) so a long live turn does not re-geocode or re-fetch
    # layers already present. Appended as the LAST history turn before the
    # user message; ``None`` (no layers, no bbox) is a no-op. Built from the
    # LIVE emitter + cached Case AOI so it reflects this turn's current truth.
    turn_history_for_contents = turn_history
    try:
        loaded_layers = (
            [layer.model_dump(mode="json") for layer in state.emitter.loaded_layers]
            if state.emitter is not None
            else []
        )
        case_state_note = build_layers_present_note(
            loaded_layers, case_bbox=_turn_case_bbox(state)
        )
        if case_state_note:
            turn_history_for_contents = list(turn_history) + [
                {"role": "user", "text": case_state_note}
            ]
    except Exception:  # noqa: BLE001 -- the note is an optimization, never fatal
        logger.debug("per-turn case-state note build failed", exc_info=True)
    contents = build_contents_from_history(user_text, turn_history_for_contents)

    # ADR 0018 (Stage 3): feed the tool-candidates outcome into the model
    # context -- the pin directive ("Use the tool 'X'"), the user's free-text
    # clarification, or the timeout proceed-autonomously note. Appended AFTER
    # the user message so the model reads the ask, then the user's routing
    # decision. No-op when the gate never fired (the common path).
    for _pin_note in _pin_notes:
        contents.append(build_user_text_content(_pin_note))

    # Wave 4.11 M6: refresh the dynamic hot set once per user-message dispatch
    # so the allowed set is primed with the user's most-dispatched tools before
    # any Gemini function_call arrives.  No-op when ``TRID3NT_DYNAMIC_HOT_SET``
    # is unset (delegates synchronously to the static path).  Failure is silent
    # -- the static fallback is always available inside ``as_frozenset_async``.
    try:
        await state.allowed_tool_set.as_frozenset_async()
    except Exception:  # noqa: BLE001 -- dynamic hot-set is best-effort
        pass

    # Per-turn usage metadata harvested from the stream (job-B6).
    last_usage: UsageMetadataEvent | None = None

    # RUNAWAY-AGENT GUARD: three independent per-turn bounds route to a
    # single clean ABORT that terminates the turn (releasing busy) instead of
    # letting the model<->tool loop run away and wedge the shared box:
    #   1. STEP CAP -- min of the historical MAX_TURN_ITERATIONS and the
    #      model-tier step cap (cheap/Nova/Haiku tiers get HALF).
    #   2. WALL-CLOCK -- a per-turn deadline aborts a slow turn even under
    #      the step cap.
    #   3. LOOP WATCHDOG -- aborts when the SAME tool+args (or identical
    #      round signature) repeats N rounds in a row with no progress.
    # ``_agent_abort`` is set to (reason_code, message) the moment a guard
    # fires; the loop breaks and the post-loop block surfaces the honest
    # typed envelope (honesty floor) like the loop_exhausted fail-stop.
    _step_cap = min(MAX_TURN_ITERATIONS, step_cap_for_model(bedrock_model))
    _turn_deadline = started_at + max_turn_seconds()
    _watchdog = LoopWatchdog()
    _agent_abort: tuple[str, str] | None = None

    # CRISP-END-AFTER-DELIVERABLE: once a terminal composer (run_model_* &
    # friends) has produced its artifact, the model should narrate a short
    # summary and STOP rather than spin to the loop_exhausted cap.
    # _deliverable_done latches on first delivery; _post_deliverable_idle
    # counts consecutive no-progress rounds and resets on a producing round
    # (multi-deliverable flows are never cut off). See
    # _POST_DELIVERABLE_WRAPUP_ROUNDS.
    _deliverable_done = False
    _post_deliverable_idle = 0
    _crisp_concluded = False

    # OPEN-14 FABRICATION BACKSTOP: tracks whether ANY round of this turn
    # dispatched a tool call. A turn that ends with this still False AND
    # whose closing narration claims a completed geospatial action gets an
    # honest caveat appended -- see the ``not turn_function_calls`` block
    # below. Never set True by a round that merely REQUESTED calls that
    # later failed validation -- turn_function_calls is the model's raw
    # request (a model that TRIED to act is not fabricating; one that never
    # tried and claims success is).
    _turn_ever_called_tool = False

    # OPEN-16 EMPTY-COMPLETION RETRY: per-turn counter of empty-round retries
    # already spent, capped at ``_EMPTY_COMPLETION_RETRY_CAP``. Past the cap the
    # empty round falls through to the existing terminal break (never an infinite
    # loop). Local-path only (guarded on ``_provider == "openai"`` below).
    _empty_retries = 0

    # Turn-loop invariants + guard (d) tracker:
    #   _turn_tools_dispatched -- every tool NAME this turn requested (the
    #       bare-geocode backstop reads it: a turn whose ONLY tool was
    #       geocode_location while the user asked for data gets one nudge).
    #   _continuation_nudged  -- the ONE-per-turn continuation-nudge budget
    #       shared by both invariants (never more than one nudge per turn).
    #   _turn_geocode_bbox    -- the last successful geocode_location bbox
    #       this turn; guard (d) appends an advisory drift WARNING to any
    #       later call whose bbox intersects neither this nor the active AOI.
    _turn_tools_dispatched: set[str] = set()
    _continuation_nudged = False
    _turn_geocode_bbox: list[float] | None = None

    # BENCH pre-dispatch block hook: latched True by the dispatch except-path
    # when a WRONG-pick block fired this round, so the turn ends after the
    # round's function-responses are on the wire (see the check after the
    # per-call loop). Unarmed sessions never touch it.
    _bench_wrong_pick_end = False

    # DISCOVERY-EXPANDS-GATE (task 2): tool names the tool-search tool
    # (search_tools) returned THIS turn that were unioned into the visible gate
    # for subsequent rounds, capped at ``_DISCOVERY_EXPAND_CAP`` per turn.
    # ``_tool_decls_dirty`` requests a one-time rebuild of ``tool_decls`` after
    # the round so the next model round sees the widened set.
    _discovery_expanded: set[str] = set()
    _tool_decls_dirty = False

    # PER-TURN TELEMETRY accumulators: token counts SUM the adapter's
    # per-round UsageMetadataEvents across the whole turn; a provider that
    # reports no usage leaves them None (tolerated, never fabricated).
    # _turn_error_class is stamped by the exception handlers below and
    # stays None on a clean turn. The record is emitted in the finally at
    # the end of this function -- one record per turn, every outcome.
    _turn_prompt_tokens: int | None = None
    _turn_completion_tokens: int | None = None
    _turn_reasoning_tokens: int | None = None
    _turn_tool_dispatch_count = 0
    _turn_error_class: str | None = None

    iterations = 0
    try:
        while iterations < _step_cap:
            # GUARD 2 (wall-clock): abort BEFORE the next (potentially long)
            # model round if this turn has already overrun its budget. Checked
            # at the top of every iteration so a turn whose rounds are each slow
            # cannot exceed the wall-clock bound by more than one round.
            if asyncio.get_running_loop().time() >= _turn_deadline:
                _agent_abort = (ABORT_WALL_CLOCK, abort_message(ABORT_WALL_CLOCK))
                break
            iterations += 1
            # Per-turn collectors: text emitted, function-calls Gemini requested.
            turn_text_parts: list[str] = []
            turn_function_calls: list[FunctionCallEvent] = []
            last_usage = None
            # Compaction UX: the step_id of the currently-open compaction card, if
            # any -- set on CompactionStartEvent, read + cleared on the matching
            # CompactionCompleteEvent. Local to this round: the adapter always
            # pairs the two 1:1 within one stream_events_with_contents call, and a
            # round can legitimately mint+complete more than one (proactive, then a
            # later reactive retry).
            _compaction_step_id: str | None = None

            # ADR 0018 wave semantics: in ASK mode, surface a pre-dispatch
            # tool-candidates WAVE before EACH subsequent round (round 1 is covered
            # by the pre-loop emission above). Excluding this turn's
            # already-dispatched tools advances the stage label so a multi-step
            # turn reads as a SEQUENCE of stage-labeled picks, not one blob. AUTO
            # mode is unchanged. Fail-open: the wave is an optimization, never a
            # wall.
            if iterations > 1 and _session_routing_mode(state) == "ask":
                try:
                    _w_pinned, _w_notes = await _maybe_emit_tool_candidates(
                        websocket,
                        state,
                        user_text,
                        exclude_tools=_turn_tools_dispatched,
                    )
                    _w_reg = _union_pinned_tool(
                        _w_pinned, _retrieval_registry, state
                    )
                    if _w_reg is not _retrieval_registry:
                        _retrieval_registry = _w_reg
                        tool_decls = build_tool_declarations(_retrieval_registry)
                    for _w_note in _w_notes:
                        contents.append(build_user_text_content(_w_note))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- wave is best-effort
                    logger.warning(
                        "per-round tool-candidates wave failed; proceeding",
                        exc_info=True,
                    )

            async for event in stream_events_with_contents(
                client,
                settings.model,
                contents,
                tool_declarations=tool_decls,
                system_prompt=SYSTEM_PROMPT,
                cached_content_name=state.gemini_cache_name,
                bedrock_model=bedrock_model,
                show_thinking=show_thinking,
            ):
                if not first_token_logged:
                    first_token_logged = True
                    elapsed_ms = (asyncio.get_running_loop().time() - started_at) * 1000.0
                    logger.info(
                        "first-token session=%s elapsed_ms=%.1f model=%s",
                        state.session_id,
                        elapsed_ms,
                        settings.model,
                    )

                if isinstance(event, TextDeltaEvent):
                    # Open a NEW bubble on the first text of a segment.
                    if current_message_id is None:
                        current_message_id = new_ulid()
                    chunk = AgentMessageChunkPayload(
                        message_id=current_message_id, delta=event.delta, done=False
                    )
                    await _session_safe_send(websocket, state.session_id,
                        _new_envelope("agent-message-chunk", state.session_id, chunk)
                    )
                    turn_text_parts.append(event.delta)
                    # Accumulate across ALL iterations -- the turn
                    # close persists the full narration for Case replay.
                    # Entry-captured list, never the live field.
                    turn_narration.append(event.delta)
                    # Also feed the OPEN-segment buffer so the
                    # boundary finalize (A3 / A4) persists exactly this run's
                    # text, and a crash leaves the un-finalized tail for the
                    # wrapper. Same registered list object -- never rebound.
                    _segment_buf.append(event.delta)

                elif isinstance(event, ThinkingDeltaEvent):
                    # F8: forward the model's reasoning-channel deltas so the web/QGIS
                    # clients render the greyed foldable thinking block. Gated on the
                    # per-turn user toggle -- with it off the /no_think suppressor is armed
                    # and the channel is not generated, but a model that leaks reasoning
                    # anyway must not reach a client that asked for it to stay hidden.
                    # Shares the segment's message_id (the thinking block and its answer
                    # live in the SAME bubble). The deltas also accumulate in
                    # _thinking_buf so _finalize_segment persists them as the ``thinking``
                    # field on the SAME agent row as the answer; a thinking-only segment
                    # still persists no row of its own (no phantom bubble) -- its buffered
                    # thinking rides the turn's next persisted row.
                    if show_thinking:
                        if current_message_id is None:
                            current_message_id = new_ulid()
                        await _session_safe_send(websocket, state.session_id,
                            _new_envelope(
                                "agent-thinking-chunk",
                                state.session_id,
                                AgentThinkingChunkPayload(
                                    message_id=current_message_id,
                                    delta=event.delta,
                                    done=False,
                                ),
                            )
                        )
                        # Thinking persistence (LANE CORE 2026-07-22):
                        # accumulate the reasoning text for THIS segment so
                        # ``_finalize_segment`` persists it as the ``thinking``
                        # field on the same agent row as the answer.
                        _thinking_buf.append(event.delta)

                elif isinstance(event, FunctionCallEvent):
                    logger.info(
                        "gemini function-call session=%s iter=%d tool=%s call_id=%s args=%r",
                        state.session_id,
                        iterations,
                        event.name,
                        event.call_id,
                        event.args,
                    )
                    turn_function_calls.append(event)

                elif isinstance(event, UsageMetadataEvent):
                    # Gemini surfaces aggregate usage on the terminal chunk. Cache the event
                    # so the post-turn block can pipe cached_content_token_count into
                    # per-tool telemetry and emit a single cache-status envelope for the
                    # live cache hit-rate UI.
                    last_usage = event
                    # PER-TURN TELEMETRY (LANE CORE 2026-07-22): sum the
                    # reported counts across the turn's model rounds. A round
                    # that reports None for a figure leaves that accumulator
                    # untouched (null stays null when NO round reports it --
                    # tolerate absent, never fabricate).
                    if event.prompt_token_count is not None:
                        _turn_prompt_tokens = (
                            (_turn_prompt_tokens or 0) + event.prompt_token_count
                        )
                    if event.candidates_token_count is not None:
                        _turn_completion_tokens = (
                            (_turn_completion_tokens or 0)
                            + event.candidates_token_count
                        )
                    if event.reasoning_token_count is not None:
                        _turn_reasoning_tokens = (
                            (_turn_reasoning_tokens or 0)
                            + event.reasoning_token_count
                        )
                    logger.info(
                        "gemini usage session=%s iter=%d cached=%s total=%s "
                        "prompt=%s candidates=%s hit=%s",
                        state.session_id,
                        iterations,
                        event.cached_content_token_count,
                        event.total_token_count,
                        event.prompt_token_count,
                        event.candidates_token_count,
                        event.cache_hit,
                    )

                elif isinstance(event, CompactionStartEvent):
                    # Compaction UX: mint the durable running "Compacting conversation..."
                    # card the instant the adapter announces compaction is about to run
                    # (proactive or the reactive clip-guard retry). Mirrors the two-card SIM
                    # observability's running-card mint; best-effort so a mint failure can
                    # never block the turn.
                    _compaction_step_id = await mint_compaction_card(
                        emitter=state.emitter
                    )

                elif isinstance(event, CompactionCompleteEvent):
                    # Compaction UX (Part A): flip the card minted above to
                    # its terminal "Conversation compacted (Nk -> Mk tokens)"
                    # state. No-op (best-effort) if the mint above failed or
                    # never fired (emitter unbound) -- see
                    # complete_compaction_card's own None-guard.
                    await complete_compaction_card(
                        emitter=state.emitter,
                        step_id=_compaction_step_id,
                        before_tokens=event.before_tokens,
                        after_tokens=event.after_tokens,
                    )
                    _compaction_step_id = None

            # Emit a cache-status envelope so the UI can render the cache
            # hit-rate live. Best-effort -- a serialization failure logs but
            # does not break the turn (the envelope is observability, not
            # part of the agent loop's correctness contract).
            if last_usage is not None:
                await _emit_cache_status(websocket, state, last_usage)

            # Turn ended.  If Gemini emitted no function_calls this turn, it
            # is finished -- either narrated the answer or had nothing more to
            # do.  Break out of the loop.
            if not turn_function_calls:
                # OPEN-16 EMPTY-COMPLETION RETRY: a round with ZERO tool calls and ZERO
                # non-whitespace text is the qwen3 empty-completion shape -- retry with a
                # corrective user nudge, bounded by _EMPTY_COMPLETION_RETRY_CAP, rather
                # than silently dropping the request. Runs BEFORE the OPEN-14 fabrication
                # backstop (disjoint: an empty round has no closing text to fabricate
                # from). Scoped to the LOCAL (MODEL_PROVIDER=openai) path only -- Bedrock's
                # production narration (a legitimately empty round) stays byte-unchanged.
                # The empty round still counts toward _step_cap and never trips the loop
                # watchdog, so a retry cannot escape the step cap or the runaway guard.
                _empty_round = not "".join(turn_text_parts).strip()
                if (
                    _provider == "openai"
                    and _empty_round
                    and _empty_retries < _EMPTY_COMPLETION_RETRY_CAP
                ):
                    _empty_retries += 1
                    logger.warning(
                        "empty-completion retry %d/%d session=%s iter=%d",
                        _empty_retries,
                        _EMPTY_COMPLETION_RETRY_CAP,
                        state.session_id,
                        iterations,
                    )
                    # Corrective user-role nudge, built with the same plain-text
                    # Content idiom the initial user message uses (adapter.
                    # build_user_text_content) -- no hand-rolled google.genai
                    # types here. Appended so the retried round sees "your last
                    # turn was empty, act or answer".
                    contents.append(build_user_text_content(_EMPTY_COMPLETION_NUDGE))
                    # Observability is log-only (above): a retry must not inject
                    # a transient note into the persisted narration segment, and
                    # inventing a new envelope type is out of scope (NATE is
                    # live) -- the log.warning is the durable retry witness.
                    continue
                # Stage 3 TURN-LOOP INVARIANTS: ONE continuation nudge per turn, shared
                # budget, injected as user-role content with the round retried:
                #   (a) NO-SILENT-END -- terminating with tool results but ZERO
                #       assistant text since the last tool round. Skipped when OPEN-16
                #       already nudged this turn (_empty_retries > 0).
                #   (b) BARE-GEOCODE BACKSTOP -- the turn's ONLY tool was
                #       geocode_location while the user asked for data or analysis.
                # Kill-switch: TRID3NT_TURN_INVARIANTS=0. The nudge round still counts
                # toward the step cap; the shared budget guarantees at most one nudge per
                # turn (unified with OPEN-16 -- no stacking). Every terminal round logs
                # one INFO line per invariant: FIRED, or SKIPPED with its reason.
                if (
                    not _continuation_nudged
                    and _empty_retries == 0
                    and _env_flag("TRID3NT_TURN_INVARIANTS", True)
                ):
                    _nudge_reason: str | None = None
                    if (
                        _turn_ever_called_tool
                        and not "".join(turn_text_parts).strip()
                    ):
                        _nudge_reason = "no-silent-end"
                    elif _turn_tools_dispatched == {
                        "geocode_location"
                    } and _asks_for_data_or_analysis(user_text):
                        _nudge_reason = "bare-geocode"
                    if _nudge_reason is not None:
                        _continuation_nudged = True
                        logger.info(
                            "turn-invariant nudge (%s) session=%s iter=%d",
                            _nudge_reason,
                            state.session_id,
                            iterations,
                        )
                        contents.append(
                            build_user_text_content(_CONTINUATION_NUDGE)
                        )
                        continue
                    # Neither invariant fired -- log each skip with its reason.
                    logger.info(
                        "turn-invariant no-silent-end skipped session=%s "
                        "iter=%d reason=%s",
                        state.session_id,
                        iterations,
                        (
                            "no-tools-dispatched"
                            if not _turn_ever_called_tool
                            else "has-closing-text"
                        ),
                    )
                    logger.info(
                        "turn-invariant bare-geocode skipped session=%s "
                        "iter=%d reason=%s tools=%s",
                        state.session_id,
                        iterations,
                        (
                            "tools-not-geocode-only"
                            if _turn_tools_dispatched != {"geocode_location"}
                            else "not-a-data-or-analysis-ask"
                        ),
                        sorted(_turn_tools_dispatched),
                    )
                elif _env_flag("TRID3NT_TURN_INVARIANTS", True):
                    logger.info(
                        "turn-invariants skipped session=%s iter=%d reason=%s",
                        state.session_id,
                        iterations,
                        (
                            "nudge-budget-spent"
                            if _continuation_nudged
                            else "empty-completion-retry-owned-this-turn"
                        ),
                    )
                else:
                    logger.info(
                        "turn-invariants skipped session=%s iter=%d "
                        "reason=disabled-by-env",
                        state.session_id,
                        iterations,
                    )
                logger.info(
                    "gemini loop terminal session=%s iter=%d text_chunks=%d",
                    state.session_id,
                    iterations,
                    len(turn_text_parts),
                )
                # OPEN-14 FABRICATION BACKSTOP: fires only on the FIRST and ONLY
                # tool-call-free round this turn ever had. Conservative: only fires when
                # the closing text pairs a completed-action verb with a geospatial-output
                # noun in the same sentence -- see
                # context_budget.looks_like_fabricated_action_claim. Ordinary Q&A
                # answers, and any turn that dispatched even one tool call, never
                # trigger this. Scoped to the LOCAL (MODEL_PROVIDER=openai) path only --
                # a local-model-path guard that must not vary Bedrock's production
                # narration.
                if _provider == "openai" and not _turn_ever_called_tool:
                    _closing_text = "".join(turn_text_parts)
                    if looks_like_fabricated_action_claim(_closing_text):
                        logger.warning(
                            "context-budget: fabrication backstop fired "
                            "session=%s iter=%d (zero tool calls this turn)",
                            state.session_id,
                            iterations,
                        )
                        if current_message_id is None:
                            current_message_id = new_ulid()
                        _caveat = f"\n\n{FABRICATION_CAVEAT}"
                        await _session_safe_send(websocket, state.session_id,
                            _new_envelope(
                                "agent-message-chunk",
                                state.session_id,
                                AgentMessageChunkPayload(
                                    message_id=current_message_id, delta=_caveat, done=False
                                ),
                            )
                        )
                        turn_narration.append(_caveat)
                        _segment_buf.append(_caveat)
                break
            _turn_ever_called_tool = True
            # Stage 3 invariants: record this round's requested tool names
            # (the bare-geocode backstop compares the turn's full set).
            _turn_tools_dispatched.update(c.name for c in turn_function_calls)

            # GUARD 3 (loop watchdog): compute THIS round's (tool, args_hash)
            # signature now, but feed it to the watchdog AFTER dispatch together
            # with a PROGRESS witness. A no-progress runaway -- the SAME tool+args
            # (or identical round signature) N rounds in a row that keeps returning
            # nothing new -- trips the watchdog and aborts. A round that PRODUCES a
            # layer/artifact, or one the circuit breaker owns (all calls failed /
            # short-circuited), is NOT counted: a producing loop runs to the
            # step-cap / loop-exhausted envelope, and the breaker handles the
            # failing tool. Recording after dispatch (vs before) costs at most ONE
            # extra identical round before the trip, still far under the step cap.
            _round_sig = [
                (c.name, compute_args_hash(c.args)) for c in turn_function_calls
            ]
            # Per-round progress witness, OR'd across the round's calls. Seeded
            # True only if EVERY call ends up failing / short-circuited (the
            # breaker's territory) -- tracked as no calls-succeeded-without-output
            # below. Starts False; set True by a producing dispatch.
            _round_made_progress = False
            _round_had_failure = False
            _round_had_success = False

            # A function-call round is about to dispatch -- close the current
            # narration bubble (if any text was emitted) BEFORE the tool cards for
            # this round land on the wire, so the next run of text opens a fresh
            # bubble that interleaves AFTER them. Fires ONCE per round, before ALL
            # calls dispatch, so multiple function calls in one round close exactly
            # one prior bubble. _finalize_segment sends done=True AND persists this
            # segment's own role="agent" row.
            if current_message_id is not None:
                await _finalize_segment(
                    websocket, state, current_message_id, _segment_buf,
                    thinking_parts=_thinking_buf,
                )
                current_message_id = None  # next text opens a fresh segment

            # Otherwise: dispatch each call, then append the call + summarized
            # response back into contents so the next Gemini turn sees them.
            for call in turn_function_calls:
                # Dispatch through the registry + emitter (Invariant 2 -- the LLM's
                # tool choice IS the classification). Routing failures (TOOL_NOT_FOUND,
                # PAYLOAD_WARNING_CANCELLED) raise typed exceptions so the except-block
                # below routes them through summarize_tool_result(error=...) -- a
                # structured {status: "error", error_code: str, retryable: bool}
                # envelope Gemini can distinguish from "tool ran and returned nothing"
                # (FR-AS-11).
                dispatch_error: BaseException | None = None
                result: Any = None
                # CRISP-END (NATE 2026-06-29): set True iff THIS call is a
                # top-level run-a-model composer that produced its deliverable.
                _call_is_terminal_deliverable = False
                _tool_start = asyncio.get_running_loop().time()
                try:
                    # Per-session circuit breaker: short-circuit before allowed-set
                    # validation and dispatch if the tool has failed repeatedly this
                    # session. Raises CircuitBreakerError, routed by the except-block below
                    # through summarize_tool_result(error=...) so Gemini reads the
                    # structured cooldown signal (not retryable).
                    if state.circuit_breaker.is_tripped(call.name):
                        remaining = state.circuit_breaker.cooldown_remaining_s(call.name)
                        raise CircuitBreakerError(call.name, remaining)
                    # Post-hoc allowed-set validation: per the CachedContent Option A
                    # architecture, Gemini sees the full catalog but our code enforces the
                    # per-turn allowed set. A function_call outside the allowed set raises
                    # OutOfAllowedSetError, routed by the except-block below through
                    # summarize_tool_result(error=...) as a structured envelope so Gemini
                    # can retry (typically by first calling list_tools_in_category).
                    validate_function_call(call.name, state.allowed_tool_set)
                    result = await _invoke_tool_via_emitter(
                        websocket, state, call.name, call.args
                    )
                    # FR-AS-10 / FR-WC-16: request_spatial_input PAUSES the turn awaiting a
                    # user-drawn FeatureCollection. The catalog tool returns the
                    # SPATIAL_INPUT_SENTINEL_KEY sentinel (it has no websocket access);
                    # here -- where the live socket + the session future registry ARE
                    # reachable -- we emit the spatial-input-request, await the drawn
                    # reply, and REPLACE result with the parsed, role-split geometry (the
                    # clean engine-ready barriers FeatureCollection + aoi_bbox + points).
                    # The LLM then calls swmm_urban_flood with barriers= straight from this
                    # result. Mirrors the geocode_location -> region-choice pause/resume
                    # seam. Fail-open: timeout / cancel / no client / malformed draw all
                    # become a TYPED result (honesty floor), never a fabricated
                    # AOI/barriers.
                    if (
                        call.name == "request_spatial_input"
                        and isinstance(result, dict)
                        and result.get(SPATIAL_INPUT_SENTINEL_KEY) is True
                    ):
                        result = await _handle_request_spatial_input(
                            websocket, state, call.args or {}
                        )
                    # Emit an impact-envelope WS envelope whenever compute_impact_envelope
                    # returns a result carrying a valid ImpactEnvelope (key signal:
                    # raw_envelope dict with n_structures_total inside). Fires IN ADDITION
                    # to the standard function_response -- the client gets both: replay for
                    # the Gemini loop, and impact-envelope for ImpactPanel state.
                    if (
                        call.name == "compute_impact_envelope"
                        and isinstance(result, dict)
                        and isinstance(result.get("raw_envelope"), dict)
                        and "n_structures_total" in result["raw_envelope"]
                    ):
                        await _maybe_emit_impact_envelope(websocket, state, result["raw_envelope"])
                    # Region-disambiguation picker: when geocode_location came back as a
                    # state-bbox-fallback snap, offer the user a narrower sub-region
                    # (default: counties) on top of the whole-state default. PAUSES the
                    # turn awaiting the region-choice-provided reply; on a "region" pick
                    # this MUTATES result["bbox"] in place so the immediate zoom-to below
                    # AND the function_response Gemini reads next turn use the narrowed
                    # extent. Fail-open: headless client / timeout / whole-state pick keeps
                    # the state bbox unchanged. MUST run BEFORE the zoom-to so the camera
                    # snaps to the final extent.
                    if (
                        call.name == "geocode_location"
                        and isinstance(result, dict)
                    ):
                        await _maybe_handle_region_choice(
                            websocket, state, result
                        )
                    # Demo UX: snap the map to a geocoded location
                    # IMMEDIATELY -- the user should not wait for a downstream
                    # layer publish to see the map move. Best-effort.
                    if (
                        call.name == "geocode_location"
                        and isinstance(result, dict)
                        and result.get("bbox")
                        and state.emitter is not None
                    ):
                        try:
                            await state.emitter.emit_map_command(
                                "zoom-to", {"bbox": list(result["bbox"])}
                            )
                            # Accumulate the turn's zoom-to so the closing CaseChatMessage persists
                            # it in map_command_emissions -- Case-reopen snap-to-location replays
                            # the LAST persisted zoom-to.
                            state.current_turn_map_commands.append(
                                {
                                    "command": "zoom-to",
                                    "args": {"bbox": list(result["bbox"])},
                                }
                            )
                        except Exception:  # noqa: BLE001 -- UX nicety only
                            logger.debug("geocode zoom-to emit failed", exc_info=True)
                    # SNAP-TO-AOI INDEPENDENT OF GEOLOCATE: the camera must snap whenever an
                    # AOI/bbox is SET, not only on a geocode_location result -- when the
                    # user gives coordinates directly the model skips geocode_location, so
                    # generalize: ANY tool result carrying a usable bbox / aoi_bbox snaps
                    # the camera (deduped against the turn's last zoom-to so a chain of
                    # bbox-bearing tools does not re-snap to the SAME extent).
                    # geocode_location is emitted above; skip it here to avoid a double-emit.
                    if call.name != "geocode_location" and state.emitter is not None:
                        aoi = _aoi_zoom_to_bbox(
                            result, state.current_turn_map_commands
                        )
                        if aoi is not None:
                            try:
                                await state.emitter.emit_map_command(
                                    "zoom-to", {"bbox": list(aoi)}
                                )
                                state.current_turn_map_commands.append(
                                    {"command": "zoom-to", "args": {"bbox": list(aoi)}}
                                )
                            except Exception:  # noqa: BLE001 -- UX nicety only
                                logger.debug(
                                    "aoi-set zoom-to emit failed", exc_info=True
                                )
                    # Emit a chart-emission WS envelope whenever a chart-generation tool
                    # returns a ChartEmissionPayload-shaped dict (key signal: envelope_type
                    # == "chart-emission" + a dict vega_lite_spec). Fires IN ADDITION to the
                    # standard function_response -- the client gets the full Vega-Lite spec
                    # on the envelope (vega-embed rendering + stacked gallery), and a
                    # COMPACT data summary on the function_response (stripped by
                    # summarize_tool_result so Gemini narrates from numbers, not inline
                    # rows). Also persists a SessionChartRecord so the chart replays on Case
                    # rehydration.
                    if is_chart_emission_result(result):
                        await _maybe_emit_chart(websocket, state, result)
                    # Emit a code-exec-result WS envelope whenever code_exec_request returns
                    # a result carrying the full code-exec-result payload (key signal:
                    # _code_exec_result with envelope_type == "code-exec-result"). Fires IN
                    # ADDITION to the standard function_response -- the client gets the full
                    # result card via the envelope, Gemini gets the COMPACT summary (spec
                    # stripped by summarize_tool_result).
                    if is_code_exec_result(result):
                        await _maybe_emit_code_exec_result(websocket, state, result)
                    # job-B8: record success so the consecutive-failure counter
                    # resets -- a recovered tool should not stay penalised.
                    state.circuit_breaker.record_success(call.name)
                    # Loop-watchdog progress witness: a successful call that PRODUCED a real
                    # artifact (layer/handle/feature set) resets the no-progress streak even
                    # if the model repeats the same call; a bare-ack return does NOT -- that
                    # is the no-op-repeat wedge shape the watchdog exists to catch.
                    _round_had_success = True
                    _call_made_progress = _dispatch_made_progress(result)
                    if _call_made_progress:
                        _round_made_progress = True
                    # CRISP-END: a top-level run-a-model composer that just produced its
                    # artifact IS the answer. Latch the deliverable + reset the
                    # post-deliverable idle streak, and stamp a one-time wrap-up directive
                    # below so the model summarizes and stops instead of spinning to the
                    # loop_exhausted cap.
                    _call_is_terminal_deliverable = (
                        _call_made_progress and _is_terminal_composer(call.name)
                    )
                    if _call_is_terminal_deliverable:
                        _deliverable_done = True
                        _post_deliverable_idle = 0
                    # On a successful dispatch, mark the tool sticky so the
                    # LLM can re-issue the same tool on a later turn with
                    # refined args without re-opening its category.
                    state.allowed_tool_set.record_dispatch(call.name)
                    # If the call was ``list_tools_in_category``, open the
                    # requested category (sticky-after-list) -- every member
                    # tool of that category is now reachable for the rest of
                    # the session.
                    if (
                        call.name == "list_tools_in_category"
                        and isinstance(result, dict)
                    ):
                        cat_id = result.get("category_id")
                        if isinstance(cat_id, str) and cat_id:
                            state.allowed_tool_set.open_category(cat_id)
                    # DISCOVERY-EXPANDS-GATE + ENGINE-DOOR expansion: when the tool-search
                    # tool OR an engine door returns candidate tool names, UNION them into
                    # this turn's visible gate (and the Case allowed-set, so validation
                    # lets the model actually call them) for SUBSEQUENT rounds. Search
                    # results are capped at _DISCOVERY_EXPAND_CAP (bounds an unbounded
                    # ranked tail); an engine door lists a CLOSED, curated set of its own
                    # templates and uses the larger _DOOR_EXPAND_CAP so select-then-call is
                    # never truncated. Only names that are real, registered, and not
                    # already visible count toward the cap; the rebuild of tool_decls is
                    # deferred to once-per-round below.
                    elif call.name in _gate_expander_tool_names():
                        _hits = _tool_names_from_search_result(result)
                        _is_door = call.name in _engine_door_tool_names()
                        _expand_cap = (
                            _DOOR_EXPAND_CAP if _is_door else _DISCOVERY_EXPAND_CAP
                        )
                        _added_now: list[str] = []
                        for _cand in _hits:
                            if len(_discovery_expanded) >= _expand_cap:
                                break
                            if (
                                _cand in TOOL_REGISTRY
                                and _cand not in _retrieval_registry
                                and _cand not in _discovery_expanded
                            ):
                                _discovery_expanded.add(_cand)
                                _added_now.append(_cand)
                        if _added_now:
                            _retrieval_registry = dict(_retrieval_registry)
                            for _cand in _added_now:
                                _retrieval_registry[_cand] = TOOL_REGISTRY[_cand]
                            state.allowed_tool_set.add_tools(set(_added_now))
                            _tool_decls_dirty = True
                            logger.info(
                                "%s-expand: +%d tool(s) into the gate "
                                "(turn total=%d/%d) via %s session=%s: %s",
                                "door" if _is_door else "discovery",
                                len(_added_now),
                                len(_discovery_expanded),
                                _expand_cap,
                                call.name,
                                state.session_id,
                                _added_now,
                            )
                except asyncio.CancelledError:
                    # Propagate cancel through the loop -- handled below.
                    raise
                except Exception as exc:  # noqa: BLE001 -- surface to Gemini
                    logger.exception(
                        "tool dispatch raised session=%s tool=%s err=%s",
                        state.session_id,
                        call.name,
                        exc,
                    )
                    # Record failure, passing the exception so the breaker counts ONLY
                    # upstream/transient faults toward the trip threshold. Deterministic
                    # CLIENT/arg errors (*ArgError, BboxInvalidError, ValueError/TypeError
                    # arg-shape errors) are model-side faults the model can self-correct and
                    # retry -- they must NOT trip a breaker that would then block the
                    # corrected-args retry. CircuitBreakerError is excluded entirely (the
                    # breaker already fired; do not increment again). BenchBlockedError is
                    # likewise excluded: a bench block is a harness artifact, not a tool
                    # fault, and must never penalize the tool's breaker.
                    if not isinstance(exc, (CircuitBreakerError, BenchBlockedError)):
                        state.circuit_breaker.record_failure(call.name, exc)
                    dispatch_error = exc
                    # BENCH pre-dispatch block hook: a WRONG-pick block ends the turn (the
                    # model must not get to pick again; the bench grades the wrong pick and
                    # moves on). A correct-block does NOT end the turn here (the bench ends
                    # it client-side after grading CORRECT_BLOCKED). Latched; the break
                    # happens once this round's calls are all recorded so the blocked
                    # tool's function-response still reaches the wire.
                    if (
                        isinstance(exc, BenchBlockedError)
                        and exc.blocked_class == "wrong_pick"
                    ):
                        _bench_wrong_pick_end = True
                    # A failed / circuit-broken call is the CIRCUIT BREAKER's territory (it
                    # delivers CIRCUIT_BREAKER_TRIPPED so the model adapts and the turn
                    # continues). Mark the round so the watchdog does NOT also count it --
                    # the breaker, not the watchdog, owns a stream-of-failures turn.
                    _round_had_failure = True
                _tool_latency_ms = (asyncio.get_running_loop().time() - _tool_start) * 1000.0

                summary = summarize_tool_result(
                    call.name, result, error=dispatch_error
                )
                _uri_reg = get_uri_registry(state.session_id)
                # ADR 0014 EMIT SEAM: the LLM-facing function_response shows SHORT
                # layer handles (L<n>) wherever a registered layer URI would appear --
                # the single biggest hallucination surface (~30 tokens per raw URI
                # echo). ONLY this LLM surface changes: the plugin-bound wire envelopes
                # keep carrying the REAL uri the plugin renders from. The rewrite never
                # raises (falls back to the unrewritten summary).
                summary = _uri_reg.rewrite_result_for_llm(summary)
                # Stage 3 guard (d): geocode drift warning. A successful
                # geocode_location pins this turn's geocoded bbox; any LATER call whose
                # bbox arg intersects NEITHER that bbox NOR the active AOI gets an
                # advisory WARNING appended to its function_response (never blocks).
                # Kill-switch: TRID3NT_GEOCODE_DRIFT_WARN=0.
                if call.name == "geocode_location":
                    if dispatch_error is None and isinstance(result, dict):
                        _gc_bbox = _coerce_bbox4(result.get("bbox"))
                        if _gc_bbox is not None:
                            _turn_geocode_bbox = list(_gc_bbox)
                elif (
                    _turn_geocode_bbox is not None
                    and isinstance(summary, dict)
                    and _env_flag("TRID3NT_GEOCODE_DRIFT_WARN", True)
                ):
                    _drift_note = _geocode_drift_note(
                        call.args, _turn_geocode_bbox, state.active_aoi_bbox
                    )
                    if _drift_note:
                        summary["aoi_drift_warning"] = _drift_note
                        logger.info(
                            "geocode-drift WARNING session=%s tool=%s "
                            "geocoded=%s",
                            state.session_id,
                            call.name,
                            _turn_geocode_bbox,
                        )
                # Surface the layer handles this dispatch registered so Gemini passes
                # HANDLES -- never raw storage paths -- into downstream *_uri params.
                # The announcement maps {layer_id: L<n>}; the server resolves either
                # form to the exact URIs it recorded (uri_registry.py).
                _new_handles = _uri_reg.drain_announcements()
                if _new_handles and dispatch_error is None:
                    summary["layer_handles"] = {
                        _layer_id: (_uri_reg.short_for_uri(_uri) or _layer_id)
                        for _layer_id, _uri in _new_handles.items()
                    }
                    # The note must make the publish step explicit --
                    # a computed/fetched layer is invisible until publish_layer
                    # adds it to the QGIS project (live finding: Gemini ended
                    # the colored-relief turn without publishing).
                    summary["layer_handles_note"] = (
                        "A layer is NOT visible on the user's map until "
                        "publish_layer(layer_uri=<handle>, "
                        "layer_id=<descriptive-id>) has run for it — if the "
                        "user asked to see this layer, call publish_layer "
                        "with the handle before finishing. Pass the short "
                        "handle (the L<n> value above) or the layer name "
                        "(the key) for any *_uri tool parameter — the server "
                        "resolves handles to the exact stored URIs. Do "
                        "NOT construct or echo gs:// paths, s3:// paths, or "
                        "any other storage URI."
                    )
                # A top-level run-a-model composer just delivered its artifact -- stamp
                # a one-time wrap-up directive on its function_response so the model
                # summarizes and STOPS rather than emitting more tool calls until the
                # loop_exhausted cap.
                if _call_is_terminal_deliverable and isinstance(summary, dict):
                    summary["completion_directive"] = _DELIVERABLE_COMPLETE_DIRECTIVE
                logger.info(
                    "function-response queued session=%s iter=%d tool=%s summary_keys=%s",
                    state.session_id,
                    iterations,
                    call.name,
                    sorted(summary.keys()),
                )

                # tool-card-expand-output: emit the raw input args + the raw
                # function_response on a tool-io sidecar keyed by THIS dispatch's
                # pipeline step. The web merges it into the matching tool card's
                # expander so a server-side / upstream-API failure the narration hides
                # becomes visible. Best-effort: a missing step_id (a dispatch that
                # never reached the emitter) skips the emit; the emitter itself never
                # raises on a bad payload.
                _io_step = (
                    state.emitter.last_tool_step
                    if state.emitter is not None
                    else None
                )
                # Guard against a STALE last_tool_step: a dispatch that raised
                # BEFORE the emitter created a step (ToolNotFoundError, payload-
                # warning cancel) leaves the prior tool's step on the accessor.
                # Only stamp IO when the recorded step is THIS tool's step.
                if _io_step is not None and _io_step.tool_name != call.name:
                    _io_step = None
                if _io_step is not None:
                    _io_is_error = dispatch_error is not None or (
                        isinstance(summary, dict)
                        and summary.get("status") == "error"
                    )
                    try:
                        await state.emitter.emit_tool_io(
                            step_id=_io_step.step_id,
                            tool_name=call.name,
                            raw_args=call.args,
                            function_response=summary,
                            is_error=_io_is_error,
                        )
                    except Exception:  # noqa: BLE001 -- expander is best-effort
                        logger.debug(
                            "tool-io emit failed session=%s tool=%s",
                            state.session_id,
                            call.name,
                            exc_info=True,
                        )

                # Fire-and-forget telemetry for this LLM-initiated function_call.
                # Non-blocking -- emit_tool_call_event wraps the write in
                # asyncio.ensure_future so no await is needed; a write failure logs at
                # WARNING and never raises. A workflow that swallowed its own exception
                # and returned a failed/partial envelope raises NO dispatch_error, but
                # summarize_tool_result stamps status="error" (honesty floor) -- derive
                # the telemetry success flag and error_code from that summary so a
                # returned-failure is recorded as a FAILURE in telemetry/routing, not a
                # silent success. A genuinely-raised exception still wins and keeps its
                # own code.
                _tel_error_code: str | None = None
                _tel_success = dispatch_error is None
                if dispatch_error is not None:
                    _tel_error_code = str(
                        getattr(dispatch_error, "error_code", None)
                        or type(dispatch_error).__name__.upper()
                    )
                elif isinstance(summary, dict) and summary.get("status") == "error":
                    _tel_success = False
                    _summary_code = summary.get("error_code")
                    _tel_error_code = (
                        str(_summary_code) if _summary_code is not None else None
                    )
                # job-B6 (Wave 4.10): the adapter now surfaces
                # ``UsageMetadataEvent`` at the end of each Gemini stream;
                # ``last_usage`` carries the most recent observation. Pipe
                # ``cached_content_token_count`` through so the telemetry
                # record empirically reflects the Vertex 90% discount.
                _tel_cached_tokens = (
                    last_usage.cached_content_token_count
                    if last_usage is not None
                    else None
                )
                # Derive result_usable at the SAME chokepoint, reusing the honesty-floor
                # signal already stamped on summary (NO_RENDERABLE_LAYER / failure-
                # tagged modeled envelope). A layer-producing tool that returned
                # status="ok" with an empty layers list is success=True but
                # result_usable=False. routed_ok stays None here -- the supersession
                # heuristic is a same-session ADJACENT-chain signal only computable at
                # aggregation time (tool_catalog_http._aggregate_records).
                _tel_result_usable = classify_result_usable(
                    call.name, result, summary
                )
                await emit_tool_call_event(
                    session_id=state.session_id,
                    ts=now_utc().isoformat(),
                    tool_name=call.name,
                    source="llm",
                    args_hash=compute_args_hash(call.args),
                    success=_tel_success,
                    latency_ms=_tool_latency_ms,
                    error_code=_tel_error_code,
                    cached_content_token_count=_tel_cached_tokens,
                    result_usable=_tel_result_usable,
                    model_id=_effective_model,
                    # turn_id = the per-user-message dispatch (pipeline) id: the
                    # recall@k join key against this turn's shadow-selection row.
                    turn_id=pipeline_id,
                )
                # PER-TURN TELEMETRY: one dispatched tool call counted at the
                # same chokepoint the per-tool record is emitted from.
                _turn_tool_dispatch_count += 1
                # Pass the thought_signature harvested off the function_call Part
                # through to the replayed model turn. Gemini 3 requires the same opaque
                # byte-blob on the replayed function_call Part or
                # generate_content_stream errors with "thought-signature mismatch".
                # Gemini 2.5 surfaces None (no signatures), which the helper treats as a
                # no-op -- forward-compat with no behavior change on the current model.
                contents.append(
                    build_function_call_content(
                        call.name,
                        call.args,
                        call.call_id,
                        thought_signature=call.thought_signature,
                    )
                )
                contents.append(
                    build_function_response_content(call.name, summary, call.call_id)
                )

            # DISCOVERY-EXPANDS-GATE (task 2): a tool-search this round widened
            # the visible gate -- rebuild ``tool_decls`` ONCE so the NEXT model
            # round sees the unioned tools. No-op unless a search actually added
            # (the common path never sets the dirty flag).
            if _tool_decls_dirty:
                tool_decls = build_tool_declarations(_retrieval_registry)
                _tool_decls_dirty = False
                logger.info(
                    "discovery-expand: rebuilt tool declarations (%d tools "
                    "visible) turn=%s session=%s",
                    len(_retrieval_registry),
                    pipeline_id,
                    state.session_id,
                )

            # BENCH pre-dispatch block hook: a WRONG-pick block this round ends the
            # turn. The blocked tool's typed function-response is already on the
            # wire above; break to the clean post-loop finalize so a turn-complete
            # is emitted and the bench grades the wrong pick and advances (a clean
            # conclusion, not an _agent_abort runaway).
            if _bench_wrong_pick_end:
                logger.info(
                    "bench-block: wrong-pick -> ending turn session=%s iter=%d",
                    state.session_id,
                    iterations,
                )
                break

            # GUARD 3 (loop watchdog) POST-DISPATCH record: the round counts toward
            # the no-progress streak ONLY when it had calls, did NOT produce a real
            # artifact, and was NOT a failure / circuit-broken round (the breaker's
            # territory). A producing round, an all-failed round, or a
            # short-circuited round resets the streak so the watchdog never
            # pre-empts loop-exhausted or CIRCUIT_BREAKER_TRIPPED. A genuine
            # no-op-repeat runaway (same successful call returning nothing new)
            # trips and aborts.
            _round_progressed = _round_made_progress or (
                _round_had_failure and not _round_had_success
            )
            _wd_trip = _watchdog.record_round(
                _round_sig, made_progress=_round_progressed
            )
            if _wd_trip is not None:
                logger.warning(
                    "loop watchdog tripped session=%s iter=%d sig=%r "
                    "made_progress=%s had_failure=%s had_success=%s",
                    state.session_id,
                    iterations,
                    _round_sig,
                    _round_made_progress,
                    _round_had_failure,
                    _round_had_success,
                )
                _agent_abort = (_wd_trip, abort_message(_wd_trip))
                break

            # CRISP-END-AFTER-DELIVERABLE: once a terminal composer has delivered, a
            # round that produces NOTHING NEW is the model spinning past a finished
            # answer. This SAFETY budget concludes the turn CLEANLY after a couple
            # of idle rounds -- a normal (no-tool) break, NOT the loop_exhausted
            # cap. A producing round resets the streak so genuine follow-up work is
            # never cut off. _agent_abort stays None: this is a clean conclusion,
            # closed exactly like a natural text-terminal exit.
            if _deliverable_done:
                if _round_made_progress:
                    _post_deliverable_idle = 0
                else:
                    _post_deliverable_idle += 1
                    if _post_deliverable_idle >= _POST_DELIVERABLE_WRAPUP_ROUNDS:
                        logger.info(
                            "crisp-end: deliverable done + %d idle round(s) "
                            "session=%s iter=%d -- concluding turn cleanly "
                            "(no loop_exhausted)",
                            _post_deliverable_idle,
                            state.session_id,
                            iterations,
                        )
                        _crisp_concluded = True
                        break

            # Loop: re-stream with the appended call + response so Gemini can
            # decide its next move (another tool call OR a narrative wrap-up).
        else:
            # Loop fell through the STEP CAP without a clean (no-tool-call) exit. A
            # guard (wall-clock / watchdog) abort instead breaks with _agent_abort
            # set and is surfaced below, so this else only handles natural
            # exhaustion of the step cap.
            #
            # RECONCILE THE STEP CAP WITH MAX_TURN_ITERATIONS: when the binding
            # bound is the historical MAX_TURN_ITERATIONS (full-tier models, where
            # _step_cap >= MAX_TURN_ITERATIONS), natural exhaustion emits the
            # pre-existing loop_exhausted / MAX_ITERATIONS_REACHED envelope (the
            # user-facing contract the web UI + tests rely on). Only when the cap
            # was TIGHTENED for a cheap/loop-prone tier (_step_cap <
            # MAX_TURN_ITERATIONS) is this a NEW, tighter runaway backstop, surfaced
            # as AGENT_STEP_LIMIT_REACHED. AGENT_LOOP_DETECTED stays reserved for
            # the watchdog (a genuine no-progress repeat), never natural exhaustion.
            if _agent_abort is None and _step_cap < MAX_TURN_ITERATIONS:
                _agent_abort = (
                    ABORT_STEP_CAP, abort_message(ABORT_STEP_CAP)
                )
            logger.warning(
                "gemini loop hit step cap=%d (full=%d) session=%s — "
                "emitting %s envelope",
                _step_cap,
                MAX_TURN_ITERATIONS,
                state.session_id,
                "agent-abort" if _agent_abort is not None else "loop_exhausted",
            )
            # Full-tier natural exhaustion: the historical loop_exhausted path.
            if _agent_abort is None:
                await _send_loop_exhausted(websocket, state.session_id)
                # The client waits for a stream-closing done=True to stop
                # spinning. A cap-hit turn ended mid tool-dispatch with no
                # trailing narration (``current_message_id is None``), so the
                # final-segment finalize below no-ops -- emit a standalone
                # terminator here with a fresh id (mirrors the abort path).
                if current_message_id is None:
                    await _session_safe_send(websocket, state.session_id,
                        _new_envelope(
                            "agent-message-chunk",
                            state.session_id,
                            AgentMessageChunkPayload(
                                message_id=new_ulid(), delta="", done=True
                            ),
                        )
                    )

        # A guard fired (tightened step cap, wall-clock, or loop watchdog).
        # Surface the honest typed abort envelope and a stream-closing terminal
        # frame, then fall through to the normal finalize/pipeline-complete path
        # so the turn TERMINATES cleanly and its busy/lock state releases. The
        # model CONTEXT is preserved (contents already hold the partial chain);
        # we just stop dispatching. NOT a loop continuation -- this only runs on
        # abort.
        if _agent_abort is not None:
            _abort_code, _abort_msg = _agent_abort
            await _send_agent_abort(
                websocket, state.session_id, _abort_code, _abort_msg
            )
            # A runaway loop exits mid tool-dispatch with no trailing narration,
            # so ``current_message_id is None`` and the segment finalize below
            # no-ops. The client still waits for a stream-closing done=True to
            # stop spinning, so emit a standalone terminator with a fresh id.
            if current_message_id is None:
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=new_ulid(), delta="", done=True
                        ),
                    )
                )

        # Terminal frame for the FINAL narration segment. Only fires if a
        # segment is actually open (text was emitted after the last tool
        # round); a turn with no trailing narration has current_message_id is
        # None (no phantom empty bubble). is_terminal=True lets it snapshot the
        # turn's layer/zoom accumulator.
        if current_message_id is not None:
            await _finalize_segment(
                websocket,
                state,
                current_message_id,
                _segment_buf,
                is_terminal=True,
                thinking_parts=_thinking_buf,
            )
            current_message_id = None
        else:
            # The turn exited with NO open segment on the wire. Two distinct shapes:
            #   (a) A narration segment WAS already streamed+finalized this turn
            #       (segments_done > 0) -- the client already got the closing
            #       summary; emit NOTHING here (re-streaming turn_narration would
            #       DOUBLE the text and duplicate the chat rows).
            #   (b) NO segment was ever streamed (segments_done == 0) yet the turn
            #       accumulated real narration text across iterations (e.g. the
            #       ONLY narration arrived in a round that ALSO carried the
            #       terminating tool call, so the boundary finalize cleared its
            #       buffer before it reached the wire as its own terminal segment).
            #
            # Recovery for (b): open ONE fresh terminal segment, replay the full
            # accumulated turn_narration as chunks, then finalize it terminal
            # (done=True wire frame + persisted role="agent" row that also
            # snapshots the layer/zoom accumulator). Honesty floor: replay EXACTLY
            # what Gemini accumulated -- never synthesize a success summary. Guarded
            # so an empty-narration turn emits NO bubble.
            _seg_done = 0
            _cur_task = asyncio.current_task()
            if _cur_task is not None:
                _seg_done = _TURN_SEGMENTS_PERSISTED_BY_TASK.get(_cur_task, 0)
            _closing = "".join(turn_narration).strip()
            if _seg_done == 0 and _closing:
                recovered_id = new_ulid()
                # Stream the recovered narration so the live client renders the
                # closing bubble (one message_id == one bubble). Each chunk
                # carries done=False; the terminal done=True comes from
                # ``_finalize_segment`` below.
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=recovered_id, delta=_closing, done=False
                        ),
                    )
                )
                # Persist + close via the SAME terminal-segment path so the
                # closing row carries the turn's layer/zoom accumulator exactly
                # like a normal text-terminal turn. ``_finalize_segment`` joins
                # its buffer arg, so feed the recovered text in as the buffer.
                await _finalize_segment(
                    websocket,
                    state,
                    recovered_id,
                    [_closing],
                    is_terminal=True,
                    thinking_parts=_thinking_buf,
                )
            elif _crisp_concluded and _seg_done == 0:
                # CRISP-END edge case: the turn delivered a composer artifact and
                # concluded via the post-deliverable idle safety, but emitted ZERO
                # narration anywhere -- so neither the segment-finalize nor the
                # recovery branch above sent a stream-closing frame. Emit a standalone
                # terminator with a fresh id (mirrors the loop_exhausted / abort paths).
                # Honesty floor: no synthesized summary, just the close-frame.
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=new_ulid(), delta="", done=True
                        ),
                    )
                )

        # Complete the outer pipeline snapshot (LLM generation phase).
        thinking_step = PipelineStep(
            step_id=step_id,
            name="llm_generation",
            tool_name="gemini_generate",
            state="complete",
        )
        state.current_pipeline_steps = [thinking_step]
        await _session_safe_send(websocket, state.session_id,
            _new_envelope(
                "pipeline-state",
                state.session_id,
                PipelineStatePayload(pipeline_id=pipeline_id, steps=[thinking_step]),
            )
        )
        # Append to the entry-captured list -- after a mid-stream
        # case switch this turn's text must not leak into the NEW Case's
        # LLM context (the carryover class, 74fc0d6).
        turn_history.append({"role": "user", "text": user_text})
        # Name an Untitled Case from its first prompt + refresh the left rail.
        # A3 moved the PRIMARY autoname to a pre-dispatch call; this tail is a
        # guarded no-op fallback that only fires when a mid-stream case switch
        # re-pinned active_case_id to a fresh Untitled case not yet seen by the
        # pre-dispatch call.
        if await _maybe_autoname_case(state, user_text):
            await _emit_case_list(websocket, state, force=True)

    except asyncio.CancelledError:
        # Invariant 8 -- distinct cancelled step state, not failed. A
        # partially-open narration segment's done=True is intentionally NOT
        # sent here (a cancelled stream has no clean terminal); current_turn_
        # narration still holds the partial text and the dispatch wrapper's
        # finally persists the un-finalized open-segment tail best-effort, so no
        # narration is lost.
        _turn_error_class = "cancelled"
        cancelled_step = PipelineStep(
            step_id=step_id,
            name="llm_generation",
            tool_name="gemini_generate",
            state="cancelled",
        )
        state.current_pipeline_steps = [cancelled_step]
        try:
            await websocket.send(
                _new_envelope(
                    "pipeline-state",
                    state.session_id,
                    PipelineStatePayload(pipeline_id=pipeline_id, steps=[cancelled_step]),
                )
            )
        except Exception:  # noqa: BLE001 -- socket may be down on cancel
            pass
        raise
    except ConnectionClosed as exc:
        # F2: the CLIENT transport died mid-turn. This is NOT a model failure
        # -- the LLM stream rides httpx, never websockets, so a
        # ConnectionClosed reaching this scope can only be a residual raw send
        # to the dead client socket. Every known per-turn send now routes
        # through _session_safe_send (never raises; sibling-socket fallback),
        # so this branch is a backstop: log once and end the turn quietly. NO
        # LLM_UNAVAILABLE error envelope and NO terminal-failure card -- a
        # client transport drop is not a model failure. The persisted
        # chat/tool rows plus the session-resume replay carry the turn's
        # results to the client when it reconnects.
        _turn_error_class = "client_disconnect"
        logger.warning(
            "client websocket closed mid-turn (transport drop, not a model "
            "failure) session=%s: %s",
            state.session_id,
            exc,
        )
    except ContextWindowExceededError as exc:
        # OPEN-14 REACTIVE CLIP GUARD: the local (Ollama/OpenAI-compat) model's
        # prompt was clipped by num_ctx even after one recompaction + retry.
        # Distinct typed envelope -- NOT the generic LLM_UNAVAILABLE bucket --
        # so the honesty floor tells the user exactly why the turn stopped (a
        # genuinely oversized Case, not a transient model outage). Mirrors the
        # terminal-failure-card persist so a reconnect / Case-reopen replay
        # shows the failed card rather than a phantom "still running" spinner.
        _turn_error_class = "context_window"
        logger.warning(
            "context-budget: turn aborted, context window exceeded session=%s "
            "num_ctx=%d",
            state.session_id,
            exc.num_ctx,
        )
        # Persist the terminal-failure card BEFORE attempting the error-envelope
        # send (not the reverse) -- persist never touches the socket or this
        # payload, so it cannot be starved by any failure in the send path.
        # Both steps are individually try/excepted with explicit logging.
        #
        # The fabrication backstop (item 4, context_budget) also applies on this
        # exception path: zero tool calls dispatched this whole turn, AND the
        # accumulated narration matches the claim regex, appends the same
        # honest caveat as the normal zero-tool-call terminal branch.
        _aborted_narration = "".join(turn_narration)
        _fabricated_claim = not _turn_ever_called_tool and looks_like_fabricated_action_claim(
            _aborted_narration
        )
        if _fabricated_claim:
            logger.warning(
                "context-budget: fabrication backstop fired on abort path "
                "session=%s (zero tool calls this turn)",
                state.session_id,
            )
        state.current_turn_context_abort_note = build_context_window_abort_note(
            fabricated_claim=_fabricated_claim
        )
        try:
            await _persist_terminal_failure_card(
                state,
                error_code="CONTEXT_WINDOW_EXCEEDED",
                message=str(exc),
                case_id=_turn_case_id(state),
            )
        except Exception:  # noqa: BLE001 -- persist is best-effort but must
            # never be allowed to skip the (equally best-effort) error send
            # below; _persist_terminal_failure_card already swallows +
            # `logger.exception`s internally, this is defense-in-depth only.
            logger.exception(
                "context-budget: terminal-failure card persist raised "
                "session=%s",
                state.session_id,
            )
        try:
            await _send_error(
                websocket,
                state.session_id,
                "CONTEXT_WINDOW_EXCEEDED",
                str(exc),
                retryable=False,
            )
        except Exception:  # noqa: BLE001 -- _send_error/_session_safe_send
            # _send_error/_session_safe_send already never raise on an ordinary
            # send failure; this is defense-in-depth logging only. NOTE: a genuine
            # asyncio.CancelledError here is NOT caught (it is a BaseException, not
            # an Exception) and is intentionally left to propagate -- cancellation
            # must never be swallowed. By the time we reach this send the
            # terminal-failure persist has ALREADY completed, so a cancelled/dead-
            # socket send can no longer suppress it.
            logger.exception(
                "context-budget: error-envelope send raised session=%s",
                state.session_id,
            )
    except UpstreamProviderError as exc:
        # UPSTREAM-PROVIDER DISCIPLINE (NATE hard rule: never internalize
        # upstream failure). The adapter already retried the transient provider
        # failure with backoff and exhausted its budget -- this turn ends with
        # an HONEST provider-unavailable narration (typed, provider NAMED,
        # verbatim detail), never a silent empty turn and never recorded as an
        # internal error (error_class="upstream_provider" on the per-turn
        # telemetry record). The wire error_code stays the contract-valid
        # LLM_UNAVAILABLE (retryable) -- the closed A.6 ErrorCode Literal is a
        # contracts surface this lane may not widen -- while the free-form
        # failure-card code carries the DISTINCT UPSTREAM_PROVIDER_UNAVAILABLE.
        _turn_error_class = "upstream_provider"
        logger.error(
            "upstream provider unavailable session=%s provider=%s attempts=%d "
            "verbatim=%s",
            state.session_id,
            exc.provider,
            exc.attempts,
            exc.detail,
        )
        _narration = (
            f"The upstream model provider ({exc.provider}) is currently "
            f"unavailable -- the request was retried {exc.attempts} time(s) "
            f"and the provider kept failing. Provider error: {exc.detail}. "
            "This is a temporary provider-side outage, not a problem with "
            "your request; please try again shortly or switch models."
        )
        # Honest closing narration IN CHAT (one bubble, streamed + terminal
        # done=True) and persisted as an agent row so a Case reopen replays
        # the same honest ending. Best-effort sends via _session_safe_send.
        _upstream_msg_id = current_message_id or new_ulid()
        await _session_safe_send(websocket, state.session_id,
            _new_envelope(
                "agent-message-chunk",
                state.session_id,
                AgentMessageChunkPayload(
                    message_id=_upstream_msg_id, delta=_narration, done=False
                ),
            )
        )
        await _session_safe_send(websocket, state.session_id,
            _new_envelope(
                "agent-message-chunk",
                state.session_id,
                AgentMessageChunkPayload(
                    message_id=_upstream_msg_id, delta="", done=True
                ),
            )
        )
        try:
            await _persist_chat_turn(
                state,
                role="agent",
                content=_narration,
                pipeline_id=state.current_turn_pipeline_id,
                layer_emissions=[],
                case_id=_turn_case_id(state),
            )
        except Exception:  # noqa: BLE001 -- persist is best-effort
            logger.exception(
                "upstream-provider narration persist failed session=%s",
                state.session_id,
            )
        try:
            await _persist_terminal_failure_card(
                state,
                error_code="UPSTREAM_PROVIDER_UNAVAILABLE",
                message=str(exc),
                case_id=_turn_case_id(state),
            )
        except Exception:  # noqa: BLE001 -- defense-in-depth logging only
            logger.exception(
                "upstream-provider failure-card persist raised session=%s",
                state.session_id,
            )
        await _send_error(
            websocket,
            state.session_id,
            "LLM_UNAVAILABLE",
            f"Upstream provider unavailable ({exc.provider}): {exc.detail}",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001 -- surface as A.6 LLM_UNAVAILABLE
        # PER-TURN TELEMETRY: a NON-transient provider rejection (auth / bad
        # request) classifies as ``provider_request`` (fail-fast, its own
        # class); anything else is honestly ``internal``. Upstream transients
        # that escaped the retry seam classify ``upstream_provider``.
        _turn_error_class = classify_provider_error_class(exc)
        logger.exception("model stream failed: %s", exc)
        await _send_error(
            websocket,
            state.session_id,
            "LLM_UNAVAILABLE",
            f"Model generation failed: {exc}",
            retryable=True,
        )
        # The error envelope above marks the in-memory pipeline failed on THIS
        # live socket, but a WS reconnect / Case-reopen replays from
        # chat_history, where nothing records this terminal failure -- any tool
        # card the user last saw spinning would replay as "running" forever.
        # Persist a role="tool" FAILED tool-card row (mirroring the existing
        # tool-card shape) so the session-resume replay renders the failed card.
        # Honesty floor: this writes a FAILURE -- never a success.
        #
        # EXCEPTION: a RuntimeError whose __cause__ is StopIteration is the
        # PEP-479 async-generator-exhaustion artifact -- the model stream
        # generator ran dry, NOT a genuine model failure (a finite mocked stream
        # shape only). Persisting a failed tool card here would inject a
        # phantom failure row into an otherwise-clean tool-terminal turn, so we
        # skip it.
        if not isinstance(exc.__cause__, StopIteration):
            await _persist_terminal_failure_card(
                state,
                error_code="LLM_UNAVAILABLE",
                message=f"Model generation failed: {exc}",
                case_id=_turn_case_id(state),
            )
    finally:
        # PER-TURN TELEMETRY: exactly ONE record per turn, every outcome
        # (clean / abort / cancel / provider failure). emit_turn_telemetry is
        # fire-and-forget + never raises, but the whole call is still wrapped
        # so a telemetry fault can never mask the turn's own outcome (including
        # a propagating CancelledError).
        try:
            _turn_wall_ms = (
                asyncio.get_running_loop().time() - started_at
            ) * 1000.0
            emit_turn_telemetry(
                turn_id=pipeline_id,
                session_id=state.session_id,
                case_id=_turn_case_id(state),
                model_id=_effective_model,
                provider=_provider,
                prompt_tokens=_turn_prompt_tokens,
                completion_tokens=_turn_completion_tokens,
                reasoning_tokens=_turn_reasoning_tokens,
                turn_wall_ms=_turn_wall_ms,
                tool_dispatch_count=_turn_tool_dispatch_count,
                error_class=_turn_error_class,
            )
        except Exception:  # noqa: BLE001 -- telemetry never breaks the turn
            logger.warning(
                "per-turn telemetry emit failed session=%s", state.session_id,
                exc_info=True,
            )


async def _handle_session_resume(
    websocket: ServerConnection,
    state: SessionState,
    *,
    client_case_id: str | None = None,
) -> None:
    """Reply with a fresh session-state. M1 in-memory only; Mongo replay
    lands when the session-records seam is wired.

    Routes through the emitter so the initial session-state is
    A.7-snapshot-shaped. Also emits a case-list so the client renders the
    left-rail Case list on initial connect; best-effort -- skipped if
    Persistence is unbound.

    ``client_case_id`` is the Case the CLIENT is currently in and is the
    AUTHORITY: when it differs from ``state.active_case_id`` we RE-BIND the
    server pointer to it BEFORE the layer replay, so a reconnect replays
    the Case the user is actually in, never a stale server pointer. A
    resume with NO ``case_id`` (older client) keeps current behavior
    untouched. We are correcting WHICH Case the replay targets, not
    removing replay -- a genuine fresh reconnect still replays the active
    Case's rendered layers.
    """
    _ensure_emitter(websocket, state)
    # Record THIS socket as a live connection of the session, then reap any
    # prior socket of the SAME session. The keeper (THIS websocket) is excluded
    # by identity so the active tab's own socket is never closed. Idempotent: a
    # keepalive resume re-registers (no-op) and reaps any newly-stale sibling.
    _register_session_connection(state.session_id, websocket)
    await _reap_prior_session_connections(state.session_id, keeper=websocket)
    # JOB C (active-case flap): a keepalive resume is any resume AFTER the
    # first one on THIS connection. Capture the keepalive verdict BEFORE
    # flipping the per-connection latch: a fresh SessionState is built per
    # connection, so the FIRST resume here is the real fresh-socket resume
    # and every later one is a keepalive ping.
    is_keepalive = state.did_first_resume
    state.did_first_resume = True
    # Warm the in-memory pointer from the persisted last_active_case_id
    # first (no-op if this session already has a live pointer this
    # process). After an EC2 auto-stop/restart the _SESSION_ACTIVE_CASE
    # cache is empty; without this a bare resume from an older client would
    # lose the Case. The client stamp below still overrides this seed on
    # any disagreement.
    await _reload_session_active_case(state)
    # Re-bind the server's active-Case pointer to the client's current Case
    # BEFORE the replay below resolves it -- the client is the authority;
    # the in-memory _SESSION_ACTIVE_CASE pointer is a cache that may be
    # stale or cold (EC2 restart). Only re-bind on a genuine change to a
    # non-None Case so an older client's bare resume (no stamp) leaves the
    # pointer alone. The active_case_id setter writes through
    # _set_session_active_case so EVERY connection observes the corrected
    # Case; also persist the pointer so it survives the next restart. A
    # change here invalidates this connection's case-context sync marker so
    # the next user-message re-syncs to the corrected Case.
    #
    # Gate the rebind on ``not is_keepalive`` -- the 25s keepalive ping must
    # NEVER rebind the shared _SESSION_ACTIVE_CASE pointer (with two sockets
    # per session each stamping its own Case, an ungated keepalive rebind
    # ping-pongs the pointer every 25s and each rebind drives an
    # authoritative layer replay that clobbers the displayed Case). The
    # pointer is rebound only on a genuine FIRST resume of a connection
    # here, and on explicit case-command(select) / user-message elsewhere --
    # the deliberate user-intent paths.
    if (
        not is_keepalive
        and client_case_id is not None
        and client_case_id != state.active_case_id
    ):
        logger.info(
            "session-resume re-binding active case session=%s server=%s client=%s",
            state.session_id,
            state.active_case_id,
            client_case_id,
        )
        state.active_case_id = client_case_id
        state.case_context_synced_to = _CASE_SYNC_NEVER
        await _persist_session_active_case(state, client_case_id)
    # Canonical reconnect entry: a freshly-opened socket sends
    # session-resume first. If a turn from a now-closed socket of this SAME
    # session is still running (a live SFINCS solve detached on disconnect),
    # rebind its emitter sink onto THIS socket so its remaining progress +
    # terminal frames land on the user's live connection.
    rebound = _rebind_live_turns(state.session_id, state.emitter)
    if rebound:
        logger.info(
            "session-resume rebound %d live turn(s) onto reconnect session=%s",
            rebound,
            state.session_id,
        )
    # Per-Case layer DURABILITY: a BARE reconnect (no live turn for this
    # session) must STILL re-render every layer already on the map -- job-
    # 0355 only rebinds LIVE in-flight turns, so a layer that completed
    # before the disconnect has no live turn. NATE hard requirement: a
    # rendered layer survives any WS reconnect without an explicit
    # case-open.
    #
    # Resolve the session's active Case and seed THIS reconnect's emitter
    # from the Case's persisted loaded_layers BEFORE emitting (the same
    # case-open / _sync_case_context seam), so the single
    # emit_session_state below carries the full A.7 replace-not-reconcile
    # snapshot the client already knows how to render.
    #
    # Dedup: when rebound > 0 a LIVE turn's emitter was just pointed at
    # THIS socket's sink and IS the writer for this session-state -- we
    # must NOT also seed + emit on the new connection's emitter (that would
    # put two emitters on the same sink and deliver duplicate frames), so
    # the bare-resume replay runs ONLY when nothing was rebound.
    #
    # Replay the active Case's layers ONCE per connection -- on the first
    # BARE (non-rebound) resume, never on the 25s keepalive ping (a
    # keepalive re-seed on every ping re-painted the active Case's layers
    # and un-hid a user-hidden layer). did_fresh_resume gates the replay to
    # the first bare resume; the flag flips ONLY when this connection's
    # emitter was actually seeded, so a rebound connection performs the
    # one-time seed+replay later once it stops being a live turn's writer.
    did_replay_now = False
    if rebound == 0 and not state.did_fresh_resume:
        await _replay_active_case_layers(state)
        state.did_fresh_resume = True
        did_replay_now = True
    await state.emitter.emit_session_state()
    # OPEN-8: force an unconditional emit only on a genuine first
    # (non-keepalive) resume of THIS connection. A later keepalive ping (or
    # a sibling socket independently resuming) goes through the
    # change-guard so an unchanged ~190-case list is not re-serialized +
    # re-sent every cycle.
    await _emit_case_list(websocket, state, force=not is_keepalive)
    # C2 (re-emit on resume): ONLY on the genuine fresh-socket resume
    # (first bare resume that just seeded + replayed this connection's
    # layers), never on a keepalive ping and never on a rebound (a rebound
    # live turn is still streaming and emits its OWN terminal frames). On a
    # real reconnect the card the user last saw spinning may have finished
    # while the socket was down; this bare whole-turn idle is the
    # belt-and-suspenders that force-completes any card the client still
    # believes is running.
    if did_replay_now:
        await _emit_turn_complete(websocket, state)


async def _replay_active_case_layers(state: SessionState) -> None:
    """Seed the reconnect emitter from the active Case's persisted layers.

    The bare-reconnect half of the per-Case layer DURABILITY requirement.
    Resolves the session's active Case and seeds this connection's emitter
    from the Case's persisted snapshot so the caller's single
    emit_session_state re-renders every already-rendered layer WITHOUT a
    case-open. Reuses the case-open / _sync_case_context rehydration seam.

    No-ops (never crashes) when there is no active Case or Persistence is
    unbound. Best-effort: a Persistence failure logs and leaves the emitter
    as-is so the resume still completes.
    """
    if state.emitter is None:  # pragma: no cover -- _ensure_emitter always binds
        return
    case_id = state.active_case_id
    if case_id is None:
        return
    p = get_persistence()
    if p is None:
        return
    try:
        session_state = await p.get_session_state(case_id)
        # JOB 2: restore the Case AOI anchor on a bare reconnect so a follow-up
        # turn after a WS blip reuses the original extent (no Case re-open).
        _cache_case_bbox_from_session_state(state, session_state)
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # Repopulate the inline-GeoJSON side-table so the replayed
        # session-state carries renderable vectors (the browser never fetches
        # object-store uris directly). Mirrors the case-open path.
        try:
            await state.emitter.reinline_vector_layers()
        except Exception:  # noqa: BLE001 -- re-inline is best-effort
            logger.warning(
                "session-resume vector re-inline failed session=%s case=%s",
                state.session_id,
                case_id,
            )
        # #147 reconnect-resync: seed the emitter's chat-history mirror from the
        # SAME persisted CaseSessionState already fetched above (do NOT
        # re-fetch) so a BARE reconnect re-renders the chat bubbles too, not
        # just the layers. Persisted CaseChatMessage list is serialized to the
        # wire dict shape SessionStatePayload.chat_history carries. Best-effort.
        try:
            state.emitter.seed_chat_history(
                [m.model_dump(mode="json") for m in session_state.chat_history]
            )
        except Exception:  # noqa: BLE001 -- chat seed is best-effort
            logger.warning(
                "session-resume chat-history seed failed session=%s case=%s",
                state.session_id,
                case_id,
            )
        # Seed the URI registry so handle-indirection resolves for layers
        # produced in a PRIOR session of this Case. REPLACE (not
        # additive-seed) -- same rationale as the case-switch call sites, so a
        # bare reconnect never leaves stale/evicted records lingering.
        await _seed_registry_for_case(
            state, case_id, session_state.loaded_layers
        )
        logger.info(
            "session-resume replayed active-case layers session=%s case=%s "
            "layers=%d",
            state.session_id,
            case_id,
            len(session_state.loaded_layers),
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the resume
        logger.exception(
            "session-resume layer replay failed session=%s case=%s",
            state.session_id,
            case_id,
        )


# --------------------------------------------------------------------------- #
# Connect-handshake (Appendix H.5 + H.3)
# --------------------------------------------------------------------------- #


def _connection_local_host(websocket: "ServerConnection | Any") -> str | None:
    """The server-side socket's local host for THIS connection.

    Used to derive the advertised sibling endpoints (remote-daemon access):
    a client dialing over the tailnet connected TO that address on the
    server side, so local_address reflects the exact reachable host to hand
    back. Defensive: websocket.local_address is a (host, port) tuple on a
    real ServerConnection but absent on the test fakes -- returns None (env
    overrides still apply).
    """
    addr = getattr(websocket, "local_address", None)
    if isinstance(addr, (tuple, list)) and addr:
        host = addr[0]
        return host if isinstance(host, str) and host else None
    return None


async def _reject_auth_handshake(
    websocket: ServerConnection,
    session_id: str,
    message: str,
) -> None:
    """Reject a connection at the handshake with a typed AUTH_FAILED close.

    Remote-daemon access (2026-07): the shared-token gate's rejection path.
    Emits the A.6 ``AUTH_FAILED`` error envelope, then closes the socket with
    the WebSocket policy-violation code (1008) -- the SAME close the client's
    ``is_auth_failure`` classifier recognizes, so the client stops its
    reconnect ladder instead of hammering a token-gated daemon forever. Never
    raises: a socket that is already down is fine.
    """
    await _send_error(websocket, session_id, "AUTH_FAILED", message)
    try:
        await websocket.close(code=1008, reason="AUTH_FAILED")
    except Exception:  # noqa: BLE001 -- socket may already be gone
        pass


async def _handle_auth_token(
    websocket: ServerConnection,
    state: SessionState,
    payload_dict: dict,
) -> None:
    """Process the client's ``auth-token`` envelope and emit ``auth-ack``.

    Per Appendix H.5:

    1. Validate the payload through ``AuthTokenEnvelope``.
    2. Call ``authenticate_token`` -> resolves to a ``User`` via Persistence
       (or provisions an anonymous fallback).
    3. Bind the resolved ``user_id`` + tier + anonymous-flag into the
       SessionState -- every subsequent envelope is scoped to this user.
    4. Emit ``auth-ack`` so the client knows its session identity.
    """
    tok: AuthTokenEnvelope | None
    try:
        tok = AuthTokenEnvelope.model_validate(payload_dict)
    except ValidationError as ve:
        await _send_error(
            websocket,
            state.session_id,
            "AUTH_TOKEN_INVALID",
            f"auth-token validation failed: {ve.errors()[0]['msg']}",
        )
        # Even on validation failure we run the anonymous fallback so the
        # connection is still usable (per H.3).
        tok = None

    # REMOTE-DAEMON ACCESS: optional shared-token gate. When
    # TRID3NT_ACCESS_TOKEN is set, the client's presented token MUST match
    # (constant-time) or the connection is rejected with a typed
    # AUTH_FAILED close (the same close the client classifies as an auth
    # failure and stops its reconnect ladder on). Unset (default) ->
    # verify_access_token returns True and behavior is byte-identical anon.
    presented = tok.token if tok is not None else None
    if not verify_access_token(presented):
        logger.info(
            "auth-token rejected session=%s (access token missing/invalid)",
            state.session_id,
        )
        await _reject_auth_handshake(
            websocket,
            state.session_id,
            "access token required: the presented token is missing or invalid",
        )
        return

    # cases-vanish fix (belt-and-suspenders): if this connection presents NO
    # usable anonymous hint but a sibling connection of the SAME session already
    # bound an anon identity this process, replay that recorded id as the hint so
    # both sockets converge on ONE anon user. Only fills a MISSING hint -- a
    # client-supplied hint always wins (it is the durable, cross-refresh id).
    tok = _apply_session_anon_hint(state.session_id, tok)

    result = await authenticate_token(tok, get_persistence())

    # cases-vanish fix: record the anon identity so a sibling/reconnecting
    # socket of the same session converges on it.
    if result.is_anonymous:
        _set_session_anon_id(state.session_id, result.user.user_id)

    _bind_auth_result(state, result)
    await _touch_session_record(state)  # D.6 heartbeat (job-0203 / M4)
    # REMOTE-DAEMON ACCESS (2026-07): advertise the sibling endpoints derived
    # from THIS connection's local address (so a tailnet client learns the
    # data + HTTP bases automatically) plus any env override.
    endpoints = derive_advertised_endpoints(_connection_local_host(websocket))
    ack = build_auth_ack(result, endpoints=endpoints)
    await websocket.send(_new_envelope("auth-ack", state.session_id, ack))
    logger.info(
        "auth-ack session=%s user_id=%s anonymous=%s tier=%s endpoints=%s",
        state.session_id,
        result.user.user_id,
        result.is_anonymous,
        result.tier,
        endpoints.model_dump(mode="json") if endpoints else None,
    )


def _bind_auth_result(state: SessionState, result: AuthResult) -> None:
    """Copy the resolved auth identity into the SessionState.

    Separate from ``_handle_auth_token`` so tests can drive the bind
    directly without parsing an envelope. Also propagates the resolved
    ``user_id`` into ``state.allowed_tool_set.user_id`` so
    ``get_dynamic_hot_set`` can filter telemetry per-user when
    ``TRID3NT_DYNAMIC_HOT_SET=1``.
    """
    state.authenticated_user_id = result.user.user_id
    state.is_anonymous = result.is_anonymous
    state.firebase_uid = result.firebase_uid
    state.tier = result.tier
    state.auth_handshake_complete = True
    # Propagate user_id so dynamic hot-set queries are per-user scoped.
    state.allowed_tool_set.user_id = result.user.user_id


async def _touch_session_record(
    state: SessionState, *, case_id: str | None = None
) -> None:
    """D.6 session-record heartbeat.

    Upserts the agent's own ``sessions`` document: ``last_active_at`` +
    ``expires_at`` advance (TTL driver per ``SESSIONS_TTL``), the active
    Case lands in ``project_ids``. Fired on auth bind, Case open/create,
    and every persisted chat turn -- none of these touches is a confirmable
    write (FR-AS-8 session-record carveout).

    Best-effort: a persistence hiccup is logged at WARNING and never
    reaches the caller.
    """
    p = get_persistence()
    if p is None:
        return
    active_case_id = case_id if case_id is not None else state.active_case_id
    try:
        await p.touch_session(
            state.session_id,
            case_id=active_case_id,
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.warning(
            "session-touch failed session=%s", state.session_id, exc_info=True
        )
    # #147 ephemeral-cases ACTIVITY HEARTBEAT (LOAD-BEARING): an
    # actively-used ANONYMOUS Case must NEVER be reaped.
    # upsert_case(ephemeral=True) stamps a numeric TTL at CREATE time only;
    # without sliding it forward on activity an anon Case would be swept one
    # TTL window after creation regardless of use. This helper fires on
    # auth bind, Case open/create, and every persisted chat turn -- exactly
    # the activity signal -- so slide the Case TTL here too. Gated STRICTLY
    # on is_anonymous: an authed Case carries no expires_at and stays
    # durable forever.
    if state.is_anonymous and active_case_id is not None:
        try:
            await p.touch_case(active_case_id)
        except Exception:  # noqa: BLE001 -- side effect, never bubble up
            logger.warning(
                "case-touch failed session=%s case=%s",
                state.session_id,
                active_case_id,
                exc_info=True,
            )


async def _persist_session_active_case(
    state: SessionState, case_id: str | None
) -> None:
    """Persist the session's active-Case pointer.

    Writes ``last_active_case_id`` onto the ``sessions`` document so the
    active pointer survives an EC2 auto-stop/restart that wipes the
    in-memory ``_SESSION_ACTIVE_CASE`` dict. The client-stamped ``case_id``
    stays the REAL authority; this is only the cold-start cache. Fired
    whenever the server re-binds the pointer to the client's Case, so a
    later restart's fresh SessionState reloads the right Case (see
    ``_reload_session_active_case``).

    Best-effort: a persistence hiccup is logged at WARNING and never
    reaches the caller's turn.
    """
    p = get_persistence()
    if p is None:
        return
    try:
        await p.set_session_active_case(state.session_id, case_id)
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.warning(
            "persist active-case pointer failed session=%s",
            state.session_id,
            exc_info=True,
        )


async def _reload_session_active_case(state: SessionState) -> None:
    """Reload the persisted active-Case pointer into the in-memory registry.

    When a fresh SessionState is built after an EC2 restart (or a
    brand-new process), the session-scoped ``_SESSION_ACTIVE_CASE`` dict is
    empty. This reloads the persisted ``last_active_case_id`` so the
    server's pointer is warm again BEFORE the first replay/turn. The
    client-stamped ``case_id`` still wins on any disagreement; this only
    seeds a sensible default for a bare resume (older client, no stamp).

    Idempotent + guarded: only seeds when the registry has NO entry for
    this session yet (a value already present is the live truth and is
    never overwritten). Best-effort: a missing record / persistence hiccup
    leaves the pointer None.
    """
    if state.session_id in _SESSION_ACTIVE_CASE:
        return
    p = get_persistence()
    if p is None:
        return
    try:
        persisted = await p.get_session_active_case(state.session_id)
    except Exception:  # noqa: BLE001 -- best-effort, never break resume
        logger.warning(
            "reload active-case pointer failed session=%s",
            state.session_id,
            exc_info=True,
        )
        return
    if persisted is not None and state.session_id not in _SESSION_ACTIVE_CASE:
        _set_session_active_case(state.session_id, persisted)
        logger.info(
            "reloaded persisted active case session=%s case=%s",
            state.session_id,
            persisted,
        )


async def _ensure_auth_handshake(
    websocket: ServerConnection,
    state: SessionState,
) -> bool:
    """Synchronous fallback: if the handshake hasn't run, run it as anonymous.

    Called when a non-``auth-token`` envelope arrives before the handshake
    has completed (the client either didn't send auth-token, or another
    envelope raced ahead). Mirrors the 5-second timeout path from H.3 --
    instead of waiting 5 seconds we trip the anonymous fallback inline so
    the user is bound before their first real interaction.

    Returns ``True`` when the connection may proceed (handshake already
    complete, or the anonymous fallback bound successfully), ``False`` when the
    shared-token gate rejected the connection (it was closed) so the caller
    must NOT dispatch the pending envelope.
    """
    if state.auth_handshake_complete:
        return True
    # REMOTE-DAEMON ACCESS (2026-07): a token-gated daemon must not accept a
    # connection that skipped the auth-token envelope entirely -- that would be
    # a trivial bypass of the token. This implicit path presents NO token, so
    # reject it with the same typed AUTH_FAILED close when a token is required.
    if not verify_access_token(None):
        logger.info(
            "implicit handshake rejected session=%s (access token required)",
            state.session_id,
        )
        await _reject_auth_handshake(
            websocket,
            state.session_id,
            "access token required: connect with a valid token",
        )
        return False
    # cases-vanish fix: this implicit-anonymous path never saw a client hint
    # (the connection skipped the auth-token envelope). If a sibling connection
    # of this session already bound an anon identity this process, reuse it so
    # both sockets converge on ONE anon user -- otherwise this path would mint a
    # fresh random ULID and fork the owner-scoped case-list.
    tok = _apply_session_anon_hint(state.session_id, None)
    result = await authenticate_token(tok, get_persistence())
    if result.is_anonymous:
        _set_session_anon_id(state.session_id, result.user.user_id)
    _bind_auth_result(state, result)
    await _touch_session_record(state)  # D.6 heartbeat (job-0203 / M4)
    endpoints = derive_advertised_endpoints(_connection_local_host(websocket))
    ack = build_auth_ack(result, endpoints=endpoints)
    try:
        await websocket.send(_new_envelope("auth-ack", state.session_id, ack))
    except Exception:  # noqa: BLE001 -- socket may be down
        pass
    logger.info(
        "auth-ack(implicit-anonymous) session=%s user_id=%s",
        state.session_id,
        result.user.user_id,
    )
    return True


# --------------------------------------------------------------------------- #
# Case lifecycle handlers (FR-MP-6)
# --------------------------------------------------------------------------- #

#: OPEN-8: the last-emitted case-list content digest PER SESSION (not
#: per connection -- SessionState is a fresh per-connection object, and
#: a session can carry more than one live socket). A session-resume
#: keepalive ping was re-serializing + re-sending the FULL case list
#: even when nothing had changed since the last emit. Cleared when the
#: session's last live connection disconnects so a later reconnect
#: always gets a fresh unconditional emit.
_SESSION_CASE_LIST_HASH: "dict[str, str]" = {}


def _case_list_digest(cases: "list[CaseSummary]") -> str:
    """Stable content digest of a case list, order-independent.

    Built from the fields a client actually renders/reacts to (id, title,
    status, timestamps) rather than a raw model dump, so field additions
    that don't change client-visible state don't force spurious re-emits.
    Sorted by ``case_id`` so the digest is independent of listing order.
    """
    parts = sorted(
        f"{c.case_id}|{c.title}|{c.status}|{c.created_at}|{c.updated_at}"
        for c in cases
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _clear_case_list_hash(session_id: str) -> None:
    """Drop the cached case-list digest for ``session_id`` (best-effort).

    Called once the session's last live connection disconnects so a fresh
    reconnect later always gets an unconditional first emit rather than
    inheriting a stale digest from a prior connection's cache.
    """
    _SESSION_CASE_LIST_HASH.pop(session_id, None)


async def _emit_case_list(
    websocket: ServerConnection, state: SessionState, *, force: bool = False
) -> None:
    """Emit the ``case-list`` envelope for the client's left rail.

    Best-effort: skips silently if Persistence is unbound; logs + skips on
    a listing failure (the case-list is a derivable view and must not break
    the chat path).

    The list is scoped by ``state.authenticated_user_id`` (Firebase UID, or
    the sticky-anonymous ULID in dev), matching the owner stamped onto
    Cases at creation -- a Case is visible only to its owner. Falls back to
    ``session_id`` only when the handshake hasn't bound a user yet.

    Change-guard: ``force=False`` (default) skips the send when the list is
    byte-for-byte the same (by content digest, see ``_case_list_digest``)
    as the last emit for this SESSION, collapsing repeat keepalive/
    duplicate-socket resumes into a no-op. Callers that just performed (or
    may have performed) a mutation pass ``force=True`` so the client is
    never left with a stale list.
    """
    p = get_persistence()
    if p is None:
        logger.debug("case-list: Persistence unbound; skipping emit")
        return
    user_id = state.authenticated_user_id or state.session_id
    try:
        cases = await p.list_cases_for_user(user_id)
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("case-list: list_cases_for_user failed")
        return
    digest = _case_list_digest(cases)
    if not force and _SESSION_CASE_LIST_HASH.get(state.session_id) == digest:
        logger.debug(
            "case-list unchanged session=%s user=%s count=%d — skipping emit",
            state.session_id,
            user_id,
            len(cases),
        )
        return
    _SESSION_CASE_LIST_HASH[state.session_id] = digest
    payload = CaseListEnvelopePayload(cases=cases)
    await websocket.send(_new_envelope("case-list", state.session_id, payload))
    logger.info(
        "case-list emitted session=%s user=%s count=%d",
        state.session_id,
        user_id,
        len(cases),
    )


def _rehydrate_case_history(
    state: SessionState,
    session_state: CaseSessionState,
    case_id: str,
) -> None:
    """Refill ``state.chat_history`` from a Case's PERSISTED messages.

    Called right after the ``state.chat_history = []`` reset in both
    ``_emit_case_open`` and ``_sync_case_context``. Converts the per-Case
    persisted ``CaseChatMessage`` list into the lightweight TEXT-turn dict
    shape ``build_contents_from_history`` consumes, appends a compact
    "layers already present" model turn, and bounds the replay to the last
    ``REHYDRATE_HISTORY_CAP`` rows so a long Case cannot blow the context
    window. Best-effort: any failure leaves the (empty) reset history
    intact.

    Guardrail: ``session_state`` belongs to exactly ONE ``case_id`` (the
    persisted store is keyed by Case), so this cannot reintroduce a
    cross-case leak.
    """
    try:
        # F20 / panel-fix: pass the Case AOI bbox so the layers-present note
        # carries the exact extent. It survives history capping, so a long
        # Case whose head turn (which named the place) was dropped can still
        # reuse the original AOI for follow-up fetch/clip instead of
        # re-geocoding / mis-scoping.
        case_bbox = getattr(getattr(session_state, "case", None), "bbox", None)
        history, dropped = rehydrate_history_from_case(
            session_state.chat_history,
            session_state.loaded_layers,
            case_bbox=case_bbox,
        )
        # REBIND, never extend the entry-captured list -- assigning a
        # fresh object keeps an in-flight turn's captured history untouched.
        state.chat_history = history
        if dropped:
            logger.info(
                "case-history-rehydrate session=%s case=%s dropped_head=%d "
                "kept=%d (cap=%d)",
                state.session_id,
                case_id,
                dropped,
                len(history),
                REHYDRATE_HISTORY_CAP,
            )
    except Exception:  # noqa: BLE001 -- rehydration is best-effort
        logger.exception(
            "case-history-rehydrate failed session=%s case=%s",
            state.session_id,
            case_id,
        )


async def _sync_case_context(
    websocket: ServerConnection, state: SessionState
) -> None:
    """Catch this CONNECTION's in-memory context up to the session's active
    Case.

    chat_history and the emitter's loaded_layers accumulator are
    per-connection state, so a case-command on a sibling connection (or a
    fresh reconnect) leaves them out of sync with the session's active
    Case. Called at the top of every user-message dispatch: on a Case
    change, replace (not reconcile) chat_history and reseed the emitter
    from the persisted Case, so add_loaded_layer dedup and
    _persist_case_loaded_layers writes operate on the full persisted
    truth set.

    Best-effort: a Persistence failure leaves the emitter seeded empty;
    the merge in _persist_case_loaded_layers prevents an unseeded
    accumulator from clobbering previously persisted layers.
    """
    current = state.active_case_id
    if state.case_context_synced_to == current:
        return
    state.case_context_synced_to = current
    # Replace-not-reconcile (applied cross-connection): this
    # connection's LLM context belongs to whatever Case it was last driving.
    # REBIND, never clear() -- an in-flight turn holds the old list
    # (captured at its stream entry) and must keep its own context intact.
    state.chat_history = []
    state.turn_count = 1  # count the in-flight turn that triggered the sync
    _ensure_emitter(websocket, state)
    if state.emitter is None:  # pragma: no cover -- _ensure_emitter always binds
        return
    if current is None:
        # JOB 2: no active Case -> no AOI anchor.
        state.case_bbox = None
        state.emitter.reset_loaded_layers([])
        # F32: no active Case -> no resolvable handles either (clears any
        # leftover registrations from whatever Case this connection last
        # drove).
        get_uri_registry(state.session_id).clear()
        return
    p = get_persistence()
    if p is None:
        state.emitter.reset_loaded_layers([])
        return
    try:
        session_state = await p.get_session_state(current)
        # JOB 2: cache the Case AOI so ``_turn_case_bbox`` has a durable
        # active-AOI anchor on this connection's turns (kills repeat-fetch /
        # re-geocode by feeding the reuse short-circuits + the per-turn note).
        _cache_case_bbox_from_session_state(state, session_state)
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # Repopulate the inline-GeoJSON side-table
        # so this connection's next session-state emission carries renderable
        # vectors (mirrors the case-open path; best-effort).
        try:
            await state.emitter.reinline_vector_layers()
        except Exception:  # noqa: BLE001
            logger.warning("case-context-sync vector re-inline failed")
        # Seed the URI registry from the persisted Case layers so
        # handle-indirection works for layers produced in PRIOR sessions of this
        # Case (the LLM history was just cleared; the registry is the only
        # place the layer_id -> uri association survives). REPLACE, not
        # additive-seed -- this IS a case-switch point; an additive seed would
        # leak the previous Case's handles/URIs into this Case's resolution.
        # ADR 0014: also restores the Case's persisted L<n> short-handle map.
        await _seed_registry_for_case(
            state, current, session_state.loaded_layers
        )
        # F17: rehydrate this connection's LLM context from the SAME persisted
        # per-Case store. state.chat_history = [] above is the cross-connection
        # clean-slate; refilling it from current's persisted messages (already
        # fetched; do NOT re-fetch) lets a sibling-connection / reconnect turn
        # see prior work and stop recomputing. Per-Case store => case-correct.
        _rehydrate_case_history(state, session_state, current)
        logger.info(
            "case-context-sync session=%s case=%s layers=%d rehydrated=%d",
            state.session_id,
            current,
            len(session_state.loaded_layers),
            len(state.chat_history),
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception(
            "case-context-sync failed session=%s case=%s",
            state.session_id,
            current,
        )
        state.emitter.reset_loaded_layers([])


async def _emit_case_open(
    websocket: ServerConnection,
    state: SessionState,
    case_id: str,
) -> None:
    """Emit a ``case-open`` envelope hydrating ``CaseSessionState`` from Mongo.

    Sets ``state.active_case_id`` BEFORE emitting so subsequent tool calls
    (and chat persistence) carry the Case context. If the Case is missing
    or Persistence is unbound, emits a ``case-open`` with ``session_state=None``
    so the client falls back to the empty state per
    ``CaseOpenEnvelopePayload`` semantics.
    """
    state.active_case_id = case_id
    # This connection runs the full case-open reset below, so its
    # context is (about to be) synced to ``case_id`` -- record it so the next
    # ``user-message`` on THIS connection skips the redundant re-sync.
    # Sibling connections of the same session keep their stale marker and
    # catch up via ``_sync_case_context`` on their next dispatch.
    state.case_context_synced_to = case_id
    # A Case switch must reset the per-connection LLM conversation, not just
    # the case state -- otherwise build_contents_from_history keeps feeding
    # old turns to Gemini and prompts misroute to the previous Case's
    # composer. Clean slate per Case (the replace-not-reconcile rule,
    # applied server-side); the visible chat replay comes from the
    # persisted Case history, not this list. REBIND, never clear() -- see
    # _sync_case_context.
    state.chat_history = []
    state.turn_count = 0
    await _touch_session_record(state, case_id=case_id)  # D.6 heartbeat (M4)
    # job-CASE-AUTHORITY: persist the active-Case pointer on explicit
    # case-open/select so the cold-start cache (``last_active_case_id``) is warm
    # for a reconnect after an EC2 restart -- even for an older client that
    # later resumes with no ``case_id`` stamp.
    await _persist_session_active_case(state, case_id)
    p = get_persistence()
    if p is None:
        logger.warning(
            "case-open session=%s case=%s: Persistence unbound; emitting empty",
            state.session_id,
            case_id,
        )
        payload = CaseOpenEnvelopePayload(session_state=None)
        await websocket.send(
            _new_envelope("case-open", state.session_id, payload)
        )
        return
    try:
        session_state = await p.get_session_state(case_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "case-open: get_session_state failed for case=%s", case_id
        )
        payload = CaseOpenEnvelopePayload(session_state=None)
        await websocket.send(
            _new_envelope("case-open", state.session_id, payload)
        )
        return
    # JOB 2: cache the opened Case's AOI so the very first turn in this Case
    # already has the active-AOI anchor (reuse short-circuits + per-turn note).
    _cache_case_bbox_from_session_state(state, session_state)
    payload = CaseOpenEnvelopePayload(session_state=session_state)
    await websocket.send(_new_envelope("case-open", state.session_id, payload))

    # Seed the emitter with the persisted loaded_layers so any subsequent
    # session-state emission carries them rather than overwriting with an
    # empty list -- the emitter's _loaded_layers is the truth set the next
    # add_loaded_layer dedups against; without seeding, a republish of an
    # existing layer would be treated as a fresh append.
    _ensure_emitter(websocket, state)
    # Opening THIS Case is the user returning to where a long solve was
    # launched. If a turn keyed to this Case is still running (detached on a
    # prior socket close), rebind its emitter sink onto the freshly-opened
    # socket so the in-flight solve's progress + terminal session-state
    # reach the live connection. Keyed to case_id so a concurrent solve in
    # another Case is untouched.
    rebound = _rebind_live_turns(
        state.session_id, state.emitter, only_turn_key=case_id
    )
    if rebound:
        logger.info(
            "case-open rebound %d live turn(s) onto reconnect session=%s case=%s",
            rebound,
            state.session_id,
            case_id,
        )
    if state.emitter is not None:
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # F32 (live-reported): seed the URI registry from the SAME persisted
        # layers the emitter/build_layers_present_note advertise. This is the
        # missing half of the explicit case-open path -- a fresh connection
        # (e.g. a QGIS dock reconnect) that opens an EXISTING Case via
        # case-command(select) reaches THIS function directly, never
        # _sync_case_context / _replay_active_case_layers. The registry is
        # session-scoped in-memory state, so on a genuinely fresh connection it
        # starts empty regardless of how many layers the Case has persisted.
        # Without this seed, the per-turn [Case state] note advertised handles
        # the registry could not resolve. REPLACE (not additive) so a Case
        # switch on this connection never leaks a prior Case's handles. ADR
        # 0014: also restores the Case's persisted L<n> short-handle map.
        await _seed_registry_for_case(
            state, case_id, session_state.loaded_layers
        )
        # Persisted VECTOR layers carry no inline GeoJSON (the side-table is
        # in-memory only), so the case-open payload above rehydrated entries the
        # browser cannot render. Re-inline from the artifact and emit one
        # follow-up session-state through the proven merge path so vectors
        # repaint.
        try:
            _reinlined = await state.emitter.reinline_vector_layers()
            if _reinlined:
                await state.emitter.emit_session_state()
        except Exception:  # noqa: BLE001 -- rehydration is best-effort
            logger.exception(
                "case-open vector re-inline failed case=%s", case_id
            )

    # F17: rehydrate the LLM conversation from THIS Case's persisted
    # messages so a follow-up turn in a reopened Case sees prior work and
    # stops recomputing. state.chat_history = [] above is the cross-case
    # clean-slate; refill from the PER-CASE persisted store (session_state
    # -- already loaded; do NOT re-fetch), which is inherently case-correct.
    _rehydrate_case_history(state, session_state, case_id)

    # cold-raster fix: a pure case-OPEN writes NO cold snapshot -- only the
    # 4 mutation triggers (create/rename/layer-publish/turn-close) do -- so
    # a freshly-opened or never-recently-mutated Case has a
    # stale-or-missing case-views/{case_id}.json until a mutating action,
    # and the box-asleep cold view cannot paint its rasters until the agent
    # wakes. Materialize the snapshot (+ thin manifest) HERE on open so the
    # cold face is warm immediately.
    #
    # FIRE-AND-FORGET (mirrors the turn-close site): create_task so the
    # Dynamo+S3 round-trips never sit on the open -> rehydrate path, and
    # both persisters swallow their own errors (best-effort), so the
    # detached task can never break the open. The snapshot sources inline
    # vectors from the emitter only when target_case == open_case, and this
    # Case is the one we just opened, so sourcing it is correct. A
    # reconnect-rebind that re-runs this open just lands a second identical
    # last-write-wins snapshot, which is harmless.
    _open_snap = asyncio.create_task(
        _persist_case_view_snapshot(state, case_id=case_id)
    )
    _BG_SNAPSHOT_TASKS.add(_open_snap)
    _open_snap.add_done_callback(_BG_SNAPSHOT_TASKS.discard)
    # #165 dual-write: refresh the thin manifest ALONGSIDE the snapshot so the
    # data-island cold index lists this Case + its layers on open too. Same
    # fire-and-forget; swallows its own errors.
    _open_manifest = asyncio.create_task(
        _persist_case_manifest(state, case_id=case_id)
    )
    _BG_SNAPSHOT_TASKS.add(_open_manifest)
    _open_manifest.add_done_callback(_BG_SNAPSHOT_TASKS.discard)

    logger.info(
        "case-open session=%s case=%s chat=%d layers=%d rehydrated=%d",
        state.session_id,
        case_id,
        len(session_state.chat_history),
        len(session_state.loaded_layers),
        len(state.chat_history),
    )


async def _handle_case_command(
    websocket: ServerConnection,
    state: SessionState,
    cmd: CaseCommandEnvelopePayload,
) -> None:
    """Dispatch one ``case-command`` (FR-MP-6 Case lifecycle).

    Commands:

    - ``create`` -- generate a new ``CaseSummary``, persist via
      ``Persistence.upsert_case``, set as active, emit ``case-open`` with
      the fresh (empty) session state, then refresh ``case-list``.
    - ``select`` -- load the persisted ``CaseSessionState`` and emit
      ``case-open`` with the full rehydration (chat history, loaded
      layers, pipeline history -- per FR-MP-6 chat-replay default).
    - ``rename`` -- update ``CaseSummary.title``, persist, emit
      ``case-list`` updated.
    - ``archive`` -- soft-archive via ``Persistence.archive_case``, emit
      ``case-list`` updated.
    - ``delete`` -- soft-delete via ``Persistence.delete_case``, emit
      ``case-list`` updated. Memory rule: the web UI confirms with the
      user BEFORE firing this command; the server does not double-confirm.

    Errors surface as ``error`` envelopes with ``error_code=INTERNAL_ERROR``
    (the case-lifecycle commands are NOT a confirmation trigger per
    FR-AS-8; only solver runs and non-session-collection Mongo writes are).
    """
    p = get_persistence()
    if p is None:
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            "case-command requires Persistence; the agent service was started "
            "without TRID3NT_MONGO_MCP_STDIO=1 and cannot satisfy FR-MP-6.",
        )
        return

    command = cmd.command

    if command == "create":
        # Generate a fresh ULID and persist. ``args.title`` is an optional hint.
        new_case_id = new_ulid()
        title = (cmd.args or {}).get("title") or "Untitled Case"
        if not isinstance(title, str) or not title.strip():
            title = "Untitled Case"
        # AOI-first: an optional ``args.bbox`` lets the user pin the AOI
        # extent BEFORE the first prompt (draw-on-map / numeric coords).
        # Coerced via the shared validator so a None / wrong-length /
        # non-finite value is dropped silently rather than crashing. When
        # present it persists on ``CaseSummary.bbox`` and seeds
        # ``state.case_bbox`` so the FIRST turn's ``_turn_case_bbox``
        # returns the user's extent and the LLM is told to REUSE it (no
        # re-geocode).
        create_bbox = _coerce_bbox4((cmd.args or {}).get("bbox"))
        now = now_utc()
        case = CaseSummary(
            case_id=new_case_id,
            title=title.strip(),
            created_at=now,
            updated_at=now,
            status="active",
            bbox=list(create_bbox) if create_bbox is not None else None,
        )
        try:
            # Stamp the creator as owner so the Case is visible to them via
            # ``list_cases_for_user``. ``authenticated_user_id`` is set by
            # the auth handshake (real Firebase UID or the sticky-anonymous
            # ULID in dev); None only on the M1 unbound-Persistence path.
            #
            # An ANONYMOUS (pre-Auth) session's Case is ephemeral -> a
            # numeric TTL ``expires_at`` is stamped so DynamoDB reaps
            # abandoned scratch Cases. An authed session (``is_anonymous``
            # False) passes ``ephemeral=False`` -> no ``expires_at`` ->
            # durable forever.
            await p.upsert_case(
                case,
                owner_user_id=state.authenticated_user_id,
                ephemeral=state.is_anonymous,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(create) upsert failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case create failed: {exc}",
            )
            return
        state.active_case_id = new_case_id
        # A fresh Case must NOT inherit the previous Case's AOI anchor.
        # Reset the in-session bbox to None BEFORE the conditional seed
        # (mirrors the select/deselect handlers) so a bbox-less create
        # starts with no anchor -> ``_turn_case_bbox`` re-geocodes from the
        # place name in the first prompt instead of reusing the prior
        # Case's extent.
        state.case_bbox = None
        # #170 AOI-first: seed the in-session AOI anchor so the FIRST turn's
        # _turn_case_bbox returns the user's pre-set extent (mirrors
        # _pin_case_aoi_from_solve). Absent/invalid bbox => leave as-is (None).
        if create_bbox is not None:
            state.case_bbox = list(create_bbox)
        # See _emit_case_open -- this connection is now synced.
        state.case_context_synced_to = new_case_id
        # Fresh Case = fresh LLM context (see _emit_case_open note).
        # REBIND, never clear() -- see _sync_case_context.
        state.chat_history = []
        state.turn_count = 0
        await _touch_session_record(state, case_id=new_case_id)  # D.6 (M4)
        # Emit case-open with the empty session state for the fresh Case.
        payload = CaseOpenEnvelopePayload(
            session_state=await p.get_session_state(new_case_id)
        )
        await websocket.send(
            _new_envelope("case-open", state.session_id, payload)
        )
        # A fresh Case starts with NO loaded layers; flush
        # the emitter's per-connection accumulator so a subsequent tool call
        # in this Case doesn't accidentally inherit layers from whatever Case
        # the user just left (replace-not-reconcile applied server-side).
        _ensure_emitter(websocket, state)
        if state.emitter is not None:
            state.emitter.reset_loaded_layers([])
        # F32: a fresh Case starts with no resolvable handles either -- clear
        # any leftover registrations from whatever Case this connection last
        # drove (mirrors the emitter flush immediately above).
        get_uri_registry(state.session_id).clear()
        # Lane A1: materialize the (empty) view snapshot for the fresh Case so a
        # view-without-agent link resolves immediately after create -- before any
        # turn lands. Emitter was just flushed, so no inline vectors to merge.
        await _persist_case_view_snapshot(state, case_id=new_case_id)
        # #165 dual-write: write the thin manifest ALONGSIDE the snapshot so the
        # data-island cold path lists the fresh Case immediately. Best-effort --
        # a manifest failure never breaks the snapshot path (own try/except).
        await _persist_case_manifest(state, case_id=new_case_id)
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command create session=%s case=%s title=%r",
            state.session_id,
            new_case_id,
            title,
        )
        return

    if command == "select":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(select) requires case_id",
            )
            return
        await _emit_case_open(websocket, state, cmd.case_id)
        return

    if command == "deselect":
        # The client navigated OUT of the active Case to the Cases root.
        # Without this command the session-scoped active Case silently kept
        # pointing at the last-opened Case: prompts sent from the root view
        # skipped auto-create and dispatched INTO the stale Case, and
        # re-selecting that same Case looked like a no-op. Clears the
        # binding + this connection's LLM context so the next root prompt
        # auto-creates a fresh Case. Does NOT touch any in-flight turn -- its
        # persistence follows the turn pin, not this binding.
        prev = state.active_case_id
        state.active_case_id = None
        state.case_context_synced_to = None
        # JOB 2: clear the cached Case AOI so a root prompt (which auto-creates
        # a FRESH Case) does not reuse the just-exited Case's extent.
        state.case_bbox = None
        # REBIND, never clear() -- see _sync_case_context.
        state.chat_history = []
        state.turn_count = 0
        if state.emitter is not None:
            state.emitter.reset_loaded_layers([])
        # F32: no active Case -> no resolvable handles from the just-exited
        # Case either (mirrors _sync_case_context's current-is-None branch).
        get_uri_registry(state.session_id).clear()
        # job-CASE-AUTHORITY: clear the persisted pointer too, so a reconnect
        # after restart does NOT re-seed the just-exited Case.
        await _persist_session_active_case(state, None)
        logger.info(
            "case-command deselect session=%s prev_case=%s",
            state.session_id,
            prev,
        )
        return

    if command == "rename":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(rename) requires case_id",
            )
            return
        new_title = (cmd.args or {}).get("title")
        if not isinstance(new_title, str) or not new_title.strip():
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(rename) requires args.title (non-empty string)",
            )
            return
        existing = await p.get_case(cmd.case_id)
        if existing is None:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case-command(rename): case {cmd.case_id!r} not found",
            )
            return
        updated = existing.model_copy(
            update={"title": new_title.strip(), "updated_at": now_utc()}
        )
        try:
            await p.upsert_case(updated)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(rename) upsert failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case rename failed: {exc}",
            )
            return
        # Lane A1: the title is part of the materialized view (``case.title``),
        # so re-snapshot the renamed Case to S3 so a cold view shows the new
        # name. Inline vectors merge only if this is the open Case (guarded in
        # _persist_case_view_snapshot); else a correct URI-only snapshot lands.
        await _persist_case_view_snapshot(state, case_id=cmd.case_id)
        # #165 dual-write: refresh the thin manifest too (``title`` is a manifest
        # field). Best-effort; never breaks the snapshot path.
        await _persist_case_manifest(state, case_id=cmd.case_id)
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command rename session=%s case=%s title=%r",
            state.session_id,
            cmd.case_id,
            new_title,
        )
        return

    if command == "set-bbox":
        # Persistent per-case AOI: the plugin's draw/edit tool sends the
        # user's rectangle here so ``CaseSummary.bbox`` is durably the
        # user's chosen extent -- not None until a tool happens to pin it.
        # The agent already injects ``state.case_bbox`` into EVERY turn
        # (``_turn_case_bbox`` -> ``build_layers_present_note``) and snaps
        # fetch bbox params to it, so a set case bbox is exactly what stops
        # the model re-deriving/geocoding the area every turn. Clones the
        # rename branch: write the field, re-snapshot the view + thin
        # manifest, re-emit the case-list; ALSO update ``state.case_bbox``
        # when this is the OPEN case so the very next turn's in-prompt AOI
        # line is correct with no reopen.
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(set-bbox) requires case_id",
            )
            return
        # The "Clear AOI" control sends set-bbox with an EXPLICIT null/empty
        # bbox to RESET the case AOI. An explicitly-present-but-empty
        # ``bbox`` (None or []) = CLEAR (``CaseSummary.bbox`` -> None,
        # ``state.case_bbox`` -> None); a MISSING bbox key or a
        # non-empty-but-malformed bbox stays the honest error below. This
        # lets the plugin's Clear-AOI actually stop the agent anchoring on
        # the old extent every turn.
        raw_args = cmd.args or {}
        has_bbox_key = "bbox" in raw_args
        raw_bbox = raw_args.get("bbox")
        clear = has_bbox_key and (raw_bbox is None or raw_bbox == [])
        bbox = None if clear else _coerce_bbox4(raw_bbox)
        if not clear and bbox is None:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(set-bbox) requires args.bbox = "
                "[min_lon, min_lat, max_lon, max_lat] (or an empty bbox to clear)",
            )
            return
        existing = await p.get_case(cmd.case_id)
        if existing is None:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case-command(set-bbox): case {cmd.case_id!r} not found",
            )
            return
        new_bbox = None if clear else list(bbox)
        updated = existing.model_copy(
            update={"bbox": new_bbox, "updated_at": now_utc()}
        )
        try:
            await p.upsert_case(updated)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(set-bbox) upsert failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case set-bbox failed: {exc}",
            )
            return
        # Open case: refresh the durable in-session pin so the next turn's
        # AOI line + fetch-bbox snapping use the new extent immediately (a
        # CLEAR nulls it so the model re-derives the area from the prompt).
        if cmd.case_id == state.active_case_id:
            state.case_bbox = new_bbox
        await _persist_case_view_snapshot(state, case_id=cmd.case_id)
        await _persist_case_manifest(state, case_id=cmd.case_id)
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command set-bbox session=%s case=%s bbox=%s",
            state.session_id,
            cmd.case_id,
            new_bbox,
        )
        return

    if command == "archive":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(archive) requires case_id",
            )
            return
        try:
            await p.archive_case(cmd.case_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(archive) failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case archive failed: {exc}",
            )
            return
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command archive session=%s case=%s",
            state.session_id,
            cmd.case_id,
        )
        return

    if command == "delete":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(delete) requires case_id",
            )
            return
        try:
            await p.delete_case(cmd.case_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(delete) failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case delete failed: {exc}",
            )
            return
        # If the deleted Case was the active one, clear the context -- any
        # subsequent publish will fall through to the single-tenant default
        # rather than mutate a soft-deleted ``.qgs``.
        if state.active_case_id == cmd.case_id:
            state.active_case_id = None
            # Preserve pre-existing behavior on THIS connection (no
            # chat clear on delete); siblings re-sync on their next dispatch.
            state.case_context_synced_to = None
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command delete session=%s case=%s",
            state.session_id,
            cmd.case_id,
        )
        return

    # Closed enum guard -- pydantic should have rejected before we got here.
    await _send_error(
        websocket,
        state.session_id,
        "INTERNAL_ERROR",
        f"unknown case-command: {command!r}",
    )


#: Cases already auto-named this process (avoid a get_case read
#: on every user turn -- only the first turn per Case checks the title).
_AUTONAMED_CASES: set[str] = set()

_TITLE_STOPWORDS = frozenset(
    "a an the and or of for with to in on at by from using use run model "
    "show me my please can you what how is are this that".split()
)


def _derive_case_title(prompt: str) -> str | None:
    """Heuristic 3-6 word Case title from the first user prompt.

    Significant tokens, title-cased, capped at ~48 chars. Returns None for
    degenerate prompts.
    """
    words = [
        w.strip(".,!?:;()[]\"'")
        for w in prompt.split()
    ]
    keep = [
        w for w in words if w and w.lower() not in _TITLE_STOPWORDS
    ][:6]
    if len(keep) < 2:
        return None
    title = " ".join(w if w[:1].isupper() else w.capitalize() for w in keep)
    return title[:48].rstrip() or None


async def _maybe_autoname_case(state: SessionState, prompt: str) -> bool:
    """Name an 'Untitled Case' from its first user prompt.

    Accumulated untitled Cases are otherwise indistinguishable in the left
    rail. Best-effort, once per Case per process; never raises.
    """
    case_id = state.active_case_id
    if not case_id or case_id in _AUTONAMED_CASES:
        return False
    p = get_persistence()
    if p is None:
        # Persistence unbound is NOT a permanent state -- do NOT mark the case
        # "named" (a later turn, once bound, can still name it from its first
        # prompt). A3 (2026-07-20): the guard used to be set unconditionally up
        # front, so ANY early miss (transient error / fresh-case read race)
        # burned the one-and-only naming attempt for that case forever.
        return False
    try:
        case = await p.get_case(case_id)
        if case is None:
            # Fresh case not visible in Persistence yet (create-then-read race)
            # -> TRANSIENT: leave unmarked so the next turn retries the name.
            return False
        if case.title != "Untitled Case":
            _AUTONAMED_CASES.add(case_id)  # already named -> stop checking
            return False
        title = _derive_case_title(prompt)
        if not title:
            # First prompt is degenerate/unnameable -> DEFINITIVE (the first
            # message defines the name); mark so later turns do not re-read.
            _AUTONAMED_CASES.add(case_id)
            return False
        await p.upsert_case(case.model_copy(update={"title": title}))
        _AUTONAMED_CASES.add(case_id)  # mark ONLY after the name actually landed
        logger.info("case auto-named case=%s title=%r", case_id, title)
        return True
    except Exception:  # noqa: BLE001 -- naming is a nicety; TRANSIENT -> unmarked,
        # so a persistence hiccup does not permanently forfeit the name.
        logger.debug("case auto-name failed case=%s", case_id, exc_info=True)
    return False


async def _auto_create_case_from_root(
    websocket: ServerConnection,
    state: SessionState,
    prompt: str,
) -> str | None:
    """Create + activate a Case for a chat prompt arriving with NO active Case.

    When a non-directive ``user-message`` arrives and the session has no
    active Case, mint one server-side BEFORE the turn dispatches so
    ``_persist_chat_turn`` + ``_persist_case_loaded_layers`` +
    ``ensure_case_qgs`` + the ``publish_layer`` case_id injection all land in
    it. The Case is named from the prompt via ``_derive_case_title``
    ("Untitled Case" fallback for degenerate prompts).

    Deliberately NOT the ``case-command(create)`` reset path: the in-flight
    message IS the Case's first turn, so the per-connection LLM context
    (``chat_history``) and the FR-FR-3 ``turn_count`` are left untouched.

    Returns the new ``case_id``, or ``None`` when Persistence is unbound or
    the upsert fails -- the M1 stateless path keeps working either way.
    """
    p = get_persistence()
    if p is None:
        return None
    title = _derive_case_title(prompt) or "Untitled Case"
    now = now_utc()
    case = CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=now,
        updated_at=now,
        status="active",
    )
    try:
        # Stamp the creator as owner so the auto-created Case is visible to
        # them via ``list_cases_for_user``.
        #
        # An anonymous root prompt mints an ephemeral Case (numeric TTL
        # ``expires_at`` so abandoned scratch work is reaped); an authed
        # session passes ``ephemeral=False`` -> durable forever.
        await p.upsert_case(
            case,
            owner_user_id=state.authenticated_user_id,
            ephemeral=state.is_anonymous,
        )
    except Exception:  # noqa: BLE001 -- fall back to the stateless path
        logger.exception(
            "auto-create-case upsert failed session=%s", state.session_id
        )
        return None
    state.active_case_id = case.case_id
    # This connection's in-memory context IS the new Case's context (the
    # triggering message is its first turn) -- mark synced so the next
    # dispatch skips the _sync_case_context reset.
    state.case_context_synced_to = case.case_id
    # The creating prompt already named the Case -- skip the
    # first-turn rename probe (it would be a wasted get_case round-trip).
    _AUTONAMED_CASES.add(case.case_id)
    await _touch_session_record(state, case_id=case.case_id)  # D.6 heartbeat
    # Fresh Case starts with zero layers -- flush the per-connection
    # accumulator (replace-not-reconcile server-side; mirrors
    # ``case-command(create)``).
    _ensure_emitter(websocket, state)
    if state.emitter is not None:
        state.emitter.reset_loaded_layers([])
    logger.info(
        "auto-created case from root session=%s case=%s title=%r",
        state.session_id,
        case.case_id,
        title,
    )
    return case.case_id


async def _emit_auto_case_open(
    websocket: ServerConnection,
    state: SessionState,
    case_id: str,
) -> None:
    """Emit ``case-open`` + ``case-list`` for an auto-created Case.

    Distinct from ``_emit_case_open``: NO context reset (no ``chat_history``
    clear, no ``turn_count`` reset, no emitter re-seed) -- the in-flight user
    message IS the first turn of this Case and
    ``_auto_create_case_from_root`` already established the connection
    context. Must be called AFTER the user turn is persisted so the
    rehydration payload carries it: Chat.tsx's case-open handler is
    replace-not-reconcile (it flushes the local message buffer and
    re-renders from ``session_state.chat_history``), so emitting before the
    persist would blank the just-typed message bubble. The client's ws.ts
    hub fans ``case-open`` out to App.tsx's socket (SESSION_SCOPED_TYPES),
    where ``useCases.onCaseOpen`` sets ``activeCaseId`` and the left rail
    flips from the Cases root into the Case view.

    A skipped (or ``session_state=None``) case-open on a rehydration
    failure would leave the client's ``activeCaseId`` unchanged -- stuck on
    the Cases root while the turn dispatches with the new case bound, so
    cards flow stamped with a ``case_id`` the client never opened and
    nothing renders until a reload. The rehydration-failure branch instead
    emits a MINIMAL non-null case-open whose ``session_state.case`` is the
    just-upserted ``CaseSummary`` (re-fetched, or a bare
    ``CaseSummary(case_id=...)`` if even that read fails).
    ``CaseSessionState`` only requires ``case`` (other fields default
    empty), so this guarantees the client flips out of the Cases root even
    when the richer rehydration momentarily fails.
    """
    p = get_persistence()
    if p is not None:
        try:
            payload = CaseOpenEnvelopePayload(
                session_state=await p.get_session_state(case_id)
            )
            await websocket.send(
                _new_envelope("case-open", state.session_id, payload)
            )
        except Exception:  # noqa: BLE001 -- emission is best-effort
            logger.exception(
                "auto-case-open emission failed session=%s case=%s",
                state.session_id,
                case_id,
            )
            # NATE 2026-06-26: fall back to a minimal non-null case-open so the
            # client still leaves the Cases root (never a null session_state).
            try:
                case = await p.get_case(case_id)
            except Exception:  # noqa: BLE001 -- re-fetch is best-effort
                case = None
            if case is None:
                # Last-resort minimal summary so session_state.case is non-null.
                now = now_utc()
                case = CaseSummary(
                    case_id=case_id,
                    title="Untitled Case",
                    created_at=now,
                    updated_at=now,
                    status="active",
                )
            try:
                fallback = CaseOpenEnvelopePayload(
                    session_state=CaseSessionState(case=case)
                )
                await websocket.send(
                    _new_envelope("case-open", state.session_id, fallback)
                )
            except Exception:  # noqa: BLE001 -- fallback emit is best-effort
                logger.exception(
                    "auto-case-open minimal fallback failed session=%s case=%s",
                    state.session_id,
                    case_id,
                )
    await _emit_case_list(websocket, state, force=True)


async def _prepare_user_turn(
    websocket: ServerConnection,
    state: SessionState,
    text: str,
    *,
    client_case_id: str | None = None,
) -> tuple[str, dict] | None:
    """Pre-dispatch sequence for one ``user-message``.

    Runs, in order, BEFORE the turn task is created (so the dispatched turn --
    Gemini stream or ``/invoke`` directive -- observes the final Case
    context):

    0. Re-bind the server's active-Case pointer to the client's stamped
       ``client_case_id`` (the Case the user is actually in) when it differs
       from the stale server pointer -- BEFORE the sync, the auto-create
       check, and the turn pin. So e.g. a 'resize bbox' turn runs in the
       client's current Case, never a Case the server pointer drifted to
       (mid-reconnect select dropped / restart wiped the cache). A message
       with NO ``case_id`` (older client) keeps the prior behavior.
    1. ``_sync_case_context`` -- catch this connection up to the (now
       corrected) session active Case.
    2. Auto-create: a non-directive prompt with NO active Case mints +
       activates a prompt-named Case (see ``_auto_create_case_from_root``).
       ``/invoke`` debug directives stay on the stateless path.
    3. ``_persist_chat_turn`` -- the user turn lands in the (possibly brand
       new) active Case. Best-effort; no Case / no Persistence = no-op.
    4. For an auto-created Case: emit ``case-open`` + ``case-list`` so the
       client switches from the Cases root into the Case view (after the
       persist -- see ``_emit_auto_case_open``).

    Returns the parsed ``/invoke`` directive (``(tool_name, params)``) or
    ``None`` for the Gemini path -- the caller branches on it.
    """
    # The client's stamped Case is the authority for this turn: re-bind the
    # session-scoped pointer to it before any sync/auto-create/pin reads
    # ``active_case_id``, so the whole turn (LLM context sync, AOI bbox,
    # every persistence write) follows the Case the user is actually
    # viewing -- not a server pointer that drifted while the socket was
    # reconnecting. Invalidate this connection's sync marker so
    # ``_sync_case_context`` below reloads the corrected Case's LLM history +
    # layer accumulator, and persist the pointer so it survives a restart.
    if client_case_id is not None and client_case_id != state.active_case_id:
        logger.info(
            "user-message re-binding active case session=%s server=%s client=%s",
            state.session_id,
            state.active_case_id,
            client_case_id,
        )
        state.active_case_id = client_case_id
        state.case_context_synced_to = _CASE_SYNC_NEVER
        await _persist_session_active_case(state, client_case_id)
    await _sync_case_context(websocket, state)
    directive = _parse_invoke_directive(text)
    auto_case_id: str | None = None
    if directive is None and state.active_case_id is None:
        auto_case_id = await _auto_create_case_from_root(
            websocket, state, text
        )
    # Pin the turn's Case binding NOW -- after the auto-create
    # hand-off, before the first write. Everything this turn persists
    # (user row, tool cards, narration, layers, charts, .qgs routing)
    # follows this pin; a mid-stream case switch must not re-aim it.
    state.current_turn_case_id = state.active_case_id
    await _persist_chat_turn(state, role="user", content=text)
    if auto_case_id is not None:
        await _emit_auto_case_open(websocket, state, auto_case_id)
    return directive


def _turn_case_id(state: SessionState) -> str | None:
    """The Case the current turn is bound to.

    Prefers the pin set by ``_prepare_user_turn`` at dispatch time; falls
    back to the live ``active_case_id`` for callers outside a prepared turn
    (direct tool invocations in tests, legacy paths). Without the pin, every
    persistence site reading ``active_case_id`` at WRITE time lets a
    ``case-command(select)`` arriving mid-stream re-aim in-flight writes at
    the newly selected Case.
    """
    return state.current_turn_case_id or state.active_case_id


def _turn_case_bbox(state: SessionState) -> Any:
    """The current turn's Case AOI bbox, or None.

    Used by the expensive-simulation reuse guard AND the fetch reuse guard as
    the AOI anchor when a request / persistence-seeded layer has no recorded
    bbox: a bbox-keyed re-run (or a bare follow-up fetch) in a single-result
    Case whose request bbox equals the Case AOI is a clear match.

    Reads ``state.case_bbox`` -- the durable cache of the active Case's
    persisted ``CaseSummary.bbox`` (set on case select / sync).
    """
    case_id = _turn_case_id(state)
    if not case_id:
        return None
    return state.case_bbox


def _cache_case_bbox_from_session_state(
    state: SessionState, session_state: Any
) -> None:
    """Cache the active Case's AOI bbox onto ``state.case_bbox``.

    Reads ``session_state.case.bbox`` -- the persisted ``CaseSummary.bbox``
    that the layers-present note already consumes -- and stores it so
    ``_turn_case_bbox`` has a durable active-AOI anchor on every live turn
    (the reuse short-circuits + the per-turn [Case state] note both read
    it). Pydantic BBox models serialize to a plain list; coerced to a list
    so the value is a cheap, JSON-shaped ``[lon_min, lat_min, lon_max,
    lat_max]`` (or ``None``). Best-effort: a missing / malformed case leaves
    the cache untouched-to-None.
    """
    try:
        case = getattr(session_state, "case", None)
        bbox = getattr(case, "bbox", None) if case is not None else None
        if bbox is None:
            state.case_bbox = None
            return
        state.case_bbox = list(bbox)
    except Exception:  # noqa: BLE001 -- best-effort cache, never break the turn
        state.case_bbox = None


# The AOI is PINNED to the solve domain: the authoritative extent IS the
# solve domain (the peak depth / mesh LayerURI bbox the workflow already
# floors + stamps), not a freehand bbox re-derived per follow-up tool call.


def _scenario_produces_domain(tool_name: str) -> bool:
    """True when ``tool_name`` is an expensive solver whose result LayerURI bbox
    is the authoritative AOI to pin (SWMM / SFINCS / MODFLOW domains).

    Any tool ``scenario_type_for_tool`` recognizes mints a domain-extent layer
    (flood-depth peak / plume) -- the SAME extent ``compute_layer_bounds`` returns
    for the produced handle. Reuses that taxonomy so a new solver auto-pins.
    """
    return scenario_type_for_tool(tool_name) is not None


async def _pin_case_aoi_from_solve(
    state: SessionState,
    *,
    case_id: str | None,
    bbox: Any,
) -> None:
    """Persist a completed solve's domain ``bbox`` as the Case AOI.

    Writes ``CaseSummary.bbox`` via ``upsert_case`` AND updates the durable
    in-session cache ``state.case_bbox`` so ``_turn_case_bbox`` returns the
    pinned extent for the rest of THIS session (every follow-up fetch
    defaults to it) and a later Case reopen rehydrates the SAME AOI from
    persistence.

    Best-effort: a missing/tombstoned Case or a Persistence hiccup is logged
    and never raised -- pinning is a side-effect, not the solve's happy
    path. Idempotent: a re-run at the SAME extent skips the round-trip (the
    persisted value already matches, within the bbox quantization
    tolerance).
    """
    coerced = _coerce_bbox4(bbox)
    if coerced is None or not case_id:
        return
    # Update the in-session anchor first -- it drives the fetch default below even
    # if the persistence write fails (e.g. an anonymous/ephemeral Case).
    state.case_bbox = list(coerced)
    p = get_persistence()
    if p is None:
        return
    try:
        case = await p.get_case(case_id)
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin: get_case failed case=%s", case_id)
        return
    if case is None:
        logger.debug("aoi-pin: case=%s missing; skipping pin", case_id)
        return
    # Idempotent: skip the write when the persisted AOI already equals the solve
    # domain (a re-run at the same extent, or a second domain-producing tool).
    if case.bbox is not None and bbox_equivalent(list(case.bbox), list(coerced)):
        return
    updated = case.model_copy(
        update={"bbox": list(coerced), "updated_at": now_utc()}
    )
    try:
        await p.upsert_case(updated)
        logger.info(
            "aoi-pin: pinned Case AOI case=%s bbox=%s (solve domain)",
            case_id,
            list(coerced),
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin: upsert failed case=%s", case_id)


def _bbox_round6(bbox: Any) -> tuple[float, float, float, float] | None:
    """Round a coerced 4-tuple bbox to 6 decimal places (~0.11 m at the
    equator) for a TIGHT change-detection comparison.

    Used only by ``_pin_case_aoi_from_tool_bbox``'s durable-write debounce --
    deliberately much tighter than the coarse ~2 km ``_BBOX_QUANT_DEG``
    scenario-reuse quant (``bbox_equivalent``'s default): that quant is
    "close enough to be the same run", whereas here we only want to skip a
    literally-repeated bbox, not silently drop a real (if small) AOI move.
    Returns ``None`` for a missing / malformed bbox.
    """
    coerced = _coerce_bbox4(bbox)
    if coerced is None:
        return None
    return (
        round(coerced[0], 6),
        round(coerced[1], 6),
        round(coerced[2], 6),
        round(coerced[3], 6),
    )


async def _pin_case_aoi_from_tool_bbox(
    state: SessionState,
    *,
    case_id: str | None,
    tool_name: str,
    params: dict,
) -> None:
    """Durably anchor the Case AOI from an ordinary bbox-taking FETCH call.

    Complements ``_pin_case_aoi_from_solve`` (above), which only fires for a
    domain-producing SOLVER (SWMM / SFINCS / MODFLOW) -- a Case whose
    activity so far is plain fetches (``fetch_dem``, ``fetch_landcover``,
    ...) would otherwise never get an AOI anchor, leaving
    ``build_layers_present_note`` with no AOI line for a follow-up prompt to
    resolve against.

    Fires ONLY for recognized bbox-taking fetchers (``fetched_kind_for_tool``);
    domain-producing solvers are explicitly excluded -- they keep their own
    post-RESULT pin from the FLOORED solve-domain bbox
    (``_pin_case_aoi_from_solve``), which must win over a pre-solve REQUEST
    bbox. Called AFTER both AOI reuse guards have already read
    ``_turn_case_bbox`` for THIS dispatch (so it never perturbs this call's
    own reuse comparison) and AFTER ``_maybe_default_fetch_bbox_to_pinned_aoi``
    has already snapped a same-area drifted/narrower box onto any existing
    pin -- so this call can only WIDEN (an explicit enclose), MOVE (a
    disjoint bbox = a genuinely different place -- latest-wins, matching the
    solve-pin's unconditional overwrite semantics), or -- the common case --
    SEED (no pin yet) the anchor. It can never silently shrink an
    already-established AOI.

    Latest-wins in-session: ``state.case_bbox`` is set unconditionally (once
    a valid bbox is present) so the persisted Case row and the in-session
    cache stay in lockstep (the invariant: ``_turn_case_bbox`` at turn end
    == ``CaseSummary.bbox``). The durable Persistence write is debounced on
    a tight 6-decimal-place comparison (``_bbox_round6``, NOT the coarse
    scenario-reuse quant) so a repeated identical bbox never round-trips
    Persistence twice. Best-effort and silent: never raises, never blocks
    the turn -- a missing active Case, an unbound Persistence, or a
    Persistence hiccup just skips the write (existing bbox-less Cases
    self-heal on their NEXT turn with any bbox-carrying fetch).
    """
    if fetched_kind_for_tool(tool_name) is None:
        return
    if _scenario_produces_domain(tool_name):
        return  # solves are pinned post-result from the floored domain bbox
    if not case_id:
        return
    coerced = _coerce_bbox4(params.get("bbox"))
    if coerced is None:
        return
    # Latest-wins: always refresh the in-session anchor first, mirroring
    # _pin_case_aoi_from_solve -- the durable write below is best-effort and
    # may legitimately no-op (debounce) or fail without undoing this.
    state.case_bbox = list(coerced)
    p = get_persistence()
    if p is None:
        return
    try:
        case = await p.get_case(case_id)
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin[fetch]: get_case failed case=%s", case_id)
        return
    if case is None:
        logger.debug("aoi-pin[fetch]: case=%s missing; skipping pin", case_id)
        return
    if _bbox_round6(case.bbox) == _bbox_round6(coerced):
        return  # debounce: the persisted AOI already matches this exact bbox
    updated = case.model_copy(
        update={"bbox": list(coerced), "updated_at": now_utc()}
    )
    try:
        await p.upsert_case(updated)
        logger.info(
            "aoi-pin[fetch]: pinned Case AOI case=%s bbox=%s (tool=%s)",
            case_id,
            list(coerced),
            tool_name,
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin[fetch]: upsert failed case=%s", case_id)


def _bbox_overlaps(a: Any, b: Any) -> bool:
    """True iff two WGS84 bboxes have a non-empty intersection (LANE-C helper).

    Used by the fetch-default rule to distinguish a DRIFTED box targeting the
    pinned AOI (overlaps -> snap to the pin) from a genuinely DIFFERENT place
    (disjoint -> honor the LLM's box). Touching-edge counts as overlap.
    """
    pa = _coerce_bbox4(a)
    pb = _coerce_bbox4(b)
    if pa is None or pb is None:
        return False
    return pa[0] <= pb[2] and pb[0] <= pa[2] and pa[1] <= pb[3] and pb[1] <= pa[3]


#: Near-exact tolerance (deg) for the fetch-default snap decision. Deliberately
#: MUCH tighter than the coarse ~2 km ``_BBOX_QUANT_DEG`` scenario-reuse quant so a
#: same-area-but-drifted box (the live ~0.005-0.01 deg under-coverage) is snapped
#: to the pin rather than waved through as "equivalent". ~1.1 m at the equator.
_AOI_DEFAULT_EQ_TOL_DEG = 1e-5


def _maybe_default_fetch_bbox_to_pinned_aoi(
    tool_name: str,
    params: dict,
    pinned_bbox: Any,
) -> dict:
    """Default a bbox-taking fetch tool to the pinned Case AOI.

    The LLM free-hands a fresh (and usually NARROWER) bbox for every
    follow-up fetch even when it means "the same area I just modeled". When
    a domain has been pinned (``state.case_bbox`` set by a solve), force
    follow-up fetches onto that SAME extent so all layers cover the AOI by
    construction.

    PRECISE RULE (honor "a different place", fix "the same place, drifted box"):
      * Only applies to recognized bbox-taking fetchers (``fetched_kind_for_tool``).
      * No pinned AOI -> no-op (returns ``params`` unchanged).
      * No / invalid ``bbox`` supplied (bare follow-up) -> inject the pin.
      * Supplied bbox that OVERLAPS the pin but does NOT already enclose it (a
        narrower / drifted box for the same area) -> REPLACE with the pin.
      * Supplied bbox that already ENCLOSES the pin (an explicit larger area) ->
        HONOR it (the user asked to widen).
      * Supplied bbox DISJOINT from the pin (a genuinely different place) ->
        HONOR it (do not drag the new area back to the old AOI).

    Pure + conservative: returns a NEW dict only when it changes ``bbox``; never
    mutates the input dict in place.
    """
    if fetched_kind_for_tool(tool_name) is None:
        return params
    pin = _coerce_bbox4(pinned_bbox)
    if pin is None:
        return params
    supplied = _coerce_bbox4(params.get("bbox"))
    if supplied is not None:
        # TIGHT tolerance for the snap decision (NOT the coarse ~2 km scenario-
        # reuse quantization): the live bug was a same-area box only ~0.005-0.01
        # deg off the pin yet covering 87% width / 63% height of the domain, which
        # the reuse quant would call "equivalent". We compare near-exactly here so
        # those drifted same-area boxes are snapped, not waved through.
        if bbox_equivalent(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params  # already (essentially) the pin -> no needless copy
        # A genuinely DIFFERENT place (disjoint) is the user's intent -> honor it.
        if not _bbox_overlaps(supplied, pin):
            return params
        # An explicit WIDEN: the supplied box ENCLOSES the pin on all four edges
        # (it is at least as large as the pin everywhere, so the user asked for a
        # bigger area). A drifted / narrower same-area box CLIPS the pin on at
        # least one edge -> not an enclose -> falls through to the snap. The tight
        # tolerance keeps a near-equal box from masquerading as a widen.
        if bbox_encloses(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params
    # Bare follow-up OR a drifted/narrower same-area box -> snap to the pinned AOI.
    new_params = dict(params)
    new_params["bbox"] = list(pin)
    logger.info(
        "aoi-default: %s bbox -> pinned Case AOI %s (was %s)",
        tool_name,
        list(pin),
        list(supplied) if supplied is not None else None,
    )
    return new_params


#: Expensive-solver scenario types whose domain IS an AOI bbox (areal solvers).
#: ``scenario_type_for_tool`` also recognizes the POINT-driven groundwater solvers
#: (``modflow_contaminant_plume`` / ``run_model_groundwater_contamination_scenario`` ->
#: ``"plume"``) which take NO bbox param -- their domain is a well / source point,
#: not a rectangle. The AOI-snap below must NOT inject a bbox into those (it would
#: be a spurious, ignored key today and latent wrong-extent debt tomorrow), so the
#: guard is restricted to these bbox-driven scenario types.
_BBOX_DRIVEN_SOLVER_SCENARIOS: frozenset[str] = frozenset({"flood-depth", "swmm-depth"})


def _maybe_default_solver_bbox_to_pinned_aoi(
    tool_name: str,
    params: dict,
    pinned_bbox: Any,
) -> dict:
    """Pin an expensive SOLVER's bbox to the active Case AOI.

    The SFINCS solve must compute ONLY within the active AOI bbox unless
    something requires it to expand. This snaps the SOLVE domain back onto
    the active AOI by the SAME conservative rule the fetch default
    (``_maybe_default_fetch_bbox_to_pinned_aoi``) uses.

    PRECISE RULE (identical to the fetch default -- honor real expansion, fix the
    drifted same-area box; "required expansion is allowed, only UN-required
    expansion is the bug"):
      * Only applies to the bbox-driven AREAL solvers (flood / urban depth).
        POINT-driven solvers (MODFLOW plume) take no bbox and are skipped.
      * No pinned AOI -> no-op. The FIRST solve in a Case (no AOI pinned yet)
        DEFINES the domain from the LLM's bbox; the pin is written AFTER it.
      * No / invalid ``bbox`` supplied -> inject the pin (solve the active AOI).
      * Supplied bbox that OVERLAPS the pin but does NOT enclose it (a wider /
        drifted same-area box that pokes outside the displayed AOI) -> REPLACE
        with the pin: solve ONLY within the active AOI.
      * Supplied bbox that already ENCLOSES the pin (an explicit larger area the
        user asked to model) -> HONOR it. REQUIRED expansion is allowed.
      * Supplied bbox DISJOINT from the pin (a genuinely different place) ->
        HONOR it.

    The SFINCS scenario-coverage archetypes (fluvial / compound / wind /
    infiltration / levee / tsunami) and coastal runs are selected by FORCING
    FLAGS (``coastal=`` / ``river=`` / ``tsunami=`` ...), NOT by an
    enclosing-wider bbox, and an explicit enclose / disjoint bbox is always
    honored -- so none of those decks are clipped by this guard.

    Pure + conservative: returns a NEW dict only when it changes ``bbox``; never
    mutates the input dict in place. Shares the exact tolerance / enclose / overlap
    semantics of the fetch default for a single, auditable AOI-snap policy.
    """
    if scenario_type_for_tool(tool_name) not in _BBOX_DRIVEN_SOLVER_SCENARIOS:
        # Non-solver, or a POINT-driven solver (MODFLOW plume) that takes no bbox
        # -- never inject one. Only the areal (bbox-driven) flood/urban solvers
        # have an AOI rectangle to snap.
        return params
    pin = _coerce_bbox4(pinned_bbox)
    if pin is None:
        return params
    supplied = _coerce_bbox4(params.get("bbox"))
    if supplied is not None:
        # Already (essentially) the active AOI -> no needless copy.
        if bbox_equivalent(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params
        # A genuinely DIFFERENT place (disjoint) is the user's intent -> honor it.
        if not _bbox_overlaps(supplied, pin):
            return params
        # An explicit WIDEN (encloses the pin on all four edges) is REQUIRED
        # expansion the user asked for -> honor it (NATE: "unless something
        # requires it to expand").
        if bbox_encloses(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params
    # Bare follow-up OR a drifted / wider same-area box that pokes outside the
    # displayed AOI -> snap the SOLVE domain to the active AOI bbox.
    new_params = dict(params)
    new_params["bbox"] = list(pin)
    logger.info(
        "aoi-solve-default: %s solve bbox -> active Case AOI %s (was %s)",
        tool_name,
        list(pin),
        list(supplied) if supplied is not None else None,
    )
    return new_params


@dataclass
class _ReuseEntry:
    """A drop-in ``RegisteredTool``-shaped shim for the reuse short-circuit.

    Carries the real tool's ``metadata`` (so the tool card / telemetry label
    is unchanged) but a ``fn`` that returns the EXISTING layer instead of
    launching the solver. ``_invoke_tool_via_emitter`` swaps the registry
    entry for this so the SAME ``emit_tool_call`` LayerURI gate fires with
    the reused layer.
    """

    metadata: Any
    layer: LayerURI

    @property
    def fn(self) -> Any:
        layer = self.layer

        def _return_existing(**_ignored: Any) -> LayerURI:
            return layer

        return _return_existing


async def _finalize_segment(
    websocket: ServerConnection,
    state: SessionState,
    message_id: str,
    segment_parts: list[str],
    *,
    is_terminal: bool = False,
    thinking_parts: list[str] | None = None,
) -> None:
    """Close ONE narration bubble + persist it as its own agent row.

    Each contiguous run of agent text between tool-call rounds is a SEGMENT.
    Closing a segment does two things at the boundary "agent text is about to
    be interrupted by tool cards (or the turn is ending)":

    (1) Send the terminal ``done=True`` ``agent-message-chunk`` for THIS
        bubble's ``message_id`` so the live client marks the bubble complete
        (web ``appendDelta`` sets ``done``). This MUST only fire for an id
        that already received text -- the caller guarantees that by only
        calling here when ``current_message_id is not None``.
    (2) Persist a ``role="agent"`` ``CaseChatMessage`` carrying ONLY this
        segment's text, so the persisted row order interleaves with the
        mid-turn tool rows (``_persist_tool_card``) and the replay
        reconstructs the live interleaved train. An empty segment persists
        NOTHING (no phantom bubble on replay; no row-count regression).

    ``layer_emissions``: non-terminal segments pass ``[]`` so they do NOT each
    duplicate the whole-turn ``current_turn_layer_ids`` /
    ``current_turn_map_commands`` accumulators. The TERMINAL segment
    (``is_terminal=True`` -- the final narration run of the turn) passes
    ``None`` so ``_persist_chat_turn`` snapshots the accumulators onto it,
    keeping layer attribution + zoom-to on the de-facto closing row.

    Best-effort persist (inherits ``_persist_chat_turn``'s swallow); the wire
    ``done=True`` still fires even if persistence is unbound. Clears the
    segment buffer and bumps the per-task finalized-count on a non-empty
    write.

    ``thinking_parts``: the per-segment reasoning-text buffer accumulated
    while the per-turn ``show_thinking`` toggle was ON. When THIS segment
    persists a non-empty row, its joined text rides the row's ``thinking``
    field (same-bubble contract) and the buffer is cleared. A thinking-only
    segment (no answer text -> no row, the no-phantom-bubble invariant)
    KEEPS its buffer so the thinking attaches to the turn's next persisted
    agent row instead of being dropped. Same clear-not-rebind discipline as
    ``segment_parts``.
    """
    text = "".join(segment_parts).strip()
    # (1) wire terminal for this bubble -- always fires (id has text).
    await _session_safe_send(websocket, state.session_id,
        _new_envelope(
            "agent-message-chunk",
            state.session_id,
            AgentMessageChunkPayload(message_id=message_id, delta="", done=True),
        )
    )
    # (2) per-segment persist -- only when there is real text.
    if text:
        thinking_text = (
            "".join(thinking_parts).strip() if thinking_parts else ""
        )
        await _persist_chat_turn(
            state,
            role="agent",
            content=text,
            pipeline_id=state.current_turn_pipeline_id,
            # Terminal segment owns the layer/zoom attribution; non-terminal
            # segments carry none (the accumulator rides the last row only).
            layer_emissions=None if is_terminal else [],
            case_id=_turn_case_id(state),
            thinking=thinking_text or None,
        )
        # Thinking consumed by this row -- clear the SAME list object (do not
        # rebind), mirroring the segment-buffer discipline below.
        if thinking_parts:
            thinking_parts.clear()
        _task = asyncio.current_task()
        if _task is not None:
            _TURN_SEGMENTS_PERSISTED_BY_TASK[_task] = (
                _TURN_SEGMENTS_PERSISTED_BY_TASK.get(_task, 0) + 1
            )
            # A TERMINAL non-empty segment row just
            # snapshotted the turn's zoom-to/layer accumulator
            # (``layer_emissions=None`` above). Record that so the wrapper's
            # finally does NOT also write a duplicate closing marker row -- the
            # marker is ONLY for the tool-terminal shape where this never fires.
            if is_terminal:
                _TURN_TERMINAL_ACC_PERSISTED_BY_TASK[_task] = True
    # The open buffer is now closed: clear the SAME list object (do not rebind)
    # so the task-registered open buffer the wrapper reads is always current.
    segment_parts.clear()


async def _persist_chat_turn(
    state: SessionState,
    *,
    role: str,
    content: str,
    pipeline_id: str | None = None,
    tool_card: ToolCardRecord | None = None,
    layer_emissions: list[str] | None = None,
    case_id: str | None = None,
    message_id: str | None = None,
    thinking: str | None = None,
) -> None:
    """Append one ``CaseChatMessage`` to Mongo for the active Case.

    Best-effort: a missing Persistence binding OR no active Case context
    short-circuits (the M1 in-memory chat keeps working). A failed write
    is logged but not raised -- chat persistence is a side-effect, not the
    happy path of message delivery.

    Per FR-AS-8 / Decision F the chat-message collection is part of the
    agent's own session record (it is per-turn replay material, not a
    solver result); the confirmation-hook carveout in
    ``CONFIRMATION_TRIGGERS`` means this write does NOT pause for user
    approval.

    ``tool_card`` carries the typed ``ToolCardRecord`` for ``role="tool"``
    rows; ``layer_emissions`` overrides the default per-turn accumulator
    snapshot (tool rows pass ``[]`` so the turn's layer ids stay attributed
    to the closing agent row).

    ``case_id`` pins the target Case explicitly (the dispatch wrappers
    capture it at task entry so even a cancel-and-redispatch race cannot
    re-aim the write); when omitted it resolves via ``_turn_case_id`` --
    never the raw write-time ``active_case_id``.

    Durable-card lifecycle: ``message_id``, when supplied, pins the row's
    stable id and routes the write through ``upsert_chat_message``
    (insert-or-replace) instead of ``append_chat_message`` -- so a SOLVE
    card persisted ``running`` at mint can be UPDATED IN PLACE to its
    terminal state without a duplicate row. Omitted (the default) keeps the
    append-a-fresh-row behavior every existing caller relies on.
    """
    target_case = case_id if case_id is not None else _turn_case_id(state)
    if not target_case:
        return
    p = get_persistence()
    if p is None:
        return
    msg = CaseChatMessage(
        message_id=message_id or new_ulid(),
        case_id=target_case,
        role=role,  # type: ignore[arg-type]
        content=content,
        # Thinking persistence (LANE CORE 2026-07-22): reasoning-channel text
        # for the same bubble; None on every non-agent row and on turns with
        # show_thinking off. Display replay ONLY -- never rehydrated into
        # LLM-bound contents (adapter.NEVER_REHYDRATE_FIELDS).
        thinking=thinking,
        pipeline_id=pipeline_id,
        tool_card=tool_card,
        layer_emissions=(
            list(state.current_turn_layer_ids)
            if layer_emissions is None
            else list(layer_emissions)
        ),
        # Persist the turn's zoom-to emissions (geocode snap) on
        # rows that snapshot the accumulator (agent/user rows) -- the
        # Case-reopen snap-to-location replays the LAST one (web).
        # Tool rows pass layer_emissions=[] and get [] here too.
        map_command_emissions=(
            list(state.current_turn_map_commands)
            if layer_emissions is None
            else []
        ),
        created_at=now_utc(),
    )
    try:
        if message_id is not None:
            # Durable-card lifecycle: insert-or-replace the SAME row so a
            # running card walks to terminal in place (no duplicate).
            await p.upsert_chat_message(msg)
        else:
            await p.append_chat_message(msg)
        # Per-turn D.6 heartbeat (M4): the chat turn is the
        # activity signal that keeps the session record's TTL fresh and
        # the turn's Case registered in ``project_ids``.
        await _touch_session_record(state, case_id=target_case)
        logger.debug(
            "chat-persist session=%s case=%s role=%s msg_id=%s pipeline_id=%s layers=%d",
            state.session_id,
            target_case,
            role,
            msg.message_id,
            pipeline_id,
            len(msg.layer_emissions),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "chat-persist failed session=%s case=%s role=%s",
            state.session_id,
            target_case,
            role,
        )


async def _persist_tool_card(
    state: SessionState,
    *,
    tool_name: str,
    label: str,
    card_state: str,
    started_at_fallback: datetime,
    duration_ms_fallback: int,
    case_id: str | None = None,
    raw_args: Any = None,
    function_response: Any = None,
    io_is_error: bool = False,
    message_id: str | None = None,
) -> None:
    """Persist one replayable tool-card row for the active Case.

    Written by ``_invoke_tool_via_emitter`` on every terminal tool dispatch
    (complete OR failed; cancelled dispatches persist nothing -- Invariant
    8). Storage shape: ``CaseChatMessage(role="tool")`` in the SAME chat
    collection as user/agent turns, so the rehydration replay interleaves
    the full stream by ``created_at`` with zero extra queries. The typed
    payload is ``tool_card`` (``ToolCardRecord``); ``content`` carries the
    identical record as a JSON string for non-contract consumers.

    Timing source of truth: the emitter's ``last_tool_step`` (the
    authoritative ``started_at`` / ``duration_ms`` stamps the live card
    displayed). The wall-clock fallbacks only engage when the emitter stamp
    is unavailable (e.g. the wire died before the terminal transition).

    Tool-card IO persistence: when ``raw_args`` / ``function_response`` are
    supplied, the SAME input args + output response the live ``tool-io``
    sidecar carries (``PipelineEmitter.emit_tool_io``) are serialized with
    the SAME helper (``_json_for_tool_io`` -- identical truncation/byte
    semantics) and populated on the TYPED ``ToolCardRecord`` under the EXACT
    live ``ToolIoPayload`` field names -- ``raw_args`` / ``function_response``
    / ``args_truncated`` / ``response_truncated`` / ``args_bytes`` /
    ``response_bytes`` / ``is_error`` (all optional/nullable).
    ``get_session_state`` replay carries them on ``m.tool_card``; the web
    renderer rehydrates the tool-card expander on Case reopen by reading
    them off the TYPED record -- the ``content`` JSON twin carries the
    identical values for non-contract consumers but is not the integration
    path.

    Best-effort, never raises: record construction is wrapped here and the
    underlying ``_persist_chat_turn`` already swallows write failures.
    """
    try:
        started_at = started_at_fallback
        duration_ms: int = max(0, int(duration_ms_fallback))
        emitter_step = (
            state.emitter.last_tool_step if state.emitter is not None else None
        )
        if emitter_step is not None and emitter_step.tool_name == tool_name:
            if emitter_step.started_at is not None:
                started_at = emitter_step.started_at
            if emitter_step.duration_ms is not None:
                duration_ms = emitter_step.duration_ms
        # The persisted IO must ride the TYPED ``ToolCardRecord`` -- replay
        # reads it off ``m.tool_card``, NOT the row ``content`` JSON.
        # Compute the IO ONCE with the SAME ``_json_for_tool_io`` helper +
        # field names the live ``tool-io`` sidecar uses, populate the typed
        # record's IO fields, and keep the identical values on the
        # ``content`` JSON twin for non-contract consumers. Only when at
        # least one of raw_args/function_response was provided (the
        # LLM-dispatch path) -- the /invoke directive path passes neither,
        # so its rows stay IO-less and the typed record's IO fields default
        # to ``None`` (existing documents validate + replay unchanged).
        _io_fields: dict[str, Any] = {}
        if raw_args is not None or function_response is not None:
            args_str, args_trunc, args_bytes = _json_for_tool_io(raw_args)
            resp_str, resp_trunc, resp_bytes = _json_for_tool_io(function_response)
            _io_fields = {
                "raw_args": args_str,
                "function_response": resp_str,
                "args_truncated": args_trunc,
                "response_truncated": resp_trunc,
                "args_bytes": args_bytes,
                "response_bytes": resp_bytes,
                "is_error": bool(io_is_error),
            }
        # Carry the ordered CHILD substeps captured by the emitter at this
        # dispatch's terminal transition. The emitter snapshots them onto
        # ``last_tool_children`` WHILE the children still exist in ``_steps``
        # -- ``close_pipeline`` (run just before this hook) has already
        # cleared ``_steps``, so this durable snapshot is the only source.
        # Reading it onto ``ToolCardRecord.children`` makes the nested
        # timeline replay READ-ONLY on a Case reopen (additive JSON -- a card
        # with no children stays ``None``). Guard the tool match so a stale
        # prior-dispatch snapshot can never attach to this row.
        _children: list | None = None
        emitter_children = (
            state.emitter.last_tool_children if state.emitter is not None else None
        )
        if (
            emitter_children
            and emitter_step is not None
            and emitter_step.tool_name == tool_name
        ):
            _children = list(emitter_children)
        record = ToolCardRecord(
            tool_name=tool_name,
            state=card_state,  # type: ignore[arg-type]
            started_at=started_at,
            duration_ms=duration_ms,
            label=label,
            children=_children,
            **_io_fields,  # C1: typed IO on the record == the integration path
        )
        # Content JSON twin: model_dump_json now already carries the IO fields
        # (they live on the typed record), so a single dump matches the wire
        # shape for non-contract consumers without a separate merge.
        content = record.model_dump_json()
        await _persist_chat_turn(
            state,
            role="tool",
            content=content,
            pipeline_id=state.current_turn_pipeline_id,
            tool_card=record,
            layer_emissions=[],
            case_id=case_id,
            message_id=message_id,
        )
    except Exception:  # noqa: BLE001 -- replay material, never the happy path
        logger.exception(
            "tool-card persist failed session=%s case=%s tool=%s",
            state.session_id,
            case_id if case_id is not None else _turn_case_id(state),
            tool_name,
        )


async def _persist_terminal_failure_card(
    state: SessionState,
    *,
    error_code: str,
    message: str,
    case_id: str | None = None,
) -> None:
    """Persist a ``role="tool"`` FAILED tool-card row for a terminal turn
    failure that did NOT flow through ``_invoke_tool_via_emitter``'s own
    failed-card persist.

    A terminal solve/tool failure must surface to the user even across a
    socket cycle -- otherwise a WS reconnect / Case-reopen replays the last
    tool card stuck in its ``running`` state forever.

    Writes the SAME ``role="tool"`` ``CaseChatMessage`` + ``ToolCardRecord``
    shape ``_persist_tool_card`` produces, with ``state="failed"``. The
    ``ToolCardRecord`` contract (case.py) has no error_code/message fields,
    so the A.6 ``error_code`` + human message ride in the row ``content`` (a
    JSON twin, exactly like the complete-card content) and the ``label`` so
    the web replay surfaces the failure reason. Honesty floor: this writes
    ONLY on a real terminal failure -- it never fabricates a success.

    Prefers the emitter's authoritative ``last_tool_step`` for the failing
    tool's identity + timing (so the persisted failed card matches the live
    card the user last saw spinning); falls back to a synthetic
    ``llm_generation`` card when no tool step is available (a pure
    model-stream failure with no in-flight tool). Best-effort, never raises.
    """
    import json

    try:
        target_case = case_id if case_id is not None else _turn_case_id(state)
        if not target_case:
            return
        emitter_step = (
            state.emitter.last_tool_step if state.emitter is not None else None
        )
        # Identify the failing operation: the last live tool step (the solve
        # / tool the user saw running) when present, else the model-
        # generation step. ``duration_ms`` / ``started_at`` mirror the live
        # card so the replayed failed card lands where the running one was.
        # When the failing operation IS the last live tool step, carry its
        # captured child substeps so the replayed failed card still nests
        # its sub-step timeline; the synthetic ``gemini_generate`` branch (a
        # pure model-stream failure, no in-flight tool) has no children.
        _children: list | None = None
        if emitter_step is not None and emitter_step.tool_name:
            tool_name = emitter_step.tool_name
            label = emitter_step.name or emitter_step.tool_name
            started_at = emitter_step.started_at or now_utc()
            duration_ms = emitter_step.duration_ms
            emitter_children = (
                state.emitter.last_tool_children
                if state.emitter is not None
                else None
            )
            if emitter_children:
                _children = list(emitter_children)
        else:
            tool_name = "gemini_generate"
            label = "llm_generation"
            started_at = now_utc()
            duration_ms = 0
        record = ToolCardRecord(
            tool_name=tool_name,
            state="failed",
            started_at=started_at,
            duration_ms=duration_ms,
            # Surface the failure reason in the human-facing label so the
            # replayed card explains WHY it failed.
            label=f"{label} — {error_code}",
            children=_children,
        )
        # The JSON-twin content carries the typed record PLUS the error_code +
        # message (the record contract cannot hold them) so non-contract
        # replay consumers still see the failure reason.
        content_payload = json.loads(record.model_dump_json())
        content_payload["error_code"] = error_code
        content_payload["message"] = message
        await _persist_chat_turn(
            state,
            role="tool",
            content=json.dumps(content_payload),
            pipeline_id=state.current_turn_pipeline_id,
            tool_card=record,
            layer_emissions=[],
            case_id=target_case,
        )
        logger.info(
            "terminal-failure card persisted session=%s case=%s tool=%s code=%s",
            state.session_id,
            target_case,
            tool_name,
            error_code,
        )
    except Exception:  # noqa: BLE001 -- replay material, never the happy path
        logger.exception(
            "terminal-failure card persist failed session=%s case=%s code=%s",
            state.session_id,
            case_id if case_id is not None else _turn_case_id(state),
            error_code,
        )


# --------------------------------------------------------------------------- #
# Payload-warning gate.
# --------------------------------------------------------------------------- #


async def _maybe_gate_on_payload_warning(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
) -> tuple[bool, dict]:
    """Run the payload-warning gate before dispatching ``tool_name``.

    Returns ``(should_dispatch, effective_params)``:

    - ``(True, params)`` -- no warning needed (no estimator, estimate below
      threshold) OR user picked ``proceed``. Dispatch with ``params``.
    - ``(True, revised_args)`` -- user picked ``narrow_scope``. Dispatch with
      the user's revised args.
    - ``(False, params)`` -- user picked ``cancel`` OR the gate timed out.
      Skip the dispatch; the caller surfaces a typed failure to chat.

    Audit-log entries are appended to ``state.payload_warning_audit_log``
    on both emission AND decision. Never raises -- a gate failure logs +
    falls through to dispatch (the gate is a UX nudge, not a hard
    invariant; a broken estimator should not break the tool).
    """
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return True, params
    estimator_name = entry.metadata.payload_mb_estimator_name
    if not estimator_name:
        return True, params
    estimator_fn = _resolve_payload_estimator(tool_name, estimator_name)
    if estimator_fn is None:
        return True, params
    try:
        estimated_mb = float(estimator_fn(**params))
    except Exception:  # noqa: BLE001 -- never let the gate kill a tool
        logger.exception(
            "payload-warning: estimator raised tool=%s name=%s; skipping gate",
            tool_name,
            estimator_name,
        )
        return True, params

    threshold_mb = _get_warning_threshold_mb()
    hard_cap_mb = _get_hard_cap_mb()
    if estimated_mb < threshold_mb:
        return True, params

    over_hard_cap = estimated_mb > hard_cap_mb
    options = (
        ["cancel", "narrow_scope"]
        if over_hard_cap
        else ["proceed", "cancel", "narrow_scope"]
    )
    recommendation = (
        f"Estimated payload {estimated_mb:.1f} MB exceeds the "
        f"{'hard cap' if over_hard_cap else 'warning threshold'} "
        f"({hard_cap_mb if over_hard_cap else threshold_mb:.0f} MB). "
        "Consider narrowing bbox or other scope parameters."
    )

    warning_id = new_ulid()
    warning_payload = PayloadWarningEnvelopePayload(
        warning_id=warning_id,
        tool_name=tool_name,
        tool_args=params,
        estimated_mb=estimated_mb,
        threshold_mb=hard_cap_mb if over_hard_cap else threshold_mb,
        recommendation=recommendation,
        options=options,
    )

    # Audit-log the emission.
    audit_entry: dict = {
        "warning_id": warning_id,
        "tool_name": tool_name,
        "estimated_mb": estimated_mb,
        "threshold_mb": warning_payload.threshold_mb,
        "options": list(options),
        "emitted_at": now_utc().isoformat(),
        "decision": None,
    }
    state.payload_warning_audit_log.append(audit_entry)

    # Create the future the inbound handler will complete.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(state.session_id, warning_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("tool-payload-warning", state.session_id, warning_payload)
    )
    logger.info(
        "payload-warning emitted session=%s tool=%s warning_id=%s estimated_mb=%.2f over_hard_cap=%s",
        state.session_id,
        tool_name,
        warning_id,
        estimated_mb,
        over_hard_cap,
    )

    # Await the confirmation (TTL on the envelope is advisory; we honour it
    # with an asyncio timeout so the dispatch coroutine doesn't hang forever).
    try:
        decision_payload: PayloadConfirmationEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(warning_payload.ttl_seconds)
        )
    except asyncio.TimeoutError:
        audit_entry["decision"] = "timeout"
        logger.warning(
            "payload-warning timeout session=%s tool=%s warning_id=%s",
            state.session_id,
            tool_name,
            warning_id,
        )
        await _send_error(
            websocket,
            state.session_id,
            "CONFIRMATION_TIMEOUT",
            f"tool {tool_name!r} payload-warning gate timed out",
        )
        return False, params
    finally:
        _pop_pending_confirmation(warning_id)

    audit_entry["decision"] = decision_payload.decision
    audit_entry["decided_at"] = now_utc().isoformat()
    logger.info(
        "payload-warning decision session=%s tool=%s warning_id=%s decision=%s",
        state.session_id,
        tool_name,
        warning_id,
        decision_payload.decision,
    )

    if decision_payload.decision == "cancel":
        await _send_error(
            websocket,
            state.session_id,
            "USER_INPUT_CANCELLED",
            f"tool {tool_name!r} cancelled by user at payload-warning gate "
            f"(estimated {estimated_mb:.1f} MB)",
        )
        return False, params
    if decision_payload.decision == "proceed":
        if over_hard_cap:
            # Defense in depth: the warning envelope omitted ``proceed`` so a
            # well-behaved client can't pick it. Refuse if it does anyway.
            await _send_error(
                websocket,
                state.session_id,
                "TOOL_PARAMS_INVALID",
                f"tool {tool_name!r} exceeds hard cap "
                f"({estimated_mb:.1f} > {hard_cap_mb:.0f} MB); "
                "'proceed' is not an allowed response",
            )
            return False, params
        return True, params
    # narrow_scope
    revised = decision_payload.revised_args or {}
    return True, revised


async def _gate_on_code_exec(
    websocket: ServerConnection,
    state: SessionState,
    params: dict,
) -> tuple[bool, dict]:
    """Confirm gate for ``code_exec_request`` -- MANDATORY, fail-closed.

    Running arbitrary Python is a consequential action; the user MUST approve
    the exact code before the sandbox runs. This gate emits a
    ``code-exec-request`` confirm card and blocks on the SAME
    ``pending_payload_warnings`` future seam the payload-warning gate uses
    (the ``code_exec_id`` is the correlation key, carried back as the
    ``tool-payload-confirmation.warning_id``).

    Returns ``(should_dispatch, effective_params)``:

    - ``(True, params + {confirmed: True, code_exec_id})`` -- user approved
      (``decision="proceed"``). The tool body runs the sandbox.
    - ``(False, params)`` -- user chose ``cancel``. The caller raises
      :class:`CodeExecConfirmationCancelledError` so Gemini sees a typed,
      non-retryable error and narrates the decline honestly.

    Raises :class:`CodeExecApprovalTimeoutError` when NO confirmation answers
    the card within ``_code_exec_approval_timeout_s()`` (default 180s, env
    ``TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S``). This wait deliberately bypasses
    the 24h local-lane ``_gate_wait_timeout`` override: an unanswerable card
    must resolve the parked tool call with a typed error so the turn
    completes instead of hanging. The pending-confirmation registry entry is
    popped in the ``finally`` below on EVERY exit -- approve, deny, timeout,
    and task cancellation (session close / turn cancel) -- so nothing leaks.

    ``narrow_scope`` is NOT offered for code-exec (you don't "narrow" a code
    snippet -- you cancel and the agent rewrites it); a ``narrow_scope``
    reply is treated as a cancel (fail-closed).
    """
    python_code = params.get("python_code")
    if not isinstance(python_code, str) or not python_code.strip():
        # No code to confirm -- let the tool body raise its own params error.
        return True, params

    code_exec_id = new_ulid()
    request_payload = CodeExecRequestPayload(
        code_exec_id=code_exec_id,
        python_code=python_code,
        layer_refs=params.get("layer_refs") or {},
        rationale=params.get("rationale"),
    )

    # Create the future the inbound ``tool-payload-confirmation`` handler completes
    # (keyed on code_exec_id == warning_id). Same seam as the payload-warning gate.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(state.session_id, code_exec_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("code-exec-request", state.session_id, request_payload)
    )
    logger.info(
        "code-exec-request emitted session=%s code_exec_id=%s code_len=%d n_layers=%d",
        state.session_id,
        code_exec_id,
        len(python_code),
        len(request_payload.layer_refs),
    )

    approval_timeout_s = _code_exec_approval_timeout_s()
    try:
        decision_payload: PayloadConfirmationEnvelopePayload = await asyncio.wait_for(
            fut, timeout=approval_timeout_s
        )
    except asyncio.TimeoutError:
        logger.warning(
            "code-exec confirm gate timeout session=%s code_exec_id=%s "
            "waited=%.0fs (approval card never answered)",
            state.session_id,
            code_exec_id,
            approval_timeout_s,
        )
        # WS envelope: ``error_code`` is the closed A.6 ``ErrorCode`` Literal
        # (contracts are read-only), so the wire code stays the contract-valid
        # CONFIRMATION_TIMEOUT; the DISTINCT typed code below
        # (CODE_EXEC_APPROVAL_TIMEOUT) rides the function_response surface,
        # which is free-form.
        await _send_error(
            websocket,
            state.session_id,
            "CONFIRMATION_TIMEOUT",
            f"code_exec_request {code_exec_id!r} approval card was not answered "
            f"within {approval_timeout_s:.0f}s; the sandbox did not run",
        )
        # Typed resolution of the parked tool call: propagates to the tool
        # dispatch except-handler -> summarize_tool_result(error=...) -> a
        # structured function_response the LLM narrates -- the turn COMPLETES.
        raise CodeExecApprovalTimeoutError(code_exec_id, approval_timeout_s)
    finally:
        # Runs on approve, deny, timeout, AND CancelledError (session close /
        # turn cancel) -- the registry never leaks a dead future.
        _pop_pending_confirmation(code_exec_id)

    logger.info(
        "code-exec confirm decision session=%s code_exec_id=%s decision=%s",
        state.session_id,
        code_exec_id,
        decision_payload.decision,
    )

    if decision_payload.decision != "proceed":
        # cancel OR narrow_scope (the latter is meaningless for code; fail-closed).
        await _send_error(
            websocket,
            state.session_id,
            "USER_INPUT_CANCELLED",
            f"code_exec_request {code_exec_id!r} declined by user "
            f"(decision={decision_payload.decision!r}); the sandbox did not run",
        )
        return False, params

    # Approved: inject the gate-cleared flags so the tool body dispatches with the
    # SAME code_exec_id the request card carried (so request/result cards correlate).
    approved = dict(params)
    approved["confirmed"] = True
    approved["code_exec_id"] = code_exec_id
    return True, approved


# F6 (live-feedback 2026-07-08): user-decision gates must NOT expire in the
# TRID3NT local build. The cloud read-decision TTLs (300s payload-warning /
# code-exec / solver-confirm / credential / region-choice, 60-300s spatial
# input) exist because a hung turn holds Bedrock-connection economics on the
# always-on box; locally the user OWNS the machine and the LLM, so a gate card
# should wait for them indefinitely. "Effectively unbounded" = 24h -- long
# enough that no human session ever hits it, finite so an abandoned process
# still unwinds its futures.
_LOCAL_GATE_TIMEOUT_SECONDS: int = 24 * 3600


def _gate_wait_cap_s() -> "float | None":
    """Optional hard ceiling (seconds) applied to EVERY gate wait window.

    Test seam (``TRID3NT_GATE_WAIT_CAP_S``): headless suites exercise the
    gate-park machinery with no client to answer the card, so the 24h F6
    local-lane lift (and even the cloud 300s defaults) would hang the run.
    When this env is set to a positive value, ``_gate_wait_timeout`` returns
    ``min(configured, cap)`` for every gate -- the F6 24h override included --
    so the wait deterministically hits the honest timeout path. UNSET (the
    production case) leaves every wait byte-identical; malformed / non-positive
    values are ignored (treated as unset). Read LIVE so a per-test env flip is
    honored.
    """
    raw = os.environ.get("TRID3NT_GATE_WAIT_CAP_S")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _gate_wait_timeout(default_seconds: float) -> float:
    """Effective ``asyncio.wait_for`` timeout for a user-decision gate future.

    Local build (``_local_compute_lane()`` -- the established
    ``solver_backend() == "local-docker"`` seam): 24h, so confirmation /
    resolution / credential / region-choice / spatial-input gates never time
    out on a user who stepped away. Cloud: ``default_seconds`` unchanged
    (byte-identical behavior when the backend is aws-batch/unset). The wire
    envelope (``ttl_seconds`` etc.) is NOT rewritten -- only the server-side
    wait changes, so the client contract is untouched.

    Test cap (``TRID3NT_GATE_WAIT_CAP_S``, see ``_gate_wait_cap_s``): when set,
    the resolved wait is floored to ``min(effective, cap)`` so headless suites
    never hang on an unanswerable card. Unset -> production behavior unchanged.
    """
    if _local_compute_lane():
        effective = float(_LOCAL_GATE_TIMEOUT_SECONDS)
    else:
        effective = float(default_seconds)
    cap = _gate_wait_cap_s()
    if cap is not None:
        return min(effective, cap)
    return effective


async def _gate_on_solver_confirm(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    _warning_id_out: dict[str, str] | None = None,
) -> tuple[bool, dict]:
    """Parameter-confirmation gate for solver composers -- fail-closed.

    Mirrors :func:`_gate_on_code_exec`: build the confirm card, emit it as a
    ``tool-payload-warning`` (the inline card the client already renders),
    block on the ``pending_payload_warnings`` future seam (``warning_id`` is
    the correlation key the ``tool-payload-confirmation`` reply carries), and
    inject ``confirmed=True`` only after an explicit ``proceed``.

    The card is built from the composer's PURE extraction (no emitter, no
    solver) so the user confirms the actual derived forcing before the
    composer re-runs the (cache-backed) extraction after approval; the
    confirmed values are deterministic, so card and run cannot diverge.

    An extraction failure here falls through to dispatch (``True``) so the
    composer raises its own typed extraction error -- the gate must not mask
    parameter problems behind a confusing confirm card.

    ``_warning_id_out``: optional out-param -- when given, this function
    stashes the emitted ``warning_id`` under key ``"warning_id"`` the moment
    a REAL gate is sent to the client. It stays unset on every fail-open
    early return (unknown tool, extraction failure, the landcover
    no-coarsening skip) since no gate was emitted there. The caller uses
    this to know whether a proceed/narrow_scope decision is worth memoizing
    into ``state.gate_decisions_this_turn``.
    """
    # #154: the SWMM autoscale result + the localized DEM path are captured here
    # so the decision tail can CAP-CLAMP a user-chosen finer resolution on a
    # narrow_scope override against the REAL build cell count (re-probing the same
    # DEM). Both None for every non-SWMM gated tool (their tail is the existing
    # proceed/cancel).
    swmm_autoscale: Any = None
    swmm_dem_path: str | None = None
    # NATE 2026-06-26: the fetch-resolution gate's suggestion (coarse_default_m /
    # finest_allowed_m / cap) so the decision tail can pin the suggested rung on
    # proceed and floor-clamp a finer narrow_scope rung. None for every non-fetch
    # gated tool (mirrors swmm_autoscale).
    fetch_suggestion: Any = None
    # #154 cadence lever: the resolved flood animation interval (minutes) shown
    # on the card, pinned into the approved params on ``proceed`` so the run uses
    # EXACTLY what the user saw. None for the pluvial path (legacy hourly) and
    # for every non-flood gated tool.
    flood_output_interval_min: float | None = None
    flood_cadence_gated: bool = False
    # The TELEMAC approve-mesh preview stats (real npoin/h/dt from the
    # mesh-only worker run) so the decision tail can pin the previewed edge
    # length on proceed / honour a chosen rung on narrow_scope. None for every
    # non-TELEMAC gated tool.
    telemac_preview: dict | None = None
    # Combined run-settings gate: the flood SFINCS resolution
    # suggestion (bbox-area estimate) so the decision tail can pin the suggested
    # grid_resolution_m on ``proceed`` or honour the user's chosen rung on a
    # ``narrow_scope`` override. None for the pluvial-only path (no bbox) and for
    # every non-flood gated tool.
    flood_grid_autoscale: Any = None
    flood_duration_hr: float = 24.0
    # True only when the flood card actually advertised an override (a
    # GranularitySuggestion and/or a TimeScaleSuggestion) i.e. ``narrow_scope``
    # was offered. A pluvial/no-bbox flood card offers ONLY proceed/cancel, so a
    # narrow_scope reply to it stays fail-closed (the card never offered it).
    flood_override_offered: bool = False
    try:
        if tool_name == "run_model_groundwater_contamination_scenario":
            from .agent.workflows.modflow.model_groundwater_contamination_scenario.model_groundwater_contamination_scenario import (
                _build_confirmation_envelope,
                extract_spill_parameters,
            )
            from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs

            article_text = params.get("article_text")
            if not isinstance(article_text, str) or not article_text.strip():
                # source_url path or missing text: let the composer surface
                # its own typed error (v0.1 live path supplies article_text).
                return True, params
            # extract_spill_parameters is synchronous (pure extraction +
            # cached geocode); off the event loop so the WS heartbeat lives.
            derived = await asyncio.to_thread(
                extract_spill_parameters, article_text, geocode=True
            )
            kwargs: dict[str, Any] = dict(
                spill_location_latlon=derived["spill_location_latlon"],
                contaminant=derived["contaminant"],
                release_rate_kg_s=derived["release_rate_kg_s"],
                duration_days=derived["duration_days"],
            )
            if params.get("aquifer_k_ms") is not None:
                kwargs["aquifer_k_ms"] = float(params["aquifer_k_ms"])
            if params.get("porosity") is not None:
                kwargs["porosity"] = float(params["porosity"])
            envelope = _build_confirmation_envelope(
                derived, MODFLOWRunArgs(**kwargs)
            )
        elif tool_name == "sfincs_flood":
            # A SFINCS solve can take ~10-20 min -- show the user what is
            # about to run before dispatch. The card carries BOTH a
            # GranularitySuggestion (grid resolution) and a
            # TimeScaleSuggestion (cadence + window) so the user reviews
            # both in one interaction, resolved off the loop in the helper.
            # Coastal/wave gets a fine minute-scale stride + time-scale row;
            # pluvial degrades to the granularity-only resolution gate.
            (
                envelope,
                flood_grid_autoscale,
                flood_output_interval_min,
                flood_duration_hr,
            ) = await _build_flood_run_settings_envelope(tool_name, params)
            flood_cadence_gated = True
            # narrow_scope is offered iff the card carried an override block.
            flood_override_offered = "narrow_scope" in envelope.options
        elif tool_name == "telemac_river_dye":
            # Approve-mesh gate: the builder runs the FAST mesh-only
            # worker and emits the wireframe preview layer BEFORE the card, so
            # the user approves a mesh they can SEE (with real node counts +
            # the CFL-coupled dt on the card). ~10-25 s pre-card.
            envelope, telemac_preview = await _build_telemac_mesh_envelope(
                params, emitter=state.emitter
            )
        elif tool_name == "swmm_urban_flood":
            # #154 granularity gate: make mesh resolution a USER
            # lever (memory: feedback_user_controlled_granularity). The enriched
            # card carries a GranularitySuggestion the user can override before
            # the heavy solve. The DEM read + autoscale arithmetic is offloaded
            # off the event loop inside the helper (no-sync-blocking norm).
            (
                envelope,
                swmm_autoscale,
                swmm_dem_path,
            ) = await _build_swmm_granularity_envelope(params)
        elif tool_name in FETCH_CONFIRM_TOOLS:
            # NATE 2026-06-26: fetch-resolution gate for the heavy raster fetchers
            # (fetch_dem / fetch_topobathy). The card carries a GranularitySuggestion
            # (resolution_param="resolution_m") the user can override; the build is
            # PURE arithmetic (no DEM read) so nothing is offloaded. fetch_suggestion
            # carries coarse_default_m / finest_allowed_m / cap for the decision tail.
            (
                envelope,
                fetch_suggestion,
            ) = await _build_fetch_resolution_envelope(tool_name, params)
            # fetch_landcover-only: skip the card when NO coarsening is needed
            # (small AOI: the suggested rung IS the native 30 m grid and the
            # caller did not request finer). NLCD has no finer-than-native knob,
            # so a confirm on a small bbox would be pure friction; the gate
            # exists to surface AUTO-COARSENING (state-scale AOIs) for user
            # override. fetch_dem/fetch_topobathy keep gating every call
            # (they have real finer/coarser rungs at any AOI size).
            if (
                tool_name == "fetch_landcover"
                and envelope.granularity is not None
                and envelope.granularity.suggested_resolution_m
                <= _LANDCOVER_DEFAULT_RES_M + 1e-9
            ):
                return True, params
        elif tool_name == "openquake_psha":
            # OpenQuake classical-PSHA solver-confirm card: SIMPLE proceed/
            # cancel (no granularity/resolution picker) since the deck builds
            # an area source over the whole bbox/AOI. The card summarizes the
            # PSHA (AOI area, IMT, PoE -> return period) so the user confirms
            # the heavy Batch run; built inline since no composer extraction
            # is needed (the run args are the tool args).
            envelope = _build_psha_confirm_envelope(params)
        elif tool_name == "elmfire_fire_spread":
            # ELMFIRE fire-spread solver-confirm card: simple proceed/cancel
            # with the approximate cell count + calibrated runtime estimate +
            # scenario weather, built inline (pure arithmetic, no fetch/
            # rasterio). A missing ignition point deliberately falls through
            # to the tool's typed FIRE_IGNITION_REQUIRED error after approval.
            envelope = _build_fire_confirm_envelope(params)
        elif tool_name == "geoclaw_inundation":
            # GeoClaw shallow-water inundation solver-confirm card: simple
            # proceed/cancel with the approximate AOI area + scenario +
            # simulated window + AMR levels, built inline (pure arithmetic,
            # no fetch/rasterio). A missing bbox deliberately falls through
            # to the tool's typed GEOCLAW_PARAMS_INCOMPLETE error after
            # approval.
            envelope = _build_geoclaw_confirm_envelope(params)
        else:  # unknown gated tool: fail open to the tool's own validation
            return True, params
    except Exception:  # noqa: BLE001 -- never mask param errors with a gate
        logger.warning(
            "solver-confirm gate could not build the confirm card for %s; "
            "falling through so the tool raises its typed error",
            tool_name,
            exc_info=True,
        )
        return True, params

    warning_id = envelope.warning_id
    if _warning_id_out is not None:
        # A real gate is about to be sent - the caller may memoize whatever
        # decision comes back (proceed/narrow_scope only; a cancel raises
        # before the caller's write site is reached).
        _warning_id_out["warning_id"] = warning_id
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(state.session_id, warning_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("tool-payload-warning", state.session_id, envelope)
    )
    logger.info(
        "solver-confirm gate emitted session=%s tool=%s warning_id=%s "
        "contaminant=%r location=%r",
        state.session_id,
        tool_name,
        warning_id,
        envelope.tool_args.get("contaminant"),
        envelope.tool_args.get("location_name"),
    )

    try:
        decision_payload: PayloadConfirmationEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(CODE_EXEC_CONFIRM_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        logger.warning(
            "solver-confirm gate timeout session=%s tool=%s warning_id=%s",
            state.session_id,
            tool_name,
            warning_id,
        )
        await _send_error(
            websocket,
            state.session_id,
            "CONFIRMATION_TIMEOUT",
            f"{tool_name} parameter-confirmation gate timed out; "
            "the solver did not run",
        )
        return False, params
    finally:
        _pop_pending_confirmation(warning_id)

    logger.info(
        "solver-confirm decision session=%s tool=%s warning_id=%s decision=%s",
        state.session_id,
        tool_name,
        warning_id,
        decision_payload.decision,
    )

    if decision_payload.decision == "cancel":
        # Explicit cancel: fail-closed (no solve), existing behavior.
        await _send_error(
            websocket,
            state.session_id,
            "USER_INPUT_CANCELLED",
            f"{tool_name} declined by user "
            f"(decision={decision_payload.decision!r}); the solver did not run",
        )
        return False, params

    if decision_payload.decision == "narrow_scope":
        # NATE 2026-06-26: fetch-resolution override. Honour the chosen
        # resolution_m, floored UP to finest_allowed_m so a finer rung on a huge
        # AOI stays bounded (finer = smaller metres). No confirmed/enable_autoscale
        # injection (fetchers do not read them). Returned BEFORE the SWMM
        # fail-closed check below so a fetch never falls through to it.
        if fetch_suggestion is not None:
            revised = decision_payload.revised_args or {}
            try:
                chosen = float(
                    revised.get("resolution_m", fetch_suggestion.coarse_default_m)
                )
            except (TypeError, ValueError):
                chosen = float(fetch_suggestion.coarse_default_m)
            clamped = _clamp_fetch_resolution(
                chosen, fetch_suggestion.finest_allowed_m
            )
            approved = dict(params)
            approved["resolution_m"] = int(clamped)
            logger.info(
                "fetch-resolution narrow_scope session=%s warning_id=%s "
                "tool=%s chosen=%.2f finest_allowed=%.2f applied=%d",
                state.session_id,
                warning_id,
                tool_name,
                chosen,
                fetch_suggestion.finest_allowed_m,
                approved["resolution_m"],
            )
            return True, approved

        # The flood gate advertises a GranularitySuggestion (grid_resolution_m)
        # AND a TimeScaleSuggestion (output_interval_min / duration_hr); the
        # user can override either (or both) in one revised_args dict, pinning
        # whatever changed and falling back to the suggested value otherwise.
        # The flood resolution is a bbox-area ESTIMATE (the real DEM autoscale
        # re-runs at build time), so the chosen rung is honoured directly
        # without a re-probe -- distinct from the SWMM real-cap-clamp path
        # below. Only honoured when an override was actually advertised (a
        # pluvial/no-bbox flood card offers only proceed/cancel).
        if flood_cadence_gated and flood_override_offered:
            revised = decision_payload.revised_args or {}
            approved = dict(params)
            approved["confirmed"] = True
            # Resolution override: honour the chosen grid_resolution_m; pin the
            # suggested rung when the user left it alone (so the build matches the
            # card). enable_autoscale=False so the builder honours the explicit
            # value rather than re-deriving its own.
            if flood_grid_autoscale is not None:
                try:
                    chosen_grid_res = float(
                        revised.get(
                            "grid_resolution_m",
                            flood_grid_autoscale.grid_resolution_m,
                        )
                    )
                except (TypeError, ValueError):
                    chosen_grid_res = float(flood_grid_autoscale.grid_resolution_m)
                if chosen_grid_res > 0:
                    approved["grid_resolution_m"] = chosen_grid_res
                    approved["enable_autoscale"] = False
            # Cadence override: honour the chosen output_interval_min (floored at
            # 1 min, matching the deck floor); pin the resolved cadence the card
            # showed when the user left it alone (coastal only -- None on pluvial
            # leaves the legacy hourly default untouched).
            chosen_interval: float | None = flood_output_interval_min
            if "output_interval_min" in revised and revised["output_interval_min"] is not None:
                try:
                    chosen_interval = max(1.0, float(revised["output_interval_min"]))
                except (TypeError, ValueError):
                    chosen_interval = flood_output_interval_min
            if chosen_interval is not None:
                approved["output_interval_min"] = float(chosen_interval)
            # Window/duration override: honour an edited simulation window
            # (positive hours only); leave params untouched otherwise.
            if "duration_hr" in revised and revised["duration_hr"] is not None:
                try:
                    chosen_duration = float(revised["duration_hr"])
                    if chosen_duration > 0:
                        approved["duration_hr"] = chosen_duration
                except (TypeError, ValueError):
                    pass
            logger.info(
                "flood run-settings narrow_scope session=%s warning_id=%s "
                "grid_res=%r output_interval_min=%r duration_hr=%r",
                state.session_id,
                warning_id,
                approved.get("grid_resolution_m"),
                approved.get("output_interval_min"),
                approved.get("duration_hr"),
            )
            return True, approved

        # TELEMAC approve-mesh override: honour the chosen edge length.
        # The composer's own override path (suggest_mesh_size_m override_m)
        # budget-clamps a reckless value and suggest_time_step_s re-couples the
        # CFL dt automatically, so no server-side clamp is needed here.
        if telemac_preview is not None:
            revised = decision_payload.revised_args or {}
            try:
                chosen_h = float(
                    revised.get("mesh_resolution_m", telemac_preview["mesh_size_m"])
                )
            except (TypeError, ValueError):
                chosen_h = float(telemac_preview["mesh_size_m"])
            approved = dict(params)
            approved["confirmed"] = True
            approved["mesh_resolution_m"] = chosen_h
            # Pin whether the ORIGINAL call carried plausible release coords
            # (the builder recorded it) BEFORE the click override below --
            # call-provided coords seed the reach (the preview already meshed
            # from them); a gate-picked click must only move the SOURCE (the
            # approved solve reproduces the previewed mesh, never relocates it).
            approved["_release_seeds_reach"] = bool(
                telemac_preview.get("release_seeds_reach")
            )
            # Decouple: the loop below overwrites release_lon/
            # release_lat with the gate click, so preserve the ORIGINAL call
            # coords the preview seeded the reach from as separate seed keys -
            # the approved solve re-seeds from THESE (reproducing the
            # previewed mesh) while the click only moves the source.
            _seed_pair = telemac_preview.get("release_seed_pair")
            if approved["_release_seeds_reach"] and _seed_pair is not None:
                approved["_seed_release_lon"] = float(_seed_pair[0])
                approved["_seed_release_lat"] = float(_seed_pair[1])
            # The user-picked release point rides revised_args.
            for _rk in ("release_lon", "release_lat"):
                if revised.get(_rk) is not None:
                    try:
                        approved[_rk] = float(revised[_rk])
                    except (TypeError, ValueError):
                        pass
            logger.info(
                "telemac approve-mesh narrow_scope session=%s warning_id=%s "
                "previewed_h=%.3g chosen_h=%.3g",
                state.session_id,
                warning_id,
                float(telemac_preview["mesh_size_m"]),
                chosen_h,
            )
            return True, approved

        # #154 granularity override: ONLY meaningful for the SWMM gate (it
        # advertised a GranularitySuggestion). For any other gated solver a
        # narrow_scope reply is meaningless -> fail-closed (existing behavior).
        if swmm_autoscale is None:
            await _send_error(
                websocket,
                state.session_id,
                "USER_INPUT_CANCELLED",
                f"{tool_name} declined by user "
                f"(decision={decision_payload.decision!r}); the solver did not run",
            )
            return False, params

        revised = decision_payload.revised_args or {}
        requested_res = float(
            swmm_autoscale.base_resolution_m
        )
        try:
            chosen_res = float(
                revised.get("target_resolution_m", swmm_autoscale.resolution_m)
            )
        except (TypeError, ValueError):
            chosen_res = float(swmm_autoscale.resolution_m)

        # CAP-CLAMP against the REAL build cell count, not the area model: the
        # area-model clamp (cells = base_cells*(base/res)**2) undershoots the
        # real ceil(extent/res) grid count build_swmm_mesh actually counts (it
        # re-reads the DEM at the clamped resolution with enable_autoscale=False
        # and does no downstream cap re-check), so an over-fine override could
        # solve over the cap. clamp_swmm_resolution_to_real_cap re-probes the
        # same localized DEM at the SWMM ladder rungs and returns the finest
        # rung whose real active-cell count fits the cap (off the event loop).
        # If the real probe is unavailable or fails, fall back to the
        # area-model clamp so the gate never blocks/orphans the override.
        clamped_res = chosen_res
        clamped = False
        used_real_clamp = False
        if swmm_dem_path:
            try:
                from .agent.workflows.swmm.swmm_mesh_builder import (
                    clamp_swmm_resolution_to_real_cap,
                )

                cap = int(swmm_autoscale.cell_cap)
                real_clamp = await asyncio.to_thread(
                    clamp_swmm_resolution_to_real_cap,
                    swmm_dem_path,
                    chosen_res,
                    cell_cap=cap,
                )
                clamped_res = float(real_clamp.resolution_m)
                clamped = bool(real_clamp.clamped)
                used_real_clamp = True
                logger.info(
                    "swmm granularity narrow_scope (REAL-cap) session=%s "
                    "warning_id=%s chosen_res=%.2f built_res=%.2f real_active=%d "
                    "clamped=%s cell_cap=%d",
                    state.session_id,
                    warning_id,
                    chosen_res,
                    clamped_res,
                    real_clamp.real_active_cells,
                    clamped,
                    cap,
                )
            except Exception:  # noqa: BLE001 -- never orphan the override on a probe fail
                logger.warning(
                    "swmm granularity narrow_scope: REAL-cap clamp probe failed "
                    "for session=%s; falling back to the area-model clamp",
                    state.session_id,
                    exc_info=True,
                )
        if not used_real_clamp:
            clamped_res, clamped = _clamp_swmm_resolution_to_cap(
                chosen_res, swmm_autoscale, requested_res
            )
            logger.info(
                "swmm granularity narrow_scope (area-model fallback) session=%s "
                "warning_id=%s chosen_res=%.2f clamped_res=%.2f clamped=%s "
                "cell_cap=%d",
                state.session_id,
                warning_id,
                chosen_res,
                clamped_res,
                clamped,
                swmm_autoscale.cell_cap,
            )

        approved = dict(params)
        approved["confirmed"] = True
        approved["target_resolution_m"] = clamped_res
        # The user pinned an EXPLICIT resolution -> disable the autoscaler so the
        # builder honours the chosen (already-real-cap-clamped) rung exactly.
        approved["enable_autoscale"] = False
        if clamped:
            approved["_granularity_clamped"] = True
        return True, approved

    # NATE 2026-06-26: fetch proceed -- pin the SUGGESTED resolution_m the card
    # showed so the fetch matches what the user approved. Do NOT inject confirmed
    # / enable_autoscale (fetchers do not read them). Returned BEFORE the solver
    # proceed pinning below so a fetch never sets confirmed.
    if fetch_suggestion is not None:
        approved = dict(params)
        approved["resolution_m"] = int(fetch_suggestion.coarse_default_m)
        return True, approved

    # proceed: pin the SUGGESTED resolution for SWMM (so the build matches the
    # card the user approved) and inject confirmed. Other solvers just confirm.
    approved = dict(params)
    approved["confirmed"] = True
    # Pin the PREVIEWED edge length so the solve mesh is byte-for-byte
    # the mesh the user saw + approved (same seed via cache-backed geocode/river
    # fetch + same explicit h -> gmsh reproduces the mesh).
    if telemac_preview is not None:
        approved["mesh_resolution_m"] = float(telemac_preview["mesh_size_m"])
        # 2026-07-18: on plain proceed the release coords (if any) are still
        # the call-provided originals - pin the same tri-state the preview
        # used so the solve re-seeds the reach exactly like the preview did.
        approved["_release_seeds_reach"] = bool(
            telemac_preview.get("release_seeds_reach")
        )
    if swmm_autoscale is not None:
        approved["target_resolution_m"] = float(swmm_autoscale.resolution_m)
        approved["enable_autoscale"] = False
    # Combined run-settings gate: pin the SUGGESTED SFINCS grid
    # resolution the card showed so the run matches the card the user approved
    # (bbox-area estimate; enable_autoscale stays default so the real DEM
    # autoscale still refines it at build time -- pinning the rung only sets the
    # ladder start). None when the gate had no bbox.
    if flood_cadence_gated and flood_grid_autoscale is not None:
        approved["grid_resolution_m"] = float(
            flood_grid_autoscale.grid_resolution_m
        )
    # #154 cadence lever: pin the resolved flood animation interval the card
    # showed so the run emits exactly the "N frames every M min" the user
    # approved (coastal/wave only; None on the pluvial path leaves the legacy
    # hourly default untouched -> unchanged pluvial behavior).
    if flood_cadence_gated and flood_output_interval_min is not None:
        approved["output_interval_min"] = float(flood_output_interval_min)
    return True, approved


async def _gate_with_turn_memory(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
) -> tuple[bool, dict]:
    """``_gate_on_solver_confirm`` wrapped with per-turn decision memory.

    Checks ``state.gate_decisions_this_turn`` (keyed by
    :func:`_gate_memory_key`) BEFORE calling the real gate. A remembered
    "proceed" / "narrow_scope" decision from earlier in the SAME turn is
    auto-applied (its recorded param DELTA is merged onto the current call's
    params) and no new gate is emitted. A "cancel" is never recorded (the
    real gate raises via ``should_run=False`` before this wrapper's write
    site), so a corrected retry after a cancel still gates fresh. A
    DIFFERENT tool or a DIFFERENT bbox in the same turn always gates
    normally (different key -> memory miss).
    """
    gate_key = _gate_memory_key(tool_name, params)
    remembered = state.gate_decisions_this_turn.get(gate_key)
    if remembered is not None:
        merged = {**params, **remembered["overrides"]}
        logger.info(
            "solver-confirm gate auto-applied from turn memory "
            "session=%s tool=%s warning_id_prior=%s",
            state.session_id,
            tool_name,
            remembered["warning_id"],
        )
        return True, merged

    pre_gate_params = dict(params)
    warning_id_box: dict[str, str] = {}
    should_run, approved = await _gate_on_solver_confirm(
        websocket, state, tool_name, params, _warning_id_out=warning_id_box
    )
    if not should_run:
        return False, approved

    prior_warning_id = warning_id_box.get("warning_id")
    if prior_warning_id is not None:
        # A real gate was sent and answered proceed/narrow_scope (a cancel
        # returns should_run=False above and is never memoized, so a
        # corrected retry after a cancel still gates fresh). Remember only
        # the DELTA the gate applied to params - not the whole approved
        # dict - so a later retry keeps ITS OWN corrected non-bbox args
        # (e.g. a fixed `dataset`) and only inherits what the gate itself
        # decided (e.g. `resolution_m`).
        overrides = {
            k: v
            for k, v in approved.items()
            if k not in pre_gate_params or pre_gate_params[k] != v
        }
        state.gate_decisions_this_turn[gate_key] = {
            "overrides": overrides,
            "warning_id": prior_warning_id,
        }
    return True, approved


def _ensure_emitter(websocket: ServerConnection, state: SessionState) -> None:
    """Bind a ``PipelineEmitter`` to this session if one isn't already.

    The emitter's sink is the WebSocket ``send`` -- every transition method
    writes one envelope on the wire (Appendix A.7 replace-not-reconcile)."""
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
        # generate_histogram chart. Best-effort inside _persist_chart_record.
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


# --------------------------------------------------------------------------- #
# Credential pipeline (job VAULT-READ): secret_ref injection + auth-error ->
# credential-request -> retry.
# --------------------------------------------------------------------------- #


async def _resolve_active_secret_ref(
    state: SessionState, tool_name: str, case_id: str | None
) -> Any | None:
    """Return the user's active ``SecretRecord`` for ``tool_name``'s provider.

    Looks up the per-Case secret first (scoped to the turn's Case) then falls
    back to user-level secrets, filtering by the provider the tool needs
    (``credential_registry.provider_for_tool``). Returns the freshest active
    record or ``None`` when the tool is not keyed, no Persistence is bound, or
    no matching active secret exists.

    Best-effort: a Persistence/MCP wobble logs and returns ``None`` so the tool
    falls back to its env path / typed auth-error (which the credential-request
    flow then acts on) -- a vault lookup hiccup must not crash the dispatch.
    """
    provider = provider_for_tool(tool_name)
    if provider is None:
        return None
    p = get_persistence()
    if p is None:
        return None
    user_id = state.authenticated_user_id or state.session_id
    try:
        # Prefer Case-scoped secrets; fall back to user-level (case_id=None)
        # records so a key the user added outside a Case still resolves.
        records = []
        if case_id:
            records = await p.list_secrets_refs(user_id=user_id, case_id=case_id)
        if not records:
            records = await p.list_secrets_refs(user_id=user_id, case_id=None)
    except Exception:  # noqa: BLE001 -- vault lookup is best-effort
        logger.debug(
            "secret_ref lookup failed tool=%s case=%s", tool_name, case_id,
            exc_info=True,
        )
        return None
    # Filter to the tool's provider. ``provider_id`` on the registry may carry a
    # value not yet in the ``ProviderID`` Literal (FIRMS pre-amendment); match
    # the SecretRecord.provider string directly.
    matches = [
        r for r in records
        if getattr(r, "provider", None) == provider.provider_id and r.is_active
    ]
    if not matches:
        return None
    # Freshest by added_at (records are SecretRecords with UTC added_at).
    matches.sort(key=lambda r: getattr(r, "added_at", None) or "", reverse=True)
    return matches[0]


async def _inject_secret_ref(
    state: SessionState,
    tool_name: str,
    params: dict,
    case_id: str | None,
) -> dict:
    """Thread the user's active per-Case ``secret_ref`` into a keyed tool's params.

    No-op for non-keyed tools, when the caller already supplied an explicit
    ``secret_ref`` / key kwarg, or when no active secret exists. The tool's
    ``_resolve_*_key`` then reads the VAULT key first (then env), per the
    eBird secret_ref convention.
    """
    if provider_for_tool(tool_name) is None:
        return params
    # Respect an explicit override already on params (dev/test path).
    if params.get("secret_ref") is not None:
        return params
    record = await _resolve_active_secret_ref(state, tool_name, case_id)
    if record is None:
        return params
    params = dict(params)
    params["secret_ref"] = record
    logger.info(
        "secret_ref injected tool=%s provider=%s secret_id=%s",
        tool_name,
        getattr(record, "provider", None),
        getattr(record, "secret_id", None),
    )
    return params


async def _maybe_handle_credential_error(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    error: BaseException,
    case_id: str | None,
) -> dict | None:
    """Handle a keyed-tool credential error: prompt + await + re-resolve.

    Returns:
    - ``dict`` (retry params with a freshly-resolved ``secret_ref``) when the
      user supplied a key (``credential-provided`` with ``provided=True``) --
      the caller retries the tool ONCE.
    - ``None`` when the error is NOT credential-shaped, the tool already
      prompted this turn (one-prompt-per-tool-per-turn guard), or the user
      declined / the gate timed out. The caller then re-raises the original
      error so it flows through the normal typed-error surface (FR-AS-11) and
      the LLM narrates the failure honestly.

    Two paths:
    1. REGISTERED tool (``provider_for_tool`` resolves): emit the real
       per-provider card (real ``signup_url`` from the registry -- the ONLY
       source of real URLs) and, on provided=True, re-resolve the per-Case
       ``secret_ref`` so the retry reads the saved key.
    2. UNREGISTERED tool with a credential-SHAPED error (NATE principle 3,
       2026-06-18): emit a NAME-ONLY generic card (credential name derived from
       the tool, ``signup_url=None``, just the secret-entry form) so the user
       still gets a card and the agent NEVER narrates a fabricated URL. On
       provided=True we retry once with the original params (the tool reads its
       own key path); there is no per-Case ``secret_ref`` to inject for an
       unregistered provider.
    """
    provider = provider_for_tool(tool_name)
    is_registered_credential = (
        provider is not None and is_credential_error(tool_name, error)
    )
    is_generic_credential = (
        provider is None and is_credential_shaped_error(tool_name, error)
    )
    if not is_registered_credential and not is_generic_credential:
        return None

    # One prompt per tool per turn -- don't loop forever on a still-bad key.
    if tool_name in state.credential_prompted_tools:
        logger.info(
            "credential-request suppressed (already prompted this turn) tool=%s",
            tool_name,
        )
        return None

    if is_generic_credential:
        # NATE principle 3: NAME-ONLY card for a tool with no registered
        # provider. ``generic_provider_for_tool`` derives a human credential
        # name and pins ``signup_url=None`` (NO fabricated URL). The emit is
        # best-effort: if the generic ``provider_id`` is not yet a valid wire
        # ``ProviderID`` (schema-owned Literal), ``_emit_credential_request_and_wait``
        # → ``_build_credential_request_payload`` returns None and we surface
        # the original typed error instead -- we still NEVER invent a URL.
        generic_provider = generic_provider_for_tool(tool_name)
        state.credential_prompted_tools.add(tool_name)
        logger.info(
            "credential-request (generic name-only) tool=%s label=%r "
            "signup_url=None — no registered provider",
            tool_name,
            generic_provider.label,
        )
        provided = await _emit_credential_request_and_wait(
            websocket, state, tool_name, generic_provider, error
        )
        if provided is None or not provided.provided:
            return None
        # Unregistered provider: no per-Case secret_ref to inject. Retry once
        # with the original params (minus any stale inline key) so the tool can
        # pick up a key from its own resolution path.
        return {
            k: v for k, v in params.items()
            if k not in ("secret_ref", "map_key", "api_key")
        }

    # REGISTERED path: real per-provider card with a real signup_url.
    assert provider is not None  # narrowed by is_registered_credential
    state.credential_prompted_tools.add(tool_name)

    provided = await _emit_credential_request_and_wait(
        websocket, state, tool_name, provider, error
    )
    if provided is None or not provided.provided:
        # Declined / timed out: surface the original typed error.
        return None

    # Key saved to the vault: re-resolve the secret_ref so the retry reads the
    # NEW key. Strip any stale secret_ref/map_key from params first.
    retry_params = {
        k: v for k, v in params.items()
        if k not in ("secret_ref", "map_key", "api_key")
    }
    retry_params = await _inject_secret_ref(
        state, tool_name, retry_params, case_id
    )
    return retry_params


async def _emit_credential_request_and_wait(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    provider: CredentialProvider,
    error: BaseException,
) -> "CredentialProvidedEnvelopePayload | None":
    """Emit a ``credential-request`` envelope and await ``credential-provided``.

    Blocks on a future keyed by the minted ``request_id`` (registered in the
    session-scoped ``_PENDING_CREDENTIALS`` registry so a reply on a sibling
    connection still resolves it). Returns the ``CredentialProvidedEnvelopePayload``
    on reply, or ``None`` on timeout (the gate gets the same 300s read-decision
    TTL as the payload-warning / code-exec gates -- fail-open to the original
    typed error so the turn is not hung).
    """
    request_id = new_ulid()
    # Prefer the tool's typed-error message (honest, specific) over the
    # registry default; both name that a key is needed (no silent dead-end).
    err_detail = str(error).strip()
    message = provider.default_message
    if err_detail:
        message = f"{provider.default_message} ({err_detail[:400]})"

    # Build the envelope scoped to the REAL provider (every registered
    # provider_id is now a valid ``ProviderID`` Literal member). If validation
    # fails for an unregistered provider, ``_build_credential_request_payload``
    # returns ``None`` -- we abandon the prompt rather than mis-scope the
    # secret-add (which would save the key where the retry can't re-resolve it).
    # The caller then surfaces the original typed error (honest narration).
    payload = _build_credential_request_payload(
        request_id=request_id,
        provider=provider,
        tool_name=tool_name,
        message=message,
    )
    if payload is None:
        return None

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_credential(state.session_id, request_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("credential-request", state.session_id, payload)
    )
    logger.info(
        "credential-request emitted session=%s tool=%s provider=%s request_id=%s",
        state.session_id,
        tool_name,
        provider.provider_id,
        request_id,
    )

    try:
        provided: CredentialProvidedEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(CODE_EXEC_CONFIRM_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        logger.warning(
            "credential-request timeout session=%s tool=%s request_id=%s",
            state.session_id,
            tool_name,
            request_id,
        )
        return None
    finally:
        _pop_pending_credential(request_id)

    logger.info(
        "credential-provided received session=%s tool=%s request_id=%s provided=%s",
        state.session_id,
        tool_name,
        request_id,
        provided.provided,
    )
    return provided


async def _emit_region_choice_and_wait(
    websocket: ServerConnection,
    state: SessionState,
    payload: "RegionChoiceRequestEnvelopePayload",
) -> "RegionChoiceProvidedEnvelopePayload | None":
    """Emit a ``region-choice-request`` and await ``region-choice-provided``.

    Blocks on a future keyed by ``payload.request_id`` (registered in the
    session-scoped ``_PENDING_REGION_CHOICES`` registry so a reply on a sibling
    connection still resolves it). Returns the ``RegionChoiceProvidedEnvelopePayload``
    on reply, or ``None`` on timeout (the gate gets the same read-decision TTL
    as the credential / payload-warning / code-exec gates -- fail-open to the
    whole-state default so the turn is never hung).
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_region_choice(state.session_id, payload.request_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("region-choice-request", state.session_id, payload)
    )
    logger.info(
        "region-choice-request emitted session=%s state=%s candidates=%d request_id=%s",
        state.session_id,
        payload.state_code,
        len(payload.candidates),
        payload.request_id,
    )

    try:
        provided: RegionChoiceProvidedEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(CODE_EXEC_CONFIRM_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        logger.info(
            "region-choice-request timeout session=%s request_id=%s; "
            "using whole-state default",
            state.session_id,
            payload.request_id,
        )
        return None
    finally:
        _pop_pending_region_choice(payload.request_id)

    logger.info(
        "region-choice-provided received session=%s request_id=%s choice=%s",
        state.session_id,
        payload.request_id,
        provided.choice,
    )
    return provided


async def _maybe_handle_region_choice(
    websocket: ServerConnection,
    state: SessionState,
    geocode_result: dict,
) -> None:
    """If ``geocode_result`` is a state-snap, offer + apply a narrower region.

    No-op unless the geocode came back as a state-bbox-fallback
    (``source == "state-bbox-fallback"``). When it did, this:

    1. Builds the candidate sub-regions (default: counties of the state) and
       emits a ``region-choice-request`` (whole-state default + candidates +
       an honest prompt).
    2. PAUSES the turn awaiting ``region-choice-provided`` (fail-open: a
       headless client / timeout keeps the whole-state bbox).
    3. On ``choice == "region"`` MUTATES ``geocode_result`` in place to the
       picked region's bbox (re-resolved by ``selected_region_id`` against
       the candidate set -- authoritative over a client-sent bbox; falls
       back to ``selected_bbox`` only when the id is unknown) and stamps
       narrowing provenance so downstream tools + the function_response
       Gemini reads use the narrowed extent. On ``choice == "whole_state"``
       leaves the state bbox unchanged.

    Best-effort: any failure leaves the whole-state bbox intact -- the
    narrowing is a UX nicety layered ON TOP of an already-correct result, so
    it must never break the turn. Never raises.
    """
    if geocode_result.get("source") != "state-bbox-fallback":
        return
    if state.emitter is None:
        # No interactive surface bound; keep the whole-state default.
        return
    try:
        request_id = new_ulid()
        payload = _build_region_choice_request_payload(
            request_id=request_id, geocode_result=geocode_result
        )
        if payload is None:
            return
        provided = await _emit_region_choice_and_wait(websocket, state, payload)
        if provided is None or provided.choice == "whole_state":
            # Declined / timed out / explicit whole-state -- keep the state bbox.
            geocode_result["region_choice"] = "whole_state"
            return
        # choice == "region": resolve the picked candidate. Prefer re-resolving
        # by region_id against the candidate set (a tampered client bbox cannot
        # redirect the workflow); fall back to the echoed bbox only if unknown.
        chosen = None
        if provided.selected_region_id:
            chosen = next(
                (
                    c
                    for c in payload.candidates
                    if c.region_id == provided.selected_region_id
                ),
                None,
            )
        new_bbox: tuple[float, float, float, float] | None = None
        chosen_name: str | None = None
        if chosen is not None:
            new_bbox = chosen.bbox
            chosen_name = chosen.name
        elif provided.selected_bbox is not None:
            new_bbox = provided.selected_bbox
        if new_bbox is None:
            # The client said "region" but supplied neither a known id nor a
            # bbox -- keep the state default rather than guess.
            geocode_result["region_choice"] = "whole_state"
            return
        # Mutate the geocode result IN PLACE so the immediate zoom-to AND the
        # function_response Gemini reads (and any downstream bbox consumer) use
        # the narrowed extent.
        geocode_result["bbox"] = list(new_bbox)
        # The result is no longer a whole-state snap -- drop the fallback source
        # so a downstream re-trigger does not re-offer the picker, and record
        # honest provenance of the narrowing.
        geocode_result["source"] = "region-choice-narrowed"
        geocode_result["region_choice"] = "region"
        geocode_result["selected_region_id"] = provided.selected_region_id
        if chosen_name:
            geocode_result["name"] = chosen_name
            geocode_result["region_name"] = chosen_name
        # Recompute a rough centroid for the narrowed bbox so map snaps + any
        # centroid consumer stay consistent with the new extent.
        geocode_result["longitude"] = (new_bbox[0] + new_bbox[2]) / 2.0
        geocode_result["latitude"] = (new_bbox[1] + new_bbox[3]) / 2.0
        logger.info(
            "region-choice: narrowed to region_id=%s name=%r bbox=%s",
            provided.selected_region_id,
            chosen_name,
            new_bbox,
        )
    except Exception:  # noqa: BLE001 -- narrowing is a best-effort UX layer
        logger.warning(
            "region-choice handling failed; keeping whole-state bbox",
            exc_info=True,
        )


# --------------------------------------------------------------------------- #
# FR-AS-10: request_spatial_input -- pause the turn, await the drawn geometry.
# --------------------------------------------------------------------------- #
#
# Mirrors the region-choice pause/resume seam (``_emit_region_choice_and_wait``).
# The LLM-facing ``request_spatial_input`` tool (tools/spatial_input_tool.py)
# returns a sentinel result that this interception in the turn loop replaces with
# the parsed, role-split drawn geometry -- so the tool surface stays catalog-clean
# while the actual websocket pause/resume lives here (where the live socket +
# session future registry are reachable). The drawn barriers FeatureCollection
# round-trips straight into ``swmm_urban_flood(barriers=...)``.

# Sentinel result the ``request_spatial_input`` catalog tool returns; the turn
# loop detects it and replaces it with the real drawn-geometry result.
SPATIAL_INPUT_SENTINEL_KEY = "_request_spatial_input"


async def _emit_spatial_input_and_wait(
    websocket: ServerConnection,
    state: SessionState,
    payload: "SpatialInputRequestPayload",
) -> "SpatialInputResponsePayload | None":
    """Emit a ``spatial-input-request`` and await ``spatial-input-response``.

    Blocks on a future keyed by ``payload.request_id`` (registered in the
    session-scoped ``_PENDING_SPATIAL_INPUTS`` registry so a reply on a sibling
    connection still resolves it -- StrictMode double-mount / reconnect). Returns
    the ``SpatialInputResponsePayload`` on reply, or ``None`` on timeout (the gate
    gets the same read-decision TTL as the credential / region-choice gates --
    fail-open to a typed "no geometry drawn" result, never a hung turn).
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_spatial_input(state.session_id, payload.request_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("spatial-input-request", state.session_id, payload)
    )
    logger.info(
        "spatial-input-request emitted session=%s mode=%s request_id=%s",
        state.session_id,
        payload.mode,
        payload.request_id,
    )

    try:
        response: SpatialInputResponsePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(payload.default_timeout_seconds)
        )
    except asyncio.TimeoutError:
        logger.info(
            "spatial-input-request timeout session=%s request_id=%s; "
            "no geometry drawn",
            state.session_id,
            payload.request_id,
        )
        return None
    except SpatialInputInvalidResponseError:
        # The user's reply ARRIVED but failed structural validation (e.g. a
        # barrier missing barrier_type). The inbound handler failed the future
        # eagerly so we wake here IN-BAND -- NOT after the read TTL. Re-raise so
        # _handle_request_spatial_input surfaces the typed error result.
        logger.info(
            "spatial-input-request invalid-response session=%s request_id=%s; "
            "resolving turn with typed error (not timeout path)",
            state.session_id,
            payload.request_id,
        )
        raise
    finally:
        _pop_pending_spatial_input(payload.request_id)

    logger.info(
        "spatial-input-response received session=%s request_id=%s "
        "cancelled=%s geometry_type=%s",
        state.session_id,
        payload.request_id,
        response.cancelled,
        response.geometry_type,
    )
    return response


async def _handle_request_spatial_input(
    websocket: ServerConnection,
    state: SessionState,
    call_args: dict[str, Any],
) -> dict[str, Any]:
    """Drive one ``request_spatial_input`` turn-pause and return the LLM result.

    Builds the request from the LLM args, emits it, PAUSES the turn awaiting the
    drawn geometry, then parses + role-splits the reply into the engine-ready
    result. Never raises -- every failure path (no client, validation, parse,
    timeout, cancellation) becomes a typed result the LLM narrates honestly. The
    ``role=="barrier"`` features become the ``barriers`` FeatureCollection that
    feeds ``swmm_urban_flood`` -> ``SWMMRunArgs.barriers`` -> the existing
    ``build_swmm_mesh`` wall=omit-conduit / flap_gate=one-way-orifice seam.
    """
    if state.emitter is None:
        # No interactive surface bound (e.g. headless eval). Honest typed error.
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_NO_CLIENT",
            "error_message": (
                "No interactive map client is connected, so the user cannot "
                "draw. Proceed without drawn barriers / AOI, or ask the user to "
                "provide a bbox in text."
            ),
        }
    request_id = new_ulid()
    payload = _build_spatial_input_request_payload(
        request_id=request_id, call_args=call_args
    )
    if payload is None:
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_PARAMS_INVALID",
            "error_message": (
                "Could not build a valid spatial-input request from the given "
                "mode/title/description."
            ),
        }
    try:
        response = await _emit_spatial_input_and_wait(websocket, state, payload)
    except asyncio.CancelledError:
        raise
    except SpatialInputInvalidResponseError as exc:
        # The reply arrived but was structurally invalid (honesty floor: a
        # malformed drawn FeatureCollection -- e.g. a barrier missing
        # barrier_type -- degrades to a TYPED error result, NOT a silent success
        # and NOT a hung turn that drains default_timeout_seconds then reads as
        # SPATIAL_INPUT_TIMEOUT). The user already saw TOOL_PARAMS_INVALID.
        logger.info(
            "spatial-input invalid-response session=%s request_id=%s code=%s",
            state.session_id,
            request_id,
            exc.error_code,
        )
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": (
                f"The drawn geometry could not be used: {exc.error_message}. "
                f"Ask the user to redraw; do not fabricate barriers or an AOI."
            ),
        }
    except Exception:  # noqa: BLE001 -- degrade to a typed result, never crash
        logger.warning(
            "spatial-input emit/wait failed session=%s request_id=%s",
            state.session_id,
            request_id,
            exc_info=True,
        )
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_FAILED",
            "error_message": (
                "The spatial-input request failed unexpectedly; no geometry was "
                "received. Do not fabricate barriers or an AOI."
            ),
        }
    return _spatial_response_to_result(response)


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
#: fetch_nexrad_reflectivity, fetch_field_boundaries) and all non-fetch sync tools
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
#:   fetch_cama_flood_discharge -> NetCDF stream + xarray open + mean + COG write
#:   fetch_gtsm_tide_surge  -> blocking CDS ZIP download + xr.open_mfdataset + per-gauge compute
_ALWAYS_OFFLOAD_SYNC_TOOLS = frozenset(
    {
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
        # satellite-animation loop-block (LIVE 2026-06-25): both of these read the
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
        "fetch_cama_flood_discharge",
        "fetch_gtsm_tide_surge",
        # conservation micro-North-Star: PC STAC raster fetchers that do
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
        # sandbox-staging: code_exec_request now PRE-FETCHES each layer_ref URI
        # (single OR a list of animation frames) from S3 into the per-run sandbox
        # workdir before the jailed executor opens them as local files, then runs
        # the executor subprocess synchronously -- multi-second sync network +
        # subprocess work. Off-load so it never stalls the WS heartbeat
        # (feedback_no_sync_blocking_on_asyncio_loop). The body is emit-free (the
        # confirm card is emitted on the loop by _gate_on_code_exec; server.py
        # emits the result envelope), so the off-load is safe.
        "code_exec_request",
        # list_run_frames reads the run's publish_manifest.json from S3
        # (completion.json -> manifest_uri -> parse) -- sync network I/O. Emit-free
        # (returns the listing dict), so off-load it for the same reason.
        "list_run_frames",
        # tools-work integration (2026-06-27): the new heavy raster/vector
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
        # quick-win batch (2026-07-07): compute_change_detection reads TWO
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
        # compute_urban_heat_island fetches MODIS LST + the 10 m land-cover COG
        # + resamples onto the class grid + writes a COG in one sync call --
        # same off-load rationale; emit-free body.
        "compute_urban_heat_island",
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
    - emits ``pipeline-state`` on every state transition (Appendix A.7),
    - re-emits ``session-state`` whenever the tool returns a ``LayerURI``,
    - propagates ``asyncio.CancelledError`` (Invariant 8) and classifies
      arbitrary exceptions into the open-set A.6 error-code registry.

    Solver dispatch keeps the same shape, yielding ``progress_percent``
    updates through ``emitter.update_progress`` between solver chunks.
    """
    _ensure_emitter(websocket, state)
    if tool_name not in TOOL_REGISTRY:
        # Raises ToolNotFoundError so the existing exception handler routes
        # through summarize_tool_result(error=...), which emits the full
        # structured envelope (error_code + retryable + message) so Gemini
        # can distinguish "tool ran and returned nothing" from "tool name was
        # never registered". function_response IS the signal Gemini reads
        # between turns -- the _send_error side-channel is not needed here.
        # (FR-AS-3, FR-AS-11.)
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
        from .agent.gates.tool_gating import BenchBlockedError, bench_block_decision

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

    # Per-Case ``.qgs`` lazy-init for ``publish_layer``: when invoked inside a
    # Case context we resolve (or initialize) the per-Case ``.qgs`` URI
    # BEFORE the tool body runs, then substitute it into
    # ``project_qgs_uri`` so the worker mutates the case-scoped file rather
    # than the shared default.
    if tool_name == "publish_layer" and turn_case_id:
        try:
            case_qgs = await ensure_case_qgs(
                get_persistence(), turn_case_id
            )
        except CaseLifecycleError as exc:
            logger.warning(
                "case-qgs lazy-init failed code=%s case=%s err=%s; "
                "falling back to default .qgs",
                exc.error_code,
                turn_case_id,
                exc,
            )
        else:
            # Substitute (additively) without clobbering an explicit override.
            params = dict(params)
            params.setdefault("project_qgs_uri", case_qgs)
            params.setdefault("case_id", turn_case_id)
            logger.info(
                "publish_layer routed to case-scoped qgs case=%s qgs=%s",
                turn_case_id,
                case_qgs,
            )

    # Drop ``case_id`` for tools that don't declare it -- defense in depth.
    # ``publish_layer`` accepts it; other tools do not.
    if tool_name != "publish_layer" and "case_id" in params:
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
        # Raises PayloadWarningCancelledError so Gemini sees a structured
        # envelope ({status: "error", error_code:
        # "PAYLOAD_WARNING_CANCELLED", retryable: False}) instead of
        # {"status": "no_result"}, which it cannot interpret. retryable=False
        # because the user explicitly cancelled. (FR-AS-11.)
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
    # raises a typed, non-retryable error so Gemini narrates the decline and
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

    # Centralized kwarg sweep: Gemini routinely invents kwargs that don't
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

    # ADR 0017: bbox AUTO-FILL. A tool whose signature REQUIRES a bbox-like
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

    # job bbox-durability (live-reported, 2026-07): anchor the Case AOI from
    # THIS bbox-carrying fetch's final (already reuse-guard-consulted /
    # AOI-defaulted) params. Runs AFTER both reuse guards above so it never
    # perturbs their read of the PRIOR pin; see _pin_case_aoi_from_tool_bbox
    # for the full root-cause + latest-wins-but-never-shrinks contract.
    await _pin_case_aoi_from_tool_bbox(
        state, case_id=turn_case_id, tool_name=tool_name, params=params
    )

    # Confirmation-before-consequence for solver composers (Invariant 9 /
    # FR-AS-8). The LLM-supplied ``confirmed`` is STRIPPED first -- the gate
    # is server-owned; only an explicit user "proceed" injects it. SKIPPED on
    # a reuse short-circuit (``_ReuseEntry``) -- there is no solver to
    # confirm. The gate also fires for the heavy raster FETCHERS
    # (FETCH_CONFIRM_TOOLS) via the SAME gate, building a fetch-resolution
    # card; ``confirmed`` is stripped only for the solver branch (fetchers do
    # not read it), and ``_gate_on_solver_confirm`` guards the
    # confirmed/enable_autoscale injection to SOLVER_CONFIRM_TOOLS so a
    # fetch's approved params carry resolution_m only. Routed through
    # ``_gate_with_turn_memory`` so a same-tool/same-bbox retry later in this
    # SAME turn replays the earlier proceed/narrow_scope decision instead of
    # hanging on an unanswered second gate.
    if (
        tool_name in (SOLVER_CONFIRM_TOOLS | FETCH_CONFIRM_TOOLS)
        and not isinstance(entry, _ReuseEntry)
    ):
        if tool_name in SOLVER_CONFIRM_TOOLS:
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
    # listing the real handles so Gemini self-corrects without inventing. See
    # uri_registry.py.
    uri_registry = get_uri_registry(state.session_id)
    params = uri_registry.resolve_params(tool_name, params)

    # 2026-07-08 small-model resilience: local 8B models omit publish_layer's
    # layer_id entirely (live TypeError). The tool itself now derives one, but
    # the wrap-site emission below keys off params["layer_id"], so inject the
    # SAME derived id here (post-URI-resolution, so a handle-resolved
    # layer_uri maps back to the producing tool's layer_id) - otherwise the
    # layer would publish without ever being announced to the map.
    if tool_name == "publish_layer" and not params.get("layer_id"):
        _pl_uri = params.get("layer_uri")
        if isinstance(_pl_uri, str) and _pl_uri:
            from .agent.tools.publish_layer.publish_layer import derive_layer_id as _derive_layer_id

            params = dict(params)
            params["layer_id"] = _derive_layer_id(_pl_uri, uri_registry)
            logger.info(
                "publish_layer: layer_id omitted by the model - derived %r "
                "from layer_uri=%r",
                params["layer_id"],
                _pl_uri,
            )

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
        # by COG-identity (TiTiler url= param), NOT by layer_id, so two
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
        # (now-saved) vault key and retry ONCE. One prompt per tool per turn
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
            # Key provided + vault re-resolved: retry the tool ONCE.
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
            # DURABILITY (layer-publish-survives-disconnect, 2026-06-23): run the
            # layer persist UNDER A SHIELD so a cancellation of the (possibly
            # detached) turn cannot interrupt the DynamoDB write of a fully-
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
            # Lane A1: re-materialize the full case view to S3 right after the
            # layer accumulator is persisted, while the emitter still holds the
            # in-memory inline vector GeoJSON (the only source of it). A layer
            # publish is the mutation that most needs the cold-view refresh.
            #
            # COLDVIEW DURABILITY (J1): AWAIT the snapshot + manifest writes here
            # instead of detaching them. A layer publish is exactly the mutation
            # whose cold-refresh must be DURABLE before the turn returns -- the
            # prior fire-and-forget detach raced daemon shutdown: the process
            # could stop AFTER the turn returned but BEFORE the detached PUT
            # landed, leaving case-views/{case_id}.json at its prior (empty)
            # contents (the "daemon-down case open shows no layers" bug). We
            # still keep the store round-trips + S3 PUT OFF the asyncio loop
            # (the no-sync-blocking norm): the persist coroutines are already
            # async and run blocking I/O via asyncio.to_thread internally, so
            # awaiting them does not pin the loop. Both swallow their own errors
            # (return False / never raise), so the await never breaks the
            # dispatch.
            #
            # DURABILITY: the snapshot + manifest are the COLD-view faces of the
            # same just-persisted layer -- they take the SAME shield so a cancel
            # in this finally cannot leave the cold faces stale-empty while the
            # Dynamo record carries the layer (or vice versa).
            try:
                await _run_to_completion_shielded(
                    _persist_case_view_snapshot(state, case_id=turn_case_id)
                )
                # #165 dual-write: persist the thin manifest ALONGSIDE the
                # snapshot (same durability requirement -- a published layer
                # must be cold-renderable from either index before the daemon
                # stops).
                await _run_to_completion_shielded(
                    _persist_case_manifest(state, case_id=turn_case_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - best-effort, never mask
                logger.exception(
                    "case-coldview-persist (finally) failed case=%s",
                    turn_case_id,
                )

    # DETERMINISTIC LAYER AUTO-PUBLISH: a tool returning a renderable RASTER
    # LayerURI with a raw object-store uri (s3://, gs://) gets AUTO-CALLED
    # through ``_auto_publish_droppable_raster`` (see its docstring) rather
    # than left for the LLM to publish.
    #
    # Gating: skip ``publish_layer`` itself (its own wrap-site is below) and
    # the reuse short-circuit (already loaded); honor the per-tool
    # ``auto_publish`` metadata flag (default True; pure intermediates like
    # fetch_dem/fetch_topobathy/fetch_3dep_extra opt OUT).
    #
    # Dedup: add_loaded_layer dedups by COG identity, so an LLM publish of the
    # SAME COG merges into the same row.
    #
    # Honesty floor: a FAILED auto-publish surfaces a typed
    # ``LAYER_AUTO_PUBLISH_FAILED`` error rather than silently narrating
    # success; the LLM-visible tool ``result`` is left unchanged.
    if (
        tool_name != "publish_layer"
        and not isinstance(entry, _ReuseEntry)
        and getattr(entry.metadata, "auto_publish", True)
    ):
        _auto_pub_candidates = (
            list(result)
            if isinstance(result, list)
            else [result]
        )
        for _cand in _auto_pub_candidates:
            if not _is_droppable_object_store_raster(_cand):
                continue
            await _auto_publish_droppable_raster(
                websocket,
                state,
                layer=_cand,
                case_id=turn_case_id,
            )

    # Register every URI the result carries (LayerURI layer_id↔uri
    # pairs + bare object-store strings) so the NEXT tool call can resolve
    # handles / detect mangles. Best-effort -- registration never breaks the
    # dispatch.
    uri_registry.register_tool_result(tool_name, result)

    # ADR 0014: persist the freshly-minted short-handle map (L<n> -> uri) WITH
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
    # Gemini an UNAMBIGUOUS function_response -- "this is the EXISTING
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

    # Track layer emissions on the active turn so the next ``CaseChatMessage``
    # write captures them. ``publish_layer`` returns a WMS URL string; we use
    # the tool's ``layer_id`` parameter as the canonical layer identifier.
    if tool_name == "publish_layer" and "layer_id" in params:
        lid = params.get("layer_id")
        if isinstance(lid, str) and lid:
            state.current_turn_layer_ids.append(lid)
            # The MISSING LINK between an atomic publish and the map:
            # ``emit_tool_call`` only feeds ``add_loaded_layer`` (and thus the
            # ``session-state`` envelope the web renders WMS layers from)
            # when a tool RETURNS a typed LayerURI -- composers do, but the
            # atomic ``publish_layer`` returns a bare WMS string. Wrap the WMS
            # URL in a LayerURI here so the existing emission/persistence
            # machinery announces it exactly as composer layers are
            # announced.
            #
            # TiTiler exit (QGIS-native swap): publish_layer now returns the
            # raw s3:// COG uri for rasters (the plugin reads it via
            # /vsicurl/), so s3:// joins http(s) as a SUCCESS shape here.
            if isinstance(result, str) and result.startswith(
                ("http://", "https://", "s3://")
            ):
                try:
                    # Route through the single emission seam: the publish
                    # return here is http(s) (WMS/durable-GeoJSON) or a raw
                    # s3:// COG (QGIS-native raster publish); the seam passes
                    # both through so this site can never regress into
                    # emitting an un-renderable shape (gs://, file://, empty).
                    _resolved_style_preset = _resolve_publish_wrap_style_preset(
                        style_preset=params.get("style_preset"),
                        layer_uri=result,
                        layer_id=lid,
                    )
                    # OPEN-9: a bare-ULID layer_id (derive_layer_id's last
                    # resort) rendered directly as the UI name is meaningless
                    # ("01KX5TEZ20BK86EE6DG8PSVFJK"). Derive a readable name
                    # from whatever IS known -- an explicit model-supplied
                    # name (params carries it even though publish_layer's own
                    # signature only uses it for logging), else the resolved
                    # style_preset, else the published URI's path segment.
                    from .agent.tools.publish_layer.publish_layer import derive_readable_layer_name

                    _layer_name = derive_readable_layer_name(
                        params.get("name"),
                        lid,
                        _resolved_style_preset,
                        result,
                    )
                    _emit_layer = emit_layer_uri(
                        LayerURI(
                            layer_id=lid,
                            name=_layer_name,
                            layer_type="raster",
                            uri=result,
                            # job duplicate-flood-layer SAFETY NET: when a
                            # re-publish of a FLOOD/DEPTH COG carries an empty
                            # style_preset, default it to continuous_flood_depth
                            # so the layer is never styleless (= viridis). Non-
                            # flood rasters keep "" (QGIS/TiTiler default).
                            style_preset=_resolved_style_preset,
                        )
                    )
                    if _emit_layer is not None:
                        await state.emitter.add_loaded_layer(_emit_layer)
                        # Re-persist AFTER this add: the dispatch's
                        # finally-persist above ran BEFORE this wrap-site
                        # emission, so the published tile layer would
                        # otherwise only live in memory -- a Case switch +
                        # reopen would rehydrate WITHOUT it.
                        if turn_case_id:
                            await _persist_case_loaded_layers(
                                state, case_id=turn_case_id
                            )
                            # Lane A1: this wrap-site add runs AFTER the
                            # dispatch finally-persist, so re-materialize the S3
                            # snapshot here too -- otherwise the published tile
                            # layer (publish_layer is the LAST tool) lands only
                            # in memory and the cold view would miss it.
                            #
                            # COLDVIEW DURABILITY (J1): AWAIT the snapshot +
                            # manifest here too. This is the publish_layer-is-the
                            # -last-tool path -- precisely the layer-publish
                            # mutation whose cold-refresh must be durable before
                            # the turn returns, or box-stop races the detached
                            # PUT and the cold view misses the just-published
                            # layer. Same loop-safety as the finally site: the
                            # persist coroutines run blocking I/O off-thread and
                            # swallow their own errors (never raise), so the
                            # inline await neither pins the loop nor breaks the
                            # emission.
                            await _persist_case_view_snapshot(
                                state, case_id=turn_case_id
                            )
                            # #165 dual-write: thin manifest ALONGSIDE the
                            # snapshot (publish_layer is the LAST tool, so the
                            # published layer would land only in memory without
                            # this). Swallows its own errors (never raises).
                            await _persist_case_manifest(
                                state, case_id=turn_case_id
                            )
                except Exception:  # noqa: BLE001 -- emission is best-effort
                    logger.exception(
                        "publish_layer loaded-layer emission failed "
                        "layer_id=%s",
                        lid,
                    )

    # Per-Case layer persistence now happens in
    # the ``finally`` block above so it ALSO fires when the tool (or its
    # post-invoke envelope emission on a dying WebSocket) raised -- the
    # emitter's accumulator already contains the layer at that point.

    # Mode 2 .gov/.edu classifier -- when web_fetch returns a dict
    # that looks like a structured-data candidate, emit a `mode2-candidate`
    # envelope and append an audit-log line. Deterministic side-effect; the
    # web modal (Wave 2/3) renders the offer. See mode2_classifier.py.
    if tool_name == "web_fetch" and isinstance(result, dict):
        await _maybe_emit_mode2_candidate(websocket, state, result)
    return result


async def _run_to_completion_shielded(coro: Awaitable[Any]) -> None:
    """Await ``coro`` so it COMPLETES even if the surrounding task is cancelled.

    DURABILITY (layer-publish-survives-disconnect): the per-tool dispatch
    ``finally`` persists the completed layer accumulator to DynamoDB. That
    ``finally`` runs on EVERY exit path -- including ``asyncio.CancelledError``
    (a same-stream re-prompt supersede, the stop button, or any cancel that
    reaches the detached turn). A bare ``await persist(...)`` in a ``finally``
    is NOT safe under cancellation: the first real suspension point inside the
    persist re-raises the pending ``CancelledError``, so the DynamoDB write is
    SKIPPED and a fully-computed layer is lost (live 2026-06-23: SFINCS run
    01KVSTC80F wrote 100+ COGs to S3 but the Case persisted 0 layers after a
    transient WS drop during the ~9-min solve).

    The fix wraps the persist in a real task + ``asyncio.shield`` so a cancel
    of the parent does NOT cancel the write; if a ``CancelledError`` does arrive
    while we wait, we keep awaiting the shielded task to completion, THEN re-raise
    the cancellation (Invariant 8: the cancel still propagates, the write still
    lands). The persist coroutines swallow their own errors (never raise), so the
    only thing that can interrupt them is the parent cancel this guard absorbs.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                # The inner task itself was cancelled (not just our shield) --
                # nothing more to wait on; propagate.
                raise
            # Parent was cancelled but the shielded write is NOT cancelled.
            # Remember the cancel, and keep waiting on the still-running write
            # (the next loop awaits the same shielded task) so the DynamoDB write
            # COMPLETES before the cancel propagates. If the write already
            # finished, the next ``await shield(task)`` returns immediately.
            cancelled = True
            continue
    if cancelled:
        # Invariant 8: the write landed; now honor the parent cancellation.
        raise asyncio.CancelledError


async def _auto_publish_droppable_raster(
    websocket: ServerConnection,
    state: SessionState,
    *,
    layer: LayerURI,
    case_id: str | None,
) -> None:
    """Deterministically publish + render a droppable object-store raster.

    ``layer`` is exactly the class ``emit_layer_uri`` DROPS -- a renderable
    raster carrying a raw ``s3://``/``gs://`` uri MapLibre cannot fetch. Calls
    ``publish_layer`` server-side, off the asyncio loop (no-sync-blocking
    norm), and feeds the resulting published uri (an http(s) face, or the raw
    ``s3://`` COG on the QGIS-native path) through the SAME
    ``emit_layer_uri`` -> ``add_loaded_layer`` -> persist machinery the
    publish_layer wrap-site uses, so dedup/z-index/snapshot/manifest behave
    identically (an LLM publish of the SAME COG merges by COG identity -- no
    double-add).

    Honesty floor: on FAILURE (raises, or returns neither an http(s) URL nor
    an s3:// COG uri) surfaces a typed ``LAYER_AUTO_PUBLISH_FAILED`` error --
    never a silent green. The raw ``s3://`` COG uri is a SUCCESS shape for
    rasters (the plugin reads it via /vsicurl/), accepted alongside http(s).
    The LLM-visible tool result is left UNCHANGED so retry-on-failure
    narration can act. Best-effort: never raises, so it cannot break the
    dispatch.
    """
    publish_entry = TOOL_REGISTRY.get("publish_layer")
    if publish_entry is None:  # pragma: no cover - publish_layer always present
        logger.warning(
            "auto-publish: publish_layer not in registry; cannot render "
            "raster layer_id=%s uri=%s",
            layer.layer_id,
            layer.uri,
        )
        return

    style_preset = _resolve_publish_wrap_style_preset(
        style_preset=layer.style_preset,
        layer_uri=layer.uri,
        layer_id=layer.layer_id,
    )

    try:
        # publish_layer is synchronous (polls TiTiler / PyQGIS); run it OFF the
        # event loop so it cannot stall the WS heartbeat. The server wrapper
        # normally resolves the case-scoped .qgs for publish_layer; here we pass
        # case_id straight through so the same per-Case routing applies inside
        # the tool body.
        published_url = await asyncio.to_thread(
            publish_entry.fn,
            layer_uri=layer.uri,
            layer_id=layer.layer_id,
            style_preset=style_preset or None,
            case_id=case_id,
        )
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - classify into honesty floor
        logger.exception(
            "auto-publish: publish_layer FAILED layer_id=%s uri=%s",
            layer.layer_id,
            layer.uri,
        )
        await _emit_auto_publish_failure(
            websocket, state, layer=layer, reason=str(exc) or exc.__class__.__name__
        )
        return

    # Honesty floor: publish_layer's SUCCESS shapes are an http(s) URL (a
    # WMS/durable-GeoJSON face) or the raw s3:// COG uri (QGIS-native raster
    # publish; the plugin reads it via /vsicurl/). Anything else -- empty/None,
    # an error string, gs://, file:// -- is NOT a renderable layer: never add
    # it + narrate success.
    if not (
        isinstance(published_url, str)
        and published_url.startswith(("http://", "https://", "s3://"))
    ):
        logger.warning(
            "auto-publish: publish_layer returned a non-renderable value for "
            "layer_id=%s uri=%s -> %r; treating as render failure",
            layer.layer_id,
            layer.uri,
            published_url,
        )
        await _emit_auto_publish_failure(
            websocket,
            state,
            layer=layer,
            reason=(
                "publish_layer did not return a renderable http(s) URL or "
                "s3:// COG uri"
            ),
        )
        return

    # Success: route the published uri (http(s) face or raw s3:// COG) through
    # the SINGLE emission seam (it passes both through untouched) and the
    # existing add_loaded_layer machinery. The published layer keeps the
    # producing layer's id/name so the COG-identity dedup collapses a later LLM
    # re-publish of the same COG into this same row.
    try:
        _emit_layer = emit_layer_uri(
            LayerURI(
                layer_id=layer.layer_id,
                name=layer.name,
                layer_type="raster",
                uri=published_url,
                style_preset=style_preset,
                role=layer.role,
                units=layer.units,
                bbox=layer.bbox,
            )
        )
        if _emit_layer is None:  # pragma: no cover - http/s3 never drops
            return
        await state.emitter.add_loaded_layer(_emit_layer)
        # Track the layer on the active turn so the closing CaseChatMessage
        # captures it (mirrors the publish_layer wrap-site).
        if layer.layer_id:
            state.current_turn_layer_ids.append(layer.layer_id)
        # Re-persist AFTER this add: the dispatch finally-persist ran BEFORE this
        # auto-publish, so without re-persisting the rendered layer would live
        # only in memory and a Case reopen would rehydrate without it (the exact
        # publish_layer-wrap-site durability concern). Shielded so a parent cancel
        # cannot interrupt the write; each persist swallows its own errors.
        if case_id:
            await _run_to_completion_shielded(
                _persist_case_loaded_layers(state, case_id=case_id)
            )
            await _run_to_completion_shielded(
                _persist_case_view_snapshot(state, case_id=case_id)
            )
            await _run_to_completion_shielded(
                _persist_case_manifest(state, case_id=case_id)
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - emission/persist is best-effort
        logger.exception(
            "auto-publish: rendered-layer emission failed layer_id=%s",
            layer.layer_id,
        )


async def _emit_auto_publish_failure(
    websocket: ServerConnection,
    state: SessionState,
    *,
    layer: LayerURI,
    reason: str,
) -> None:
    """Surface a typed 'computed but not displayable' state (honesty floor).

    When the deterministic auto-publish cannot produce a renderable http(s) URL,
    we MUST NOT silently drop the layer and narrate success. Emit a typed
    ``LAYER_AUTO_PUBLISH_FAILED`` error envelope so the failure is visible to the
    user (a degraded card / honest error) and the LLM-visible retry loop can act.
    Best-effort: never raises.
    """
    try:
        # The A.6 ErrorCode literal is a closed set; INTERNAL_ERROR is the right
        # wire code for an unexpected server-side render failure. The typed
        # ``[LAYER_AUTO_PUBLISH_FAILED]`` marker leads the human-readable message
        # so the surface is unambiguous + greppable (and the web can special-case
        # a degraded layer card off it) without widening the contract enum.
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            (
                f"[LAYER_AUTO_PUBLISH_FAILED] Computed layer {layer.name!r} "
                f"({layer.layer_id}) could not be displayed: {reason}. The result "
                f"was produced but is not renderable on the map."
            ),
            retryable=True,
        )
    except Exception:  # noqa: BLE001 - the honesty surface must never break dispatch
        logger.debug(
            "auto-publish failure-envelope emit failed layer_id=%s",
            layer.layer_id,
            exc_info=True,
        )


async def _persist_case_layer_handles(
    state: SessionState, *, case_id: str | None
) -> None:
    """ADR 0014: persist the session registry's short-handle map to the Case.

    Writes the ``{L<n>: uri}`` map as a storage-only ``layer_handles`` field
    on the cases doc (see ``Persistence.set_case_layer_handles``) so a
    reconnect / Case reopen (``_seed_registry_for_case``) restores the exact
    handles the LLM has already been shown. Skips when nothing new was
    minted since the last write (``shorts_dirty``). Best-effort: any failure
    is logged and swallowed -- the dispatch is never broken, and the registry
    stays dirty so the next dispatch retries the write.
    """
    if not case_id:
        return
    reg = get_uri_registry(state.session_id)
    if not reg.shorts_dirty:
        return
    p = get_persistence()
    if p is None:
        return
    try:
        await p.set_case_layer_handles(case_id, reg.export_short_handles())
        reg.mark_shorts_persisted()
    except Exception:  # noqa: BLE001 -- best-effort, never break the dispatch
        logger.exception(
            "case layer-handle persist failed case=%s", case_id
        )


async def _seed_registry_for_case(
    state: SessionState, case_id: str | None, loaded_layers: Any
) -> None:
    """ADR 0014: reset the URI registry to a Case AND restore its handle map.

    The single reseed path for every case-open / case-switch / resume call
    site: replace-not-merge from the Case's persisted ``loaded_layers`` (the
    F32 contract), importing the Case's persisted ``{L<n>: uri}`` map FIRST
    so already-announced short handles keep their numbers and fresh layers
    mint past the persisted maximum. Best-effort on the persistence read --
    a hiccup degrades to fresh minting (stale L<n> references then reject
    typed with the current inventory, which is honest and retryable).
    """
    reg = get_uri_registry(state.session_id)
    persisted: dict[str, str] | None = None
    p = get_persistence()
    if p is not None and case_id:
        try:
            persisted = await p.get_case_layer_handles(case_id)
        except Exception:  # noqa: BLE001 -- degrade to fresh minting
            logger.warning(
                "case layer-handle map read failed case=%s (fresh mint)",
                case_id,
                exc_info=True,
            )
    reg.replace_from_layers(loaded_layers, short_handles=persisted)


def _set_active_aoi_from_payload(state: SessionState, raw: Any) -> None:
    """ADR 0017: bind/clear the session's active canvas AOI.

    Called when a ``user-message`` payload carries the ``aoi_bbox`` key
    (``[min_lon, min_lat, max_lon, max_lat]`` EPSG:4326, ``None`` when no AOI
    is drawn). A valid bbox sets the active AOI; an explicit ``None`` clears
    it; a malformed value is logged and ignored (never blocks the turn, never
    clobbers a good AOI with garbage).
    """
    if raw is None:
        if state.active_aoi_bbox is not None:
            logger.info(
                "active-aoi cleared session=%s", state.session_id
            )
        state.active_aoi_bbox = None
        return
    coerced = coerce_bbox_value(raw)
    if (
        coerced is None
        or not all(math.isfinite(v) for v in coerced)
        or not (coerced[0] < coerced[2] and coerced[1] < coerced[3])
    ):
        logger.warning(
            "active-aoi ignoring malformed aoi_bbox=%r session=%s",
            raw,
            state.session_id,
        )
        return
    state.active_aoi_bbox = coerced
    logger.info(
        "active-aoi set session=%s bbox=%s", state.session_id, coerced
    )


async def _persist_case_loaded_layers(
    state: SessionState, *, case_id: str | None = None
) -> None:
    """Sync the emitter's ``_loaded_layers`` onto the turn's ``CaseSummary``.

    Writes the current ``ProjectLayerSummary[]`` accumulator into
    ``Case.loaded_layer_summaries`` (full dicts for rehydration) and keeps
    ``Case.layer_summary`` (the lightweight ``layer_id[]`` projection) in
    lockstep. Idempotent and dedup-by-uri because the emitter already dedups
    upstream.

    Best-effort: a Persistence failure is logged but never raised. The Case
    lookup gates the write -- an archived/deleted Case is silently skipped
    (no surprise resurrection via this side-channel).

    ``case_id`` pins the target Case explicitly (callers inside a tool
    dispatch pass their entry-time capture); default resolves via
    ``_turn_case_id`` so a mid-turn Case switch never re-aims attribution.
    """
    target_case = case_id if case_id is not None else _turn_case_id(state)
    p = get_persistence()
    if p is None or state.emitter is None or not target_case:
        return
    try:
        case = await p.get_case(target_case)
    except Exception:  # noqa: BLE001
        logger.exception(
            "case-layer-persist: get_case failed case=%s",
            target_case,
        )
        return
    if case is None:
        logger.debug(
            "case-layer-persist: case=%s missing; skipping",
            target_case,
        )
        return

    loaded = state.emitter.loaded_layers  # defensive copy from the emitter
    emitter_dicts: list[dict] = [layer.model_dump(mode="json") for layer in loaded]

    # MERGE (append + replace-by-layer_id) instead of wholesale replace: an
    # emitter never seeded with the Case's persisted layers (fresh
    # connection, sync failure, sibling-socket dispatch) must never CLOBBER
    # previously persisted summaries down to its own partial view -- union
    # them, with the emitter's fresher entry winning on a layer_id collision.
    merged: list[dict] = [
        dict(d) for d in case.loaded_layer_summaries if isinstance(d, dict)
    ]

    # D3 (persist-side frame supersede): a re-run's "Flood depth step N" frames
    # carry NEW run-id-suffixed layer_ids, so the layer_id merge below would
    # APPEND them on top of the prior run's step-N frames -> the persisted case
    # grows by a full frame series per re-run (and re-surfaces on reopen via
    # reset_loaded_layers). Before the layer_id merge, drop any PRIOR persisted
    # frame whose (role + "Flood depth step N") series key matches an INCOMING
    # frame, so run B's step N replaces run A's step N in storage too. Keys on
    # the engine-agnostic name token (SWMM + SFINCS share it); mirrors
    # pipeline_emitter._frame_series_key. Non-frame layers are untouched.
    def _dict_frame_series_key(d: dict) -> str | None:
        name = d.get("name")
        if (
            d.get("role") == "context"
            and isinstance(name, str)
            and _FLOOD_FRAME_NAME_RE.match(name)
        ):
            return f"flood-frame::{name}"
        return None

    incoming_frame_keys = {
        k
        for d in emitter_dicts
        if (k := _dict_frame_series_key(d)) is not None
    }
    if incoming_frame_keys:
        merged = [
            d
            for d in merged
            if _dict_frame_series_key(d) not in incoming_frame_keys
        ]

    index_by_layer_id = {
        d.get("layer_id"): i for i, d in enumerate(merged) if d.get("layer_id")
    }
    for d in emitter_dicts:
        lid = d.get("layer_id")
        pos = index_by_layer_id.get(lid)
        if pos is None:
            index_by_layer_id[lid] = len(merged)
            merged.append(d)
        else:
            merged[pos] = d
    layer_ids: list[str] = [
        d.get("layer_id") for d in merged if isinstance(d.get("layer_id"), str)
    ]

    # If nothing has changed, skip the round-trip.
    if (
        case.loaded_layer_summaries == merged
        and case.layer_summary == layer_ids
    ):
        return

    updated = case.model_copy(
        update={
            "loaded_layer_summaries": merged,
            "layer_summary": layer_ids,
            "updated_at": now_utc(),
        }
    )
    try:
        await p.upsert_case(updated)
        logger.debug(
            "case-layer-persist case=%s layers=%d",
            target_case,
            len(layer_ids),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "case-layer-persist: upsert failed case=%s",
            target_case,
        )


async def _persist_case_view_snapshot(
    state: SessionState, *, case_id: str | None = None
) -> None:
    """Materialize the full case view to S3 (Lane A1: view-without-agent).

    Writes ``s3://$TRID3NT_RUNS_BUCKET/case-views/{case_id}.json`` -- the EXACT
    ``CaseOpenEnvelopePayload`` the live ``case-open`` ships, PLUS the in-memory
    inline vector GeoJSON merged onto ``loaded_layers`` so vectors paint when
    the agent box is asleep. Called on every Case mutation (layer publish,
    per-turn persist, case create/rename); idempotent, last-write-wins.

    The inline vector GeoJSON + dense-vector tags live ONLY on the live emitter
    (``add_loaded_layer`` / ``reinline_vector_layers`` populate them; the
    persisted Case carries URI-only summaries). Source them from
    ``state.emitter`` here so the snapshot captures them durably at the moment
    of the mutation -- exactly when the agent still holds them.

    Best-effort: a missing Persistence binding / no Case / no emitter
    short-circuits; ``write_case_view_snapshot`` itself swallows S3 errors and
    returns ``False`` (same discipline as ``_persist_case_loaded_layers`` /
    chart persistence). Never raises, never blocks the turn's happy path.
    """
    target_case = case_id if case_id is not None else _turn_case_id(state)
    if not target_case:
        return
    p = get_persistence()
    if p is None:
        return
    inline: dict[str, Any] = {}
    density: dict[str, Any] = {}
    # Only source the emitter's in-memory inline-vector side-tables when the
    # snapshot target IS the Case currently open on THIS connection -- the
    # emitter holds exactly one Case's accumulator, so merging it into a
    # DIFFERENT Case's snapshot (e.g. renaming Case B while Case A is open)
    # would stamp the wrong Case's vectors. When they differ we still write a
    # correct URI-only snapshot (title/chat/layers from persisted state); the
    # next layer/turn mutation on the open Case re-materializes its vectors.
    open_case = _turn_case_id(state)
    if state.emitter is not None and target_case == open_case:
        # Defensive copies from the emitter's in-memory side-tables (the only
        # place the inline vector GeoJSON exists at publish time).
        inline = state.emitter.inline_geojson_by_layer_id
        density = state.emitter.density_meta_by_layer_id
    try:
        await p.write_case_view_snapshot(
            target_case,
            inline_geojson_by_layer_id=inline,
            density_meta_by_layer_id=density,
        )
    except Exception:  # noqa: BLE001 -- snapshot is a side-effect, never a gate
        logger.warning(
            "case-view-snapshot: persist failed case=%s", target_case
        )


async def _persist_case_manifest(
    state: SessionState, *, case_id: str | None = None
) -> None:
    """Materialize the THIN per-case manifest to S3 (data-island index).

    DUAL-WRITE sibling of ``_persist_case_view_snapshot``: writes
    ``s3://$TRID3NT_RUNS_BUCKET/case-manifests/{case_id}.json`` -- the thin
    ``CaseManifest`` (title / bbox / hazard + layer asset URLs) a future cold
    path lists cases + their layers from WITHOUT downloading the fat snapshot.
    Called ALONGSIDE the snapshot at the SAME Case mutation call-sites; the
    snapshot path is unchanged (this is additive -- dual-write only).

    Sourced entirely from the persisted Case doc (``loaded_layer_summaries``);
    it does NOT need the emitter's in-memory inline-vector side-tables (those
    are only for the fat snapshot's cold-paint), so this helper is simpler
    than the snapshot one.

    Best-effort: a missing Persistence binding / no Case short-circuits;
    ``write_case_manifest`` swallows its own S3 / build errors and returns
    ``False`` -- a manifest failure must never break the snapshot path or the
    turn.
    """
    target_case = case_id if case_id is not None else _turn_case_id(state)
    if not target_case:
        return
    p = get_persistence()
    if p is None:
        return
    try:
        await p.write_case_manifest(target_case)
    except Exception:  # noqa: BLE001 -- manifest is a side-effect, never a gate
        logger.warning("case-manifest: persist failed case=%s", target_case)


async def _maybe_emit_impact_envelope(
    websocket: ServerConnection,
    state: SessionState,
    raw_envelope: dict,
) -> None:
    """Emit an ``impact-envelope`` WS envelope for the ImpactPanel.

    Called when ``compute_impact_envelope`` returns a result containing a
    valid ``raw_envelope`` dict (ImpactEnvelope shape, key signal:
    ``n_structures_total`` present at the top level).

    Emitted IN ADDITION to the standard ``function_response`` so the client
    gets both:

    - ``function_response`` -> Gemini-loop replay (Gemini reads the summary).
    - ``impact-envelope``   -> ImpactPanel state update.

    Wire shape::

        {
          "type": "impact-envelope",
          "session_id": str,
          "payload": { ...full ImpactEnvelope dict... }
        }

    Best-effort: a serialization / wire failure is logged but never raised --
    the ``function_response`` path must not be interrupted by a side-channel
    emission failure.
    """
    import json as _json

    try:
        await websocket.send(
            _json.dumps(
                {
                    "type": "impact-envelope",
                    "session_id": state.session_id,
                    "payload": raw_envelope,
                }
            )
        )
        logger.info(
            "impact-envelope emitted session=%s n_structures_total=%s",
            state.session_id,
            raw_envelope.get("n_structures_total"),
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.exception(
            "impact-envelope emission failed session=%s", state.session_id
        )


async def _maybe_emit_code_exec_result(
    websocket: ServerConnection,
    state: SessionState,
    code_exec_result: dict,
) -> None:
    """Emit a ``code-exec-result`` WS envelope.

    Called when ``code_exec_request`` returns a result carrying the full
    code-exec-result payload under ``_code_exec_result``
    (``is_code_exec_result(result)`` is True). Fires IN ADDITION to the
    standard ``function_response``:

    - ``code-exec-result`` -> the FULL result payload (status + stdout/stderr
      tails + the structured result descriptor + truncated flag + duration)
      for the client to render the result card. The function_response Gemini
      reads is the COMPACT summary (stripped by
      ``adapter.summarize_tool_result`` via the ``_code_exec_result`` key) so
      narration sources the structured ``result``, not the raw logs.

    Wire shape mirrors ``chart-emission``::

        {
          "type": "code-exec-result",
          "session_id": str,
          "payload": { ...full CodeExecResultPayload dict... }
        }

    Best-effort: never raised on a serialization/wire failure. Code-exec
    results are ephemeral (not persisted to the session ``charts`` array) --
    a re-opened Case replays chat + charts, not transient computations.
    """
    import json as _json

    payload = code_exec_result.get(CODE_EXEC_RESULT_KEY)
    if not isinstance(payload, dict):
        return
    try:
        await websocket.send(
            _json.dumps(
                {
                    "type": "code-exec-result",
                    "session_id": state.session_id,
                    "payload": payload,
                }
            )
        )
        logger.info(
            "code-exec-result emitted session=%s code_exec_id=%s status=%s truncated=%s",
            state.session_id,
            payload.get("code_exec_id"),
            payload.get("status"),
            payload.get("truncated"),
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.exception(
            "code-exec-result emission failed session=%s", state.session_id
        )


async def _maybe_emit_chart(
    websocket: ServerConnection,
    state: SessionState,
    chart_result: dict,
) -> None:
    """Emit a ``chart-emission`` WS envelope + persist the chart.

    Called when a chart-generation tool (``generate_histogram`` /
    ``generate_choropleth_legend`` / ``generate_time_series`` /
    ``generate_damage_distribution``) returns a ChartEmissionPayload-shaped
    dict (``is_chart_emission_result(result)`` is True). Fires IN ADDITION to
    the standard ``function_response``:

    - ``chart-emission`` -> the FULL Vega-Lite spec for the client to render
      via vega-embed (inline stacked preview + gallery). The function_response
      Gemini reads is a COMPACT summary with the spec stripped
      (``adapter.summarize_tool_result``) so narration sources the numbers,
      not the inline rows.
    - ``SessionChartRecord`` persisted to the ``sessions`` collection so the
      chart replays on Case rehydration.

    ``created_turn_id`` is stamped here (from the per-turn pipeline id) when
    the tool did not set one, so the client groups charts from the same turn
    into one UI stack.

    Wire shape::

        {
          "type": "chart-emission",
          "session_id": str,
          "payload": { ...full ChartEmissionPayload dict... }
        }

    Best-effort: a serialization / wire / persistence failure is logged but
    never raised -- the ``function_response`` path must not be interrupted by
    a side-channel emission failure.
    """
    import json as _json

    payload = dict(chart_result)
    # Stamp the UI stack-grouping key from the current turn if the tool left it
    # unset, so charts from the same turn render as one stack (chart_contracts
    # ``created_turn_id`` semantics).
    if not payload.get("created_turn_id"):
        turn_id = (
            state.current_turn_pipeline_id
            or state.current_pipeline_id
            or state.session_id
        )
        payload["created_turn_id"] = turn_id

    try:
        await websocket.send(
            _json.dumps(
                {
                    "type": "chart-emission",
                    "session_id": state.session_id,
                    "payload": payload,
                }
            )
        )
        logger.info(
            "chart-emission emitted session=%s chart_id=%s title=%r",
            state.session_id,
            payload.get("chart_id"),
            payload.get("title"),
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.exception(
            "chart-emission emission failed session=%s", state.session_id
        )

    # Persist the chart so it replays on Case rehydration (best-effort).
    await _persist_chart_record(state, payload)


async def _persist_chart_record(state: SessionState, payload: dict) -> None:
    """Append a ``SessionChartRecord`` to the session document.

    Resolves the ``Persistence`` singleton and ``$push``es the record onto the
    session document's append-only ``charts`` array via the underlying MCP
    ``update-one`` call (charts go directly on the MCP client like telemetry,
    keeping the Persistence public API narrow).

    Keyed by the active Case id when one is selected (so charts replay on Case
    rehydration via the same document the chat history lives on), else by the
    session id. ``upsert=True`` so the first chart on a fresh session document
    creates it.

    Never raises -- a persistence failure is logged at WARNING. This is only
    the write half of the contract; replay is session-resume scope.
    """
    persistence = get_persistence()
    if persistence is None:
        # M1 in-memory / CI-without-Atlas path: charts live only in-flight.
        logger.debug(
            "chart persistence skipped (no Persistence bound) session=%s",
            state.session_id,
        )
        return

    try:
        from trid3nt_contracts.chart_contracts import (
            ChartEmissionPayload,
            SessionChartRecord,
        )
        from .persistence import DEFAULT_DATABASE, SESSIONS_COLLECTION

        # Charts are turn-scoped emissions -- key them by the Case
        # that OWNS the turn, not whatever Case is visible at write time.
        doc_id = _turn_case_id(state) or state.session_id
        record = SessionChartRecord(
            session_id=doc_id,
            payload=ChartEmissionPayload.model_validate(payload),
            emitted_at=now_utc(),
        )
        body = record.model_dump(mode="json")
        await persistence._mcp.call_tool(  # noqa: SLF001 -- telemetry-writer pattern
            "update-one",
            {
                "database": DEFAULT_DATABASE,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": doc_id},
                "update": {"$push": {"charts": body}},
                "upsert": True,
            },
        )
        logger.info(
            "chart persisted session=%s doc_id=%s chart_id=%s",
            state.session_id,
            doc_id,
            payload.get("chart_id"),
        )
    except Exception:  # noqa: BLE001 -- persistence must not break the loop
        logger.warning(
            "chart persistence failed session=%s chart_id=%s",
            state.session_id,
            payload.get("chart_id"),
            exc_info=True,
        )


async def _maybe_emit_mode2_candidate(
    websocket: ServerConnection, state: SessionState, result: dict
) -> None:
    """Run ``classify_for_mode2`` and emit ``mode2-candidate`` if it lands.

    Best-effort: a classifier or send failure is logged but never raised -- the
    caller already returned the tool result and we will not let a side-effect
    take down a perfectly good ``web_fetch`` invocation (FR-AS-7 boundary).
    """
    import json as _json

    try:
        candidate = classify_for_mode2(result)
        if candidate is None:
            return
        envelope = Mode2CandidateEnvelope(candidate=candidate)
        await websocket.send(
            _json.dumps(
                {
                    "type": "mode2-candidate",
                    "session_id": state.session_id,
                    "payload": envelope.to_wire_dict(),
                }
            )
        )
        # Mode 2 offer-to-add: stash the candidate keyed by candidate_id (which
        # the plugin card echoes back as catalog-addition-response.request_id)
        # so a later positive reply can draft + append the full catalog entry.
        # Fire-and-forget (never blocks this turn); the registry is bounded.
        _register_pending_catalog_offer(
            state.session_id,
            candidate.candidate_id,
            envelope.to_wire_dict()["candidate"],
        )
        # Mode-2 candidate audit (M4) routes through the MCP
        # ``audit_log`` collection (D.15) -- the bespoke JSONL file writer
        # was deleted (remove-don't-shim). When Persistence is unbound
        # (explicit CI path) the event is logged-and-dropped, same policy
        # as telemetry (M3) and chart persistence.
        p_audit = get_persistence()
        if p_audit is not None:
            try:
                await p_audit.append_audit(
                    "mode2-candidate",
                    {
                        "session_id": state.session_id,
                        "candidate": envelope.to_wire_dict()["candidate"],
                    },
                )
            except Exception:  # noqa: BLE001 -- audit is best-effort
                logger.warning(
                    "mode2 audit write failed session=%s",
                    state.session_id,
                    exc_info=True,
                )
        else:
            logger.debug(
                "mode2 audit skipped (no Persistence bound) session=%s",
                state.session_id,
            )
        logger.info(
            "mode2-candidate session=%s url=%s confidence=%.2f patterns=%s",
            state.session_id,
            candidate.url,
            candidate.confidence,
            candidate.detected_patterns,
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.exception("mode2-candidate emission failed")


def _parse_invoke_directive(text: str) -> tuple[str, dict] | None:
    """If ``text`` is an ``/invoke <tool_name> <json-params>`` directive,
    return ``(tool_name, params)``; else return None.

    Used by the M4 live-evidence harness to drive real tool invocations
    end-to-end through the registry + emitter. NOT the LLM tool-call path --
    that lands when Gemini-side function-calling is wired (M4 follow-up).
    The directive shape is debug-only; intentionally not in Appendix A.
    """
    if not text.startswith("/invoke "):
        return None
    rest = text[len("/invoke ") :].strip()
    # Split on first whitespace: "<tool_name> <json>"
    parts = rest.split(None, 1)
    if not parts:
        return None
    tool_name = parts[0]
    if len(parts) == 1:
        return tool_name, {}
    import json as _json

    try:
        params = _json.loads(parts[1])
        if not isinstance(params, dict):
            return None
    except Exception:  # noqa: BLE001
        return None
    return tool_name, params


# --------------------------------------------------------------------------- #
# Dispatch wrappers with chat persistence (FR-MP-6)
# --------------------------------------------------------------------------- #


async def _dispatch_gemini_and_persist(
    websocket: ServerConnection,
    state: SessionState,
    settings: ModelSettings,
    user_text: str,
    research_mode: str,
    bedrock_model: str | None = None,
    show_thinking: bool = False,
) -> None:
    """Stream Gemini reply, then persist the agent's reply to the active Case.

    Wraps ``_stream_model_reply`` so the Case chat-history append happens
    after the stream completes (the streamed text is the canonical
    ``content`` field on ``CaseChatMessage``). On cancel/error we still
    attempt a best-effort persist of whatever the narration accumulator
    captured before the stream died.

    The persisted ``content`` is the REAL accumulated narration --
    ``_stream_model_reply`` resets ``state.current_turn_narration`` at
    stream start and appends every ``TextDeltaEvent`` delta across all loop
    iterations.
    """
    # Capture the turn's Case at task entry -- the finally-persist
    # below must land in the Case that OWNED this turn even when the user
    # switched Cases (or a newer turn re-pinned the binding) mid-stream.
    turn_case_id = _turn_case_id(state)
    # Bind the owning Case into the per-task ContextVar so EVERY
    # envelope this turn emits (chunks, pipeline-state, session-state, …)
    # carries Envelope.case_id and the web routes it to the right stream.
    bind_turn_case(turn_case_id)
    # Per-turn object capture: a concurrent turn (or Case switch) re-points
    # both SessionState fields mid-stream, so this wrapper gauges completion
    # against THIS turn's history list and joins the narration list
    # registered under the running task (mocked streams that never
    # registered fall back to the live field).
    turn_history = state.chat_history
    pre_chat_len = len(turn_history)
    try:
        await _stream_model_reply(
            websocket, state, settings, user_text, research_mode,
            bedrock_model=bedrock_model,
            show_thinking=show_thinking,
        )
    finally:
        # Close out the turn's narration persistence. Each FINALIZED
        # narration segment is already persisted in-loop by
        # ``_finalize_segment`` (interleaved with the mid-turn tool rows), so
        # this wrapper must NOT re-persist finalized segments -- it only owns
        # the un-finalized remainder + the legacy fallbacks:
        #
        #   * ``open_tail``     -- text in a segment the stream NEVER finalized
        #                         (crash/cancel mid-segment). Persisted as ONE
        #                         agent row (the de-facto terminal row) so no
        #                         narration is lost; layer_emissions=None lets
        #                         it carry the layer/zoom accumulator.
        #   * ``segments_done`` -- count of finalized agent rows this turn.
        #                         When 0 AND the stream completed cleanly with
        #                         no open tail, write the legacy single marker
        #                         row (content == joined narration, possibly "").
        #
        # All three per-task registries are popped (mocked-stream tests that
        # never registered fall back to the live field).
        _own_task = asyncio.current_task()
        if _own_task is not None:
            turn_narration = _TURN_NARRATION_BY_TASK.pop(_own_task, None)
            open_segment = _TURN_OPEN_SEGMENT_BY_TASK.pop(_own_task, None)
            segments_done = _TURN_SEGMENTS_PERSISTED_BY_TASK.pop(_own_task, 0)
            terminal_acc_persisted = _TURN_TERMINAL_ACC_PERSISTED_BY_TASK.pop(
                _own_task, False
            )
        else:
            turn_narration = None
            open_segment = None
            segments_done = 0
            terminal_acc_persisted = False
        if turn_narration is None:
            turn_narration = state.current_turn_narration
        narration = "".join(turn_narration).strip()
        open_tail = "".join(open_segment or []).strip()
        stream_completed = len(turn_history) > pre_chat_len
        # When the turn aborted on ``ContextWindowExceededError``,
        # ``_stream_model_reply``'s except handler stashed the honest abort
        # verdict here. Read + clear it once so it lands on exactly the row
        # that carries the (unverified) streamed text below, and never leaks
        # into a later turn.
        _abort_note = state.current_turn_context_abort_note
        state.current_turn_context_abort_note = None
        if turn_case_id:
            if open_tail:
                # Crash/cancel left an un-finalized open segment carrying text
                # (its done=True never fired). Persist the tail so the partial
                # narration survives; as the de-facto terminal row it also
                # captures the turn's layer/zoom accumulator (layer_emissions
                # default None). No double-persist: finalized segments already
                # cleared their buffer, so this is ONLY the un-finalized text.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content=(open_tail + _abort_note) if _abort_note else open_tail,
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
            elif segments_done == 0 and (narration or stream_completed or _abort_note):
                # No segment was finalized AND no open tail: either a clean
                # narration-LESS completed turn (content="" marker -- replay row
                # count unchanged from pre-fix), a mocked-stream test that
                # populated only ``current_turn_narration`` (legacy one-row
                # contract), or an abort with NO streamed text at all (still
                # write the row so the abort note itself is not lost). Mirror
                # the previous single-row write exactly, plus the note.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content=(narration + _abort_note) if _abort_note else narration,
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
            elif (
                not terminal_acc_persisted
                and (state.current_turn_map_commands or state.current_turn_layer_ids)
            ):
                # Invariant: EVERY turn that emitted a zoom-to/layer must
                # persist at least one chat row carrying it. When
                # segments_done > 0 (interleaved rows already persisted) and
                # no open tail, but the turn's final round ended in tool
                # calls with no trailing narration, none of the persisted
                # segment rows carried the zoom-to/layer accumulator. Write
                # an EMPTY marker row (content="" -- no phantom bubble) with
                # layer_emissions=None so ``_persist_chat_turn`` snapshots
                # ``current_turn_layer_ids``/``current_turn_map_commands``
                # onto it. ``terminal_acc_persisted`` guards a double-write
                # when the turn DID end in narration; the non-empty
                # accumulator guard means an accumulator-less + text-less
                # turn writes nothing.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content="",
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
            # else: either the terminal segment already snapshotted the
            # accumulator (segments_done > 0 ending in narration), or the turn
            # emitted no zoom-to/layer accumulator at all -> every narration run
            # was already persisted as its own interleaved row. Nothing to add.
            # Lane A1: the per-turn chat (+ layers) for this Case is now
            # persisted, so re-materialize the full case view to S3 ONCE at
            # turn close. Captures chat-only turns the layer-publish path never
            # touches, and refreshes the cold view's chat replay. Best-effort
            # (swallows S3 errors) so it cannot break turn teardown.
            # A1 FIX 5 (latency): fire-and-forget so the snapshot's Dynamo+S3
            # round-trips never sit on the turn-close (-> resume) path (the
            # snapshot never raises, so the detached task leaks no exception).
            _t = asyncio.create_task(
                _persist_case_view_snapshot(state, case_id=turn_case_id)
            )
            _BG_SNAPSHOT_TASKS.add(_t)
            _t.add_done_callback(_BG_SNAPSHOT_TASKS.discard)
            # #165 dual-write: refresh the thin manifest ONCE at turn close too,
            # ALONGSIDE the snapshot. Fire-and-forget; swallows its own errors.
            _tm = asyncio.create_task(
                _persist_case_manifest(state, case_id=turn_case_id)
            )
            _BG_SNAPSHOT_TASKS.add(_tm)
            _tm.add_done_callback(_BG_SNAPSHOT_TASKS.discard)
        # C2: whole-turn idle signal -- fires on EVERY exit (clean, cancel,
        # error) so the client settles any card still spinning ``running`` after
        # the turn ends (its terminal pipeline-state frame may have died on a
        # dropped socket). Outside the ``if turn_case_id`` guard so a root-stream
        # turn (no Case) still idles its cards; ``_emit_turn_complete`` reads the
        # turn's Case from the ContextVar bound at task entry. Best-effort.
        await _emit_turn_complete(
            websocket, state, pipeline_id=state.current_turn_pipeline_id
        )


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

    B-rev FIX: ``_invoke_tool_via_emitter`` now raises ``ToolNotFoundError``
    when the directive references an unregistered tool name. This caller is
    the ``/invoke`` directive path -- a manual operator-debug surface dispatched
    via ``asyncio.create_task`` (no awaiter exists to catch propagated
    exceptions). To prevent the typed exception from surfacing as an
    unhandled-task "exception was never retrieved" warning, we catch it here
    and route it through ``_send_error`` so the operator's chat surface
    receives a structured ``error`` envelope (``TOOL_NOT_FOUND`` /
    ``retryable=False``) -- the same shape Gemini's multi-turn loop produces
    via ``summarize_tool_result``. Other typed routing exceptions
    (``PayloadWarningCancelledError``) are also caught so the manual surface
    sees the cancellation reason explicitly instead of disappearing.
    """
    # Entry-time Case capture -- see _dispatch_gemini_and_persist.
    turn_case_id = _turn_case_id(state)
    bind_turn_case(turn_case_id)  # job-0277: envelope tagging
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
        # rationale as _dispatch_gemini_and_persist. Best-effort.
        await _emit_turn_complete(
            websocket, state, pipeline_id=state.current_turn_pipeline_id
        )


# --------------------------------------------------------------------------- #
# Secrets envelope handlers (FR-AS-4 + §F.3)
# --------------------------------------------------------------------------- #


async def _emit_secrets_list(
    websocket: ServerConnection,
    state: SessionState,
    *,
    case_id: str | None = None,
) -> None:
    """Emit a fresh ``secrets-list`` envelope for the caller.

    Multi-tenant isolation: scopes the listing on
    ``state.authenticated_user_id``. Falls back to the session_id when
    auth-handshake hasn't completed (the in-flight handshake fallback
    elsewhere in the dispatcher ensures this is rare).

    Best-effort on Persistence unbound -- emits an empty list rather than
    raising so the client UI can render the "no secrets yet" empty state.
    """
    p = get_persistence()
    user_id = state.authenticated_user_id or state.session_id
    if p is None:
        logger.warning(
            "secrets-list session=%s: Persistence unbound; emitting empty",
            state.session_id,
        )
        empty = SecretsListEnvelopePayload(secrets=[])
        await websocket.send(
            _new_envelope("secrets-list", state.session_id, empty)
        )
        return
    try:
        payload = await handle_secrets_list(
            user_id=user_id, case_id=case_id, persistence=p
        )
    except SecretError as exc:
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            f"secrets-list failed: {exc}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("secrets-list failed session=%s", state.session_id)
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            f"secrets-list failed: {exc}",
        )
        return
    await websocket.send(
        _new_envelope("secrets-list", state.session_id, payload)
    )
    logger.info(
        "secrets-list emitted session=%s case=%s count=%d",
        state.session_id,
        case_id,
        len(payload.secrets),
    )


async def _handle_secret_add(
    websocket: ServerConnection,
    state: SessionState,
    envelope: SecretAddEnvelopePayload,
) -> None:
    """Process a ``secret-add`` envelope and emit a refreshed ``secrets-list``.

    Per Decision F the raw ``key_value`` field is consumed by the handler
    (written to GCP Secret Manager) and **never** echoed back; the handler
    returns a vault-ref-only ``SecretRecord`` which we drop, re-emitting a
    full ``secrets-list`` so the client renders the new entry.

    Per FR-AS-8 this is NOT a confirmation trigger -- the user typing the key
    into the form IS the confirmation -- so the handler proceeds without a
    ``confirmation-request`` pause, matching the Case-lifecycle pattern.
    """
    p = get_persistence()
    user_id = state.authenticated_user_id or state.session_id
    if p is None:
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            "secret-add requires Persistence; the agent service was started "
            "without TRID3NT_MONGO_MCP_STDIO=1.",
        )
        return
    try:
        await handle_secret_add(
            envelope, user_id=user_id, persistence=p,
        )
    except SecretError as exc:
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            f"secret-add failed: {exc}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("secret-add failed session=%s", state.session_id)
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            f"secret-add failed: {exc}",
        )
        return
    # Re-emit the full secrets-list so the client refreshes its panel.
    await _emit_secrets_list(
        websocket, state, case_id=envelope.case_id
    )


async def _handle_secret_revoke(
    websocket: ServerConnection,
    state: SessionState,
    envelope: SecretRevokeEnvelopePayload,
) -> None:
    """Process a ``secret-revoke`` envelope (soft-revoke + refresh list).

    The GCP Secret Manager entry is intentionally NOT deleted -- preserves
    audit trail. Re-emits a refreshed ``secrets-list`` so the client UI
    drops the revoked entry from its active list.
    """
    p = get_persistence()
    user_id = state.authenticated_user_id or state.session_id
    if p is None:
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            "secret-revoke requires Persistence; the agent service was "
            "started without TRID3NT_MONGO_MCP_STDIO=1.",
        )
        return
    try:
        await handle_secret_revoke(
            envelope.secret_id, user_id=user_id, persistence=p,
        )
    except SecretError as exc:
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            f"secret-revoke failed: {exc}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("secret-revoke failed session=%s", state.session_id)
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            f"secret-revoke failed: {exc}",
        )
        return
    await _emit_secrets_list(websocket, state)


async def _delete_case_loaded_layer(
    state: SessionState, layer_id: str, *, case_id: str | None = None
) -> None:
    """Persist a layer deletion AUTHORITATIVELY (replace, not union).

    Mirrors the in-memory emitter's drop of ``layer_id`` from
    ``_loaded_layers`` onto the persisted ``CaseSummary`` so it cannot
    RESURRECT on the next turn or a Case reopen.

    Deliberately bypasses ``_persist_case_loaded_layers`` (that path UNIONs
    the emitter view with ``case.loaded_layer_summaries``, which would re-add
    the deleted layer). Here we REMOVE ``layer_id`` from both
    ``loaded_layer_summaries`` and ``layer_summary`` and write the result.

    Best-effort: a Persistence failure is logged but never raised; a missing
    / tombstoned Case is silently skipped. ``case_id`` pins the target Case
    explicitly; default resolves via ``_turn_case_id`` (never the raw live
    ``active_case_id``).
    """
    target_case = case_id if case_id is not None else _turn_case_id(state)
    p = get_persistence()
    if p is None or not target_case:
        return
    try:
        case = await p.get_case(target_case)
    except Exception:  # noqa: BLE001
        logger.exception(
            "layer-delete-persist: get_case failed case=%s", target_case
        )
        return
    if case is None:
        logger.debug(
            "layer-delete-persist: case=%s missing; skipping", target_case
        )
        return

    surviving_summaries: list[dict] = [
        dict(d)
        for d in case.loaded_layer_summaries
        if isinstance(d, dict) and d.get("layer_id") != layer_id
    ]
    surviving_ids: list[str] = [
        d.get("layer_id")
        for d in surviving_summaries
        if isinstance(d.get("layer_id"), str)
    ]

    # Nothing referenced this layer_id in the persisted set -- no write needed.
    if (
        case.loaded_layer_summaries == surviving_summaries
        and case.layer_summary == surviving_ids
    ):
        return

    updated = case.model_copy(
        update={
            "loaded_layer_summaries": surviving_summaries,
            "layer_summary": surviving_ids,
            "updated_at": now_utc(),
        }
    )
    try:
        await p.upsert_case(updated)
        logger.debug(
            "layer-delete-persist case=%s layer=%s remaining=%d",
            target_case,
            layer_id,
            len(surviving_ids),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "layer-delete-persist: upsert failed case=%s layer=%s",
            target_case,
            layer_id,
        )


async def _handle_layer_delete(
    websocket: ServerConnection,
    state: SessionState,
    payload_dict: Any,
) -> None:
    """Process a ``layer-delete`` envelope.

    Removes ``layer_id`` from the live emitter's ``loaded_layers``, emits a
    refreshed ``session-state`` (Map.tsx replace-not-reconcile drops the
    overlay), and persists the post-deletion list authoritatively. The
    deletion also propagates to the agent's loaded-layers awareness -- both
    the emitter's in-memory ``_loaded_layers`` and the persisted
    ``loaded_layer_summaries`` -- so ``build_layers_present_note`` stops
    listing it.

    The payload is loosely-shaped ``{layer_id: str}`` (read inline for
    forward-compat). A malformed / empty ``layer_id`` surfaces a typed
    ``TOOL_PARAMS_INVALID`` error.
    """
    layer_id: str | None = None
    if isinstance(payload_dict, dict):
        lid = payload_dict.get("layer_id")
        if isinstance(lid, str) and lid:
            layer_id = lid
    if not layer_id:
        await _send_error(
            websocket,
            state.session_id,
            "TOOL_PARAMS_INVALID",
            "layer-delete requires a non-empty string layer_id.",
        )
        return

    # Pin the target Case the same way every persistence site does so a
    # mid-turn Case switch never mis-aims the delete.
    target_case = _turn_case_id(state)

    _ensure_emitter(websocket, state)
    if state.emitter is None:  # pragma: no cover -- _ensure_emitter always binds
        return

    # Drop the layer from the live accumulator. reset_loaded_layers also
    # prunes the inline-GeoJSON side-table to the surviving ids.
    survivors: list[dict] = [
        layer.model_dump(mode="json")
        for layer in state.emitter.loaded_layers
        if layer.layer_id != layer_id
    ]
    state.emitter.reset_loaded_layers(survivors)

    # Re-inline surviving vectors BEFORE emit so a delete never transiently
    # drops sibling vector layers: emit_session_state only attaches
    # inline_geojson for ids already in _inline_geojson_by_layer_id, and the
    # client never fetches s3:// directly, so a missing inline payload means
    # the layer cannot render. ``reinline_vector_layers`` is idempotent, so
    # this is a cheap no-op when the side-table is already full.
    try:
        await state.emitter.reinline_vector_layers()
    except Exception:  # noqa: BLE001 -- re-inline is best-effort
        logger.warning(
            "layer-delete vector re-inline failed session=%s case=%s",
            state.session_id,
            target_case,
        )

    # Emit the refreshed session-state. Map.tsx removes the now-absent layer
    # from MapLibre via replace-not-reconcile (Appendix A.7). session-state is
    # session-scoped fan-out on the client, so every connection of this
    # session converges on the new loaded_layers list.
    await state.emitter.emit_session_state()

    # Persist authoritatively (replace, not the union merge -- see helper).
    await _delete_case_loaded_layer(state, layer_id, case_id=target_case)

    logger.info(
        "layer-delete session=%s case=%s layer=%s survivors=%d",
        state.session_id,
        target_case,
        layer_id,
        len(survivors),
    )


# --------------------------------------------------------------------------- #
# Per-session connection registry: session_id -> set of live ServerConnection.
# --------------------------------------------------------------------------- #
# Reap invariant: never close the keeper (the resuming connection) - it is
# identified by object identity and excluded before any close (mis-targeting
# kills the active tab). Single asyncio loop, one process -> a plain dict/set
# mutated from coroutine context needs no lock. The value-set is keyed by the
# connection object so a re-register is a no-op; an empty bucket is pruned so
# the dict cannot grow unbounded. See ADR 0027 (session durability).

#: Application close code for a prior socket reaped because a newer connection
#: of the SAME session resumed. 4xxx is the WebSocket spec's reserved
#: application range; the client treats it like any other close.
SESSION_SUPERSEDED_CLOSE_CODE = 4408

_SESSION_WS_CONNECTIONS: "dict[str, set[ServerConnection]]" = {}


def _register_session_connection(
    session_id: str, websocket: "ServerConnection"
) -> None:
    """Record ``websocket`` as a live connection of ``session_id`` (idempotent).

    Called once the connection's ``session_id`` is known (first inbound
    envelope routed through ``_handle_session_resume`` / the handler). Set
    semantics make a re-register a no-op.
    """
    if not session_id:
        return
    _SESSION_WS_CONNECTIONS.setdefault(session_id, set()).add(websocket)


def _deregister_session_connection(
    session_id: str, websocket: "ServerConnection"
) -> None:
    """Drop ``websocket`` from ``session_id``'s live-connection set.

    Called from the handler ``finally`` on EVERY exit path. ``discard`` never
    raises; an emptied bucket is pruned so the registry cannot grow unbounded.
    """
    if not session_id:
        return
    bucket = _SESSION_WS_CONNECTIONS.get(session_id)
    if bucket is None:
        return
    bucket.discard(websocket)
    if not bucket:
        _SESSION_WS_CONNECTIONS.pop(session_id, None)


def session_connection_count(session_id: str) -> int:
    """Number of live connections currently tracked for ``session_id``.

    Surfaced for tests (and post-mortem) so the per-session reap can be asserted
    directly. NEVER negative; 0 for an unknown session.
    """
    return len(_SESSION_WS_CONNECTIONS.get(session_id, ()))


async def _reap_prior_session_connections(
    session_id: str, keeper: "ServerConnection"
) -> int:
    """Proactively close every PRIOR socket of ``session_id`` except ``keeper``.

    Called on each session-resume handshake. The ``keeper`` (the resuming
    connection) is excluded by object identity FIRST so its own live socket is
    never closed. Returns the number of prior sockets closed; best-effort, a
    close that raises is swallowed and the stale socket is dropped from the
    registry either way so the count cannot wedge.
    """
    # Reap DISABLED: the eager per-session reap is incompatible with the
    # dual-socket design (2 sockets per session share a session_id) - it closed
    # the legitimate sibling and killed its mid-stream turn with 4408. Re-enable
    # ONLY with a policy that preserves the dual-socket pair and never closes a
    # socket whose session has an in-flight turn/solve. _register_session_connection
    # stays (cheap, useful); the code below is retained for that re-enable.
    return 0
    bucket = _SESSION_WS_CONNECTIONS.get(session_id)
    if not bucket:
        return 0
    # Snapshot + exclude the keeper by identity BEFORE any close so we can never
    # target the resuming connection's own live socket.
    priors = [c for c in bucket if c is not keeper]
    reaped = 0
    for prior in priors:
        # Drop from the registry first so a re-entrant reap (a near-simultaneous
        # resume) cannot double-target the same stale socket.
        bucket.discard(prior)
        try:
            await prior.close(
                code=SESSION_SUPERSEDED_CLOSE_CODE,
                reason="superseded by a newer session connection",
            )
            reaped += 1
        except Exception:  # noqa: BLE001 - best-effort; never break the resume
            # The prior socket is already closing/closed; still count it gone.
            reaped += 1
    if not bucket:
        _SESSION_WS_CONNECTIONS.pop(session_id, None)
    if reaped:
        logger.info(
            "session-resume reaped %d prior socket(s) session=%s remaining=%d",
            reaped,
            session_id,
            session_connection_count(session_id),
        )
    return reaped


def inflight_turn_count() -> int:
    """Number of in-flight turns detached from a (possibly-dead) connection.

    A long solver turn survives a socket drop (``_SESSION_LIVE_TURNS``); this
    counts the turns still running even if zero sockets are open. Kept as the
    observability probe over the live-turn registry (tests assert turn
    lifecycle through it). Counts only not-yet-done tasks (a done task is
    awaiting its self-removing callback).
    """
    total = 0
    for bucket in _SESSION_LIVE_TURNS.values():
        for live in bucket.values():
            try:
                if not live.task.done():
                    total += 1
            except Exception:  # noqa: BLE001 -- defensive; never break health
                continue
    return total


def _make_handler(settings: ModelSettings):
    """Build the per-connection coroutine, closing over the resolved settings."""

    async def handler(websocket: ServerConnection) -> None:
        # The session_id will be set on the first inbound envelope; we surface
        # an error if the client speaks before establishing one.
        state: SessionState | None = None

        # WS-30s STORM FIX (primary): start the per-connection data heartbeat so
        # the client's inbound-activity timer is reset on a fast server clock
        # (every HEARTBEAT_INTERVAL_SECONDS), independent of the possibly-slow
        # session-resume reply. Cancelled in the finally below on EVERY exit path.
        # The session_id is bound on the first inbound envelope; the heartbeat
        # frame's session_id is purely cosmetic (the client routes by transport,
        # not session, for a liveness frame) so a pre-handshake placeholder ULID
        # of zeros is fine until ``state`` is set.
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(websocket, "00000000000000000000000000")
        )

        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")

                # Pre-validate the envelope. Bad shapes get a typed error.
                try:
                    # We don't know the payload type yet; parse generically.
                    import json as _json

                    parsed = _json.loads(raw)
                    msg_type = parsed.get("type")
                    session_id = parsed.get("session_id")
                except Exception as exc:  # noqa: BLE001
                    await websocket.send(
                        _new_envelope(
                            "error",
                            "00000000000000000000000000",
                            ErrorPayload(
                                error_code="INTERNAL_ERROR",
                                message=f"malformed envelope: {exc}",
                            ),
                        )
                    )
                    continue

                if state is None:
                    state = SessionState(session_id=session_id)
                elif state.session_id != session_id:
                    await _send_error(
                        websocket,
                        state.session_id,
                        "INTERNAL_ERROR",
                        "session_id changed mid-connection",
                    )
                    continue

                payload_dict = parsed.get("payload", {})

                # WS-30s TELEMETRY (NATE): log EVERY inbound frame's type BEFORE
                # routing so "did the user's prompt arrive?" is directly visible
                # in the agent journal. The high-frequency keepalive (the client's
                # ``session-resume`` ping ~every 25s) is logged at DEBUG so it does
                # not flood the INFO stream; everything else (user-message,
                # case-command, ...) is logged at INFO. The server-sent
                # ``heartbeat`` is OUTBOUND only and never reaches this point.
                if msg_type == "session-resume":
                    logger.debug(
                        "ws-recv session=%s type=%s", session_id, msg_type
                    )
                else:
                    logger.info(
                        "ws-recv session=%s type=%s", session_id, msg_type
                    )

                # Dispatch on message type. Every payload is re-validated
                # through its concrete trid3nt_contracts model.
                try:
                    # Appendix H.5/H.3: the auth-token envelope is the
                    # connect-handshake. Anything else, before the handshake
                    # completes, trips the anonymous fallback inline so
                    # ``SessionState.authenticated_user_id`` is bound before
                    # any user-scoped action runs.
                    if msg_type == "auth-token":
                        await _handle_auth_token(
                            websocket, state, payload_dict
                        )
                        continue
                    # Implicit anonymous fallback when any other envelope
                    # arrives before the handshake -- keeps the legacy
                    # no-auth-token clients working. Remote-daemon access
                    # (2026-07): when a token gate is set, the implicit path
                    # rejects + closes the socket (returns False) so we must
                    # NOT dispatch the pending envelope.
                    if not state.auth_handshake_complete:
                        if not await _ensure_auth_handshake(websocket, state):
                            continue

                    if msg_type == "session-resume":
                        sr = SessionResumePayload.model_validate(payload_dict)
                        await _handle_session_resume(
                            websocket, state, client_case_id=sr.case_id
                        )

                    elif msg_type == "user-message":
                        um = UserMessagePayload.model_validate(payload_dict)
                        # ADR 0017 (Lane S): structured canvas AOI. Read the
                        # optional ``aoi_bbox`` DEFENSIVELY off the raw
                        # payload dict -- the UserMessagePayload contract field
                        # lands in the client lane; this seam works the moment
                        # the field arrives and is a no-op for clients that
                        # never send it. Key-present semantics: a bbox SETS
                        # the active AOI, an explicit null CLEARS it, an
                        # absent key (older client) leaves the prior AOI.
                        if "aoi_bbox" in payload_dict:
                            _set_active_aoi_from_payload(
                                state, payload_dict.get("aoi_bbox")
                            )
                        # ADR 0018: routing-visibility mode, carried as the
                        # user-message's ``tool_choice_mode`` field. Read
                        # defensively off the raw dict; a set value updates
                        # the session's sticky mode, absent/None leaves the
                        # prior mode (env default otherwise -- see
                        # _session_routing_mode). session-config below is the
                        # alternate config path.
                        _tcm = payload_dict.get("tool_choice_mode")
                        if isinstance(_tcm, str) and _tcm.strip().lower() in (
                            "auto",
                            "ask",
                        ):
                            state.routing_mode = _tcm.strip().lower()
                        # FR-FR-3: check the turn cap BEFORE dispatching.
                        # Increment first so "26th turn" fires on turn_count ==
                        # MAX_TURNS_PER_SESSION + 1. Sessions that already hit
                        # the cap are refused on every subsequent user-message
                        # with the same cap-hit envelope.
                        state.turn_count += 1
                        if (
                            MAX_TURNS_PER_SESSION > 0
                            and state.turn_count > MAX_TURNS_PER_SESSION
                        ):
                            await _handle_max_turns_reached(websocket, state)
                            continue
                        # Reset the per-turn layer accumulator before dispatch
                        # so the CaseChatMessage write captures only this
                        # turn's emissions. KNOWN LIMIT: these slots are still
                        # session-shared -- a turn running concurrently in
                        # ANOTHER Case may interleave layer/pipeline-id
                        # attribution on the closing agent row (Case targeting
                        # itself stays safe via the turn pin).
                        state.current_turn_layer_ids = []
                        state.current_turn_pipeline_id = None
                        state.current_turn_map_commands = []
                        # Pre-dispatch sequence (see ``_prepare_user_turn``):
                        # sibling-connection Case sync, AUTO-CREATE Case for a
                        # non-directive prompt from the Cases root (named via
                        # _derive_case_title; case-open + case-list emitted so
                        # the UI flips into the Case view), and the user-turn
                        # chat persist -- all BEFORE the turn task starts so
                        # chat + layer attribution land on the right (possibly
                        # brand-new) Case. Returns the parsed ``/invoke``
                        # directive; None streams through Gemini.
                        directive = await _prepare_user_turn(
                            websocket, state, um.text, client_case_id=um.case_id
                        )
                        # Stream-scoped cancellation: only a re-prompt in the
                        # SAME stream (Case, or root) replaces that stream's
                        # in-flight turn; turns in other Cases keep running.
                        # The key comes from the turn pin set by
                        # _prepare_user_turn (auto-created Cases mint a fresh
                        # ULID so they never collide with a running turn).
                        turn_key = (
                            state.current_turn_case_id or _ROOT_STREAM_KEY
                        )
                        # A same-stream re-prompt SUPERSEDES (cancels) the
                        # prior turn, even one DETACHED to the module-level
                        # registry by a prior socket close. Check this
                        # connection first, then the session-scoped live-turn
                        # registry.
                        prior = state.inflight_tasks.get(turn_key)
                        if prior is None or prior.done():
                            prior = _find_live_turn(state.session_id, turn_key)
                        if prior is not None and not prior.done():
                            prior.cancel()
                        for _done_key in [
                            k
                            for k, t in state.inflight_tasks.items()
                            if t.done()
                        ]:
                            state.inflight_tasks.pop(_done_key, None)
                        # A fresh socket may rebind onto a prior, still-running
                        # turn for this session (e.g. a live solve launched on
                        # a now-closed socket) so its progress + terminal
                        # frames reach the new socket. Harmless when no live
                        # turns exist.
                        _ensure_emitter(websocket, state)
                        _rebind_live_turns(state.session_id, state.emitter)
                        # In-chat model selector: hot-swap the model per turn.
                        # A non-None model_id in the message overrides the
                        # session default; None means "keep whatever was last
                        # chosen" (or the env default if never set).
                        #
                        # VALIDATE before use: a stale client (or a removed /
                        # access-disabled / non-tool-capable id like the old
                        # malformed `us.anthropic.claude-haiku-4-5` or DeepSeek-R1)
                        # must NEVER reach ConverseStream -- an invalid id throws a
                        # raw ValidationException that surfaced to NATE as
                        # "provided model identifier is invalid". resolve_selected_model
                        # maps an unknown id to None (use the capable default) and
                        # returns a notice we log; the turn then runs on the default
                        # rather than crashing.
                        if um.model_id is not None:
                            from .agent.adapters.bedrock_adapter import (
                                resolve_selected_model as _resolve_selected_model,
                            )

                            _effective_model, _model_notice = _resolve_selected_model(
                                um.model_id
                            )
                            if _model_notice is not None:
                                logger.warning(
                                    "model selector: %s (requested=%r session=%s)",
                                    _model_notice,
                                    um.model_id,
                                    state.session_id,
                                )
                            state.selected_model = _effective_model
                        _turn_bedrock_model = state.selected_model
                        if directive is not None:
                            tool_name, params = directive
                            task = asyncio.create_task(
                                _dispatch_tool_and_persist(
                                    websocket, state, tool_name, params, um.text
                                )
                            )
                        else:
                            task = asyncio.create_task(
                                _dispatch_gemini_and_persist(
                                    websocket,
                                    state,
                                    settings,
                                    um.text,
                                    um.research_mode,
                                    bedrock_model=_turn_bedrock_model,
                                    show_thinking=bool(um.show_thinking),
                                )
                            )
                        state.inflight_tasks[turn_key] = task
                        # Register this turn in the module registry NOW (not
                        # only on disconnect), keyed by (session_id, turn_key)
                        # with a self-removing done-callback, so a subsequent
                        # socket close just drops the per-connection ref while
                        # the running task stays durable; a reconnect rebinds
                        # the recorded emitter's sink.
                        _register_live_turn(
                            state.session_id, turn_key, task, state.emitter
                        )

                    elif msg_type == "case-command":
                        # Case lifecycle dispatch (FR-MP-6). The
                        # envelope is validated through the pydantic model
                        # so an unknown command raises ValidationError and
                        # surfaces TOOL_PARAMS_INVALID via the outer block
                        # (closed enum -- see CaseCommand Literal).
                        cmd = CaseCommandEnvelopePayload.model_validate(
                            payload_dict
                        )
                        await _handle_case_command(websocket, state, cmd)

                    elif msg_type == "layer-delete":
                        # Per-layer delete: drops ``layer_id`` from the live
                        # emitter's loaded_layers, emits a fresh session-state
                        # (Map.tsx replace-not-reconcile removes the overlay),
                        # and persists the post-deletion list AUTHORITATIVELY
                        # (replace, not the union merge, which would
                        # resurrect it) -- including the loaded-layers note
                        # source so ``build_layers_present_note`` stops
                        # listing it. Payload is loosely-shaped; read inline.
                        await _handle_layer_delete(
                            websocket, state, payload_dict
                        )

                    elif msg_type == "secret-add":
                        # Per-Case secret add (FR-AS-4 + §F.3).
                        # Key value is consumed by the handler (written to
                        # GCP Secret Manager) and never echoed back. The
                        # reply is a refreshed ``secrets-list`` envelope.
                        sa = SecretAddEnvelopePayload.model_validate(
                            payload_dict
                        )
                        await _handle_secret_add(websocket, state, sa)

                    elif msg_type == "secret-revoke":
                        # Soft-revoke a per-Case secret.
                        secret_revoke = SecretRevokeEnvelopePayload.model_validate(
                            payload_dict
                        )
                        await _handle_secret_revoke(
                            websocket, state, secret_revoke
                        )

                    elif msg_type == "secrets-list-request":
                        # Explicit list-refresh request. The
                        # envelope payload is loosely-shaped (an empty
                        # object for global list; optional ``case_id`` to
                        # scope) -- kept untyped on the schema side for
                        # forward-compat. We read case_id directly here.
                        req_case_id = None
                        if isinstance(payload_dict, dict):
                            cid = payload_dict.get("case_id")
                            if isinstance(cid, str) and cid:
                                req_case_id = cid
                        await _emit_secrets_list(
                            websocket, state, case_id=req_case_id
                        )

                    elif msg_type == "cancel":
                        CancelPayload.model_validate(payload_dict)
                        logger.info("cancel session=%s", state.session_id)
                        # Target the VISIBLE stream's turn (the
                        # stop button lives in the active Case's composer);
                        # fall back to any live turn so the pre-0269
                        # "cancel cancels the run" contract still holds
                        # when the binding moved.
                        cancel_key = (
                            state.active_case_id or _ROOT_STREAM_KEY
                        )
                        cancel_task = state.inflight_tasks.get(cancel_key)
                        if cancel_task is None or cancel_task.done():
                            live = [
                                t
                                for t in state.inflight_tasks.values()
                                if not t.done()
                            ]
                            cancel_task = live[-1] if live else None
                        # The targeted turn may have been DETACHED to the
                        # module-level live-turn registry by a prior socket
                        # close (disconnect stops cancelling but the task
                        # keeps running). The explicit stop button must still
                        # reach it -- try the keyed entry, then any live
                        # detached turn for the session.
                        if cancel_task is None or cancel_task.done():
                            cancel_task = _find_live_turn(
                                state.session_id, cancel_key
                            ) or _any_live_turn(state.session_id)
                        if cancel_task is not None and not cancel_task.done():
                            cancel_task.cancel()
                            # Wait briefly so the cancel completes deterministically
                            # within NFR-R-3 (30s budget). The pipeline-state
                            # cancelled frame is emitted from inside the task's
                            # CancelledError branch.
                            try:
                                await asyncio.wait_for(cancel_task, timeout=5.0)
                            except (asyncio.CancelledError, asyncio.TimeoutError):
                                pass

                    elif msg_type == "tool-payload-confirmation":
                        # Route the confirmation to the paused
                        # dispatch coroutine. Validate the envelope here so
                        # malformed payloads don't poison the future.
                        try:
                            conf = (
                                PayloadConfirmationEnvelopePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"tool-payload-confirmation invalid: {ve.errors()[0]['msg']}",
                            )
                            continue
                        # Resolve via the SESSION-scoped module
                        # registry -- the gate may have been registered on a
                        # DIFFERENT WebSocket connection of this same session
                        # (StrictMode double-mount / reconnect).
                        if not _resolve_pending_confirmation(
                            state.session_id, conf
                        ):
                            logger.warning(
                                "tool-payload-confirmation for unknown/closed "
                                "warning_id=%s session=%s",
                                conf.warning_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "tool-payload-confirmation accepted session=%s "
                            "warning_id=%s decision=%s",
                            state.session_id,
                            conf.warning_id,
                            conf.decision,
                        )

                    elif msg_type == "credential-provided":
                        # Resolves the paused dispatch coroutine's future once
                        # the user saves/declines a requested credential -- the
                        # tool retries (provided=True) or re-raises the
                        # original typed error (provided=False). Carries NO
                        # key material (Decision F); the key itself was saved
                        # via ``secret-add`` on its own envelope path.
                        try:
                            cp = (
                                CredentialProvidedEnvelopePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"credential-provided invalid: {ve.errors()[0]['msg']}",
                            )
                            continue
                        if not _resolve_pending_credential(state.session_id, cp):
                            logger.warning(
                                "credential-provided for unknown/closed "
                                "request_id=%s session=%s",
                                cp.request_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "credential-provided accepted session=%s "
                            "request_id=%s provided=%s",
                            state.session_id,
                            cp.request_id,
                            cp.provided,
                        )

                    elif msg_type == "region-choice-provided":
                        # region-disambiguation picker: the user narrowed the
                        # state-bbox-fallback geocode to a sub-region (or kept
                        # the whole state). Resolve the paused dispatch
                        # coroutine's future so it applies the picked bbox (or
                        # keeps the state bbox). Mirrors credential-provided --
                        # may arrive on a sibling connection of the session.
                        try:
                            rc = (
                                RegionChoiceProvidedEnvelopePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"region-choice-provided invalid: {ve.errors()[0]['msg']}",
                            )
                            continue
                        if not _resolve_pending_region_choice(
                            state.session_id, rc
                        ):
                            logger.warning(
                                "region-choice-provided for unknown/closed "
                                "request_id=%s session=%s",
                                rc.request_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "region-choice-provided accepted session=%s "
                            "request_id=%s choice=%s",
                            state.session_id,
                            rc.request_id,
                            rc.choice,
                        )

                    elif msg_type == "spatial-input-response":
                        # FR-AS-10 / FR-WC-16: the user finished (or cancelled)
                        # the terra-draw surface. Resolve the paused
                        # request_spatial_input future so the dispatch coroutine
                        # parses the drawn FeatureCollection into engine-ready
                        # barriers / AOI / points. Mirrors region-choice-provided
                        # -- may arrive on a sibling connection of the session.
                        try:
                            spatial_resp = (
                                SpatialInputResponsePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            # FR-WC-16 untagged-barrier mismatch (the critical
                            # correctness fix): the reply ARRIVED but failed
                            # structural validation (e.g. a barrier feature
                            # missing barrier_type). The user-facing notification
                            # stays, but we MUST also FAIL the pending future
                            # eagerly so the paused request_spatial_input turn
                            # wakes IN-BAND with a typed error result instead of
                            # hanging until default_timeout_seconds (~300s) then
                            # degrading to SPATIAL_INPUT_TIMEOUT. The request_id
                            # is parsed defensively from the raw payload (it may
                            # itself be absent/garbage on a totally malformed
                            # envelope -- then we just notify + continue, no crash).
                            err_msg = ve.errors()[0]["msg"]
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"spatial-input-response invalid: {err_msg}",
                            )
                            req_id = None
                            if isinstance(payload_dict, dict):
                                rid = payload_dict.get("request_id")
                                if isinstance(rid, str) and rid:
                                    req_id = rid
                            if req_id is not None and _fail_pending_spatial_input(
                                state.session_id,
                                req_id,
                                "SPATIAL_INPUT_BAD_BARRIER_TYPE",
                                err_msg,
                            ):
                                logger.info(
                                    "spatial-input-response invalid: FAILED "
                                    "pending future session=%s request_id=%s "
                                    "(no timeout wait)",
                                    state.session_id,
                                    req_id,
                                )
                            else:
                                logger.warning(
                                    "spatial-input-response invalid with no "
                                    "resolvable pending request_id=%s session=%s "
                                    "(notified only)",
                                    req_id,
                                    state.session_id,
                                )
                            continue
                        if not _resolve_pending_spatial_input(
                            state.session_id, spatial_resp
                        ):
                            logger.warning(
                                "spatial-input-response for unknown/closed "
                                "request_id=%s session=%s",
                                spatial_resp.request_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "spatial-input-response accepted session=%s "
                            "request_id=%s cancelled=%s geometry_type=%s",
                            state.session_id,
                            spatial_resp.request_id,
                            spatial_resp.cancelled,
                            spatial_resp.geometry_type,
                        )

                    elif msg_type == "tool-choice":
                        # ADR 0018: the user's reply to a pending
                        # ``tool-candidates`` card, parsed defensively as a
                        # loose dict (until the typed contracts model lands).
                        # Resolves the paused turn's future -- may arrive on a
                        # sibling connection of the session.
                        if not isinstance(payload_dict, dict) or not isinstance(
                            payload_dict.get("request_id"), str
                        ):
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                "tool-choice requires a request_id",
                            )
                            continue
                        if not _resolve_pending_tool_choice(
                            state.session_id, payload_dict
                        ):
                            logger.warning(
                                "tool-choice for unknown/closed request_id=%s "
                                "session=%s",
                                payload_dict.get("request_id"),
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "tool-choice accepted session=%s request_id=%s "
                            "tool=%r free_text=%s",
                            state.session_id,
                            payload_dict.get("request_id"),
                            payload_dict.get("tool_name"),
                            bool(payload_dict.get("free_text")),
                        )

                    elif msg_type == "catalog-addition-response":
                        # §F.1.2 Mode 2 offer-to-add: the user accepted /
                        # rejected a mode2-candidate. The plugin card echoes the
                        # candidate_id back as request_id. On accept the server
                        # drafts + probes + appends the entry to the user-overlay
                        # catalog; reject / cancel just drops the pending offer.
                        # Non-blocking + best-effort (never hangs the loop).
                        try:
                            car = CatalogAdditionResponsePayload.model_validate(
                                payload_dict
                            )
                        except ValidationError as ve:
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"catalog-addition-response invalid: "
                                f"{ve.errors()[0]['msg']}",
                            )
                            continue
                        await _handle_catalog_addition_response(
                            websocket, state, car
                        )
                        logger.info(
                            "catalog-addition-response accepted session=%s "
                            "request_id=%s decision=%s cancelled=%s",
                            state.session_id,
                            car.request_id,
                            car.decision,
                            car.cancelled,
                        )

                    elif msg_type == "session-config":
                        # ADR 0018 (Stage 3): per-session settings. Currently
                        # the routing-visibility ``mode`` ('auto' | 'ask') --
                        # read DEFENSIVELY off the raw dict (the contracts
                        # lane declares the typed model). Unknown fields are
                        # ignored for forward-compat.
                        if isinstance(payload_dict, dict):
                            _cfg_mode = payload_dict.get("mode")
                            if isinstance(_cfg_mode, str) and _cfg_mode.strip().lower() in (
                                "auto",
                                "ask",
                            ):
                                state.routing_mode = _cfg_mode.strip().lower()
                                logger.info(
                                    "session-config: routing mode=%s session=%s",
                                    state.routing_mode,
                                    state.session_id,
                                )
                            elif _cfg_mode is not None:
                                logger.warning(
                                    "session-config: unknown mode %r ignored "
                                    "session=%s",
                                    _cfg_mode,
                                    state.session_id,
                                )
                            # BENCH pre-dispatch block hook: arms/disarms the
                            # bench tool-block config (absent=untouched,
                            # dict=arm, null/false=disarm). Bench-only -- a
                            # normal client never sends this key.
                            if "bench_tool_block" in payload_dict:
                                from .agent.gates.tool_gating import parse_bench_block_config

                                _bench_cfg = parse_bench_block_config(payload_dict)
                                state.bench_block_config = _bench_cfg
                                logger.info(
                                    "session-config: bench_tool_block %s "
                                    "session=%s (allow=%d always=%d block=%d)",
                                    "armed" if _bench_cfg else "disarmed",
                                    state.session_id,
                                    len(_bench_cfg.allow) if _bench_cfg else 0,
                                    len(_bench_cfg.always_allowed) if _bench_cfg else 0,
                                    len(_bench_cfg.block_at_invocation)
                                    if _bench_cfg
                                    else 0,
                                )

                    elif msg_type in (
                        "confirm-response",
                        "disambiguation-response",
                        "clarification-response",
                    ):
                        # M1: scaffolding only -- no triggers yet. Log and
                        # acknowledge without acting.
                        logger.info("noop M1 message_type=%s", msg_type)

                    else:
                        await _send_error(
                            websocket,
                            state.session_id,
                            "INTERNAL_ERROR",
                            f"unknown message type: {msg_type!r}",
                        )

                except ValidationError as ve:
                    await _send_error(
                        websocket,
                        state.session_id,
                        "TOOL_PARAMS_INVALID",
                        f"payload validation failed: {ve.errors()[0]['msg']}",
                    )

        except (ConnectionClosedError, ConnectionClosedOK) as exc:
            # Normal/abnormal peer closes (pong timeout, tab/mobile close,
            # network blip, StrictMode socket churn) are not crashes - log a
            # quiet one-liner instead of a full traceback.
            #
            # WS-30s TELEMETRY (NATE): log the close code + reason at INFO so the
            # "why did the socket die?" question is directly answerable from the
            # journal (e.g. a 1006/no-close-frame storm vs a clean 1000/1001 tab
            # close). This is one line per disconnect, not a per-frame flood.
            logger.info(
                "ws-close session=%s code=%s reason=%r",
                getattr(state, "session_id", None),
                getattr(exc, "code", None),
                getattr(exc, "reason", None),
            )
        except Exception:
            logger.exception("connection handler crashed")
        finally:
            # Drop this socket from the per-session connection registry on EVERY
            # exit path so the reaper never targets a connection already gone and
            # the registry cannot grow unbounded. Guard on ``state`` - a socket
            # that closed before its first envelope never bound a session_id.
            # Idempotent: a socket already reaped by a sibling's resume is a
            # harmless discard.
            if state is not None:
                _deregister_session_connection(state.session_id, websocket)
                # OPEN-8: once the session's LAST live socket is gone, drop the
                # cached case-list digest too -- otherwise a later reconnect
                # (fresh SessionState, unaware of the stale digest) could
                # inherit an emit-skip decision from a connection that no
                # longer exists. ``session_connection_count`` is 0 only when
                # every sibling socket of this session has also deregistered.
                if session_connection_count(state.session_id) == 0:
                    _clear_case_list_hash(state.session_id)
            # WS-30s STORM FIX: stop the per-connection data heartbeat on EVERY
            # exit path (normal close, crash, cancellation, loop exhaustion) so
            # the background task never outlives its socket. Cancel + await so the
            # CancelledError is observed (no "Task was destroyed but it is pending"
            # warning); a never-started/already-done task is a harmless no-op.
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # CancelledError is the expected clean-stop path; any other error
                # from the dying task must not mask the disconnect handling below.
                pass
            # A socket close must NOT cancel an in-flight turn: on disconnect,
            # DETACH rather than cancel. Each turn is registered in the
            # module-level ``_SESSION_LIVE_TURNS`` registry at spawn (keyed by
            # (session_id, turn_key)) with a self-removing done-callback, so it
            # survives the death of this connection; a reconnecting socket
            # rebinds the live turn's emitter sink so progress + terminal
            # frames still reach the user, and a fully-disconnected solve
            # still publishes + persists its layer (rehydrates on the next
            # case-open). ``wait_for_completion``'s own 1800s budget bounds a
            # stuck solve. Genuine cancellation (stop button, same-stream
            # supersede) still cancels -- only the disconnect path stops
            # cancelling. Cheap LLM-only turns are simply left to finish;
            # their done-callback removes them from the registry.
            if state:
                for _turn_key, _t in list(state.inflight_tasks.items()):
                    if _t.done():
                        continue
                    # Ensure the durable registry holds it (it was registered at
                    # spawn for user-message turns; re-assert for any path that
                    # populated inflight_tasks without registering -- defensive,
                    # idempotent). NB: this finally DETACHES and KEEPS the turn
                    # RUNNING -- it never sets ``state.emitter = None``. The live
                    # turn keeps driving its OWN emitter, whose ``_sink`` still
                    # closes over THIS (now-dead) socket and silently no-ops on
                    # send, until a reconnecting socket rebinds that emitter's
                    # sink (``_rebind_live_turns``) so the remaining progress +
                    # terminal frames land on the user's live connection.
                    if _find_live_turn(state.session_id, _turn_key) is not _t:
                        _register_live_turn(
                            state.session_id, _turn_key, _t, state.emitter
                        )
                    logger.info(
                        "connection closed with in-flight turn session=%s "
                        "turn_key=%s: DETACHED (kept running), not cancelled",
                        state.session_id,
                        _turn_key,
                    )

    return handler


async def run_server(host: str = "127.0.0.1", port: int | None = None) -> None:
    """Serve forever. Override port via ``TRID3NT_AGENT_PORT``.

    Best-effort inits the ``Persistence`` singleton; without a provisioned
    MCP environment the agent starts anyway (the M1 in-memory chat/pipeline
    path keeps working, and any caller requiring persistence raises a clear
    error). Also mounts the read-only HTTP catalog endpoint at
    ``TRID3NT_AGENT_HTTP_PORT`` (default 8766) as a sibling of the WS server
    (same loop, same process); a failure to start it logs but does not abort
    WS startup.
    """
    if port is None:
        port = int(os.environ.get("TRID3NT_AGENT_PORT", "8765"))
    # Bind host override so the dev agent is reachable from the
    # LAN / tailnet (phone demos). Default stays loopback-only; opt in via
    # TRID3NT_AGENT_HOST=0.0.0.0. The real public surface is a later increment.
    host = os.environ.get("TRID3NT_AGENT_HOST", host)
    settings = load_settings()
    # Log the ACTUAL active provider + its real model -- never the dormant
    # Vertex/Gemini settings defaults. Under MODEL_PROVIDER=openai this prints
    # the OpenAI model; under bedrock the Bedrock model id; scripted/replay/fake
    # or the retained-dormant vertex seam fall back to the settings model.
    from .agent.adapters.bedrock_adapter import (
        model_provider as _active_model_provider,
        bedrock_model_id as _active_bedrock_model_id,
    )

    _active_provider = _active_model_provider()
    if _active_provider == "openai":
        from .agent.adapters import openai_adapter as _active_oa
        _active_model = _active_oa.openai_model(None)
    elif _active_provider == "bedrock":
        _active_model = _active_bedrock_model_id()
    else:
        _active_model = settings.model
    logger.info(
        "starting agent server host=%s port=%d provider=%s model=%s",
        host,
        port,
        _active_provider,
        _active_model,
    )
    # #6 (loop-safety): armed-only emit-free safety gate for the staged
    # sync-tool dispatch off-load. No-op (one log line) under the dark default;
    # raises and aborts startup if TRID3NT_SYNC_TOOL_OFFLOAD is armed for a tool
    # whose body would touch the loop-bound emitter from a worker thread.
    _assert_sync_offload_safe()
    try:
        await init_persistence_from_env()
    except Exception as exc:  # noqa: BLE001 -- startup must not abort on MCP issues
        logger.warning("Persistence init failed (continuing without MCP): %s", exc)
    # One-time idempotent migration: stamps every pre-Auth Case (no
    # ``user_id``) with the MIGRATION_ANON_UID sentinel instead of leaking to
    # every signed-in user. Best-effort -- a hiccup must not abort startup.
    await _run_preauth_case_migration()
    # COLDVIEW FRESHNESS BACKFILL (daemon restart): re-materialize every live
    # Case's cold snapshot+manifest so a daemon-down Case serves a CURRENT cold
    # face without a warm re-open (closes the snapshot-freshness gap).
    # Fire-and-forget so the sweep NEVER delays accepting the first connection
    # after restart; it is tracked in _BG_SNAPSHOT_TASKS so the
    # graceful-shutdown drain awaits it and an unreferenced task is not GC'd
    # mid-flight (same discipline as the per-turn snapshot writes).
    # _run_coldview_backfill is self-guarding (no-Persistence / disabled /
    # per-Case best-effort) and never raises.
    _coldview_task = asyncio.create_task(_run_coldview_backfill())
    _BG_SNAPSHOT_TASKS.add(_coldview_task)
    _coldview_task.add_done_callback(_BG_SNAPSHOT_TASKS.discard)

    # TOOL-RETRIEVAL INDEX WARM-AT-STARTUP: when retrieval is enabled
    # (shadow/enforce), build the discover index off-loop NOW instead of
    # lazily on the first search_tools tool call. Without this every
    # turn's _discover_topk sees a COLD index and FAIL-OPENS to the full
    # ~176-tool registry -- harmless for 200k-context cloud models, but a
    # SMALL-CONTEXT local model (offline build, e.g. 16k Ollama) gets its
    # request silently truncated, so it cannot see tool schemas and guesses
    # argument names. Fire-and-forget: a failed warm just leaves the
    # documented fail-open behavior in place; never delays serving.
    if _tool_retrieval_mode() != "off":
        async def _warm_discover_index() -> None:
            try:
                from .agent.tools.search.search_tools import search_tools as _dd_warm
                await asyncio.to_thread(_dd_warm._get_index)
                logger.info("tool_retrieval: discover index warmed at startup")
            except Exception:  # noqa: BLE001 -- warm is best-effort
                logger.warning(
                    "tool_retrieval: startup index warm failed; fail-open stays",
                    exc_info=True,
                )
        _warm_task = asyncio.create_task(_warm_discover_index())
        _BG_SNAPSHOT_TASKS.add(_warm_task)
        _warm_task.add_done_callback(_BG_SNAPSHOT_TASKS.discard)

    handler = _make_handler(settings)

    # Wave 4.10 C1: best-effort mount of the catalog HTTP listener.
    http_server = None
    try:
        from .tool_catalog_http import serve_catalog_http

        http_server = await serve_catalog_http(host=host)
    except Exception:  # noqa: BLE001 -- discovery surface, never blocks WS
        logger.exception(
            "tool-catalog HTTP listener failed to start; "
            "continuing without /api/tool-catalog"
        )

    try:
        # A1 FIX 3 (EXPLICIT SERVE KEEPALIVE): the bare ``serve(handler, host,
        # port)`` left websockets on its defaults, so the server emitted no
        # protocol-level pings and gave a stalled send the default close grace.
        # Pin ping_interval/ping_timeout (~20s/20s) so the SERVER actively
        # probes liveness and reaps a truly-dead peer on its own clock (the
        # client's app-level session-resume keepalive is the belt; this is the
        # suspenders), and a sane ``close_timeout`` so a terminal frame written
        # onto a half-closed socket doesn't hang the handler. These are
        # deliberately looser than the client's 25s/10s app keepalive so the
        # two layers don't fight (the client force-reconnects first on a real
        # stall; the server ping just keeps an otherwise-idle-but-alive socket
        # from being culled by an intermediary).
        async with serve(
            handler,
            host,
            port,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ):
            await asyncio.Future()  # serve forever
    finally:
        # COLDVIEW DURABILITY (J1): graceful-shutdown drain of any outstanding
        # detached case-view snapshot / manifest writes. A SIGTERM (graceful
        # uvicorn/process stop) cancels ``await asyncio.Future()`` and unwinds
        # here while the per-turn / turn-close sites may still have fire-and-
        # forget snapshot PUTs in ``_BG_SNAPSHOT_TASKS``. gather them with a
        # bounded timeout so the flush cannot hang shutdown indefinitely; each
        # task swallows its own errors (returns False / never raises), and
        # ``return_exceptions=True`` plus the wait_for guard keep a slow/failed
        # PUT from blocking the rest of teardown. (The publish site already
        # awaits inline; this closes the per-turn/turn-close write race and any
        # future fire-and-forget site so an immediate StopInstances does not
        # leave a stale cold snapshot.)
        await _drain_bg_snapshot_tasks()
        if http_server is not None:
            http_server.close()
            try:
                await http_server.wait_closed()
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "run_server",
    "SessionState",
    "_invoke_tool_via_emitter",
    "_maybe_gate_on_payload_warning",
    "_parse_invoke_directive",
    "get_persistence",
    "set_persistence",
    "init_persistence_from_env",
    # Live-turn registry probe (tests assert detached-turn lifecycle with it).
    "inflight_turn_count",
    # Case lifecycle handlers + chat persistence.
    "_emit_case_list",
    "_emit_case_open",
    "_handle_case_command",
    "_persist_chat_turn",
    # Lane A1: view-without-agent -- materialize the full case view to S3.
    "_persist_case_view_snapshot",
    # #165 data-island: dual-write the THIN per-case manifest to S3.
    "_persist_case_manifest",
    # COLDVIEW DURABILITY (J1): graceful-shutdown drain of detached snapshot
    # writes (the publish site now awaits inline; per-turn/turn-close stay
    # detached and are flushed here on shutdown).
    "_drain_bg_snapshot_tasks",
    # Turn-start Case binding (cross-Case contamination fix).
    "_turn_case_id",
    "_dispatch_tool_and_persist",
    "_dispatch_gemini_and_persist",
    # Auto-create Case from the Cases root.
    "_auto_create_case_from_root",
    "_emit_auto_case_open",
    "_prepare_user_turn",
    # Secrets envelope handlers.
    "_emit_secrets_list",
    "_handle_secret_add",
    "_handle_secret_revoke",
    # job VAULT-READ: credential pipeline (secret_ref injection + JIT prompt).
    "_inject_secret_ref",
    "_resolve_active_secret_ref",
    "_maybe_handle_credential_error",
    "_emit_credential_request_and_wait",
    "_resolve_pending_credential",
    # job-B8+B9 (Wave 4.10 Stage 3): circuit breaker + loop_exhausted.
    "_send_loop_exhausted",
    "CircuitBreakerError",
    "ToolCircuitBreaker",
]
