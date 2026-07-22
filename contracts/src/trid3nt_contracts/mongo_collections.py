"""Wave 4.11 telemetry + audit collection contracts (job-0200/job-0201).

Three new Atlas collections that land in Wave 4.11 Stage 1A as the telemetry
substrate.  These are SEPARATE from the five core collections in
``trid3nt_contracts.collections`` (projects / runs / articles / events /
sessions) — they serve observability and dev-acceleration use cases, not
primary application data.

Collections:

``tool_call_telemetry``
    One document per LLM-initiated or workflow-initiated tool invocation.
    The headline use case for Wave 4.11: data-driven hot-set composition,
    per-wave routing-regression detection, discover_dataset co-occurrence
    scoring, and cache-hit observability.  TTL: 90 days.

``description_audit``
    One document per tool-description variant evaluated in an A/B routing
    experiment.  Records the description text, a routing-correctness score,
    and the session batch it was measured on.  Long-lived (no TTL); small
    table (~1 row per description variant per wave).

``case_telemetry``
    One document per Case (aggregated from ``tool_call_telemetry``); carries
    Case-level aggregate statistics (total tools called, most-called tool,
    error rate, last-activity).  Refreshed on a scheduled basis by the
    hot-set query job (job-0205).

Design notes:
- All models extend ``DocModel`` from ``collections.py`` so they get the
  ``_id``/``id`` alias pattern and ``by_alias=True`` dump convention.
- The ``MONGO_DUMP_KWARGS`` constant from ``collections.py`` applies here too.
- Atlas JSON Schema validators for these collections are authored in
  ``job-0201`` and provisioned via Atlas CLI at collection-bootstrap time.
  The Pydantic models here are the Python contract side; they mirror the
  Atlas validators but the Atlas validators are authoritative for the DB.
- ``called_at_utc`` on ``ToolCallTelemetryDocument`` is the TTL index key.
  The Atlas TTL index (90 days) is provisioned by job-0201 bootstrap.
- ``ULIDStr`` ids are time-sortable — no secondary ``created_at`` index is
  needed on telemetry for date-range queries; the id prefix sorts by time.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .collections import DocModel
from .common import UTCDatetime, ULIDStr

__all__ = [
    "ToolCallTelemetryDocument",
    "DescriptionAuditDocument",
    "CaseTelemetryDocument",
    "TELEMETRY_COLLECTION",
    "DESCRIPTION_AUDIT_COLLECTION",
    "CASE_TELEMETRY_COLLECTION",
    "TELEMETRY_TTL_DAYS",
    "TELEMETRY_INDEX_CONFIG",
    "DESCRIPTION_AUDIT_INDEX_CONFIG",
]


# ---------------------------------------------------------------------------
# Collection name constants — used by Persistence and the Atlas bootstrap CLI
# ---------------------------------------------------------------------------

#: Atlas collection for per-tool-invocation telemetry events.
TELEMETRY_COLLECTION = "tool_call_telemetry"

#: Atlas collection for tool-description A/B routing experiments.
DESCRIPTION_AUDIT_COLLECTION = "description_audit"

#: Atlas collection for Case-level aggregated telemetry.
CASE_TELEMETRY_COLLECTION = "case_telemetry"

#: TTL for ``tool_call_telemetry`` documents in days.  After 90 days old
#: records roll off automatically via the Atlas TTL index on ``called_at_utc``.
TELEMETRY_TTL_DAYS: int = 90


# ---------------------------------------------------------------------------
# Atlas index configuration constants
# ---------------------------------------------------------------------------

#: ``tool_call_telemetry`` index config for Atlas CLI / job-0201 bootstrap.
#:
#: - TTL index on ``called_at_utc`` (expireAfterSeconds = 90 days)
#: - BM25 (Atlas Search) index on ``tool_name`` for fast routing-audit queries
TELEMETRY_INDEX_CONFIG: dict[str, Any] = {
    "ttl": {
        "field": "called_at_utc",
        "expire_after_seconds": TELEMETRY_TTL_DAYS * 24 * 60 * 60,
    },
    "search": {
        "name": "telemetry_tool_name_search",
        "definition": {
            "mappings": {
                "dynamic": False,
                "fields": {
                    "tool_name": {"type": "string"},
                    "session_id": {"type": "string"},
                    "case_id": {"type": "string"},
                    "result_ok": {"type": "boolean"},
                },
            }
        },
    },
}

#: ``description_audit`` index config for Atlas CLI / job-0201 bootstrap.
#:
#: - BM25 (Atlas Search) on ``tool_name`` + ``description`` for A/B lookup
#: - Dense vector index on ``description_embedding`` (768-dim default) for
#:   semantic similarity search across description variants
DESCRIPTION_AUDIT_INDEX_CONFIG: dict[str, Any] = {
    "search": {
        "name": "description_audit_bm25",
        "definition": {
            "mappings": {
                "dynamic": False,
                "fields": {
                    "tool_name": {"type": "string"},
                    "description_text": {"type": "string"},
                    "wave_label": {"type": "string"},
                },
            }
        },
    },
    "vector_search": {
        "name": "description_audit_dense",
        "definition": {
            "fields": [
                {
                    "type": "vector",
                    "path": "description_embedding",
                    "numDimensions": 768,
                    "similarity": "cosine",
                }
            ]
        },
    },
}


# ---------------------------------------------------------------------------
# Document models
# ---------------------------------------------------------------------------


class ToolCallTelemetryDocument(DocModel):
    """``tool_call_telemetry``: one record per LLM-initiated tool invocation.

    Written by the telemetry writer in ``adapter.py`` (job-0202) as a
    fire-and-forget non-blocking MCP ``insert-one``.  Consumers:

    - ``discover_dataset`` co-occurrence scoring (job-0204): queries
      ``tool_name`` pairs co-called within the same session.
    - ``get_hot_set`` (job-0205): queries top-N tools from last 30 sessions
      per user.
    - Routing-quality dashboard (job-0206): aggregates ``result_ok``,
      ``latency_ms``, ``called_at_utc`` per ``tool_name``.
    - Cache-hit observability: ``cached_content_token_count`` per turn.

    Field notes:
    - ``id`` (``_id`` on wire) is a ULID — time-sortable, no secondary date
      index needed for time-range queries.
    - ``case_id`` is optional — not all sessions have an active Case (the M1
      anonymous chat path has no Case context).
    - ``user_id`` is optional — anonymous sessions produce no user linkage
      until Auth (job-0203) wires user_id from the auth handshake.
    - ``args_hash`` is the SHA-256 hex digest over the JSON-serialized args
      dict (see ``telemetry.compute_args_hash``).  Not the args themselves —
      this keeps the telemetry compact while enabling dedup and tracing.
    - ``error_code`` uses the SCREAMING_SNAKE_CASE A.6 error code convention.
    """

    id: ULIDStr = Field(alias="_id")

    # --- Identity context ---
    session_id: str
    user_id: str | None = None    # absent for anonymous sessions
    case_id: str | None = None    # absent when no active Case

    # --- Tool invocation facts ---
    tool_name: str
    called_at_utc: UTCDatetime
    source: Literal["llm", "workflow", "manual"]
    args_hash: str                # SHA-256 hex of JSON-serialized args

    # --- Outcome ---
    result_ok: bool
    latency_ms: float
    error_code: str | None = None
    retry_attempt: int = 0        # 0 = first attempt; 1 = first retry; etc.

    # --- Tool-accuracy panel (NATE 2026-06-17) ---
    #: Whether the call produced a USABLE result, distinct from ``result_ok``
    #: (which only says the call did not raise / was not failure-tagged). A
    #: layer-producing tool that returned status="ok" while carrying an EMPTY
    #: layers list is ``result_ok=True`` but ``result_usable=False`` (the
    #: honesty-floor NO_RENDERABLE_LAYER case). ``None`` where the notion does
    #: not apply (meta / control-plane tools that never produce a layer or data
    #: payload). Derived at the dispatch chokepoint by
    #: ``adapter.classify_result_usable``.
    result_usable: bool | None = None
    #: Routing-quality heuristic (NOT ground truth): ``False`` when this call
    #: was IMMEDIATELY superseded within the same session by a DIFFERENT tool
    #: for the same logical step (a retry/correction of a mis-route), ``True``
    #: when it was not superseded, ``None`` when the signal is unavailable. A
    #: defensible signal for "the model picked the right tool", clearly labelled
    #: heuristic so the dashboard never claims certainty.
    routed_ok: bool | None = None

    # --- Cache observability ---
    cached_content_token_count: int | None = None  # from Gemini UsageMetadata

    # --- Model dimension (in-chat model selector, NATE 2026-06-17) ---
    #: The Bedrock model id that served this call (e.g.
    #: ``"us.anthropic.claude-sonnet-4-6"``).  ``None`` for legacy records
    #: written before this field was added, and for non-Bedrock providers.
    #: Used by the routing-quality dashboard ``by_model`` breakdown.
    model_id: str | None = None


class DescriptionAuditDocument(DocModel):
    """``description_audit``: one record per tool-description variant.

    Used for A/B routing experiments: different description phrasings are
    evaluated against a routing-correctness score (fraction of prompts where
    the tool was correctly selected).  Analysts compare variants across waves
    to drive description rewrites.

    Consumers:
    - Wave 4.11 routing-quality dashboard (job-0206): surface most-effective
      description per tool and per wave.
    - ``discover_dataset`` dense index (job-0204): ``description_embedding``
      enables semantic similarity retrieval in addition to BM25.

    Field notes:
    - ``wave_label`` is a human string like ``"wave-4.10"`` or ``"4.11-b"``;
      not a ULID — it's a label, not an id.
    - ``routing_correctness_score`` is a float [0, 1] = fraction of test
      prompts where the tool was dispatched correctly under this description.
      ``None`` before evaluation runs.
    - ``description_embedding`` is the 768-dim text-embedding-005 vector of
      ``description_text``; populated by the daily index refresh job.  Stored
      as a list of floats; ``None`` before the embedding batch runs.
    """

    id: ULIDStr = Field(alias="_id")

    tool_name: str
    wave_label: str                     # e.g. "wave-4.10", "wave-4.11-b"
    description_text: str               # the full tool docstring under test
    description_hash: str               # SHA-256 hex — dedup across waves
    routing_correctness_score: float | None = None  # [0, 1]; None = not yet scored
    sample_session_count: int = 0       # sessions used to derive the score
    created_at: UTCDatetime
    updated_at: UTCDatetime

    # Dense embedding — populated by daily index refresh (job-0204).
    description_embedding: list[float] | None = None


class CaseTelemetryDocument(DocModel):
    """``case_telemetry``: Case-level aggregate refreshed by hot-set query job.

    One document per Case; refreshed on schedule (Cloud Scheduler → job-0205
    aggregation query over ``tool_call_telemetry``).  Provides a lightweight
    per-Case summary for the routing-quality dashboard and for future per-user
    preferred-tool routing.

    Field notes:
    - ``total_tool_calls`` = total dispatched tool invocations in this Case.
    - ``error_rate`` = fraction of calls where ``result_ok=False``; float [0, 1].
    - ``top_tool_names`` = top-5 most-dispatched tool names (by call count),
      ordered descending.  Maximum 5 entries.
    - ``refreshed_at`` = when this document was last written by the aggregation
      job; stale if older than 1 hour in the dashboard.
    """

    id: str = Field(alias="_id")        # == case_id (not a new ULID)

    case_id: str
    user_id: str | None = None

    total_tool_calls: int = 0
    error_rate: float = 0.0             # [0, 1]
    avg_latency_ms: float | None = None
    top_tool_names: list[str] = Field(default_factory=list, max_length=5)
    last_activity_at: UTCDatetime | None = None
    refreshed_at: UTCDatetime
