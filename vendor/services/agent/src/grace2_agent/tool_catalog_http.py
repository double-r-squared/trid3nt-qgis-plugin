"""HTTP catalog endpoint (Wave 4.10 Stage 3 — job C1).

Exposes two read-only JSON endpoints:

- ``GET /api/tool-catalog`` — the agent's atomic-tool surface (Wave 4.10 C1).
- ``GET /api/telemetry/summary`` — aggregated routing-quality stats over the
  most recent 30 sessions, backing the Wave 4.11 M7 routing-quality
  dashboard (this module is the only HTTP seam — adding a second endpoint
  keeps the listener as a single asyncio TCP server).

Why a dedicated HTTP endpoint when the rest of the agent talks WebSockets?

- The catalog is a **discovery surface** for human users browsing what the
  agent can do. It is not part of the chat envelope contract (Appendix A) —
  it does not stream, does not maintain session state, and does not require
  an authenticated user. A plain HTTP GET is the right shape.
- The catalog payload is small (~71 tools × ~1.5 KB each ≈ 100 KB) and
  cacheable. Routing it through the WS path would couple a static catalog
  read to session lifecycle.

The endpoint runs on its own asyncio TCP listener (default port 8766;
override via ``GRACE2_AGENT_HTTP_PORT``). It is mounted as a sibling of the
WebSocket server in ``server.run_server``, NOT in its own process — single
process, single asyncio loop, no thread sharing.

Backed entirely by:
- ``grace2_agent.categories.CATEGORIES`` / ``PRIMARY_CATEGORY`` /
  ``SECONDARY_CATEGORIES`` — the 12 categories landed by job-B5.
- ``grace2_agent.tools.TOOL_REGISTRY`` — every registered tool's
  ``AtomicToolMetadata`` carries the MCP annotation hints
  (``read_only_hint``, ``open_world_hint``, ``destructive_hint``,
  ``idempotent_hint``) + ``supports_global_query`` +
  ``payload_mb_estimator_name``.
- ``data/tool_query_corpus.yaml`` — example sample-queries keyed by tool name.

CORS: ``Access-Control-Allow-Origin: *`` so the Vite dev server (5173) and
production builds on any origin can hit the endpoint without preflight
friction. The endpoint is read-only and unauthenticated; permissive CORS is
the correct posture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("grace2_agent.tool_catalog_http")

__all__ = [
    "build_catalog_payload",
    "load_query_corpus",
    "serve_catalog_http",
    "build_telemetry_summary",
    "DEFAULT_HTTP_PORT",
]


DEFAULT_HTTP_PORT = 8766

# Module-level cache: loaded once on the first request, retained until the
# agent process restarts. Matches the "reset on agent restart" requirement
# in the C1 kickoff (no hot-reload semantics needed for an internal
# discovery endpoint).
_CORPUS_CACHE: dict[str, list[str]] | None = None
_PAYLOAD_CACHE: dict[str, Any] | None = None


def _default_corpus_path() -> Path:
    """Resolve ``data/tool_query_corpus.yaml`` under the package's ``data/`` dir.

    Mirrors the resolution logic in ``discover_dataset._default_corpus_path``
    so both consumers read the same file by default. Honours the
    ``GRACE2_TOOL_CORPUS_YAML`` env override for test/dev pinning.
    """
    env_path = os.environ.get("GRACE2_TOOL_CORPUS_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parent / "data" / "tool_query_corpus.yaml"


def load_query_corpus(path: Path | None = None) -> dict[str, list[str]]:
    """Load + cache the synthetic example-query corpus YAML.

    Returns a mapping ``tool_name -> [sample_query, ...]``. Cached for the
    lifetime of the process; the cache reset is implicit on agent restart
    (process-level state, no persistence).

    Missing files / parse errors return an empty dict — the catalog still
    renders, just without sample queries. Failure to load the corpus must
    not block the discovery surface.
    """
    global _CORPUS_CACHE
    if _CORPUS_CACHE is not None:
        return _CORPUS_CACHE
    p = path if path is not None else _default_corpus_path()
    if not p.exists():
        logger.warning(
            "tool_catalog_http: corpus YAML missing at %s — catalog will "
            "render without sample queries",
            p,
        )
        _CORPUS_CACHE = {}
        return _CORPUS_CACHE
    try:
        with p.open() as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception(
            "tool_catalog_http: failed to parse corpus YAML at %s", p
        )
        _CORPUS_CACHE = {}
        return _CORPUS_CACHE
    if not isinstance(data, dict):
        _CORPUS_CACHE = {}
        return _CORPUS_CACHE
    parsed: dict[str, list[str]] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, list):
            parsed[k] = [str(q) for q in v if isinstance(q, str)]
    _CORPUS_CACHE = parsed
    logger.info(
        "tool_catalog_http: loaded %d tool query entries from %s",
        len(parsed),
        p,
    )
    return _CORPUS_CACHE


def _reset_caches_for_tests() -> None:
    """Drop module-level caches. ONLY for tests."""
    global _CORPUS_CACHE, _PAYLOAD_CACHE
    _CORPUS_CACHE = None
    _PAYLOAD_CACHE = None


def build_catalog_payload(
    *,
    corpus: dict[str, list[str]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Assemble the ``/api/tool-catalog`` JSON payload.

    Shape::

        {
          "categories": [
            {"id": "...", "name": "...", "description": "...", "tool_count": N},
            ...12...
          ],
          "tools": [
            {
              "name": "fetch_dem",
              "description": "...",        # first-line/short docstring
              "description_full": "...",   # full docstring
              "category_id": "terrain_elevation",
              "secondary_category_ids": [],
              "supports_global_query": false,
              "annotations": {
                "read_only_hint": true,
                "open_world_hint": true,
                "destructive_hint": false,
                "idempotent_hint": true
              },
              "estimate_payload_mb_default": null,
              "ttl_class": "static-30d",
              "source_class": "dem",
              "cacheable": true,
              "sample_queries": ["show me elevation data for the Grand Canyon", ...]
            },
            ...
          ]
        }

    A tool registered without a primary category falls back to
    ``geographic_primitives`` (the catch-all for platform plumbing). The
    full description carries the complete docstring so the UI can show a
    short snippet by default and let the user expand the entry for the
    full text.
    """
    # Import here to avoid an import cycle: categories.py imports from
    # ``tools``, ``tools`` imports submodules that register decorators.
    # Importing categories at module load time is fine, but we want the
    # payload to reflect whatever the registry holds AT BUILD TIME, so we
    # snapshot here.
    from .categories import (
        CATEGORIES,
        PRIMARY_CATEGORY,
        SECONDARY_CATEGORIES,
    )
    from .tools import TOOL_REGISTRY

    global _PAYLOAD_CACHE
    if use_cache and _PAYLOAD_CACHE is not None:
        return _PAYLOAD_CACHE

    corpus_map = corpus if corpus is not None else load_query_corpus()

    # First pass: build the tools list.
    tools_out: list[dict[str, Any]] = []
    for name in sorted(TOOL_REGISTRY.keys()):
        entry = TOOL_REGISTRY[name]
        meta = entry.metadata
        doc_full = (entry.fn.__doc__ or "").strip()
        description = _first_paragraph(doc_full)
        primary_cat = PRIMARY_CATEGORY.get(name, "geographic_primitives")
        secondaries = list(SECONDARY_CATEGORIES.get(name, ()))
        sample_queries = list(corpus_map.get(name, []))
        # Cap to 3 sample queries in the payload — the UI shows 2-3; sending
        # all 5-10 wastes bandwidth on a discovery surface.
        sample_queries = sample_queries[:3]
        tools_out.append(
            {
                "name": name,
                "description": description,
                "description_full": doc_full,
                "category_id": primary_cat,
                "secondary_category_ids": secondaries,
                "supports_global_query": bool(meta.supports_global_query),
                "annotations": {
                    "read_only_hint": bool(meta.read_only_hint),
                    "open_world_hint": bool(meta.open_world_hint),
                    "destructive_hint": bool(meta.destructive_hint),
                    "idempotent_hint": bool(meta.idempotent_hint),
                },
                "estimate_payload_mb_default": None,
                "ttl_class": str(meta.ttl_class),
                "source_class": meta.source_class,
                "cacheable": bool(meta.cacheable),
                "sample_queries": sample_queries,
            }
        )

    # Second pass: count tools per category. Counted from PRIMARY_CATEGORY +
    # SECONDARY_CATEGORIES so a cross-listed tool shows up in both. Tools
    # without an explicit primary category fall through to
    # ``geographic_primitives`` — match the per-tool fallback above.
    category_counts: dict[str, int] = {c.id: 0 for c in CATEGORIES}
    for name in TOOL_REGISTRY:
        primary = PRIMARY_CATEGORY.get(name, "geographic_primitives")
        if primary in category_counts:
            category_counts[primary] += 1
        for sec in SECONDARY_CATEGORIES.get(name, ()):
            if sec in category_counts:
                category_counts[sec] += 1

    categories_out = [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "tool_count": category_counts.get(c.id, 0),
        }
        for c in CATEGORIES
    ]

    payload = {"categories": categories_out, "tools": tools_out}
    if use_cache:
        _PAYLOAD_CACHE = payload
    return payload


