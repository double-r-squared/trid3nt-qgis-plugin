"""THE LIBRARY: what is here to run, and what data there is to run it on.

Two facets over two registries nobody else joins: every registered tool under
the subsystem it belongs to, and every source row under the class and kind
the match sorts on. Both halves are sorted here, because the panel that reads
them never re-sorts, and every fact is metadata the tool or the row already
carries - the listing computes none of its own."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from trid3nt_server.server.protocol.doors.transport import failure, json_reply

#: module prefix -> the subsystem a tool listed under it belongs to. First match
#: wins, so the order is the specificity order; ``tier="template"`` is read off
#: the metadata before this table, because a template is a subsystem by what it
#: is rather than by where it sits.
_SUBSYSTEM_BY_MODULE: tuple[tuple[str, str], ...] = (
    ("trid3nt_server.tools.fetchers", "fetchers"),
    ("trid3nt_server.tools.derive", "derives"),
    ("trid3nt_server.tools.search", "search"),
    ("trid3nt_server.render", "render"),
    ("trid3nt_server.mesh", "mesh"),
    ("trid3nt_server.workflows.solver", "solver"),
    ("trid3nt_server.workflows.runtime", "runtime"),
    ("trid3nt_server.workflows.telemac", "engine"),
    ("trid3nt_server.gates", "gates"),
)

#: The order the subsystems are listed in: what a run is made of, then what it
#: is made from, then the operations beside them. A subsystem this tuple does
#: not name is listed after these, alphabetically.
_SUBSYSTEM_ORDER: tuple[str, ...] = (
    "templates", "fetchers", "mesh", "derives", "render", "solver", "runtime",
    "engine", "search", "gates",
)

#: How many tools one library search ranks, the ceiling the search tool clamps
#: to, so the person and the model are answered off one shortlist.
_SEARCH_TOP_K = 25

_LIBRARY_CACHE: dict[str, Any] | None = None


def _facet_str(value: Any) -> str | None:
    """Coerce a metadata facet (enum / str / None) to a plain string or None."""
    if value is None:
        return None
    s = str(value)
    return s or None


def _subsystem_of(entry: Any) -> str:
    """Which subsystem one registered tool belongs to; a module under no known
    prefix is listed as ``other`` rather than dropped, so the listing can never
    be quietly shorter than the registry."""
    if _facet_str(getattr(entry.metadata, "tier", None)) == "template":
        return "templates"
    module = str(entry.module)
    for prefix, subsystem in _SUBSYSTEM_BY_MODULE:
        if module.startswith(prefix):
            return subsystem
    return "other"


def _tool_entry(name: str, entry: Any) -> dict[str, Any]:
    """One tool as the library states it: the name a direct run takes, the
    docstring in its routing spelling where the tool declares one, and the
    metadata facets the tool already carries."""
    meta = entry.metadata
    doc = (getattr(entry.fn, "routing_doc", None) or entry.fn.__doc__ or "").strip()
    facts: dict[str, Any] = {
        "engine": _facet_str(getattr(meta, "engine", None)),
        "tier": _facet_str(getattr(meta, "tier", None)),
        "source_class": _facet_str(meta.source_class),
        "cacheable": bool(meta.cacheable) or None,
    }
    return {
        "name": name,
        "description": doc,
        "facts": {k: v for k, v in facts.items() if v is not None},
    }


def _row_name(fetcher: str, row: Any) -> str:
    """What names one source row under its class and kind: the request values
    it is fetched under, which are the only thing separating two rows of one
    fetcher there. A row asked under nothing is its fetcher's only row there and
    takes the fetcher's name."""
    asked = " ".join(f"{k}={row.ask[k]}" for k in sorted(row.ask))
    return asked or fetcher


def _data_entry(fetcher: str, row: Any) -> dict[str, Any]:
    """One source row as the library states it: the fetcher that answers it,
    the row's own words for what it covers, and its cell, window and datum."""
    window = row.window
    facts: dict[str, Any] = {
        "cell_m": row.resolution_m,
        "records": "per instant" if window.series else "measured once",
        "earliest": window.earliest,
        "latest": window.latest,
        "cadence": window.cadence,
        "datum": row.datum,
        "extent": row.extent.kind,
        "reach_km": row.reach_km,
    }
    return {
        "name": _row_name(fetcher, row),
        "fetcher": fetcher,
        "description": row.extent.note,
        "facts": {k: v for k, v in facts.items() if v is not None and v != ""},
    }


