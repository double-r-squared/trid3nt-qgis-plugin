"""HTTP catalog endpoint: the read-only discovery surface.

It serves the tool catalog as JSON and as an HTML page, plus a telemetry
summary, on its own listener beside the WebSocket server. Every facet derives
from the tool registry, and the endpoints are unauthenticated with open CORS."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trid3nt_server.adapters import model_discovery

logger = logging.getLogger("trid3nt_server.server.protocol.catalog_http")

__all__ = [
    "build_catalog_payload",
    "render_catalog_page",
    "load_query_corpus",
    "serve_catalog_http",
    "build_telemetry_summary",
    "build_case_list_payload",
    "DEFAULT_HTTP_PORT",
]


DEFAULT_HTTP_PORT = 8766

# Module-level cache: loaded once on the first request and retained until the
# process restarts; a discovery endpoint needs no hot-reload semantics.
_CORPUS_CACHE: dict[str, list[str]] | None = None
_PAYLOAD_CACHE: dict[str, Any] | None = None


def _default_corpus_path() -> Path:
    """Resolve the residual corpus file under the package: the
    ``TRID3NT_TOOL_CORPUS_YAML`` override when set, else the packaged path."""
    env_path = os.environ.get("TRID3NT_TOOL_CORPUS_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return _package_tools_dir() / "tool_query_corpus.yaml"


def _package_tools_dir() -> Path:
    """The ``trid3nt_server/tools`` directory, anchored on the package root so the
    corpus resolves regardless of this module's depth in the package tree."""
    import trid3nt_server

    return Path(trid3nt_server.__file__).resolve().parent / "tools"


def _package_workflows_dir() -> Path:
    """The ``trid3nt_server/workflows`` directory - engine templates and their
    co-located corpus files. Every per-engine simulation shim lives under
    ``tools/`` or ``workflows/``; there is no third location to search."""
    import trid3nt_server

    return Path(trid3nt_server.__file__).resolve().parent / "workflows"


class _CorpusFormatError(Exception):
    """Non-string entry in a corpus YAML list: an unquoted phrasing containing a
    colon parses as a one-key dict, and a dropped phrasing would vanish from
    retrieval with no signal, so it is refused instead."""


def _read_corpus_yaml(p: Path) -> dict[str, list[str]]:
    """Load a single corpus YAML into ``{tool: [queries]}``; a missing file
    yields ``{}``, and a non-string entry raises rather than being dropped."""
    if not p.exists():
        return {}
    try:
        with p.open() as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("catalog_http: failed to parse corpus YAML at %s", p)
        return {}
    if not isinstance(data, dict):
        return {}
    parsed: dict[str, list[str]] = {}
    for k, v in data.items():
        if not (isinstance(k, str) and isinstance(v, list)):
            continue
        queries: list[str] = []
        for q in v:
            if not isinstance(q, str):
                raise _CorpusFormatError(
                    f"non-string corpus entry in {p}: tool {k!r} has entry "
                    f"{q!r} ({type(q).__name__}) -- likely an unquoted "
                    "phrasing containing a colon; quote it as a YAML string"
                )
            queries.append(q)
        parsed[k] = queries
    return parsed


def _compose_corpus_from_tree() -> dict[str, list[str]]:
    """Compose the flat corpus: every co-located ``corpus.yaml`` under the tools
    and workflows trees, merged with the residual monolith."""
    tools_dir = _package_tools_dir()
    composed: dict[str, list[str]] = {}
    for base in (tools_dir, _package_workflows_dir()):
        for cpath in sorted(base.rglob("corpus.yaml")):
            composed.update(_read_corpus_yaml(cpath))
    composed.update(_read_corpus_yaml(tools_dir / "tool_query_corpus.yaml"))
    return composed


def load_query_corpus(path: Path | None = None) -> dict[str, list[str]]:
    """Load and cache the example-query corpus as ``tool_name -> [query]``,
    composing the co-located files with the residual monolith unless a single
    file is pinned. A missing or malformed file degrades to fewer queries."""
    global _CORPUS_CACHE
    if _CORPUS_CACHE is not None:
        return _CORPUS_CACHE
    if path is not None:
        _CORPUS_CACHE = _read_corpus_yaml(path)
    else:
        env_path = os.environ.get("TRID3NT_TOOL_CORPUS_YAML")
        if env_path:
            _CORPUS_CACHE = _read_corpus_yaml(Path(env_path).expanduser().resolve())
        else:
            _CORPUS_CACHE = _compose_corpus_from_tree()
    logger.info(
        "catalog_http: loaded %d tool query entries", len(_CORPUS_CACHE)
    )
    return _CORPUS_CACHE


def _facet_str(value: Any) -> str | None:
    """Coerce a metadata facet (enum / str / None) to a plain string or None."""
    if value is None:
        return None
    s = str(value)
    return s or None


