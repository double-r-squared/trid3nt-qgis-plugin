"""Stratified data pool -- Design 3 (docs/specs/stratified-pools.md).

The DATA-source lane of the stratified-retrieval architecture. Under
``TRID3NT_CATALOG_ARM=3`` the 14 spec-served sources leave the ambient declarable
pool (tier="catalog", set in registration.register_spec) and are surfaced instead
by ONE composed generic fetcher whose ``source`` enum is the harness-narrowed
top-k candidates -- the model NEVER initiates discovery.

Mechanisms (all harness-side, zero model-facing search tools):
  1. STRATUM SPLIT: a SEPARATE retrieval index scoped to ONLY the 14 spec-served
     sources, ranked with the SAME BM25 + dense + name/RRF machinery the core
     pass uses. The per-pool BM25 IDF sharpening (14 docs vs the full registry)
     is EXPECTED -- a smaller pool is a sharper index, not a bug.
  2. QUOTA MERGE: the source stratum contributes its own composed-fetcher
     declaration when ACTIVATED (top normalized score >= ACTIVATION_THRESHOLD);
     core tools fill their share independently. An unbounded pool never crowds
     the core surface.
  3. TRIGGER + ESCALATION: threshold, not an intent classifier. When the top
     stratum score is data-ish (>= DATAISH_FLOOR) but under the activation bar,
     a dense-heavy escalation pass re-ranks; it activates on the (lower)
     ESCALATION_THRESHOLD. Below the floor -> not data-ish -> no declaration.
  4. DISPATCH: the composed fetch_from_catalog(source, params) resolves the spec
     and runs router.route (pydantic validate_params -> typed error -> retry),
     exactly the arms 0-2 contract.

Activation gate: RRF rank scores saturate in a tiny pool (something is always
rank-1 among 14 docs), so they cannot say whether the pool is ACTUALLY relevant.
The gate is instead the top source's ABSOLUTE dense cosine to the ask -- a real
semantic-relevance signal in [0, 1]. It is deliberately RECALL-biased (a low
floor): missing activation on a real data ask is a guaranteed selection miss,
whereas over-activating on a data-adjacent ask only costs if the model then picks
a pool source over the correct core tool -- which the controls leakage gate
measures head-on. This mirrors the retrieve_visible_tools recall-over-precision
doctrine (over-inclusion is cheap; a dropped needed tool is a silent break).

ASCII only.
"""

from __future__ import annotations

import logging
from typing import Any

from . import registration as _reg

logger = logging.getLogger("trid3nt_server.agent.tools.fetchers._router.stratified")

__all__ = [
    "SOURCE_ENUM_K",
    "ACTIVATION_THRESHOLD",
    "DATAISH_FLOOR",
    "ESCALATION_THRESHOLD",
    "source_stratum_index",
    "reset_source_stratum_index_for_tests",
    "rank_source_stratum",
    "stratum_declaration_plan",
    "render_cards_context",
    "compose_fetcher_declaration",
    "COMPOSED_FETCHER_NAME",
]

#: The composed generic fetcher the stratum declares (reuses the real
#: fetch_from_catalog source-passthrough dispatch, arm-gated in that module).
COMPOSED_FETCHER_NAME = "fetch_from_catalog"

#: k for the source enum -- small, rank-ordered (spec: 3-5). The harness narrows
#: the model's data choice to this many top candidates.
SOURCE_ENUM_K = 5

#: RRF constant, mirrors search_tools/_reciprocal_rank_fusion default.
_RRF_K = 60

#: Activation gate on the top-ranked source's ABSOLUTE dense cosine (recall-biased,
#: see module docstring). >= ACTIVATION_THRESHOLD: declare the composed fetcher
#: directly. [DATAISH_FLOOR, ACTIVATION_THRESHOLD): data-ish but weak -> dense-heavy
#: escalation re-rank, activate if the escalated top clears ESCALATION_THRESHOLD.
#: < DATAISH_FLOOR: not data-ish -> no declaration (core surface only).
ACTIVATION_THRESHOLD = 0.30
DATAISH_FLOOR = 0.18
ESCALATION_THRESHOLD = 0.24

#: Lean routing docstring for the composed declaration (front-loaded; the source
#: cards carry the per-source detail as context, not this description).
_COMPOSED_DOC = (
    "Fetch one vetted U.S. data source into a map layer. Use this WHENEVER the "
    "ask needs real data from one of the sources listed in `source` (weather, "
    "census/demographics, land cover, tides/currents, water quality, fire "
    "perimeters/burn severity, drought, infrastructure, rivers/waterbodies). "
    "Pick the `source` whose card (in context) matches the ask, then form "
    "`params` from that card's typed param schema. The router validates params "
    "and dispatches; a typed error is returned for you to correct and retry."
)