def _first_paragraph(doc: str, *, max_chars: int = 400) -> str:
    """Return a short snippet from a docstring.

    Strategy: take the first non-empty line, then continue until a blank
    line OR ``max_chars`` is reached. The full docstring is also surfaced
    on the wire (``description_full``) so the UI can click-to-expand.
    """
    if not doc:
        return ""
    lines = doc.splitlines()
    out: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if not stripped:
                continue
            started = True
        if started and not stripped:
            break
        out.append(stripped)
        if sum(len(s) + 1 for s in out) >= max_chars:
            break
    snippet = " ".join(out)
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1].rstrip() + "…"
    return snippet


# ---------------------------------------------------------------------------
# Telemetry summary (Wave 4.11 M7 — routing-quality dashboard backend).
# ---------------------------------------------------------------------------


_DEFAULT_TELEMETRY_PATH = "/tmp/grace2_tool_call_telemetry.jsonl"


def _get_telemetry_path() -> Path:
    """Resolve the JSONL fallback path (env override + default)."""
    return Path(
        os.environ.get("GRACE2_TELEMETRY_PATH", _DEFAULT_TELEMETRY_PATH)
    )


# tool-retrieval SHADOW recall@k (tool-retrieval kickoff). The shadow-selection
# rows share the tool_call_telemetry sink, tagged with this discriminator.
_SHADOW_RECORD_TYPE = "tool_retrieval_shadow"

#: Terminal North-Star solver tools -> the flow they identify. A turn is
#: attributed to a flow when it dispatched one of these (the recall@k per-flow
#: breakdown the kickoff asks for: SWMM / SFINCS / MODFLOW).
_FLOW_BY_SOLVER_TOOL: dict[str, str] = {
    "run_swmm_urban_flood": "SWMM",
    "run_model_flood_scenario": "SFINCS",
    "run_model_flood_habitat_scenario": "SFINCS",
    "run_modflow_job": "MODFLOW",
    "run_model_groundwater_contamination_scenario": "MODFLOW",
}


def _normalize_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Coerce a single telemetry record into the summary's canonical shape.

    The local-file (Wave 4.10) writer uses ``success`` + ``ts``; the MCP
    writer (Wave 4.11 M3) uses ``result_ok`` + ``called_at_utc``. We accept
    either form so the summary builder doesn't care which substrate
    produced the data.
    """
    out: dict[str, Any] = {}
    out["session_id"] = rec.get("session_id") or ""
    out["tool_name"] = rec.get("tool_name") or ""
    out["source"] = rec.get("source") or "llm"
    # Either ``success`` (local file) or ``result_ok`` (Mongo).
    if "result_ok" in rec:
        out["result_ok"] = bool(rec.get("result_ok"))
    else:
        out["result_ok"] = bool(rec.get("success", True))
    out["latency_ms"] = float(rec.get("latency_ms") or 0.0)
    out["error_code"] = rec.get("error_code")
    out["retry_attempt"] = int(rec.get("retry_attempt") or 0)
    out["cached_content_token_count"] = rec.get("cached_content_token_count")
    # Tool-accuracy panel (NATE 2026-06-17). ``result_usable`` is bool|None
    # (None = the notion doesn't apply, e.g. a meta tool); ``routed_ok`` is
    # bool|None and is the per-record carrier of the routing-quality heuristic.
    # Both substrates use the same key names, so a plain get suffices.
    out["result_usable"] = rec.get("result_usable")
    out["routed_ok"] = rec.get("routed_ok")
    # Timestamp: prefer the Mongo field name; fall back to the file form.
    out["called_at_utc"] = rec.get("called_at_utc") or rec.get("ts") or ""
    # In-chat model selector dimension (NATE 2026-06-17). None when the record
    # predates the feature; _aggregate_records buckets it as "unknown".
    out["model_id"] = rec.get("model_id")
    # turn_id (the per-user-message dispatch / pipeline id) -- the recall@k join
    # key against the turn's tool-retrieval shadow row. Absent on pre-feature
    # records (None); recall only counts dispatches that carry one.
    out["turn_id"] = rec.get("turn_id")
    return out


def _empty_solve_telemetry() -> dict[str, Any]:
    """Return the zero-state solve_telemetry section (no solves recorded yet).

    Matches the WIRE CONTRACT: ``recent`` is an empty list and the percentiles
    are zeros until at least one solve has been logged.
    """
    return {
        "recent": [],
        "wall_clock_p50_s": 0.0,
        "wall_clock_p95_s": 0.0,
    }


def _empty_summary() -> dict[str, Any]:
    """Return the zero-state summary shape (no telemetry recorded yet)."""
    return {
        "total_dispatches": 0,
        "session_count": 0,
        "error_rate_overall": 0.0,
        "cache_hit_rate": 0.0,
        "average_latency_ms": 0.0,
        # Tool-accuracy panel additions (WIRE CONTRACT, NATE 2026-06-17).
        "success_rate": 0.0,
        "result_usability_rate": None,
        "routing_accuracy_rate": None,
        "latency_p50_ms": 0.0,
        "latency_p95_ms": 0.0,
        "dispatches_by_tool": [],   # [{name, count, error_rate, avg_latency_ms, ...}]
        "dispatches_by_source": {}, # {llm: int, workflow: int, manual: int}
        "error_rate_by_tool": [],   # [{name, error_rate, error_count, total}]
        "top_routing_chains": [],   # [{chain: [a, b], count}]
        "by_model": [],             # [{model_id, count, success_rate, ...}]
        "solve_telemetry": _empty_solve_telemetry(),
        # tool-retrieval shadow recall@k (folded in by build_telemetry_summary).
        "recall_at_k": _empty_recall_at_k(),
        "source": "empty",
    }


def _percentile(values: list[float], q: float) -> float:
    """Return the ``q``-th percentile (q in [0,1]) via linear interpolation.

    Empty input yields ``0.0``. Uses the same "linear" method numpy defaults to
    so the p50/p95 line up with any external numpy-based recompute. Pure-stdlib
    (no numpy import — telemetry must stay light + always importable).
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _rate_over_bools(values: list[bool | None]) -> float | None:
    """Fraction of ``True`` among the non-``None`` entries.

    Returns ``None`` when EVERY entry is ``None`` (the notion does not apply to
    any record — e.g. result_usable for an all-meta-tool slice), so the wire
    field is an honest null rather than a misleading ``0.0``. This is the
    contract for ``result_usability_rate`` / ``routing_accuracy_rate``.
    """
    considered = [v for v in values if v is not None]
    if not considered:
        return None
    trues = sum(1 for v in considered if v)
    return trues / len(considered)


