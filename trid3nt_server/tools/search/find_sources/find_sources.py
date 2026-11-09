"""``find_sources`` - the one door to every data source: a class of thing at a place, ranked, or a free-text query.

The model's face on THE MATCH: the same filter, sort and filler a template's need row resolves through, so a free run and
a template run never disagree about the world. Each row comes back FILLED - the request held server-side under a handle
``S<n>`` the case never reuses - and is shown in our words: what it is, READY or what it needs, the box and the window,
and what else it accepts. A row that does not reach is one line with the reason and the edit that would make it reach.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from trid3nt_contracts.coverage import (
    DATA_CLASSES, DataClass, SourceChoice, SourceOption)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.extent import extent as ingest_extent
from trid3nt_server.inputs.instant import instant as ingest_instant
from trid3nt_server.inputs.point import point as ingest_point
from trid3nt_server.render.uri_registry import mint_source_handle
from trid3nt_server.settings import setting
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.search.match import (
    AROUND_PLACE_DEG, RANKED_ROWS, Need, excluded, filled, match, needed, picked_row,
    remedy, shown_params, sources_with_coverage)

__all__ = ["FIND_SOURCES", "find_sources", "FindSourcesError", "params_line", "window_of"]

logger = logging.getLogger("trid3nt_server.tools.search.find_sources")


class FindSourcesError(RuntimeError):
    """The class asked about is not one a source can be filtered on."""

    error_code = "FIND_SOURCES_NEED_UNKNOWN"
    retryable = False


class FindSourcesAskError(FindSourcesError):
    """Neither or both of ``need`` and ``query`` came."""

    error_code = "FIND_SOURCES_ASK_UNCLEAR"



#: The name the registry and the dispatch gate know this tool by. The gate
#: EXPANDS on it: a fetcher with a row is not described to the model,
#: so the sources this names are how its schema reaches the turn.
FIND_SOURCES = "find_sources"


_METADATA = AtomicToolMetadata(
    name=FIND_SOURCES,
    ttl_class="live-no-cache",
    source_class=None,
)


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def find_sources(
    need: DataClass | None = None,
    place: list[float] | None = None,
    window: list[str] | None = None,
    frame: str = "",
    query: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """WHICH source holds the data, each row a request already filled - the one door to every data source.

    ROUTING: any question that has to get real numbers or a map layer in - "how deep is
    it here", "what is the river doing", "flood zones", "satellite imagery". Call this
    FIRST, then `fetch("S1")` with a row's handle; change a row with
    `fetch("S1", edits={"box": [...], "window": [...]})`. Never copy a row into args.

    Pass `need` ONE class of the coverage vocabulary for a ranked list at a place, or
    `query` free text for the sources whose words match best, unranked. `place` is a
    point, a [west, south, east, north] box or a drawn area. `window` is one moment or
    [opens, closes]; unstated, it is the moment now. `frame` is the run's datum.

    Returns {"results": [...]}: each row has `handle`, `source`, `what`, `status` (READY,
    or "needs: X" - pass X in edits), `box`, `window`, `also accepts`. `not reaching`
    lists rows that do not reach, each with the edit that would.
    """
    if bool(need) == bool(query and str(query).strip()):
        raise FindSourcesAskError(
            "pass exactly one of need (a data class: " + ", ".join(DATA_CLASSES)
            + ") or query (free text naming what you want).")
    bbox, lon, lat = await _place(place)
    opens, until = window_of(window)
    if not need:
        return {"results": await _query(str(query).strip(), bbox, lon, lat, opens, until)}
    data_class = str(need or "").strip()
    if data_class not in DATA_CLASSES:
        raise FindSourcesError(
            f"{data_class!r} is not a class a source states coverage in. The "
            "vocabulary is: " + ", ".join(DATA_CLASSES))
    candidates = [(name, row) for name, row in sources_with_coverage()
                  if row.data_class == data_class]
    if not candidates:
        raise FindSourcesError(
            f"no registered source states coverage of {data_class!r}: nothing "
            "here can say where it is measured. State the value on the call.")
    asked = Need(slot=data_class, data_class=data_class, lon=lon, lat=lat,
                 opens=opens, until=until, frame=str(frame or ""))
    choice = match(asked, candidates)
    rows = {(name, row.kind): row for name, row in candidates}
    # A row that reaches but asks for a param nothing here fills is SHOWN, saying what it needs; the match
    # drops it because a template's probe cannot call it, while the model can name the value in edits.
    shown = [option for option in choice.rows if not option.excluded or (
        needed(option.fetcher, rows[(option.fetcher, option.kind)], asked)
        and not excluded(asked, rows[(option.fetcher, option.kind)], option.fetcher))]
    found: dict[str, Any] = {
        "results": [_row(choice, option, bbox, asked) for option in shown],
        "sentence": choice.sentence,
        "tie": choice.tie,
    }
    away = [f"{option.fetcher}: {option.excluded}; edit: "
            + remedy(rows[(option.fetcher, option.kind)], asked)
            for option in choice.rows if option not in shown]
    if away:
        found["not reaching"] = away
    return found


async def _query(text: str, bbox: Sequence[float] | None, lon: float | None,
                 lat: float | None, opens: str, until: str | None) -> list[dict[str, Any]]:
    """The sources whose words match ``text``, each filled for this place and window; none is ranked on the match's facts."""
    from trid3nt_server.tools.search.search_tools import search_tools as st
    from trid3nt_server.tools.search.tool_retrieval import doc_similarities, ranked_docs

    index = st._get_source_index()
    sims = doc_similarities(text, index)
    # Rows are cut on embedding similarity, not the fused rank score: the sources within SOURCE_MARGIN of the top row, in rank order.
    floor = max(sims) * (1.0 - float(setting("SOURCE_MARGIN"))) if sims else None
    rows: list[dict[str, Any]] = []
    for at, _score in ranked_docs(text, index):
        if len(rows) >= RANKED_ROWS:
            break
        if floor is not None and sims[at] < floor:
            continue
        rows.append(_source_row(index.tool_names[at], index.descriptions[at],
                                bbox, lon, lat, opens, until))
    return rows