def build_catalog_payload(
    *,
    corpus: dict[str, list[str]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Assemble the flat ``/api/tool-catalog`` payload: a thin reader over the
    registry listing every tool with its REAL docstring, the exact text the
    model routes on, and the metadata facets the tool already carries."""
    from trid3nt_server.tools import TOOL_REGISTRY

    global _PAYLOAD_CACHE
    if use_cache and _PAYLOAD_CACHE is not None:
        return _PAYLOAD_CACHE

    corpus_map = corpus if corpus is not None else load_query_corpus()

    tools_out: list[dict[str, Any]] = []
    for name in sorted(TOOL_REGISTRY.keys()):
        entry = TOOL_REGISTRY[name]
        meta = entry.metadata
        # A declared tool renders TWO views of its docstring; this page is a
        # CHOOSE-the-tool surface, so it takes the routing one. Everything else
        # has only its written docstring.
        doc_full = (getattr(entry.fn, "routing_doc", None)
                    or entry.fn.__doc__ or "").strip()
        # Cap to 3 sample queries -- the page shows 2-3; sending all 5-10 wastes
        # bandwidth on a discovery surface.
        sample_queries = list(corpus_map.get(name, []))[:3]
        tools_out.append(
            {
                "name": name,
                "docstring": doc_full,
                "engine": _facet_str(getattr(meta, "engine", None)),
                "tier": _facet_str(getattr(meta, "tier", "general")) or "general",
                "source_class": _facet_str(meta.source_class),
                "supports_global_query": bool(meta.supports_global_query),
                "cacheable": bool(meta.cacheable),
                "ttl_class": str(meta.ttl_class),
                "annotations": {
                    "read_only_hint": bool(meta.read_only_hint),
                    "open_world_hint": bool(meta.open_world_hint),
                    "destructive_hint": bool(meta.destructive_hint),
                    "idempotent_hint": bool(meta.idempotent_hint),
                },
                "sample_queries": sample_queries,
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "tool_count": len(tools_out),
        "tools": tools_out,
    }
    if use_cache:
        _PAYLOAD_CACHE = payload
    return payload


# Self-contained catalog page. Inline CSS + JS + embedded data -- NO external
# assets (a strict-CSP / offline viewer must render it unchanged). The data is
# embedded as a JSON <script> block the inline JS reads once; facets and search
# are derived from it entirely client-side.
_CATALOG_PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRID3NT tool catalog</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--muted:#666;--card:#f6f7f9;--border:#dcdfe4;--badge:#e6ebf2;--accent:#2d6cdf}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e6e8eb;--muted:#9aa2ad;--card:#1d2027;--border:#2c313a;--badge:#252b36;--accent:#5b8cf0}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--border);padding:16px 20px;z-index:2}
h1{margin:0 0 4px;font-size:20px}
.sub{color:var(--muted);font-size:13px}
.controls{margin-top:12px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
#q{flex:1 1 260px;min-width:200px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}
select{padding:7px 8px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}
main{padding:16px 20px;max-width:1100px;margin:0 auto}
.tool{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:14px 16px;margin:0 0 12px}
.tool h2{margin:0;font:600 15px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
.badges{margin:6px 0 8px;display:flex;flex-wrap:wrap;gap:6px}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--badge);color:var(--muted);white-space:nowrap}
.badge.eng{color:var(--accent)}
pre.doc{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--fg)}
.sq{margin-top:8px;font-size:12px;color:var(--muted)}
.sq b{color:var(--fg);font-weight:600}
.empty{color:var(--muted);padding:40px 0;text-align:center}
</style></head>
<body>
<header>
  <h1>TRID3NT tool catalog</h1>
  <div class="sub">The agent's-eye view: every registered tool with the exact docstring the model routes on. <span id="count"></span></div>
  <div class="controls">
    <input id="q" type="search" placeholder="Search name + docstring..." autocomplete="off">
    <select id="f-engine"><option value="">engine: all</option></select>
    <select id="f-tier"><option value="">tier: all</option></select>
    <select id="f-source"><option value="">source_class: all</option></select>
  </div>
</header>
<main id="list"></main>
<script id="catalog-data" type="application/json">__DATA__</script>
<script>
(function(){
  var data=JSON.parse(document.getElementById("catalog-data").textContent);
  var tools=data.tools||[];
  var list=document.getElementById("list");
  var q=document.getElementById("q");
  var fEngine=document.getElementById("f-engine"),fTier=document.getElementById("f-tier"),fSource=document.getElementById("f-source");
  function opts(sel,vals){vals.forEach(function(v){var o=document.createElement("option");o.value=v;o.textContent=v;sel.appendChild(o);});}
  function uniq(key){var s={};tools.forEach(function(t){if(t[key])s[t[key]]=1;});return Object.keys(s).sort();}
  opts(fEngine,uniq("engine"));opts(fTier,uniq("tier"));opts(fSource,uniq("source_class"));
  function esc(x){return (x==null?"":String(x)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function badges(t){var b=[];if(t.engine)b.push('<span class="badge eng">'+esc(t.engine)+'</span>');
    b.push('<span class="badge">tier: '+esc(t.tier)+'</span>');
    if(t.source_class)b.push('<span class="badge">'+esc(t.source_class)+'</span>');
    if(t.supports_global_query)b.push('<span class="badge">global</span>');
    if(t.annotations&&t.annotations.read_only_hint)b.push('<span class="badge">read-only</span>');
    return b.join("");}
  function render(){
    var term=q.value.trim().toLowerCase();
    var e=fEngine.value,ti=fTier.value,so=fSource.value;
    var html="",n=0;
    tools.forEach(function(t){
      if(e&&t.engine!==e)return;if(ti&&t.tier!==ti)return;if(so&&t.source_class!==so)return;
      if(term&&(t.name+" "+t.docstring).toLowerCase().indexOf(term)<0)return;
      n++;
      var sq=(t.sample_queries&&t.sample_queries.length)?'<div class="sq"><b>e.g.</b> '+t.sample_queries.map(esc).join(" &middot; ")+'</div>':"";
      html+='<div class="tool"><h2>'+esc(t.name)+'</h2><div class="badges">'+badges(t)+'</div><pre class="doc">'+esc(t.docstring)+'</pre>'+sq+'</div>';
    });
    list.innerHTML=n?html:'<div class="empty">No tools match.</div>';
    document.getElementById("count").textContent=n+" of "+tools.length+" shown";
  }
  q.addEventListener("input",render);fEngine.addEventListener("change",render);
  fTier.addEventListener("change",render);fSource.addEventListener("change",render);
  render();
})();
</script>
</body></html>"""


def render_catalog_page(payload: dict[str, Any] | None = None) -> bytes:
    """Render the self-contained HTML catalog page as UTF-8 bytes; the payload
    is embedded as inline JSON with ``</`` escaped, so a docstring containing it
    cannot break out of the script block."""
    data = payload if payload is not None else build_catalog_payload()
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    safe = raw.replace("</", "<\\/")
    return _CATALOG_PAGE_TEMPLATE.replace("__DATA__", safe).encode("utf-8")




def _get_telemetry_path() -> Path:
    """Resolve the JSONL read path by delegating to the telemetry module's own
    segmented resolver, so a dashboard read and a live write agree on where the
    sink lives. Kept as a named callable because tests pin it directly."""
    from trid3nt_server import telemetry as _telemetry

    return Path(_telemetry._get_telemetry_path())


# The shadow-selection rows share the tool-call sink, tagged with this
# discriminator.
_SHADOW_RECORD_TYPE = "tool_retrieval_shadow"

#: Terminal solver tools -> the flow they identify. A turn is attributed to a
#: flow when it dispatched one of these, which drives the recall@k per-flow
#: breakdown. Keys must be registered engine templates; a key that no longer
#: registers reports a permanently empty flow.
_FLOW_BY_SOLVER_TOOL: dict[str, str] = {
    "telemac_river_dye": "river-plume",
    "telemac_river_oil_spill": "river-oil-slick",
    "telemac_river_scour": "river-mobile-bed",
    "telemac_river_sediment_plume": "river-sediment-plume",
    "telemac_do_sag": "oxygen-sag",
    "telemac_rain_on_grid": "rainfall-runoff",
    "telemac3d_stratified_flow": "stratified-flow",
    "artemis_harbor_agitation": "harbor-agitation",
}

#: Flow order in the per-flow breakdown. Derived so a flow can never be reported
#: without a tool that produces it, nor a tool added without its flow appearing.
_FLOWS: tuple[str, ...] = tuple(dict.fromkeys(_FLOW_BY_SOLVER_TOOL.values()))


def _normalize_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Coerce a single telemetry record into the summary's canonical shape,
    accepting either the ``success``/``ts`` or the ``result_ok``/``called_at_utc``
    spelling, so the summary builder does not care which writer produced it."""
    out: dict[str, Any] = {}
    out["session_id"] = rec.get("session_id") or ""
    out["tool_name"] = rec.get("tool_name") or ""
    out["source"] = rec.get("source") or "llm"
    # Either spelling of the outcome flag.
    if "result_ok" in rec:
        out["result_ok"] = bool(rec.get("result_ok"))
    else:
        out["result_ok"] = bool(rec.get("success", True))
    out["latency_ms"] = float(rec.get("latency_ms") or 0.0)
    out["error_code"] = rec.get("error_code")
    out["retry_attempt"] = int(rec.get("retry_attempt") or 0)
    out["cached_content_token_count"] = rec.get("cached_content_token_count")
    # ``result_usable`` is None when the notion does not apply, as for a meta
    # tool; ``routed_ok`` carries the routing-quality heuristic per record.
    out["result_usable"] = rec.get("result_usable")
    out["routed_ok"] = rec.get("routed_ok")
    # Timestamp under either spelling.
    out["called_at_utc"] = rec.get("called_at_utc") or rec.get("ts") or ""
    # None on a record written before the model dimension existed; the
    # aggregator buckets that as "unknown".
    out["model_id"] = rec.get("model_id")
    # The per-turn dispatch id is the recall@k join key against that turn's
    # shadow row, and recall counts only dispatches that carry one.
    out["turn_id"] = rec.get("turn_id")
    return out


def _empty_solve_telemetry() -> dict[str, Any]:
    """The zero-state solve-telemetry section: an empty ``recent`` list and zero
    percentiles until at least one solve has been logged."""
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
        # Tool-accuracy fields.
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
    """The ``q``-th percentile, q in [0,1], by linear interpolation; empty input
    yields ``0.0``. Pure stdlib, matching numpy's default method so an external
    recompute agrees."""
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
    """Fraction of ``True`` among the non-``None`` entries, or ``None`` when
    every entry is ``None``, so a rate the data cannot support is an honest null
    rather than a misleading zero."""
    considered = [v for v in values if v is not None]
    if not considered:
        return None
    trues = sum(1 for v in considered if v)
    return trues / len(considered)


def _derive_routed_ok(records: list[dict[str, Any]]) -> dict[int, bool]:
    """Derive the routing-quality heuristic per record, keyed by ``id(rec)`` so
    two identical records score independently. A value the writer already
    supplied wins; this only fills the gap where it left one None."""
    # HEURISTIC, NOT GROUND TRUTH: a call counts as mis-routed when it FAILED
    # and the same session's NEXT call, by timestamp, is a DIFFERENT tool - the
    # model abandoned it and reached for another for the same logical step. Any
    # other completed call is routed_ok.
    out: dict[int, bool] = {}
    sess_buckets: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        sid = r.get("session_id") or ""
        if not sid:
            # No session context -- cannot judge supersession; routed_ok stays
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
    """Compute the dashboard summary over normalized records, as a
    JSON-serializable dict; every read path funnels through here, so the
    aggregation lives in one place."""
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
        # result_usable (bool|None -- meta tools contribute None).
        usable = r.get("result_usable")
        by_tool_usable.setdefault(tool, []).append(usable)
        all_usable.append(usable)
        # routed_ok (the derived heuristic; None when no session context).
        routed = routed_ok_by_id.get(id(r))
        by_tool_routed.setdefault(tool, []).append(routed)
        all_routed.append(routed)
        src = r["source"] or "llm"
        by_source_count[src] = by_source_count.get(src, 0) + 1
        # Cache hit rate: a present cached-token count means the provider
        # reported a cached-content path, so the call counts as a hit.
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
                # Tool-accuracy fields.
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

    # Per-model breakdown (in-chat model selector).
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
        # Tool-accuracy fields.
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
        # Model dimension (in-chat model selector).
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


# The solve-telemetry section of the summary.
#
# The per-solve metrics - grid resolution, active cells, vCPU, wall clock,
# backend, AOI - are read from the JSONL the solve writer already maintains and
# folded into the summary.

_DEFAULT_SOLVE_TELEMETRY_PATH = "/tmp/trid3nt_solve_telemetry.jsonl"

#: How many recent solve records to surface in the ``recent`` array.
_SOLVE_RECENT_CAP = 20


def _get_solve_telemetry_path() -> Path:
    """Resolve the solve-telemetry JSONL path, env override then default, the
    same way the writer does so reader and writer agree."""
    return Path(
        os.environ.get(
            "TRID3NT_SOLVE_TELEMETRY_PATH", _DEFAULT_SOLVE_TELEMETRY_PATH
        )
    )


def _load_solve_records_from_file(path: Path) -> list[dict[str, Any]]:
    """Read the solve-telemetry JSONL in file order; a missing or unreadable
    file yields an empty list and the summary carries its zero state."""
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
    """Build the solve-telemetry section: ``recent`` newest-first and capped,
    with percentiles over every record carrying a numeric wall clock. Empty
    input yields the zero-state section."""
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
    path: Path | list[Path],
    *,
    last_n_sessions: int = 30,
) -> list[dict[str, Any]]:
    """Read one JSONL file, or a list of retained segments, and return the
    records of the most-recent ``last_n_sessions`` distinct sessions, newest
    first; an unreadable target is skipped rather than fatal."""
    targets = [path] if isinstance(path, Path) else list(path)
    out: list[dict[str, Any]] = []
    for target in targets:
        if not target.exists():
            continue
        try:
            with target.open("r", encoding="utf-8") as fh:
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
                    # Shadow rows share this sink but are not dispatches; the
                    # recall@k path reads them separately.
                    if rec.get("record_type") == _SHADOW_RECORD_TYPE:
                        continue
                    out.append(_normalize_record(rec))
        except OSError:
            continue
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


def _load_shadow_records_from_file(path: Path | list[Path]) -> list[dict[str, Any]]:
    """Read the tool-retrieval SHADOW rows from one JSONL file or a list of
    retained segments, keyed for recall@k by session and turn; an unreadable
    target is skipped."""
    targets = [path] if isinstance(path, Path) else list(path)
    out: list[dict[str, Any]] = []
    for target in targets:
        if not target.exists():
            continue
        try:
            with target.open("r", encoding="utf-8") as fh:
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
            continue
    return out


def _normalize_shadow_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Coerce a shadow row into the recall@k canonical shape, normalizing
    ``visible_tools`` to a set of strings."""
    vis = rec.get("visible_tools") or []
    try:
        visible = {str(t) for t in vis}
    except Exception:  # noqa: BLE001 -- a malformed row contributes an empty set
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
    """Compute recall@k of the tool-retrieval shadow selection, PURE: per turn,
    the model-dispatched tools present in that turn's would-be-visible set over
    all its dispatched tools, so a dropped tool is a MISS."""
    # A turn with no shadow row is EXCLUDED: recall is defined only where a
    # would-be set was logged. The join key is (session_id, turn_id).
    # Index shadow rows by (session_id, turn_id) -> visible set.
    shadow_by_turn: dict[tuple[str, str], set[str]] = {}
    k_values: list[int] = []
    for s in shadow_records:
        norm = _normalize_shadow_record(s)
        sid = norm["session_id"]
        tid = norm["turn_id"]
        if not tid:
            continue
        # If one turn logged several shadow rows, the union is the safe choice:
        # over-inclusion never penalizes recall.
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

    # Determine each turn's solver flow from the terminal solver tool it
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
    for flow in _FLOWS:
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


async def build_telemetry_summary(
    *,
    last_n_sessions: int = 30,
    all_segments: bool = False,
) -> dict[str, Any]:
    """Build the routing-quality summary served by the telemetry endpoint, over
    the current boot segment by default or every retained segment; nothing found
    yields the all-zero empty summary."""
    if all_segments:
        from trid3nt_server import telemetry as _telemetry

        read_targets: Path | list[Path] = [
            Path(p) for p in _telemetry.telemetry_read_paths(all_segments=True)
        ]
    else:
        read_targets = _get_telemetry_path()

    records: list[dict[str, Any]] = _load_recent_records_from_file(
        read_targets, last_n_sessions=last_n_sessions
    )
    used_source = "file" if records else "empty"

    summary = _aggregate_records(records)
    summary["source"] = used_source

    # Fold in the tool-retrieval shadow recall@k section.
    # The would-be-visible shadow rows share the SAME JSONL sink (tagged by
    # record_type); load them and join against the dispatched llm tools above by
    # turn_id. Best-effort: a read/compute fault leaves the zero-state section
    # seeded by _aggregate_records (never breaks the dashboard).
    try:
        shadow_records = _load_shadow_records_from_file(read_targets)
        summary["recall_at_k"] = compute_recall_at_k(records, shadow_records)
    except Exception:  # noqa: BLE001 -- never break the dashboard on recall read
        logger.warning("telemetry summary: recall@k read failed", exc_info=True)
        summary["recall_at_k"] = _empty_recall_at_k()

    # Fold in the live big-sim solve_telemetry section. Read
    # from the solve-telemetry JSONL the solve writer maintains; best-effort so
    # a missing/unreadable sink leaves the zero-state section _aggregate_records
    # already seeded. Independent of the tool-call source above -- solves are
    # logged on their own sink.
    try:
        solve_records = _load_solve_records_from_file(_get_solve_telemetry_path())
        summary["solve_telemetry"] = _aggregate_solve_telemetry(solve_records)
    except Exception:  # noqa: BLE001 -- never break the dashboard on solve read
        logger.warning("telemetry summary: solve telemetry read failed", exc_info=True)
        summary["solve_telemetry"] = _empty_solve_telemetry()

    # Fold in the PER-TURN per-model aggregates: turns,
    # mean token counts, mean wall ms, upstream_error_count per model_id. Read
    # from the turn-telemetry JSONL sink the server's turn loop writes
    # (telemetry.emit_turn_telemetry -- its own sink, like solve telemetry);
    # the aggregation lives in telemetry.build_turn_summary. Best-effort: a
    # read/compute fault leaves the zero-state section (never breaks the
    # dashboard).
    try:
        from trid3nt_server.telemetry import build_turn_summary, load_turn_records

        summary["turns_by_model"] = build_turn_summary(load_turn_records())
    except Exception:  # noqa: BLE001 -- never break the dashboard on turn read
        logger.warning("telemetry summary: turn telemetry read failed", exc_info=True)
        from trid3nt_server.telemetry import empty_turn_summary

        summary["turns_by_model"] = empty_turn_summary()
    return summary


# Building click-to-enrich detail endpoint.
#
# The building footprint inline GeoJSON now carries ID-only props (osm_id /
# osm_type / a composite fid). The full tag bag (building / height / levels /
# name / addr:*) is persisted in a per-AOI sidecar next to the .fgb
# (cache/static-30d/buildings/<key>.tags.json) keyed by fid. This endpoint reads
# that sidecar for a clicked (osm_type, osm_id); if no sidecar carries the fid it
# falls back to a LIVE Overpass-by-id query. Non-blocking: S3 + Overpass run via
# asyncio.to_thread so the agent's WS heartbeat is never starved.


class _BuildingDetailNotFound(Exception):
    """No tag bag found for the requested building (sidecar miss + live miss)."""


class _BuildingDetailBadRequest(Exception):
    """Malformed /api/building-detail request (missing/invalid osm_type|osm_id)."""


def _building_fid(osm_type: str, osm_id: str) -> str:
    """The building feature id the fetcher writes: type initial plus id."""
    return f"{osm_type[:1]}{osm_id}"


def _parse_building_detail_qs(query_string: str) -> tuple[str, str]:
    """Parse and validate the OSM element kind and id from the query string;
    anything malformed raises, so the handler emits a typed 400 rather than a
    fabricated success."""
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
    """Scan the buildings tag sidecars for ``fid`` and return its tag bag, or
    ``None``. SYNC: the caller off-loads it. The request carries no bbox, so the
    bounded sidecar prefix is listed; a storage fault degrades to the live read."""
    try:

        from trid3nt_server.tools.cache import CACHE_BUCKET, cache_path
        # fetch_buildings is folded to the router: the sidecar identity
        # (source_class / ttl / .tags.json ext) now lives in the promoted spec, not a
        # coded twin. Read it from the spec, falling back to the load-bearing literals
        # so a cold spec registry never breaks the enrich read.
        from trid3nt_server.tools.fetchers._router.registration import get_spec
    except Exception:  # noqa: BLE001 -- import wiring fault -> live fallback
        logger.warning("building-detail: sidecar import wiring failed", exc_info=True)
        return None

    _spec = get_spec("fetch_buildings")
    if _spec is not None:
        source_class = _spec.source_class
        ttl_class = _spec.cache.ttl_class
        sidecar_ext = str(((_spec.ingest or {}).get("sidecar_write") or {}).get("ext", "tags.json"))
    else:
        source_class, ttl_class, sidecar_ext = "buildings", "static-30d", "tags.json"

    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    # Derive the buildings/<...> prefix from cache_path with a placeholder key.
    sentinel = cache_path(source_class, ttl_class, "KEY", sidecar_ext)
    prefix = sentinel.rsplit("KEY", 1)[0]  # cache/static-30d/buildings/
    suffix = f".{sidecar_ext}"
    try:
        from trid3nt_server.workflows.solver.solver import _get_s3_client

        s3 = _get_s3_client()
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
    """Live by-id fallback for one OSM element's tag bag, or ``None`` when the
    element is unknown, untagged or unreachable, in which case the handler emits
    a typed 404. SYNC: the caller off-loads it."""
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return None
    ql = f"[out:json][timeout:25];{osm_type}({osm_id});out tags;"
    try:
        with httpx.Client(
            timeout=30.0, headers={"User-Agent": "trid3nt-building-detail/1.0"}
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
    """Resolve the ``{fid, tags}`` body for the building-detail route; malformed
    input raises a 400 and tags found in neither the sidecar nor the live read
    raise a 404. Both reads run off the event loop."""
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




class _ProviderConfigBadRequest(Exception):
    """POST /api/provider-config body was malformed. SECURITY: the message
    NEVER echoes the request body or the api_key -- only field-shape complaints
    (a malformed body could itself be a mistyped key)."""


class _ProviderConfigIncoherent(_ProviderConfigBadRequest):
    """base_url and model name DIFFERENT providers, so the push is refused with
    the env left exactly as it was. The message may name the base URL HOST and
    the model id, never the full URL or the api_key."""


#: Ollama's fixed listen port. The ONLY signal that an OpenAI-compatible
#: endpoint is Ollama rather than vLLM / llama.cpp / LM Studio, which also serve
#: on loopback but accept HuggingFace-style ``vendor/model`` ids -- so a bare
#: localhost host must NEVER be read as Ollama.
_OLLAMA_PORT = 11434

#: Live /api/tags probe budget. The dock's post_provider_config timeout is 5s;
#: this must stay far under it so a slow endpoint never stalls Save.
_OLLAMA_PROBE_TIMEOUT_S = 1.5


def _provider_family(base_url: str) -> str | None:
    """The provider family for an OpenAI-compatible base URL, or ``None`` when
    the endpoint has no fixed model-id convention; only families whose ids are
    UNAMBIGUOUS are named, and everything else is never gated."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
        port = parts.port
    except ValueError:  # unparsable authority -> no identity, no gate
        return None
    if (parts.hostname or "").lower().endswith("openrouter.ai"):
        return "openrouter"
    if port == _OLLAMA_PORT:
        return "ollama"
    return None


def _ollama_serves_model(base_url: str, model: str) -> bool | None:
    """SYNC live probe of the installed-model list: True or False when the
    endpoint answers, ``None`` when it cannot be reached. ``None`` must never be
    read as incoherence - only a non-empty answer can prove a model absent."""
    import httpx

    root = model_discovery._ollama_root(base_url)
    if not root:
        return None
    try:
        with httpx.Client(timeout=_OLLAMA_PROBE_TIMEOUT_S) as client:
            resp = client.get(f"{root}/api/tags")
            resp.raise_for_status()
            payload = resp.json()
    except Exception:  # noqa: BLE001 -- unreachable / slow / non-JSON -> unknown
        return None
    raw = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return None

    def _norm(name: str) -> str:
        return name[: -len(":latest")] if name.endswith(":latest") else name

    installed = {
        _norm(m["name"].strip())
        for m in raw
        if isinstance(m, dict)
        and isinstance(m.get("name"), str)
        and m["name"].strip()
    }
    if not installed:
        return None
    return _norm(model) in installed


def _check_provider_coherence(base_url: str, model: str) -> None:
    """Refuse when base_url and model belong to DIFFERENT providers. Called
    BEFORE any env mutation, on the RESOLVED pair rather than per field."""
    # A client saves fields independently, so a base-URL-only push can strand a
    # model id from the previous provider and leave the daemon dialling an
    # endpoint that does not serve it. Static identification is the gate; the
    # live probe runs only for the one shape static form cannot settle, and an
    # unreachable probe never rejects.
    family = _provider_family(base_url)
    if family is None or not model:
        return
    host = model_discovery._base_url_host(base_url)
    if family == "openrouter" and "/" not in model:
        raise _ProviderConfigIncoherent(
            f"provider mismatch: base_url host {host!r} is OpenRouter but model "
            f"{model!r} is not an OpenRouter id. OpenRouter ids are namespaced "
            "'vendor/model' (e.g. 'meta-llama/llama-3.3-70b-instruct:free'). "
            "Set base_url and model to the same provider."
        )
    if family == "ollama":
        if model.endswith(":free"):
            raise _ProviderConfigIncoherent(
                f"provider mismatch: base_url host {host!r} is Ollama but model "
                f"{model!r} is an OpenRouter free-tier id. Expected an installed "
                "Ollama tag (e.g. 'qwen3:8b-24k'). Set base_url and model to "
                "the same provider."
            )
        if "/" in model and _ollama_serves_model(base_url, model) is False:
            raise _ProviderConfigIncoherent(
                f"provider mismatch: base_url host {host!r} is Ollama but model "
                f"{model!r} is not installed there (it looks like another "
                "provider's namespaced id). Pull it with 'ollama pull' or set "
                "base_url and model to the same provider."
            )


def _apply_provider_config(raw_body: bytes) -> bytes:
    """Update the provider env from the POST body and return
    ``{"ok", "model", "base_url_host"}``; the adapter reads the env per call, so
    a push takes effect on the NEXT turn with no restart."""
    # The RESOLVED pair - this body over the live env - must pass the coherence
    # check before anything is written, so a rejected push leaves the env
    # byte-identical rather than half-applied. SECURITY: the api_key is written
    # to the env but NEVER logged, echoed or raised; only the base URL HOST and
    # the effective model name leave this function.
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Deliberately generic -- a decode error message can carry a fragment of
        # the raw body, which may be the mistyped key. Never surface it.
        raise _ProviderConfigBadRequest("body must be valid JSON") from None
    if not isinstance(payload, dict):
        raise _ProviderConfigBadRequest(
            'body must be a JSON object like {"base_url": "...", "model": "..."}'
        )
    field_env = {
        "base_url": "TRID3NT_OPENAI_BASE_URL",
        "api_key": "TRID3NT_OPENAI_API_KEY",
        "model": "TRID3NT_OPENAI_MODEL",
        "num_ctx": "TRID3NT_OPENAI_NUM_CTX",
    }
    updates: dict[str, str] = {}
    for field, env_name in field_env.items():
        if field in payload and payload[field] is not None:
            value = str(payload[field]).strip()
            if value:
                updates[env_name] = value
    # Gate on the pair that would be IN FORCE after this push -- a body carrying
    # only base_url still has to agree with the model already set.
    _check_provider_coherence(
        updates.get("TRID3NT_OPENAI_BASE_URL")
        or os.environ.get("TRID3NT_OPENAI_BASE_URL", "").strip(),
        updates.get("TRID3NT_OPENAI_MODEL")
        or os.environ.get("TRID3NT_OPENAI_MODEL", "").strip(),
    )
    os.environ.update(updates)
    # A same-name model must re-discover its num_ctx (the provider/num_ctx
    # switch invalidates the process-lifetime cache).
    try:
        from trid3nt_server.gates.context_budget import reset_num_ctx_cache

        reset_num_ctx_cache()
    except Exception:  # noqa: BLE001 -- cache reset is best-effort, never fatal
        pass
    effective_model = os.environ.get("TRID3NT_OPENAI_MODEL", "").strip() or None
    base_url = os.environ.get("TRID3NT_OPENAI_BASE_URL", "").strip()
    host = model_discovery._base_url_host(base_url) if base_url else ""
    return json.dumps(
        {"ok": True, "model": effective_model, "base_url_host": host},
        separators=(",", ":"),
    ).encode("utf-8")




# The cold case list: the same rows the WS session emits, over plain HTTP, so a
# client can populate its dialog BEFORE a WebSocket connection exists.
#
# The WS path scopes rows to the handshake's user. A cold caller has no
# handshake, and this build collapses every connection onto one fixed local
# user id, so it resolves the identical id anyway.


class _CaseListPersistenceUnavailable(Exception):
    """Persistence is unbound; the case list cannot be sourced (-> 503)."""


def _case_list_route_enabled() -> bool:
    """The route is served: this build has ONE fixed local user to resolve to."""
    return True


def _case_summary_to_wire(case: Any) -> dict[str, Any]:
    """One ``CaseSummary`` as a case-list row: the model's own serializers,
    narrowed to the four fields the client needs, with an honest ``None`` bbox
    when the case has none."""
    dumped = case.model_dump(mode="json")
    return {
        "case_id": dumped.get("case_id"),
        "title": dumped.get("title"),
        "updated_at": dumped.get("updated_at"),
        "bbox": dumped.get("bbox"),
    }


async def build_case_list_payload() -> dict[str, Any]:
    """Assemble the case-list payload newest-first, through the same
    persistence call the WS path makes; unbound persistence raises, so the route
    answers an honest 503 rather than a fabricated empty list."""
    from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID
    from trid3nt_server.server import get_persistence

    persistence = get_persistence()
    if persistence is None:
        raise _CaseListPersistenceUnavailable("persistence unavailable")

    cases = await persistence.list_cases_for_user(LOCAL_SINGLE_USER_ID)
    rows = [_case_summary_to_wire(c) for c in cases]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return {"cases": rows}


# /api/ingest-layer(-file) -- bidirectional layer push (QGIS plugin -> case).
#
# The reverse seam of layer materialization: the plugin's "Push layer" button
# sends the user's ACTIVE QGIS layer (vector or raster) into the current
# case as a first-class input layer. Two routes, ONE upload flow:
#
#   POST /api/ingest-layer-file?filename=<name>  -- raw request-body upload.
#     The QGIS Python runtime has no boto3 (stdlib-only), so the plugin
#     cannot PUT to MinIO directly; it streams the exported file's bytes
#     here (Content-Type: application/octet-stream, NOT multipart/form-data
#     -- this codebase has no multipart parser anywhere and a raw-body PUT is
#     the simplest correct shape for a single-file upload) and the agent does
#     the actual object-store write. Returns {"s3_uri": "s3://..."}.
#
#   POST /api/ingest-layer {"case_id", "name", "kind", "s3_uri",
#     "crs_authid"?, "make_aoi"?}  -- registers an ALREADY-uploaded object
#     (normally the s3_uri from the call above) onto the case. Runs the
#     ingest_user_layer core (cases/ingest_user_layer.py): validates the object
#     exists + is within the size cap, converts/validates the artifact,
#     merges it into the case's durable loaded_layer_summaries, and
#     best-effort-pins the AOI when make_aoi is true.
#
# Served whenever the agent runs the local single-user seam.


class _IngestLayerBadRequest(Exception):
    """Malformed /api/ingest-layer(-file) request."""


def _ingest_layer_route_enabled() -> bool:
    """The routes are served (mirrors ``_case_list_route_enabled``)."""
    return True


def _ingest_layer_fn():
    """Lazy-import seam for the ingest core (heavy geo deps load on first
    call, not at listener start; monkeypatchable in tests)."""
    from trid3nt_server.cases.ingest_user_layer import ingest_user_layer

    return ingest_user_layer


def _upload_layer_file_fn():
    """Lazy-import seam for the staging-upload helper (monkeypatchable)."""
    from trid3nt_server.cases.ingest_user_layer import upload_layer_file

    return upload_layer_file


async def _handle_ingest_layer_post(raw_body: bytes) -> bytes:
    """Resolve the JSON body for the ingest-layer route: malformed input raises
    a 400, and the ingest's own typed errors propagate for the dispatcher to map
    to honest 4xx bodies."""
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _IngestLayerBadRequest(f"body must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _IngestLayerBadRequest(
            'body must be a JSON object like {"case_id": "...", "name": "...", '
            '"kind": "vector"|"raster", "s3_uri": "s3://..."}'
        )
    case_id = payload.get("case_id")
    name = payload.get("name")
    kind = payload.get("kind")
    s3_uri = payload.get("s3_uri")
    if not isinstance(case_id, str) or not case_id.strip():
        raise _IngestLayerBadRequest("missing or empty `case_id`")
    if not isinstance(kind, str) or kind not in ("vector", "raster"):
        raise _IngestLayerBadRequest(
            f'`kind` must be "vector" or "raster", got {kind!r}'
        )
    if not isinstance(s3_uri, str) or not s3_uri.strip():
        raise _IngestLayerBadRequest("missing or empty `s3_uri`")
    crs_authid = payload.get("crs_authid")
    if crs_authid is not None and not isinstance(crs_authid, str):
        raise _IngestLayerBadRequest("`crs_authid` must be a string when given")
    make_aoi = bool(payload.get("make_aoi", False))

    result = await _ingest_layer_fn()(
        case_id=case_id.strip(),
        name=name.strip() if isinstance(name, str) else "",
        kind=kind,
        s3_uri=s3_uri.strip(),
        crs_authid=crs_authid,
        make_aoi=make_aoi,
    )
    return json.dumps(result, separators=(",", ":")).encode("utf-8")


def _parse_ingest_layer_filename(query_string: str) -> str:
    """Extract + validate the ``filename`` query param for the upload route."""
    from urllib.parse import parse_qs

    params = parse_qs(query_string, keep_blank_values=False)
    filename = (params.get("filename") or [""])[0].strip()
    if not filename:
        raise _IngestLayerBadRequest("missing `filename` query param")
    return filename


# The deterministic map-click point probe: samples every raster layer, and any
# detected frame sequence, on the case at one point. Served whenever the agent
# runs the local single-user seam.


class _ProbePointBadRequest(Exception):
    """Malformed /api/probe-point request."""


def _probe_point_route_enabled() -> bool:
    """The route is served (mirrors ``_case_list_route_enabled``)."""
    return True


def _probe_point_fn():
    """Lazy-import seam for the probe core (heavy geo deps load on first
    call, not at listener start; monkeypatchable in tests)."""
    from trid3nt_server.cases.probe_point import probe_point_at

    return probe_point_at


async def _handle_probe_point_post(raw_body: bytes) -> bytes:
    """Resolve the JSON body for the probe-point route: malformed input raises a
    400, and the probe's own typed errors propagate for the dispatcher to map to
    honest 4xx bodies."""
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ProbePointBadRequest(f"body must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _ProbePointBadRequest(
            'body must be a JSON object like {"case_id": "...", "lon": -85.4, '
            '"lat": 30.1}'
        )
    case_id = payload.get("case_id")
    lon = payload.get("lon")
    lat = payload.get("lat")
    if not isinstance(case_id, str) or not case_id.strip():
        raise _ProbePointBadRequest("missing or empty `case_id`")
    if lon is None or lat is None:
        raise _ProbePointBadRequest("`lon` and `lat` are both required")

    result = await _probe_point_fn()(case_id=case_id.strip(), lon=lon, lat=lat)
    return json.dumps(result, separators=(",", ":")).encode("utf-8")





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
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }.get(status, "OK")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        # CORS -- see module docstring. POST is scoped to the ingest/probe routes.
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
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
    """Handle one HTTP request. The protocol implementation is deliberately
    minimal: an unknown path is 404, an unknown method 405, and a body is read
    to Content-Length or end-of-stream so a stray POST cannot hang."""
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

    # Drain headers; the ones we consume are Content-Length (so a POST body
    # can be read) and Host (so /plugin-repo/plugins.xml can build a download_url
    # that matches the host:port the client actually dialed -- e.g. a
    # tailnet client's daemon-host address, not a hardcoded 127.0.0.1). The
    # socket must be advanced past the rest before we close so the client
    # sees our response cleanly.
    content_length = 0
    host_header = ""
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            break
        if not line or line == b"\r\n" or line == b"\n":
            break
        name, _, value = line.decode("latin-1", "replace").partition(":")
        header_name = name.strip().lower()
        if header_name == "content-length":
            try:
                content_length = int(value.strip())
            except ValueError:
                content_length = 0
        elif header_name == "host":
            host_header = value.strip()

    if method == "OPTIONS":
        # CORS preflight.
        writer.write(_format_response(204, b""))
        await writer.drain()
        writer.close()
        return

    proxy_path, _, proxy_qs = path.partition("?")

    
    if method == "POST" and proxy_path == "/api/ingest-layer-file":
        # Bidirectional layer push, half 1: stage the client's raw upload bytes
        # to object storage.
        if not _ingest_layer_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        from trid3nt_server.cases.ingest_user_layer import MAX_INGEST_BYTES

        if content_length <= 0:
            writer.write(
                _format_response(400, b'{"error":"missing or empty request body"}')
            )
            await writer.drain()
            writer.close()
            return
        if content_length > MAX_INGEST_BYTES:
            # Reject BEFORE reading the oversized body into memory.
            writer.write(
                _format_response(
                    413,
                    json.dumps(
                        {
                            "error": f"upload is {content_length} bytes, exceeds "
                            f"the {MAX_INGEST_BYTES}-byte cap"
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            )
            await writer.drain()
            writer.close()
            return
        from trid3nt_server.cases.ingest_user_layer import ImportLayerError, ObjectTooLargeError

        try:
            filename = _parse_ingest_layer_filename(proxy_qs)
            raw_body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=120.0
            )
            s3_uri = await asyncio.to_thread(
                _upload_layer_file_fn(), filename, raw_body
            )
            writer.write(
                _format_response(
                    200,
                    json.dumps({"s3_uri": s3_uri}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except _IngestLayerBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            writer.write(
                _format_response(400, b'{"error":"upload body read failed"}')
            )
        except ObjectTooLargeError as exc:
            writer.write(
                _format_response(
                    413,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except ImportLayerError as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("ingest-layer-file upload failed")
            writer.write(_format_response(500, b'{"error":"layer upload failed"}'))
        await writer.drain()
        writer.close()
        return

    if method == "POST" and proxy_path == "/api/ingest-layer":
        # Bidirectional layer push, half 2: register an already-uploaded
        # object onto the case (see the module section above for the
        # request/response contract).
        if not _ingest_layer_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        raw_body = b""
        if content_length > 0:
            try:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                raw_body = b""
        from trid3nt_server.cases.ingest_user_layer import CaseNotFoundError, ImportLayerError, ObjectNotFoundError

        try:
            body = await _handle_ingest_layer_post(raw_body)
            writer.write(_format_response(200, body))
        except _IngestLayerBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except (CaseNotFoundError, ObjectNotFoundError) as exc:
            writer.write(
                _format_response(
                    404,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except ImportLayerError as exc:
            # INVALID_INPUT / OBJECT_TOO_LARGE / UNREADABLE_LAYER / other typed
            # core errors -- the request was well-formed HTTP but ingestion
            # cannot succeed.
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("ingest-layer run failed")
            writer.write(_format_response(500, b'{"error":"layer ingest failed"}'))
        await writer.drain()
        writer.close()
        return

    if method == "POST" and proxy_path == "/api/probe-point":
        # Deterministic map-click point probe.
        if not _probe_point_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        raw_body = b""
        if content_length > 0:
            try:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                raw_body = b""
        from trid3nt_server.cases.probe_point import ProbePointCaseNotFoundError, ProbePointInputError

        try:
            body = await _handle_probe_point_post(raw_body)
            writer.write(_format_response(200, body))
        except _ProbePointBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except ProbePointCaseNotFoundError as exc:
            writer.write(
                _format_response(
                    404,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except ProbePointInputError as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("probe-point run failed")
            writer.write(_format_response(500, b'{"error":"probe point failed"}'))
        await writer.drain()
        writer.close()
        return

    if method == "POST" and proxy_path == "/api/provider-config":
        # The live provider config: a provider, model or key switch takes effect
        # on the NEXT turn with no restart, because the adapter reads the env per
        # call. SECURITY: the api_key rides the body and is written to the env,
        # never logged or echoed - only the base URL host and effective model
        # return. Runs in a thread, because the coherence gate may make a short
        # blocking probe that must never sit on the event loop.
        if not model_discovery._local_models_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        raw_body = b""
        if content_length > 0:
            try:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                raw_body = b""
        try:
            body = await asyncio.to_thread(_apply_provider_config, raw_body)
            writer.write(_format_response(200, body))
        except _ProviderConfigBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001 -- NEVER surface the body/key in logs
            # A generic static message + no request context: the traceback
            # references the raw body variable by name only, never its value.
            logger.exception("provider-config update failed")
            writer.write(
                _format_response(500, b'{"error":"provider config update failed"}')
            )
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
    elif proxy_path == "/catalog":
        # Self-contained HTML catalog page (the agent's-eye view). Inline
        # CSS + JS + embedded data -- no external assets.
        try:
            body = render_catalog_page()
            writer.write(
                _format_response(
                    200, body, content_type="text/html; charset=utf-8"
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("catalog page render failed")
            writer.write(
                _format_response(
                    500,
                    b"<!doctype html><p>catalog render failed</p>",
                    content_type="text/html; charset=utf-8",
                )
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
    elif proxy_path == "/api/case-list":
        # The cold case list, for a client with no WebSocket session yet.
        if not _case_list_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
        else:
            try:
                payload = await build_case_list_payload()
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                writer.write(_format_response(200, body))
            except _CaseListPersistenceUnavailable as exc:
                writer.write(
                    _format_response(
                        503,
                        json.dumps(
                            {"error": str(exc)}, separators=(",", ":")
                        ).encode("utf-8"),
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("case-list build failed")
                writer.write(
                    _format_response(500, b'{"error":"case list failed"}')
                )
    elif proxy_path == "/api/local-models":
        # The installed local models, for a client's model picker. The route is
        # absent, like any unknown path, unless the local provider is active,
        # and the upstream fetch runs off the event loop.
        if not model_discovery._local_models_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
        else:
            try:
                body = await asyncio.to_thread(model_discovery._fetch_local_models)
                writer.write(_format_response(200, body))
            except model_discovery._LocalModelsUpstreamError as exc:
                writer.write(
                    _format_response(
                        502,
                        json.dumps(
                            {"error": str(exc)}, separators=(",", ":")
                        ).encode("utf-8"),
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("local-models listing failed")
                writer.write(
                    _format_response(500, b'{"error":"local models failed"}')
                )
    elif proxy_path == "/api/building-detail":
        # Click-to-enrich: the building footprint inline
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
    elif path == "/api/version":
        # Daemon git sha + active model provider -- the version indicator the
        # removed plugin-settings Update section wanted. Cheap: one
        # `git rev-parse` subprocess, off the
        # event loop.
        try:
            from trid3nt_server import plugin_repo

            payload = await asyncio.to_thread(plugin_repo.build_version_payload)
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            writer.write(_format_response(200, body))
        except Exception:  # noqa: BLE001
            logger.exception("version payload build failed")
            writer.write(_format_response(500, b'{"error":"version lookup failed"}'))
    elif proxy_path == "/plugin-repo/plugins.xml":
        # QGIS custom plugin repository index. The
        # packaged plugins.xml carries a HOST_SENTINEL; the download_url host
        # is filled from the REQUEST's own Host header so a tailnet client's
        # "Add repository" URL (http://<daemon-host>:8766/plugin-repo/plugins.xml)
        # round-trips to a reachable zip URL without a hardcoded host.
        from trid3nt_server import plugin_repo

        try:
            host = host_header or (
                f"127.0.0.1:{os.environ.get('TRID3NT_AGENT_HTTP_PORT', DEFAULT_HTTP_PORT)}"
            )
            body = await asyncio.to_thread(plugin_repo.render_plugins_xml, host)
            writer.write(
                _format_response(200, body, content_type="text/xml; charset=utf-8")
            )
        except plugin_repo.PluginRepoBuildError as exc:
            writer.write(
                _format_response(
                    503,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("plugins.xml serve failed")
            writer.write(
                _format_response(500, b'{"error":"plugin repo index failed"}')
            )
    elif proxy_path == "/plugin-repo/trid3nt.zip":
        # THE zip Plugin Manager / Install-from-ZIP downloads -- every
        # plugins.xml download_url now points here. Fixed name (must match
        # plugin_repo.FRESH_ZIP_URL_PATH), built on demand straight from
        # plugin/ and mtime-cached. No deploy-time
        # package_plugin_repo() step required. ?v=<version> (already
        # stripped into proxy_qs above) is a pure cache-busting hint.
        from trid3nt_server import plugin_repo

        try:
            data, _version, zip_filename = await asyncio.to_thread(
                plugin_repo.build_fresh_zip
            )
            writer.write(
                _format_response(
                    200,
                    data,
                    content_type="application/zip",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{zip_filename}"'
                    },
                )
            )
        except plugin_repo.PluginRepoBuildError as exc:
            writer.write(
                _format_response(
                    503,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("plugin zip (fresh) serve failed")
            writer.write(_format_response(500, b'{"error":"plugin zip failed"}'))
    elif proxy_path.startswith("/plugin-repo/") and proxy_path.endswith(".zip"):
        # The versioned zip built by package_plugin_repo() -- kept as a
        # manual-QA / fallback path; served straight from the packaged
        # directory (deploy-time artifact). Not what plugins.xml advertises
        # anymore (see the /plugin-repo/trid3nt.zip branch above).
        from trid3nt_server import plugin_repo

        filename = proxy_path[len("/plugin-repo/") :]
        try:
            zip_path = await asyncio.to_thread(plugin_repo.served_zip_path, filename)
            data = await asyncio.to_thread(zip_path.read_bytes)
            writer.write(
                _format_response(
                    200,
                    data,
                    content_type="application/zip",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{zip_path.name}"'
                    },
                )
            )
        except FileNotFoundError:
            writer.write(_format_response(404, b'{"error":"not found"}'))
        except Exception:  # noqa: BLE001
            logger.exception("plugin zip serve failed")
            writer.write(_format_response(500, b'{"error":"plugin zip failed"}'))
    else:
        writer.write(_format_response(404, b'{"error":"not found"}'))
    await writer.drain()
    writer.close()


async def serve_catalog_http(
    host: str = "127.0.0.1",
    port: int | None = None,
) -> asyncio.AbstractServer:
    """Start the catalog HTTP listener and return the server handle; the port
    comes from ``TRID3NT_AGENT_HTTP_PORT`` when not passed. It runs on the same
    loop and process as the WebSocket server."""
    if port is None:
        try:
            port = int(os.environ.get("TRID3NT_AGENT_HTTP_PORT", DEFAULT_HTTP_PORT))
        except ValueError:
            port = DEFAULT_HTTP_PORT
    server = await asyncio.start_server(_handle_http, host, port)
    logger.info(
        "tool-catalog HTTP server listening host=%s port=%d", host, port
    )
    return server