#: Cached source-stratum index (built lazily over the 14 spec-served sources).
_STRATUM_INDEX: Any = None


def reset_source_stratum_index_for_tests() -> None:
    """Drop the cached stratum index (tests / per-process warm)."""
    global _STRATUM_INDEX
    _STRATUM_INDEX = None


def _source_snapshot() -> dict[str, Any]:
    """The registry snapshot scoped to ONLY the spec-served sources (the stratum)."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    names = _reg.registered_spec_names()
    return {n: TOOL_REGISTRY[n] for n in names if n in TOOL_REGISTRY}


def source_stratum_index() -> Any:
    """Lazily build + cache the source-only retrieval index (the stratum split).

    Reuses search_tools._build_index over a snapshot of ONLY the 14 spec-served
    sources, so BM25 + dense + name channels rank within the pool (sharpened IDF).
    """
    global _STRATUM_INDEX
    if _STRATUM_INDEX is None:
        from trid3nt_server.agent.tools.search.search_tools.search_tools import (
            _build_index,
        )

        _STRATUM_INDEX = _build_index(registry_snapshot=_source_snapshot())
    return _STRATUM_INDEX


def _dense_sims(index: Any, query_clean: str) -> Any:
    """Query-to-source dense cosine vector over ``index``, or None when dense is
    unavailable. Sources are L2-normalized at index build; the query is normalized
    here, so the dot product is cosine in [-1, 1]."""
    if (
        index.dense_matrix is None
        or index.dense_encode_fn is None
        or getattr(index, "backend_name", None)
        not in ("sentence_transformers", "hashed", None)
    ):
        return None
    try:
        import numpy as _np

        q_vec = index.dense_encode_fn([query_clean])
        qn = _np.linalg.norm(q_vec, axis=1, keepdims=True)
        qn[qn == 0.0] = 1.0
        q_vec = q_vec / qn
        return (index.dense_matrix @ q_vec[0]).astype("float32")
    except Exception:  # noqa: BLE001 -- drop the channel
        logger.warning("stratified: dense channel failed", exc_info=True)
        return None


def rank_source_stratum(
    query: str, k: int = SOURCE_ENUM_K, *, dense_heavy: bool = False
) -> tuple[list[tuple[str, float]], float]:
    """Rank the source stratum for ``query``; return (ranked[(name, score)], top_cos).

    ``ranked`` is the BM25 + dense + name/RRF fusion (the same machinery the core
    pass uses), rank-ordered, capped to ``k``. ``dense_heavy`` (the escalation
    pass) duplicates the dense channel in the RRF input so semantic match
    dominates. ``top_cos`` is the ABSOLUTE dense cosine of the top-RANKED source
    to the ask -- the activation gate signal (0.0 when nothing matched / dense is
    unavailable).
    """
    from trid3nt_server.agent.tools.search.search_tools.search_tools import (
        _lexical_reinforcement,
        _reciprocal_rank_fusion,
    )
    from trid3nt_server.agent.tools.search.tool_retrieval import (
        _build_channel_rankings,
    )

    if not isinstance(query, str) or not query.strip():
        return [], 0.0
    index = source_stratum_index()
    if not getattr(index, "tool_names", None):
        return [], 0.0
    query_clean = query.strip()

    rankings, bm25_ranking = _build_channel_rankings(query_clean, index)
    if not rankings:
        return [], 0.0
    sims = _dense_sims(index, query_clean)
    fuse_input = list(rankings)
    if dense_heavy and sims is not None:
        dense = sorted(range(len(sims)), key=lambda i: float(sims[i]), reverse=True)
        fuse_input.append(dense)  # double the dense channel's RRF weight

    fused = _reciprocal_rank_fusion(fuse_input, k=_RRF_K)
    fused = _lexical_reinforcement(
        fused, bm25_ranking, getattr(index, "tiers", None), k=_RRF_K
    )
    ranked = [(index.tool_names[i], float(s)) for i, s in fused]
    if not ranked:
        return [], 0.0

    top_idx = index.tool_names.index(ranked[0][0])
    top_cos = round(float(sims[top_idx]), 4) if sims is not None else 0.0
    return ranked[:k], top_cos


def stratum_declaration_plan(query: str, k: int = SOURCE_ENUM_K) -> dict[str, Any]:
    """Decide whether the source stratum activates for ``query`` + what it declares.

    Returns a plan dict:
      - ``activated``: whether the composed fetcher should be declared this turn.
      - ``escalated``: whether the escalation (dense-heavy) pass was used.
      - ``sources``: the enum source names IN RANK ORDER (top-k), [] when inactive.
      - ``cards``: full source CARDS (registration.spec_card) for the enum sources.
      - ``top_cos`` / ``top_cos_escalated``: the gate cosines (diagnostics).
    """
    ranked, top_cos = rank_source_stratum(query, k=k)
    plan: dict[str, Any] = {
        "activated": False,
        "escalated": False,
        "sources": [],
        "cards": [],
        "top_cos": top_cos,
        "top_cos_escalated": None,
    }

    chosen: list[tuple[str, float]] = []
    if top_cos >= ACTIVATION_THRESHOLD:
        chosen = ranked
    elif top_cos >= DATAISH_FLOOR:
        # Data-ish but weak -> escalation pass (dense-heavy re-rank).
        ranked2, cos2 = rank_source_stratum(query, k=k, dense_heavy=True)
        plan["escalated"] = True
        plan["top_cos_escalated"] = cos2
        if cos2 >= ESCALATION_THRESHOLD:
            chosen = ranked2
    if not chosen:
        return plan

    plan["activated"] = True
    plan["sources"] = [name for name, _ in chosen]
    cards: list[dict[str, Any]] = []
    for name, score in chosen:
        spec = _reg._SPEC_REGISTRY.get(name)
        if spec is not None:
            cards.append(_reg.spec_card(spec, score))
    plan["cards"] = cards
    return plan


def render_cards_context(plan: dict[str, Any]) -> str:
    """Render the enum sources' full cards as a plain-text context block.

    The card (full untruncated docstring + typed param schema + gates/caveats/
    fallback) is the model's per-source detail view -- it escapes the provider
    ~1000-char tool-description limit by riding in context, not the declaration.
    """
    cards = plan.get("cards") or []
    if not cards:
        return ""
    lines = [
        "DATA SOURCES available via fetch_from_catalog(source=..., params=...). "
        "Choose the `source` matching the ask and form `params` from its schema:",
    ]
    for c in cards:
        lines.append("")
        lines.append(f"source: {c['name']}  (class={c.get('source_class')})")
        doc = (c.get("docstring") or "").strip()
        if doc:
            lines.append(doc)
        params = c.get("params") or {}
        if params:
            lines.append("params:")
            for pn, pv in params.items():
                bits = [f"type={pv.get('type')}", f"required={pv.get('required')}"]
                if pv.get("default") is not None:
                    bits.append(f"default={pv.get('default')!r}")
                if pv.get("values"):
                    bits.append(f"values={pv.get('values')}")
                if pv.get("min") is not None:
                    bits.append(f"min={pv.get('min')}")
                if pv.get("max") is not None:
                    bits.append(f"max={pv.get('max')}")
                lines.append(f"  - {pn}: " + ", ".join(bits))
        if c.get("gates"):
            lines.append(f"gates: {c['gates']}")
        if c.get("caveats"):
            lines.append("caveats: " + " ".join(c["caveats"]))
        if c.get("fallback"):
            lines.append("fallback: " + " ".join(c["fallback"]))
    return "\n".join(lines)


def compose_fetcher_declaration(plan: dict[str, Any]) -> Any:
    """Build the composed generic-fetcher FunctionDeclaration for an active plan.

    ``source`` is a STRING enum = the matched candidates IN RANK ORDER (the
    harness-narrowed choice); ``params`` is a free-form object validated router-
    side. Returns a genai FunctionDeclaration, or None when the plan is inactive.
    """
    if not plan.get("activated") or not plan.get("sources"):
        return None
    from google.genai import types as genai_types

    sources = list(plan["sources"])
    params_schema = genai_types.Schema(
        type=genai_types.Type.OBJECT,
        properties={
            "source": genai_types.Schema(
                type=genai_types.Type.STRING,
                enum=sources,
                description=(
                    "The data source to fetch, chosen from this ask's candidates "
                    "(listed best-first). Match the source card in context."
                ),
            ),
            "params": genai_types.Schema(
                type=genai_types.Type.OBJECT,
                description=(
                    "Request params per the chosen source's typed schema (see its "
                    "card in context): e.g. bbox, variable, start_date, end_date."
                ),
            ),
        },
        required=["source", "params"],
        property_ordering=["source", "params"],
    )
    return genai_types.FunctionDeclaration(
        name=COMPOSED_FETCHER_NAME,
        description=_COMPOSED_DOC,
        parameters=params_schema,
    )