def _source_row(name: str, about: str, bbox: Sequence[float] | None,
                lon: float | None, lat: float | None, opens: str,
                until: str | None) -> dict[str, Any]:
    """One source a query reached: what it is, and the request it is filled with."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    classes = sorted({row.data_class for row in getattr(get_spec(name), "coverage", ())})
    picked = SourceChoice(slot="query", need=classes[0] if classes else "query",
                          picked=name)
    need = Need(slot="query", data_class=picked.need, lon=lon, lat=lat,
                opens=opens, until=until)
    return _shown(name, about.split("\n", 1)[0].strip(), picked, bbox, need, purpose="")


def _row(choice: SourceChoice, option: SourceOption, bbox: Sequence[float] | None,
         need: Need) -> dict[str, Any]:
    """One survivor, filled for THIS row - a source serving a measured and a predicted series of one class is on the
    list twice, and each row answers with its own."""
    picked = choice.model_copy(update={"picked": option.fetcher, "rows": [option]})
    row = picked_row(picked)
    facts = [f"{option.kind} {need.data_class}",
             row.window.cadence if row is not None and row.window.series else option.resolution,
             option.distance]
    return _shown(option.fetcher, ", ".join(fact for fact in facts if fact), picked,
                  bbox, need, purpose=need.data_class)


def _shown(name: str, what: str, picked: SourceChoice, bbox: Sequence[float] | None,
           need: Need, *, purpose: str) -> dict[str, Any]:
    """A filled row as the model reads it, its request held under a fresh handle."""
    request = filled(picked, purpose, bbox, need)
    status, box, accepts = shown_params(name, picked, request)
    held = {"source": name, "args": request, "class": picked.need,
            "kind": getattr(picked_row(picked), "kind", ""),
            "box": list(bbox) if bbox is not None else None,
            "opens": need.opens, "until": need.until, "frame": need.frame}
    shown: dict[str, Any] = {"handle": mint_source_handle(held), "source": name,
                             "what": what, "status": status}
    if box is not None:
        shown["box"] = box
    shown["window"] = (need.opens if not need.until or need.until == need.opens
                       else [need.opens, need.until])
    if accepts:
        shown["also accepts"] = accepts
    return shown


def params_line(spec: Any) -> str:
    """A source's parameters on one line: ``name`` then ``*`` where required, ``{a|b}`` for an enum, ``:type`` otherwise."""
    parts = []
    for name, decl in (getattr(spec, "params", None) or {}).items():
        required = bool(decl.required and decl.default is None)
        if decl.type == "enum" and decl.values:
            shown = [str(v) for v in decl.values[:_ENUM_SHOWN]]
            kind = "{" + "|".join(shown + (["..."] if len(decl.values) > _ENUM_SHOWN else [])) + "}"
        else:
            kind = _TYPE_WORD.get(decl.type, "")
        parts.append(f"{name}{'*' if required else ''}{kind}")
    return ", ".join(parts)