def _derive_routed_ok(records: list[dict[str, Any]]) -> dict[int, bool]:
    """Derive the routing-quality heuristic per record (id() -> routed_ok).

    DEFENSIBLE HEURISTIC, NOT GROUND TRUTH (clearly labelled on the wire as
    ``routing_accuracy_rate``): a tool call is "mis-routed" when it FAILED
    (result_ok=False) and the SAME session's NEXT call (by timestamp) is a
    DIFFERENT tool — i.e. the model abandoned this tool and reached for another
    one for the same logical step. Such a call gets ``routed_ok=False``. Any
    other completed call gets ``routed_ok=True``. We leverage ``retry_attempt``
    too: a call with retry_attempt>0 that itself failed and was followed by a
    different tool is the clearest mis-route signal, but the failed+superseded
    rule already captures it.

    A per-record value the writer ALREADY supplied (``routed_ok`` not None) wins
    — this only fills the gap for records whose writer left it None (the current
    emit path, where supersession is not yet observable). Keyed by ``id(rec)``
    so two records with identical contents are scored independently.
    """
    out: dict[int, bool] = {}
    sess_buckets: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        sid = r.get("session_id") or ""
        if not sid:
            # No session context — cannot judge supersession; routed_ok stays
            # absent (treated as None/unavailable downstream).
            continue
        sess_buckets.setdefault(sid, []).append(r)
    for recs in sess_buckets.values():
        recs_sorted = sorted(recs, key=lambda r: str(r.get("called_at_utc") or ""))
        for i, rec in enumerate(recs_sorted):
            preset = rec.get("routed_ok")
            if preset is not None:
                out[id(rec)] = bool(preset)
                continue
            tool = rec.get("tool_name") or ""
            failed = not rec.get("result_ok", True)
            superseded = False
            if i + 1 < len(recs_sorted):
                nxt = recs_sorted[i + 1]
                ntool = nxt.get("tool_name") or ""
                if ntool and tool and ntool != tool:
                    superseded = True
            out[id(rec)] = not (failed and superseded)
    return out