def _classes(fetchers: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """Every source row under its class and then its kind, narrowed to the
    named fetchers when one is given. The class is the row's own declaration and
    never inferred, so no row is listed without one."""
    from trid3nt_server.tools.search.match import sources_with_coverage

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name, row in sources_with_coverage():
        if fetchers is not None and name not in fetchers:
            continue
        by_kind = grouped.setdefault(str(row.data_class), {})
        by_kind.setdefault(str(row.kind), []).append(_data_entry(name, row))
    return [
        {
            "name": data_class,
            "kinds": [{"name": kind, "rows": by_kind[kind]}
                      for kind in sorted(by_kind)],
        }
        for data_class, by_kind in sorted(grouped.items())
    ]


def build_library_payload(*, use_cache: bool = True) -> dict[str, Any]:
    """The ``/api/library`` listing: every registered tool under its subsystem,
    every source row under its class and kind. Read-only, and cached for the
    life of the process because both registries are filled at import."""
    from trid3nt_server.tools import TOOL_REGISTRY

    global _LIBRARY_CACHE
    if use_cache and _LIBRARY_CACHE is not None:
        return _LIBRARY_CACHE

    by_subsystem: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(TOOL_REGISTRY.keys()):
        entry = TOOL_REGISTRY[name]
        by_subsystem.setdefault(_subsystem_of(entry), []).append(
            _tool_entry(name, entry))
    ordered = [s for s in _SUBSYSTEM_ORDER if s in by_subsystem]
    ordered += sorted(s for s in by_subsystem if s not in _SUBSYSTEM_ORDER)

    payload = {
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "tool_count": len(TOOL_REGISTRY),
        "subsystems": [{"name": s, "tools": by_subsystem[s]} for s in ordered],
        "classes": _classes(),
    }
    if use_cache:
        _LIBRARY_CACHE = payload
    return payload


def _classes_named_by(query: str) -> list[str]:
    """The data classes an ask names, best overlap first.

    A covered source is reached by asking the world for a CLASS, so it carries
    no corpus phrasing and the tool index cannot rank it. The ask is read
    against the class vocabulary itself, through the index's own tokenizer, so
    both halves of a search are split on the same words."""
    from trid3nt_server.tools.search.search_tools.search_tools import (
        _STOPWORDS, _tokenize,
    )
    from trid3nt_contracts.coverage import DATA_CLASSES

    asked = {t for t in _tokenize(query) if t not in _STOPWORDS}
    if not asked:
        return []
    scored = [(len(asked & set(_tokenize(name))), name) for name in DATA_CLASSES]
    return [name for overlap, name in
            sorted((s for s in scored if s[0]), key=lambda s: (-s[0], s[1]))]


async def build_library_search(query: str, top_k: int = 10) -> dict[str, Any]:
    """The ``/api/library/search`` hits: one ask over both facets. The tools are
    ranked by the BM25 corpus the model routes on; the rows are those of the
    classes the ask names, which is how a covered source is reached at all."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.search.search_tools.search_tools import search_tools

    try:
        k = max(1, min(_SEARCH_TOP_K, int(top_k)))
    except (TypeError, ValueError):
        k = 10
    found = await search_tools(query=query, top_k=k)

    hits: list[dict[str, Any]] = []
    for ranked in found.get("results", []):
        name = str(ranked.get("tool_name") or "")
        entry = TOOL_REGISTRY.get(name)
        if entry is None:
            # The index was built when a tool was registered that no longer is.
            continue
        hit = _tool_entry(name, entry)
        hit["kind"] = "tool"
        hit["group"] = _subsystem_of(entry)
        score = ranked.get("score")
        if isinstance(score, (int, float)):
            hit["score"] = score
        hits.append(hit)

    # The rows carry no score: rank here is the class overlap, and a number
    # beside a row would read as a measurement of the row.
    named = _classes_named_by(query)
    by_class = {cls["name"]: cls for cls in _classes()}
    for class_name in named:
        listed = by_class.get(class_name)
        if listed is None:
            continue
        for kind in listed["kinds"]:
            for row in kind["rows"]:
                hits.append(dict(row, kind="row",
                                 group=f"{class_name} / {kind['name']}"))

    return {"query": query, "hits": hits}


@failure("library listing failed")
async def _listing(_request: web.Request) -> web.Response:
    """``GET /api/library``: every registered tool under its subsystem and every
    source row under its class and kind."""
    return json_reply(build_library_payload())


@failure("library search failed")
async def _search(request: web.Request) -> web.Response:
    """``GET /api/library/search?q=``: the ranked hits over the BM25 corpus the
    model routes on. An empty query answers with no hits rather than the whole
    library, which the listing route already serves."""
    return json_reply(
        await build_library_search(request.query.get("q", "").strip()))


def add_routes(app: web.Application) -> None:
    """Register the library's two routes on the door's one app."""
    app.router.add_get("/api/library", _listing, allow_head=False)
    app.router.add_get("/api/library/search", _search, allow_head=False)
