"""``find_sources`` - what measures one class of thing at one place, ranked.

The model's face on THE MATCH: the same filter, the same sort and the same ask
a template's need row resolves through, so a free run and a template run never
disagree about the world. What comes back is the ranked list the card renders
plus, per survivor, the arguments that fetcher is ready to be called with -
the caller reads the top row, calls the fetcher it names, and states the
values nothing measures.
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
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.search.match import (
    Need, ask_for, base_ask, match, sources_with_coverage)

__all__ = ["find_sources", "FindSourcesError"]

logger = logging.getLogger("trid3nt_server.tools.search.find_sources")


class FindSourcesError(RuntimeError):
    """The class asked about is not one a source can be filtered on."""

    error_code = "FIND_SOURCES_NEED_UNKNOWN"
    retryable = False


#: How far past a POINT the box reaches when the place is a point rather than an
#: area, in degrees - about a hundred metres, so a source called by a box is
#: asked for the ground the point stands on and not for a region around it.
_AROUND_POINT = 0.001


_METADATA = AtomicToolMetadata(
    name="find_sources",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def find_sources(
    need: DataClass,
    place: list[float] | None = None,
    window: list[str] | None = None,
    frame: str = "",
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """WHICH source measures one class of thing at one place, ranked, with the
    call ready to make.

    ROUTING: any question that has to get real numbers into a run - "how deep is
    it here", "what is the river doing", "what fell on this catchment" - before
    a fetcher is named. Call this FIRST for every data slot, then call the
    fetcher the top row names with the arguments it hands back.

    `need` is ONE class of the coverage vocabulary the schema enumerates - the
    kind of thing measured, never a source name. `place` is a point, a
    [west, south, east, north] box or a drawn area - a point is read as the
    ground it stands on, so pass a box to ask over an area. `window` is the
    moment or the [opens, closes] a series has to cover; `frame` is the vertical
    datum the run is on, which flags a source counted from another zero.

    Returns {"results": [{tool_name, ask, ...}], "sentence", "picked", "tie",
    "choice"}: the survivors in rank order with the arguments each is ready to
    be called with, then every source a filter dropped and why. A tie says so -
    pick on the facts shown, never silently. Nothing measuring it here is an
    answer: state the value on the call, or ask about a place a source reaches.
    """
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


def _row(choice: SourceChoice, row: SourceOption, purpose: str,
         bbox: Sequence[float] | None, lon: float | None, lat: float | None,
         opens: str | None, until: str | None) -> dict[str, Any]:
    """One survivor: the facts it was ranked on, and the call it is ready for.

    The ask is built for THIS row rather than for the pick, because a caller
    reading a tie has to be able to call either one without asking again. The
    list is narrowed to the row as well as the name: a source serving a measured
    and a predicted series of one class is on it twice, and each is called with
    what ITS row says to pass."""
    picked = choice.model_copy(update={"picked": row.fetcher, "rows": [row]})
    ask = ask_for(picked, base_ask(row.fetcher, purpose, bbox, lon, lat,
                                   opens, until), lon, lat)
    return {"tool_name": row.fetcher, "kind": row.kind,
            "distance": row.distance, "resolution": row.resolution,
            "recency": row.recency, "datum": row.datum, "extent": row.extent,
            "ask": ask}


async def _place(value: Any) -> tuple[list[float] | None, float | None,
                                      float | None]:
    """The place as the box a source is called with and the point it is ranked
    against.

    Four numbers, a drawn area or a GeoJSON document is a box; anything a point
    arrives as is the ground it stands on, which is a box a source called by one
    can answer. Nothing given is no place, and the match ranks on class alone."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return (None, None, None)
    if _is_pair(value):
        pt = await ingest_point(value, label="place")
        if pt is None:
            return (None, None, None)
        return ([pt.lon - _AROUND_POINT, pt.lat - _AROUND_POINT,
                 pt.lon + _AROUND_POINT, pt.lat + _AROUND_POINT],
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

    One value is a moment; two are a span. Nothing given is no window, and a
    series source is then ranked on how current it is rather than filtered."""
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