def _aggregate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the dashboard summary over a list of normalized records.

    Returns a JSON-serializable dict; called by both the MCP-backed and
    file-fallback code paths so the aggregation logic stays in one place.
    """
    if not records:
        return _empty_summary()

    total = len(records)
    # Sessions present
    sessions = {r["session_id"] for r in records if r["session_id"]}
    session_count = len(sessions)

    # Routing-quality heuristic (per-record, id()-keyed). Derived here because
    # supersession is a same-session ADJACENT-chain signal, not knowable at
    # single-call emit time.
    routed_ok_by_id = _derive_routed_ok(records)

    # Per-tool aggregation
    by_tool_count: dict[str, int] = {}
    by_tool_errors: dict[str, int] = {}
    by_tool_latency_sum: dict[str, float] = {}
    by_tool_latencies: dict[str, list[float]] = {}
    by_tool_usable: dict[str, list[bool | None]] = {}
    by_tool_routed: dict[str, list[bool | None]] = {}
    by_source_count: dict[str, int] = {}
    # Per-model aggregation (in-chat model selector dimension).
    by_model_count: dict[str, int] = {}
    by_model_errors: dict[str, int] = {}
    by_model_latency_sum: dict[str, float] = {}
    by_model_latencies: dict[str, list[float]] = {}
    by_model_usable: dict[str, list[bool | None]] = {}
    by_model_routed: dict[str, list[bool | None]] = {}
    total_errors = 0
    total_latency = 0.0
    all_latencies: list[float] = []
    all_usable: list[bool | None] = []
    all_routed: list[bool | None] = []
    cache_hit_count = 0
    cache_total = 0

    for r in records:
        tool = r["tool_name"] or "unknown"
        lat = float(r["latency_ms"])
        by_tool_count[tool] = by_tool_count.get(tool, 0) + 1
        by_tool_latency_sum[tool] = by_tool_latency_sum.get(tool, 0.0) + lat
        by_tool_latencies.setdefault(tool, []).append(lat)
        if not r["result_ok"]:
            by_tool_errors[tool] = by_tool_errors.get(tool, 0) + 1
            total_errors += 1
        total_latency += lat
        all_latencies.append(lat)
        # result_usable (bool|None — meta tools contribute None).
        usable = r.get("result_usable")
        by_tool_usable.setdefault(tool, []).append(usable)
        all_usable.append(usable)
        # routed_ok (the derived heuristic; None when no session context).
        routed = routed_ok_by_id.get(id(r))
        by_tool_routed.setdefault(tool, []).append(routed)
        all_routed.append(routed)
        src = r["source"] or "llm"
        by_source_count[src] = by_source_count.get(src, 0) + 1
        # Cache hit rate: presence of a non-zero cached_content_token_count
        # treated as a "cache hit" since the Gemini SDK reports the cached
        # token count when the cached content path engaged.
        cct = r.get("cached_content_token_count")
        if cct is not None:
            cache_total += 1
            if isinstance(cct, (int, float)) and cct > 0:
                cache_hit_count += 1
        # Per-model accumulation (in-chat model selector dimension).
        # Null/missing model_id is bucketed as "unknown" so legacy records
        # still surface in the by_model section.
        mid = r.get("model_id") or "unknown"
        by_model_count[mid] = by_model_count.get(mid, 0) + 1
        by_model_latency_sum[mid] = by_model_latency_sum.get(mid, 0.0) + lat
        by_model_latencies.setdefault(mid, []).append(lat)
        if not r["result_ok"]:
            by_model_errors[mid] = by_model_errors.get(mid, 0) + 1
        by_model_usable.setdefault(mid, []).append(usable)
        by_model_routed.setdefault(mid, []).append(routed)

    by_tool_sorted: list[dict[str, Any]] = []
    error_rate_by_tool: list[dict[str, Any]] = []
    for tool, cnt in sorted(by_tool_count.items(), key=lambda kv: (-kv[1], kv[0])):
        errs = by_tool_errors.get(tool, 0)
        avg_latency = by_tool_latency_sum.get(tool, 0.0) / cnt if cnt else 0.0
        rate = (errs / cnt) if cnt else 0.0
        lats = by_tool_latencies.get(tool, [])
        usability_rate = _rate_over_bools(by_tool_usable.get(tool, []))
        routing_rate = _rate_over_bools(by_tool_routed.get(tool, []))
        by_tool_sorted.append(
            {
                "name": tool,
                "count": cnt,
                "error_count": errs,
                "error_rate": round(rate, 4),
                "avg_latency_ms": round(avg_latency, 2),
                # Tool-accuracy panel additions (WIRE CONTRACT).
                "success_rate": round(1.0 - rate, 4),
                "result_usability_rate": (
                    round(usability_rate, 4) if usability_rate is not None else None
                ),
                "routing_accuracy_rate": (
                    round(routing_rate, 4) if routing_rate is not None else None
                ),
                "latency_p50_ms": round(_percentile(lats, 0.50), 2),
                "latency_p95_ms": round(_percentile(lats, 0.95), 2),
            }
        )
        error_rate_by_tool.append(
            {
                "name": tool,
                "error_rate": round(rate, 4),
                "error_count": errs,
                "total": cnt,
            }
        )

    # Routing chains: most common 2-tool sequences within a single session.
    # Group records by session_id then by their called_at_utc to walk pairs.
    chains: dict[tuple[str, str], int] = {}
    sess_buckets: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        sid = r["session_id"]
        if not sid:
            continue
        sess_buckets.setdefault(sid, []).append(r)
    for sid, recs in sess_buckets.items():
        # Sort by timestamp (ISO strings sort lexicographically when in UTC Z).
        recs_sorted = sorted(recs, key=lambda r: str(r.get("called_at_utc") or ""))
        for a, b in zip(recs_sorted[:-1], recs_sorted[1:]):
            ta = a.get("tool_name") or ""
            tb = b.get("tool_name") or ""
            if not ta or not tb or ta == tb:
                continue
            chains[(ta, tb)] = chains.get((ta, tb), 0) + 1
    top_chains = sorted(chains.items(), key=lambda kv: -kv[1])[:5]
    chains_out = [
        {"chain": [a, b], "count": cnt} for (a, b), cnt in top_chains
    ]

    error_rate_overall = (total_errors / total) if total else 0.0
    cache_hit_rate = (cache_hit_count / cache_total) if cache_total else 0.0
    avg_latency_ms = (total_latency / total) if total else 0.0
    success_rate = (1.0 - error_rate_overall) if total else 0.0
    usability_rate_overall = _rate_over_bools(all_usable)
    routing_rate_overall = _rate_over_bools(all_routed)

    # Per-model breakdown (in-chat model selector, NATE 2026-06-17).
    # Shape: list of {model_id, count, success_rate, result_usability_rate,
    #                 routing_accuracy_rate, latency_p50_ms, latency_p95_ms}
    # Sorted descending by count; "unknown" last.
    by_model_sorted: list[dict[str, Any]] = []
    for mid, cnt in sorted(
        by_model_count.items(),
        key=lambda kv: (kv[0] == "unknown", -kv[1], kv[0]),
    ):
        m_errs = by_model_errors.get(mid, 0)
        m_rate = (m_errs / cnt) if cnt else 0.0
        m_lats = by_model_latencies.get(mid, [])
        m_usability = _rate_over_bools(by_model_usable.get(mid, []))
        m_routing = _rate_over_bools(by_model_routed.get(mid, []))
        by_model_sorted.append(
            {
                "model_id": mid,
                "count": cnt,
                "success_rate": round(1.0 - m_rate, 4),
                "result_usability_rate": (
                    round(m_usability, 4) if m_usability is not None else None
                ),
                "routing_accuracy_rate": (
                    round(m_routing, 4) if m_routing is not None else None
                ),
                "latency_p50_ms": round(_percentile(m_lats, 0.50), 2),
                "latency_p95_ms": round(_percentile(m_lats, 0.95), 2),
            }
        )

    return {
        "total_dispatches": total,
        "session_count": session_count,
        "error_rate_overall": round(error_rate_overall, 4),
        "cache_hit_rate": round(cache_hit_rate, 4),
        "average_latency_ms": round(avg_latency_ms, 2),
        # Tool-accuracy panel additions (WIRE CONTRACT, NATE 2026-06-17).
        "success_rate": round(success_rate, 4),
        "result_usability_rate": (
            round(usability_rate_overall, 4)
            if usability_rate_overall is not None
            else None
        ),
        "routing_accuracy_rate": (
            round(routing_rate_overall, 4)
            if routing_rate_overall is not None
            else None
        ),
        "latency_p50_ms": round(_percentile(all_latencies, 0.50), 2),
        "latency_p95_ms": round(_percentile(all_latencies, 0.95), 2),
        "dispatches_by_tool": by_tool_sorted,
        "dispatches_by_source": by_source_count,
        "error_rate_by_tool": error_rate_by_tool,
        "top_routing_chains": chains_out,
        # Model dimension (in-chat model selector, NATE 2026-06-17).
        # The accuracy panel UI can compare success_rate / usability / routing
        # across model choices without a UI redesign in this job.
        "by_model": by_model_sorted,
        # solve_telemetry is folded in by build_telemetry_summary (it reads its
        # own JSONL/collection sink); seed the empty section so _aggregate_records
        # called standalone still emits the full contract shape.
        "solve_telemetry": _empty_solve_telemetry(),
        # recall_at_k is likewise folded in by build_telemetry_summary (it joins
        # the shadow rows against these dispatches); seed the empty section.
        "recall_at_k": _empty_recall_at_k(),
        "source": "telemetry",
    }


# ---------------------------------------------------------------------------
# solve_telemetry section (live big-sim panel — NATE 2026-06-17).
#
# The solve-telemetry record is written to the SAME file+structured-log dual
# sink as before (telemetry.emit_solve_telemetry); we read its JSONL here to
# fold per-solve metrics (grid resolution / active cells / vCPU / wall-clock /
# backend / aoi) into /api/telemetry/summary. The lightest path consistent with
# the existing file+mongo dual-sink: read the JSONL the solve writer already
# maintains. No Mongo collection is required (none exists for solves), matching
# the writer's own "JSONL + structured log, not MCP-routed" decision.
# ---------------------------------------------------------------------------

_DEFAULT_SOLVE_TELEMETRY_PATH = "/tmp/grace2_solve_telemetry.jsonl"

#: How many recent solve records to surface in the ``recent`` array.
_SOLVE_RECENT_CAP = 20


def _get_solve_telemetry_path() -> Path:
    """Resolve the solve-telemetry JSONL path (env override + default).

    Mirrors ``telemetry._get_solve_telemetry_path`` so reader + writer agree.
    """
    return Path(
        os.environ.get(
            "GRACE2_SOLVE_TELEMETRY_PATH", _DEFAULT_SOLVE_TELEMETRY_PATH
        )
    )


def _load_solve_records_from_file(path: Path) -> list[dict[str, Any]]:
    """Read the solve-telemetry JSONL (newest-last as written).

    Returns the parsed records in file order; missing/unreadable file yields an
    empty list (the summary then carries the zero-state solve section).
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def _aggregate_solve_telemetry(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the ``solve_telemetry`` section from solve records.

    Shape (WIRE CONTRACT): ``{recent: [{run_id, solver, grid_resolution_m,
    active_cell_count, vcpus, wall_clock_seconds, backend, aoi_km2}],
    wall_clock_p50_s, wall_clock_p95_s}``. ``recent`` is newest-first, capped at
    ``_SOLVE_RECENT_CAP``. Percentiles are over every record that carries a
    numeric ``wall_clock_seconds``. Empty input -> the zero-state section.
    """
    if not records:
        return _empty_solve_telemetry()
    # Newest-first by ts (ISO Z strings sort lexicographically).
    ordered = sorted(
        records, key=lambda r: str(r.get("ts") or ""), reverse=True
    )
    recent: list[dict[str, Any]] = []
    for rec in ordered[:_SOLVE_RECENT_CAP]:
        recent.append(
            {
                "run_id": rec.get("run_id"),
                "solver": rec.get("solver"),
                "grid_resolution_m": rec.get("grid_resolution_m"),
                "active_cell_count": rec.get("active_cell_count"),
                "vcpus": rec.get("vcpus"),
                "wall_clock_seconds": rec.get("wall_clock_seconds"),
                "backend": rec.get("backend"),
                "aoi_km2": rec.get("aoi_km2"),
            }
        )
    wall_clocks = [
        float(rec["wall_clock_seconds"])
        for rec in records
        if isinstance(rec.get("wall_clock_seconds"), (int, float))
        and not isinstance(rec.get("wall_clock_seconds"), bool)
    ]
    return {
        "recent": recent,
        "wall_clock_p50_s": round(_percentile(wall_clocks, 0.50), 2),
        "wall_clock_p95_s": round(_percentile(wall_clocks, 0.95), 2),
    }


def _load_recent_records_from_file(
    path: Path,
    *,
    last_n_sessions: int = 30,
) -> list[dict[str, Any]]:
    """Read the JSONL fallback file and return records from the most-recent
    ``last_n_sessions`` distinct sessions (newest first).

    Returns an empty list when the file is missing or unreadable — the
    dashboard renders an empty state in that case.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                # tool-retrieval SHADOW rows share this JSONL sink but are NOT
                # tool-call dispatches -- skip them here (the recall@k path reads
                # them separately via _load_shadow_records_from_file).
                if rec.get("record_type") == _SHADOW_RECORD_TYPE:
                    continue
                out.append(_normalize_record(rec))
    except OSError:
        return []
    if not out:
        return out
    # Newest-first, then keep only records belonging to the last N sessions.
    out.sort(key=lambda r: str(r.get("called_at_utc") or ""), reverse=True)
    seen_sessions: list[str] = []
    keep: list[dict[str, Any]] = []
    for r in out:
        sid = r.get("session_id") or ""
        if sid and sid not in seen_sessions:
            if len(seen_sessions) >= last_n_sessions:
                break
            seen_sessions.append(sid)
        keep.append(r)
    return keep


def _load_shadow_records_from_file(path: Path) -> list[dict[str, Any]]:
    """Read the tool-retrieval SHADOW rows from the JSONL sink.

    Shadow rows carry ``record_type == _SHADOW_RECORD_TYPE`` and a
    ``visible_tools`` array (the would-be-visible set for that turn). Keyed for
    recall@k by ``(session_id, turn_id)``. Returns an empty list when the file is
    missing / unreadable.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("record_type") == _SHADOW_RECORD_TYPE:
                    out.append(rec)
    except OSError:
        return []
    return out


def _normalize_shadow_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Coerce a shadow row into the recall@k canonical shape.

    Accepts either the file form (``visible_tools`` list) or a mongo form;
    ``visible_tools`` is normalized to a set of strings.
    """
    vis = rec.get("visible_tools") or []
    try:
        visible = {str(t) for t in vis}
    except Exception:  # noqa: BLE001 — a malformed row contributes an empty set
        visible = set()
    return {
        "session_id": rec.get("session_id") or "",
        "turn_id": rec.get("turn_id") or "",
        "visible_tools": visible,
        "k": rec.get("k"),
    }


def compute_recall_at_k(
    tool_records: list[dict[str, Any]],
    shadow_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute recall@k of the tool-retrieval shadow selection (PURE).

    For each turn that has a shadow row, recall counts the LLM-dispatched tools
    (``source == "llm"``) for that turn that WERE present in the turn's
    would-be-visible set, divided by the count of dispatched llm tools for that
    turn. A dispatched tool the retrieval would have DROPPED is a MISS.

    Returns the recall@k section of the summary::

        {
          "overall": float | None,         # 0..1; None when no measurable turns
          "turns_measured": int,           # turns with a shadow row + >=1 llm dispatch
          "dispatches_measured": int,      # total dispatched llm tools across those turns
          "hits": int,
          "misses": int,
          "k": int | None,                 # the k the shadow rows were taken at (modal)
          "by_flow": [ {flow, recall, turns, dispatches, hits, misses}, ... ],
          "missed_tools": [ {name, count, flows: [..]}, ... ],  # tools retrieval dropped
        }

    Turns without a shadow row (e.g. mode==off when the dispatch happened, or a
    pre-feature record) are EXCLUDED -- recall is only defined where we logged a
    would-be set. The join key is ``(session_id, turn_id)``.
    """
    # Index shadow rows by (session_id, turn_id) -> visible set.
    shadow_by_turn: dict[tuple[str, str], set[str]] = {}
    k_values: list[int] = []
    for s in shadow_records:
        norm = _normalize_shadow_record(s)
        sid = norm["session_id"]
        tid = norm["turn_id"]
        if not tid:
            continue
        # If the same turn logged multiple shadow rows (shouldn't happen), the
        # union is the safe choice (over-inclusion never penalizes recall).
        key = (sid, tid)
        shadow_by_turn.setdefault(key, set()).update(norm["visible_tools"])
        kv = norm.get("k")
        if isinstance(kv, int):
            k_values.append(kv)

    if not shadow_by_turn:
        return {
            "overall": None,
            "turns_measured": 0,
            "dispatches_measured": 0,
            "hits": 0,
            "misses": 0,
            "k": None,
            "by_flow": [],
            "missed_tools": [],
        }

    # Group dispatched llm tools by (session_id, turn_id).
    dispatches_by_turn: dict[tuple[str, str], list[str]] = {}
    for r in tool_records:
        if (r.get("source") or "llm") != "llm":
            continue
        tid = r.get("turn_id")
        if not tid:
            continue
        sid = r.get("session_id") or ""
        tool = r.get("tool_name") or ""
        if not tool:
            continue
        dispatches_by_turn.setdefault((sid, tid), []).append(tool)

    # Determine each turn's North-Star flow from the terminal solver tool it
    # dispatched (if any). A turn maps to at most one flow.
    def _turn_flow(tools: list[str]) -> str | None:
        for t in tools:
            flow = _FLOW_BY_SOLVER_TOOL.get(t)
            if flow:
                return flow
        return None

    total_hits = 0
    total_misses = 0
    total_dispatches = 0
    turns_measured = 0
    # Per-flow accumulators.
    flow_hits: dict[str, int] = {}
    flow_misses: dict[str, int] = {}
    flow_dispatches: dict[str, int] = {}
    flow_turns: dict[str, int] = {}
    # Missed-tool tally: tool -> count + the flows it was missed under.
    missed_count: dict[str, int] = {}
    missed_flows: dict[str, set[str]] = {}

    for key, tools in dispatches_by_turn.items():
        visible = shadow_by_turn.get(key)
        if visible is None:
            # No shadow row for this turn -> not measurable, exclude.
            continue
        if not tools:
            continue
        turns_measured += 1
        flow = _turn_flow(tools)
        if flow is not None:
            flow_turns[flow] = flow_turns.get(flow, 0) + 1
        for tool in tools:
            total_dispatches += 1
            if flow is not None:
                flow_dispatches[flow] = flow_dispatches.get(flow, 0) + 1
            if tool in visible:
                total_hits += 1
                if flow is not None:
                    flow_hits[flow] = flow_hits.get(flow, 0) + 1
            else:
                total_misses += 1
                missed_count[tool] = missed_count.get(tool, 0) + 1
                missed_flows.setdefault(tool, set())
                if flow is not None:
                    missed_flows[tool].add(flow)
                    flow_misses[flow] = flow_misses.get(flow, 0) + 1

    overall = (
        (total_hits / total_dispatches) if total_dispatches else None
    )

    by_flow: list[dict[str, Any]] = []
    for flow in ("SWMM", "SFINCS", "MODFLOW"):
        disp = flow_dispatches.get(flow, 0)
        hits = flow_hits.get(flow, 0)
        misses = flow_misses.get(flow, 0)
        by_flow.append(
            {
                "flow": flow,
                "recall": round(hits / disp, 4) if disp else None,
                "turns": flow_turns.get(flow, 0),
                "dispatches": disp,
                "hits": hits,
                "misses": misses,
            }
        )

    missed_tools = [
        {
            "name": name,
            "count": cnt,
            "flows": sorted(missed_flows.get(name, set())),
        }
        for name, cnt in sorted(
            missed_count.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]

    # The k the shadow rows were taken at (modal value; informational only).
    k_modal: int | None = None
    if k_values:
        from collections import Counter

        k_modal = Counter(k_values).most_common(1)[0][0]

    return {
        "overall": round(overall, 4) if overall is not None else None,
        "turns_measured": turns_measured,
        "dispatches_measured": total_dispatches,
        "hits": total_hits,
        "misses": total_misses,
        "k": k_modal,
        "by_flow": by_flow,
        "missed_tools": missed_tools,
    }


def _empty_recall_at_k() -> dict[str, Any]:
    """Zero-state recall@k section (no shadow rows logged yet)."""
    return {
        "overall": None,
        "turns_measured": 0,
        "dispatches_measured": 0,
        "hits": 0,
        "misses": 0,
        "k": None,
        "by_flow": [],
        "missed_tools": [],
    }


async def _load_shadow_records_from_mongo(
    persistence: Any,
) -> list[dict[str, Any]]:
    """Query the ``tool_call_telemetry`` collection for SHADOW rows via MCP.

    Best-effort: any failure returns an empty list (recall@k then degrades to
    the empty section / the file-backed shadow rows).
    """
    try:
        from grace2_contracts.mongo_collections import TELEMETRY_COLLECTION
        from .persistence import DEFAULT_DATABASE
    except Exception:  # noqa: BLE001
        return []
    try:
        raw = await persistence._mcp.call_tool(
            "find",
            {
                "database": DEFAULT_DATABASE,
                "collection": TELEMETRY_COLLECTION,
                "filter": {"record_type": _SHADOW_RECORD_TYPE},
                "sort": {"called_at_utc": -1},
                "limit": 2000,
            },
        )
    except Exception:  # noqa: BLE001 — never break the dashboard on MCP error
        logger.warning("recall@k: shadow mongo find failed", exc_info=True)
        return []
    docs: Any = raw
    if isinstance(raw, dict):
        if "documents" in raw:
            docs = raw["documents"]
        elif "content" in raw and isinstance(raw["content"], list) and raw["content"]:
            first = raw["content"][0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                try:
                    docs = json.loads(first["text"])
                except json.JSONDecodeError:
                    docs = []
    if isinstance(docs, dict):
        docs = [docs]
    if not isinstance(docs, list):
        return []
    return [d for d in docs if isinstance(d, dict)]


async def _load_recent_records_from_mongo(
    persistence: Any,
    *,
    last_n_sessions: int = 30,
) -> list[dict[str, Any]]:
    """Query the ``tool_call_telemetry`` collection via the MCP client.

    Best-effort: any failure falls back to an empty list so the dashboard
    can still render the file-backed path or an empty state.
    """
    try:
        from grace2_contracts.mongo_collections import TELEMETRY_COLLECTION
        from .persistence import DEFAULT_DATABASE
    except Exception:  # noqa: BLE001
        return []
    try:
        # Fetch newest 2000 records, then narrow to the last N sessions.
        # The cap keeps a runaway collection from stalling the dashboard.
        raw = await persistence._mcp.call_tool(
            "find",
            {
                "database": DEFAULT_DATABASE,
                "collection": TELEMETRY_COLLECTION,
                # Exclude tool-retrieval SHADOW rows -- they share this
                # collection but are not tool-call dispatches (recall@k reads
                # them via _load_shadow_records_from_mongo).
                "filter": {"record_type": {"$ne": _SHADOW_RECORD_TYPE}},
                "sort": {"called_at_utc": -1},
                "limit": 2000,
            },
        )
    except Exception:  # noqa: BLE001 — never break the dashboard on MCP error
        logger.warning("telemetry summary: mongo find failed", exc_info=True)
        return []
    # Unwrap the MCP result envelope (mirrors Persistence._unwrap_mcp_result).
    docs: Any = raw
    if isinstance(raw, dict):
        if "documents" in raw:
            docs = raw["documents"]
        elif "content" in raw and isinstance(raw["content"], list) and raw["content"]:
            first = raw["content"][0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                try:
                    docs = json.loads(first["text"])
                except json.JSONDecodeError:
                    docs = []
    if isinstance(docs, dict):
        docs = [docs]
    if not isinstance(docs, list):
        return []
    # Defensive: even if the $ne filter was a no-op on this MCP backend, never
    # let a SHADOW row leak into the per-tool aggregation.
    normalized = [
        _normalize_record(d)
        for d in docs
        if isinstance(d, dict) and d.get("record_type") != _SHADOW_RECORD_TYPE
    ]
    # Constrain to last N sessions.
    normalized.sort(key=lambda r: str(r.get("called_at_utc") or ""), reverse=True)
    seen_sessions: list[str] = []
    keep: list[dict[str, Any]] = []
    for r in normalized:
        sid = r.get("session_id") or ""
        if sid and sid not in seen_sessions:
            if len(seen_sessions) >= last_n_sessions:
                break
            seen_sessions.append(sid)
        keep.append(r)
    return keep


async def build_telemetry_summary(
    *,
    last_n_sessions: int = 30,
) -> dict[str, Any]:
    """Build the routing-quality summary served by /api/telemetry/summary.

    Routing order:

    1. If the Persistence singleton is bound, query the MongoDB
       ``tool_call_telemetry`` collection via MCP. If that returns records,
       we aggregate against them.
    2. Otherwise (or on MCP failure / empty), fall back to the
       ``/tmp/grace2_tool_call_telemetry.jsonl`` file written by the M3
       file-backed path.

    Returns the empty-summary shape (all-zero counts) if nothing is found.
    """
    persistence = None
    try:
        from .server import get_persistence as _server_get_persistence
        persistence = _server_get_persistence()
    except Exception:  # noqa: BLE001 — early-startup ImportError tolerated
        persistence = None

    records: list[dict[str, Any]] = []
    used_source = "empty"
    if persistence is not None:
        records = await _load_recent_records_from_mongo(
            persistence, last_n_sessions=last_n_sessions
        )
        if records:
            used_source = "mongo"
    if not records:
        records = _load_recent_records_from_file(
            _get_telemetry_path(), last_n_sessions=last_n_sessions
        )
        if records:
            used_source = "file"

    summary = _aggregate_records(records)
    summary["source"] = used_source

    # Fold in the tool-retrieval SHADOW recall@k section (tool-retrieval kickoff).
    # Load the would-be-visible shadow rows (mongo when bound, else the JSONL
    # sink) and join them against the dispatched llm tools above by turn_id.
    # Best-effort: a read/compute fault leaves the zero-state section seeded by
    # _aggregate_records (never breaks the dashboard).
    try:
        shadow_records: list[dict[str, Any]] = []
        if persistence is not None:
            shadow_records = await _load_shadow_records_from_mongo(persistence)
        if not shadow_records:
            shadow_records = _load_shadow_records_from_file(_get_telemetry_path())
        summary["recall_at_k"] = compute_recall_at_k(records, shadow_records)
    except Exception:  # noqa: BLE001 — never break the dashboard on recall read
        logger.warning("telemetry summary: recall@k read failed", exc_info=True)
        summary["recall_at_k"] = _empty_recall_at_k()

    # Fold in the live big-sim solve_telemetry section (NATE 2026-06-17). Read
    # from the solve-telemetry JSONL the solve writer maintains; best-effort so
    # a missing/unreadable sink leaves the zero-state section _aggregate_records
    # already seeded. Independent of the tool-call source above — solves are
    # logged on their own sink.
    try:
        solve_records = _load_solve_records_from_file(_get_solve_telemetry_path())
        summary["solve_telemetry"] = _aggregate_solve_telemetry(solve_records)
    except Exception:  # noqa: BLE001 — never break the dashboard on solve read
        logger.warning("telemetry summary: solve telemetry read failed", exc_info=True)
        summary["solve_telemetry"] = _empty_solve_telemetry()
    return summary


# ---------------------------------------------------------------------------
# Building click-to-enrich detail endpoint (NATE 2026-06-27).
#
# The building footprint inline GeoJSON now carries ID-only props (osm_id /
# osm_type / a composite fid). The full tag bag (building / height / levels /
# name / addr:*) is persisted in a per-AOI sidecar next to the .fgb
# (cache/static-30d/buildings/<key>.tags.json) keyed by fid. This endpoint reads
# that sidecar for a clicked (osm_type, osm_id); if no sidecar carries the fid it
# falls back to a LIVE Overpass-by-id query. Non-blocking: S3 + Overpass run via
# asyncio.to_thread so the agent's WS heartbeat is never starved.
# ---------------------------------------------------------------------------


class _BuildingDetailNotFound(Exception):
    """No tag bag found for the requested building (sidecar miss + live miss)."""


class _BuildingDetailBadRequest(Exception):
    """Malformed /api/building-detail request (missing/invalid osm_type|osm_id)."""


def _building_fid(osm_type: str, osm_id: str) -> str:
    """Mirror ``data_fetch._building_fid``: ``<first-letter-of-type><id>``."""
    return f"{osm_type[:1]}{osm_id}"


def _parse_building_detail_qs(query_string: str) -> tuple[str, str]:
    """Parse + validate ``osm_type`` + ``osm_id`` from the raw query string.

    Returns ``(osm_type, osm_id)`` with ``osm_type`` normalized to the OSM
    element kind (``way`` / ``relation`` / ``node``) and ``osm_id`` a digit
    string. Raises ``_BuildingDetailBadRequest`` on anything malformed (so the
    handler emits a typed 400, never a fabricated success).
    """
    from urllib.parse import parse_qs

    params = parse_qs(query_string, keep_blank_values=False)
    osm_type_raw = (params.get("osm_type") or [""])[0].strip().lower()
    osm_id_raw = (params.get("osm_id") or [""])[0].strip()
    if osm_type_raw not in ("way", "relation", "node"):
        raise _BuildingDetailBadRequest(
            f"osm_type must be way|relation|node, got {osm_type_raw!r}"
        )
    if not osm_id_raw or not osm_id_raw.isdigit():
        raise _BuildingDetailBadRequest(
            f"osm_id must be a positive integer, got {osm_id_raw!r}"
        )
    return osm_type_raw, osm_id_raw


def _read_tags_from_sidecars(fid: str) -> dict[str, Any] | None:
    """Scan the buildings tag sidecars for ``fid`` -> its tag bag (or None).

    SYNC (boto3); the caller wraps it in ``asyncio.to_thread``. The detail
    request carries only ``(osm_type, osm_id)``, not the AOI bbox the sidecar
    key is derived from, so we list the bounded ``buildings/`` sidecar prefix and
    check each ``.tags.json`` for the fid. Best-effort: any S3 fault returns None
    so the handler degrades to the live Overpass-by-id fallback.
    """
    try:
        import boto3

        from .tools.cache import CACHE_BUCKET, cache_path
        from .tools.data_fetch import (
            BUILDINGS_TAGS_SIDECAR_EXT,
            _FETCH_BUILDINGS_METADATA,
        )
    except Exception:  # noqa: BLE001 -- import wiring fault -> live fallback
        logger.warning("building-detail: sidecar import wiring failed", exc_info=True)
        return None

    bucket = os.environ.get("GRACE2_CACHE_BUCKET") or CACHE_BUCKET
    meta = _FETCH_BUILDINGS_METADATA
    # Derive the buildings/<...> prefix from cache_path with a placeholder key.
    sentinel = cache_path(
        meta.source_class, meta.ttl_class, "KEY", BUILDINGS_TAGS_SIDECAR_EXT
    )
    prefix = sentinel.rsplit("KEY", 1)[0]  # cache/static-30d/buildings/
    suffix = f".{BUILDINGS_TAGS_SIDECAR_EXT}"
    try:
        s3 = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "")
                if not key.endswith(suffix):
                    continue
                try:
                    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    data = json.loads(raw)
                except Exception:  # noqa: BLE001 -- skip an unreadable sidecar
                    continue
                if isinstance(data, dict):
                    bag = data.get(fid)
                    if isinstance(bag, dict):
                        return bag
    except Exception:  # noqa: BLE001 -- S3 fault -> live fallback
        logger.warning("building-detail: sidecar scan degraded", exc_info=True)
        return None
    return None


def _read_tags_from_overpass(osm_type: str, osm_id: str) -> dict[str, Any] | None:
    """Live Overpass-by-id fallback for one element -> its tag bag (or None).

    SYNC (httpx); the caller wraps it in ``asyncio.to_thread``. Returns the OSM
    ``tags`` dict for the element, or None when the element is unknown / has no
    tags / Overpass is unreachable (the handler then emits a typed 404).
    """
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return None
    ql = f"[out:json][timeout:25];{osm_type}({osm_id});out tags;"
    try:
        with httpx.Client(
            timeout=30.0, headers={"User-Agent": "grace2-building-detail/1.0"}
        ) as client:
            resp = client.post(
                "https://overpass-api.de/api/interpreter", data={"data": ql}
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception:  # noqa: BLE001 -- Overpass unreachable / non-JSON
        logger.warning("building-detail: live Overpass-by-id failed", exc_info=True)
        return None
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        return None
    for el in elements:
        if not isinstance(el, dict):
            continue
        tags = el.get("tags")
        if isinstance(tags, dict) and tags:
            return tags
    return None


async def _handle_building_detail(query_string: str) -> bytes:
    """Resolve the JSON body for ``GET /api/building-detail``.

    Returns the encoded ``{fid, tags:{...}}`` body on success. Raises
    ``_BuildingDetailBadRequest`` (-> 400) on malformed input and
    ``_BuildingDetailNotFound`` (-> 404) when neither the sidecar nor live
    Overpass yields tags. Both the S3 sidecar scan and the live Overpass query
    run off the event loop via ``asyncio.to_thread``.
    """
    osm_type, osm_id = _parse_building_detail_qs(query_string)
    fid = _building_fid(osm_type, osm_id)

    tags = await asyncio.to_thread(_read_tags_from_sidecars, fid)
    if tags is None:
        # Sidecar miss (cold box, evicted, or never written) -> live by-id.
        tags = await asyncio.to_thread(_read_tags_from_overpass, osm_type, osm_id)
    if tags is None:
        raise _BuildingDetailNotFound(
            f"no tags for {osm_type}/{osm_id} (sidecar + live Overpass both empty)"
        )
    return json.dumps(
        {"fid": fid, "tags": tags}, separators=(",", ":")
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server (asyncio, stdlib only)
# ---------------------------------------------------------------------------


_HTTP_VERSION = b"HTTP/1.1"
_CRLF = b"\r\n"


def _format_response(
    status: int,
    body: bytes,
    *,
    content_type: str = "application/json; charset=utf-8",
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """Assemble a minimal HTTP/1.1 response."""
    reason = {
        200: "OK",
        204: "No Content",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
        502: "Bad Gateway",
    }.get(status, "OK")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        # CORS — see module docstring.
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)
    header_lines = (
        _HTTP_VERSION
        + b" "
        + str(status).encode()
        + b" "
        + reason.encode()
        + _CRLF
    )
    for k, v in headers.items():
        header_lines += f"{k}: {v}".encode() + _CRLF
    return header_lines + _CRLF + body


async def _handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle one HTTP request.

    The wire-protocol implementation is intentionally minimal — we only need
    to serve GET ``/api/tool-catalog`` and respond to CORS preflights. Any
    other path returns 404; any other method returns 405. Body is read until
    Content-Length OR end-of-stream so a stray POST doesn't hang.
    """
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    except asyncio.TimeoutError:
        writer.close()
        return
    if not request_line:
        writer.close()
        return
    try:
        method, path, _version = request_line.decode("ascii", "replace").split()
    except ValueError:
        body = _format_response(400, b'{"error":"bad request line"}')
        writer.write(body)
        await writer.drain()
        writer.close()
        return

    # Drain headers; we don't need them, but the socket must be advanced past
    # them before we close so the client sees our response cleanly.
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            break
        if not line or line == b"\r\n" or line == b"\n":
            break

    if method == "OPTIONS":
        # CORS preflight.
        writer.write(_format_response(204, b""))
        await writer.drain()
        writer.close()
        return

    if method != "GET":
        writer.write(
            _format_response(405, b'{"error":"method not allowed"}')
        )
        await writer.drain()
        writer.close()
        return

    # job-0255: streaming WMS proxy. Handled BEFORE the buffered
    # ``_format_response`` paths because it writes a chunked/streamed response
    # directly to ``writer`` (whole tiles are never buffered in agent memory —
    # contract lens). Env-gated: when ``QGIS_PROXY_ENABLED`` is off (default),
    # the route is treated as absent and falls through to the 404 below, so
    # TODAY'S behavior is unchanged until job-0257 flips the flag in prod.
    proxy_path, _, proxy_qs = path.partition("?")
    if proxy_path == "/qgis-proxy":
        from .qgis_proxy import qgis_proxy_enabled

        if not qgis_proxy_enabled():
            # Route absent when disabled — 404 exactly like an unknown path.
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        await _handle_qgis_proxy(proxy_qs, writer)
        # ``_handle_qgis_proxy`` owns draining + closing the writer.
        return

    if path == "/api/tool-catalog":
        try:
            payload = build_catalog_payload()
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            writer.write(_format_response(200, body))
        except Exception:  # noqa: BLE001
            logger.exception("tool-catalog payload build failed")
            writer.write(
                _format_response(500, b'{"error":"catalog build failed"}')
            )
    elif path == "/api/telemetry/summary":
        try:
            summary = await build_telemetry_summary()
            body = json.dumps(summary, separators=(",", ":")).encode("utf-8")
            writer.write(_format_response(200, body))
        except Exception:  # noqa: BLE001
            logger.exception("telemetry summary build failed")
            writer.write(
                _format_response(500, b'{"error":"telemetry summary failed"}')
            )
    elif proxy_path == "/api/building-detail":
        # Click-to-enrich (NATE 2026-06-27): the building footprint inline
        # GeoJSON is now SLIM (id-only props). The popup fetches the full tag
        # bag on demand by (osm_type, osm_id) here. Cold/box-off friendly + off
        # the event loop (S3 + Overpass run via asyncio.to_thread).
        try:
            body = await _handle_building_detail(proxy_qs)
            writer.write(_format_response(200, body))
        except _BuildingDetailNotFound as exc:
            writer.write(
                _format_response(
                    404,
                    json.dumps(
                        {"error": "building detail not found", "detail": str(exc)},
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            )
        except _BuildingDetailBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps(
                        {"error": "bad request", "detail": str(exc)},
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("building-detail lookup failed")
            writer.write(
                _format_response(500, b'{"error":"building detail failed"}')
            )
    elif path == "/api/health":
        # Autostop liveness probe (agent-box auto-stop/wake infra). The idle
        # Lambda polls this and its safety gate reads ``busy`` to decide whether
        # the always-on agent EC2 box may be stopped: it stops ONLY after N
        # consecutive polls with busy == false (Stage 3: a merely-open IDLE
        # connection no longer keeps the box up; ``active_connections`` is still
        # reported for observability). ``busy`` comes from the in-flight turn +
        # solver markers in ``server.py`` (same process, same asyncio loop), and
        # the idle Lambda additionally ORs its own Batch DescribeJobs check.
        # Best-effort: if the snapshot raises for any reason
        # we fall back to a conservative busy=true so a transient glitch can
        # never trick the gate into stopping a live box.
        try:
            from .server import liveness_snapshot

            health = liveness_snapshot()
        except Exception:  # noqa: BLE001 — never let the probe stop a live box
            logger.exception("liveness snapshot failed; reporting busy=true")
            health = {"ok": True, "active_connections": 1, "busy": True}
        body = json.dumps(health, separators=(",", ":")).encode("utf-8")
        writer.write(_format_response(200, body))
    else:
        writer.write(_format_response(404, b'{"error":"not found"}'))
    await writer.drain()
    writer.close()


def _format_streaming_head(
    status: int,
    headers: dict[str, str],
) -> bytes:
    """Assemble the status line + headers for a STREAMED response (no body).

    Unlike ``_format_response`` (which knows the full body and sets a
    Content-Length), the proxy does not buffer the body — it relays chunks as
    they arrive. We forward the upstream's filtered headers (which include the
    upstream Content-Length / Content-Type for the tile), add permissive CORS
    so the browser can fetch tiles cross-origin, and force ``Connection: close``
    so the client knows the body ends at EOF even when the upstream omitted a
    Content-Length.
    """
    reason = {
        200: "OK",
        204: "No Content",
        206: "Partial Content",
        301: "Moved Permanently",
        302: "Found",
        304: "Not Modified",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }.get(status, "OK")
    out_headers: dict[str, str] = {}
    # Upstream's relayable headers first (Content-Type/Length/Cache etc.).
    out_headers.update(headers)
    # CORS — WMS tiles are images, not credentialed data; permissive origin is
    # the correct posture (matches the catalog endpoint above).
    out_headers["Access-Control-Allow-Origin"] = "*"
    out_headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    out_headers["Access-Control-Allow-Headers"] = "Content-Type"
    out_headers["Connection"] = "close"
    head = (
        _HTTP_VERSION
        + b" "
        + str(status).encode()
        + b" "
        + reason.encode()
        + _CRLF
    )
    for k, v in out_headers.items():
        head += f"{k}: {v}".encode() + _CRLF
    return head + _CRLF


async def _handle_qgis_proxy(
    query_string: str,
    writer: asyncio.StreamWriter,
) -> None:
    """Stream a QGIS Server WMS response to ``writer`` (job-0255).

    Bridges the proxy module's ``stream_qgis_response`` to the raw asyncio
    stream writer: writes the status line + filtered headers when the upstream
    responds, then relays each body chunk as it arrives. Owns draining +
    closing the writer in all paths (success, upstream-unreachable 502, error).
    """
    from .qgis_proxy import ProxyResult, stream_qgis_response

    head_written = False

    async def _write_head(result: "ProxyResult") -> None:
        nonlocal head_written
        writer.write(_format_streaming_head(result.status, result.headers))
        await writer.drain()
        head_written = True

    async def _write_chunk(chunk: bytes) -> None:
        writer.write(chunk)
        await writer.drain()

    try:
        await stream_qgis_response(query_string, _write_head, _write_chunk)
    except Exception:  # noqa: BLE001 — upstream unreachable / transport error
        logger.warning("qgis-proxy: upstream relay failed", exc_info=True)
        if not head_written:
            # No bytes on the wire yet — we can still send an honest 502.
            writer.write(_format_response(502, b'{"error":"qgis upstream unreachable"}'))
    finally:
        try:
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        writer.close()


async def serve_catalog_http(
    host: str = "127.0.0.1",
    port: int | None = None,
) -> asyncio.AbstractServer:
    """Start the catalog HTTP listener and return the server handle.

    Designed to be mounted alongside the WebSocket server in
    ``server.run_server`` — same asyncio loop, single process, no threads.

    Reads ``GRACE2_AGENT_HTTP_PORT`` if ``port`` is not passed (default
    ``DEFAULT_HTTP_PORT``).
    """
    if port is None:
        try:
            port = int(os.environ.get("GRACE2_AGENT_HTTP_PORT", DEFAULT_HTTP_PORT))
        except ValueError:
            port = DEFAULT_HTTP_PORT
    server = await asyncio.start_server(_handle_http, host, port)
    logger.info(
        "tool-catalog HTTP server listening host=%s port=%d", host, port
    )
    return server