#: How many values of an enum a params line spells before it says there are more.
_ENUM_SHOWN = 6

#: The word a params line gives a non-enum parameter's type, where the name does not already say it.
_TYPE_WORD = {"iso_date": ":date", "date_compact": ":date", "int": ":int", "float": ":num",
              "bool": ":bool", "point": ":[lon,lat]", "int_range": ":[a,b]",
              "datetime_range": ":[t0,t1]", "float_list": ":[nums]", "str_list": ":[text]"}


async def _place(value: Any) -> tuple[list[float] | None, float | None,
                                      float | None]:
    """The place as the box a source is called with and the point it is ranked against.

    Four numbers, a drawn area or GeoJSON is a box; a point is the ground it stands on, a box a source called by one can answer.
    Nothing given is no place, and the match ranks on class alone."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return (None, None, None)
    if _is_pair(value):
        pt = await ingest_point(value, label="place")
        if pt is None:
            return (None, None, None)
        return ([pt.lon - AROUND_PLACE_DEG, pt.lat - AROUND_PLACE_DEG,
                 pt.lon + AROUND_PLACE_DEG, pt.lat + AROUND_PLACE_DEG],
                pt.lon, pt.lat)
    area = await ingest_extent(value, label="place")
    if area is None:
        return (None, None, None)
    west, south, east, north = (float(v) for v in area.bbox)
    return ([west, south, east, north], (west + east) / 2.0,
            (south + north) / 2.0)


def _is_pair(value: Any) -> bool:
    """Whether the place arrived as a lon/lat pair rather than as an area."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    return len(value) == 2 and all(isinstance(v, (int, float)) for v in value)


def window_of(value: Any) -> tuple[str, str | None]:
    """The moment a series has to cover, as the instant it opens and closes at.

    One value is a moment; two are a span. Nothing given is the moment now, to the minute."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return (_read("now") or "", None)
    if isinstance(value, Mapping):
        opens = _read(value.get("opens") or value.get("start"))
        return (opens or _read("now") or "",
                _read(value.get("until") or value.get("end")))
    if isinstance(value, (str, bytes)):
        return (_read(value) or "", None)
    if isinstance(value, Sequence):
        stamps = [_read(v) for v in value]
        return (stamps[0] if stamps and stamps[0] else _read("now") or "",
                stamps[1] if len(stamps) > 1 else None)
    return (_read(value) or "", None)


def _read(value: Any) -> str | None:
    """One wire stamp as a UTC instant, or ``None`` where nothing came."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return ingest_instant(value, label="window")
