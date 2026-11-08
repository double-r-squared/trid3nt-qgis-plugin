"""``find_sources`` - the one door to every data source: a class of thing at a place, ranked, or a free-text query.

The model's face on THE MATCH: the same filter, sort and ask a template's need row resolves through, so a free run and a
template run never disagree about the world. What comes back is the ranked list the card renders plus, per survivor, the
arguments that source is ready to be called with through ``fetch``; a query ranks the sources by name and words and returns
them unranked on the facts the match weighs, since a source with no coverage row has none to weigh; only the sources
within SOURCE_MARGIN of the best match are returned.
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
from trid3nt_server.settings import setting
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.search.match import (
    AROUND_PLACE_DEG, RANKED_ROWS, Need, ask_for, base_ask, match,
    sources_with_coverage)

__all__ = ["FIND_SOURCES", "find_sources", "FindSourcesError"]

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
    """WHICH source holds the data, with the call ready to make - the one door to every data source.

    ROUTING: any question that has to get real numbers or a map layer in - "how deep is
    it here", "what is the river doing", "flood zones", "fire perimeters", "satellite
    imagery" - before any fetch. Call this FIRST, then `fetch(source, args)` with a row's
    `source` and its `ask`.

    Pass `need` ONE class of the coverage vocabulary (the kind of thing measured) for
    a ranked list at a place, or `query` free text (what you want, in your own words)
    for the sources whose names and descriptions match best, unranked. `place` is a point, a
    [west, south, east, north] box or a drawn area - a point is read as the ground it
    stands on, so pass a box to ask over an area. `window` is the moment or the
    [opens, closes] a series has to cover; `frame` is the vertical datum of the run.

    Returns {"results": [{source, ask, ...}]}: each source with the arguments it is
    ready to be called with; a ranked list adds "sentence", "picked", "tie", "choice"
    and what a filter dropped, a query row adds `about` and `params` (* is required).
    Nothing measuring it here is an answer: state the value, or ask about a place a
    source reaches.
    """
    if bool(need) == bool(query and str(query).strip()):
        raise FindSourcesAskError(
            "pass exactly one of need (a data class: " + ", ".join(DATA_CLASSES)
            + ") or query (free text naming what you want).")
    if not need:
        return await _query(str(query).strip(), place, window)
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
    bbox, lon, lat = await _place(place)
    opens, until = _window(window)
    choice = match(
        Need(slot=data_class, data_class=data_class, lon=lon, lat=lat,
             opens=opens, until=until, frame=str(frame or "")),
        candidates)
    return {
        "results": [_row(choice, row, data_class, bbox, lon, lat, opens, until)
                    for row in choice.rows if not row.excluded],
        "sentence": choice.sentence,
        "picked": choice.picked,
        "tie": choice.tie,
        "choice": choice.model_dump(mode="json"),
    }


async def _query(text: str, place: Any, window: Any) -> dict[str, Any]:
    """The sources whose words match ``text``, each with the call it is ready for; none is ranked on the match's facts."""
    from trid3nt_server.tools.search.search_tools import search_tools as st
    from trid3nt_server.tools.search.tool_retrieval import doc_similarities, ranked_docs

    index = st._get_source_index()
    bbox, lon, lat = await _place(place)
    opens, until = _window(window)
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
    return {"results": rows}


def _source_row(name: str, about: str, bbox: Sequence[float] | None,
                lon: float | None, lat: float | None, opens: str | None,
                until: str | None) -> dict[str, Any]:
    """One source a query reached: what it is, what it takes, and the call it is ready for."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    spec = get_spec(name)
    classes = sorted({row.data_class for row in getattr(spec, "coverage", ())})
    picked = SourceChoice(slot="query", need=classes[0] if classes else "query",
                          picked=name)
    ask = base_ask(picked, "", bbox, lon, lat, opens, until)
    ask.pop("purpose", None)
    row: dict[str, Any] = {
        "source": name, "kind": "unranked", "about": about,
        "params": params_line(spec), "ask": ask_for(picked, ask, lon, lat)}
    if classes:
        row["classes"] = classes
    return row


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


def _row(choice: SourceChoice, row: SourceOption, purpose: str,
         bbox: Sequence[float] | None, lon: float | None, lat: float | None,
         opens: str | None, until: str | None) -> dict[str, Any]:
    """One survivor: the facts it was ranked on, and the call it is ready for.

    The ask is built for THIS row, not the pick, so a caller reading a tie can call either; the list is narrowed to the row
    as well as the name, since a source serving a measured and a predicted series of one class is on it twice."""
    picked = choice.model_copy(update={"picked": row.fetcher, "rows": [row]})
    ask = ask_for(picked, base_ask(picked, purpose, bbox, lon, lat,
                                   opens, until), lon, lat)
    return {"source": row.fetcher, "kind": row.kind,
            "distance": row.distance, "resolution": row.resolution,
            "recency": row.recency, "datum": row.datum, "extent": row.extent,
            "ask": ask}


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


def _window(value: Any) -> tuple[str | None, str | None]:
    """The moment a series has to cover, as the instant it opens and closes at.

    One value is a moment; two are a span. Nothing given is no window, and a series source is then ranked on currency, not filtered."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return (None, None)
    if isinstance(value, Mapping):
        return (_read(value.get("opens") or value.get("start")),
                _read(value.get("until") or value.get("end")))
    if isinstance(value, (str, bytes)):
        return (_read(value), None)
    if isinstance(value, Sequence):
        stamps = [_read(v) for v in value]
        return (stamps[0] if stamps else None,
                stamps[1] if len(stamps) > 1 else None)
    return (_read(value), None)


def _read(value: Any) -> str | None:
    """One wire stamp as a UTC instant, or ``None`` where nothing came."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return ingest_instant(value, label="window")
