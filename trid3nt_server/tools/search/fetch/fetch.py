"""``fetch`` - send one data source the request a ``find_sources`` row holds, by its handle, or call a source by name.

The model's one way to a source: a call to ``fetch`` is translated at the dispatch entry into a call of the source itself, so
gates, cards, the cache, the reuse guard and emission are the source's own, exactly as for a direct call. A handle's
edits are checked against the names its source declares, and a moved box or window is filled again and re-checked for
what the source reaches.
"""
from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from trid3nt_contracts.coverage import SourceChoice, SourceOption
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.render.uri_registry import SOURCE_HANDLE_RE, held_source_request
from trid3nt_server.tools import TOOL_REGISTRY, register_tool
from trid3nt_server.tools.search.find_sources.find_sources import params_line, window_of
from trid3nt_server.tools.search.match import (
    AROUND_PLACE_DEG, Need, filled, unreached, window_params)
from trid3nt_server.tools.tool_arg_normalizer import _SILENT_DROP, normalize_args

__all__ = ["FETCH", "FetchArgError", "FetchReachError", "FetchSourceError", "fetch",
           "resolve_fetch"]

#: The name the registry, the dispatch translation and the turn loop know this tool by.
FETCH = "fetch"

#: Keys every source takes beside its own params: the router pops these before validation, the dispatch pops the refetch trio.
_CONTROL_KEYS = frozenset({"purpose", "visualize", "force_refetch", "refetch", "force"})

#: The two edits said in our words rather than a source's own params; the filler translates them.
_OUR_WORDS = ("box", "window")

_NEAREST = 3


class FetchSourceError(RuntimeError):
    """The source or row handle named is not one this daemon or this case holds."""

    error_code = "FETCH_SOURCE_UNKNOWN"
    retryable = False


class FetchArgError(RuntimeError):
    """An argument or edit is not a parameter of the source, or the call is not one of fetch's three forms."""

    error_code = "FETCH_ARG_UNKNOWN"
    retryable = False


class FetchReachError(RuntimeError):
    """An edited row no longer reaches what its source covers."""

    error_code = "FETCH_ROW_UNREACHED"
    retryable = False


_METADATA = AtomicToolMetadata(
    name=FETCH,
    ttl_class="live-no-cache",
    source_class=None,
)


def resolve_fetch(params: Mapping[str, Any], held: Any = None) -> tuple[str, dict[str, Any]]:
    """The source a ``fetch`` call names and the arguments to call it with; refuses an unknown source, row or argument by name.

    ``held`` is the session's registry a row handle is read from; without it, the registry active for this dispatch."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec, registered_spec_names

    named = str(params.get("source") or "").strip()
    stray = sorted(k for k in params if k not in ("source", "args", "edits")
                   and not str(k).startswith("_"))
    if stray:
        raise FetchArgError(
            f"{', '.join(repr(k) for k in stray)} goes inside "
            + ("edits: fetch('S1', edits={...})." if SOURCE_HANDLE_RE.match(named)
               else "args: fetch(source='fetch_x', args={...}), or a find_sources row's handle: fetch('S1')."))
    if SOURCE_HANDLE_RE.match(named):
        if params.get("args"):
            raise FetchArgError(f"{named} takes edits={{...}}, not args: its request is already filled.")
        name, args = _from_handle(named, _object(params.get("edits"), "edits", named), held)
    else:
        if params.get("edits"):
            raise FetchArgError("edits change a find_sources row: pass its handle, e.g. fetch('S1', edits={...}).")
        sources = sorted(registered_spec_names())
        name = named if named in sources else f"fetch_{named}"
        if name not in sources:
            near = difflib.get_close_matches(named, sources, n=_NEAREST, cutoff=0.5)
            raise FetchSourceError(
                f"no source named {named!r}; nearest: {', '.join(near) or 'none'}. "
                "Ask find_sources for the sources that hold what you want.")
        args = _object(params.get("args"), "args", name)
    # The source's own alias table maps the spellings it knows; whatever is left unnamed by the row is refused, not dropped.
    kwargs = normalize_args(name, dict(args or {}), TOOL_REGISTRY[name].fn)
    spec = get_spec(name)
    unknown = sorted(k for k in kwargs if k not in spec.params and k not in _CONTROL_KEYS
                     and k not in _SILENT_DROP)
    if unknown:
        raise FetchArgError(_unknown_message(name, spec, unknown))
    return name, kwargs


def _object(value: Any, called: str, name: str) -> dict[str, Any]:
    """``args`` or ``edits`` as an object; text holding one is read."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec, registered_spec_names

    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except ValueError:
            value = ""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    takes = (params_line(get_spec(name)) if name in registered_spec_names()
             else "box, window, or a name the row lists")
    raise FetchArgError(f"{called} must be an object of {name}'s parameters: {takes}.")


def _from_handle(handle: str, edits: dict[str, Any], held: Any) -> tuple[str, dict[str, Any]]:
    """The request a row holds, with ``edits`` merged in: box and window filled again through the
    match's filler, a native name set as given, and the result re-checked for what the source reaches."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    row = held_source_request(handle, held)
    if row is None:
        raise FetchSourceError(f"no row {handle} in this case; find_sources again.")
    name, args = str(row["source"]), dict(row.get("args") or {})
    if not edits:
        return name, args
    params = list(getattr(get_spec(name), "params", {}) or {})
    unknown = [key for key in edits if key not in _OUR_WORDS and key not in params]
    if unknown:
        raise FetchArgError(
            f"{', '.join(repr(k) for k in unknown)} is not editable on {handle}; its editable "
            f"names: {', '.join([*_OUR_WORDS, *params])}.")
    box = row.get("box")
    if "box" in edits or "bbox" in edits:
        box = _box(edits.get("box", edits.get("bbox")), handle)
    opens, until = row.get("opens"), row.get("until")
    if "window" in edits:
        opens, until = window_of(edits["window"])
    starts, ends = window_params()
    opens = next((str(edits[p]) for p in starts if p in edits), opens)
    until = next((str(edits[p]) for p in ends if p in edits), until)
    lon, lat = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0) if box else (None, None)
    picked = SourceChoice(slot=row["class"], need=row["class"], picked=name,
                          rows=[SourceOption(fetcher=name, kind=row.get("kind") or "")])
    need = Need(slot=row["class"], data_class=row["class"], lon=lon, lat=lat,
                opens=opens, until=until, frame=row.get("frame") or "")
    away = unreached(picked, need)
    if away:
        moved = " and ".join(w for w in _OUR_WORDS if w in edits) or "edit"
        raise FetchReachError(f"{handle} does not reach that {moved}: {away}; find_sources again.")
    if any(word in edits for word in _OUR_WORDS):
        args = filled(picked, str(args.get("purpose") or ""), box, need)
    args.update({key: value for key, value in edits.items() if key not in _OUR_WORDS})
    return name, args


def _box(value: Any, handle: str) -> list[float]:
    """An edited box: four numbers, or a lon/lat point read as the ground it stands on."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) \
            and all(isinstance(v, (int, float)) for v in value):
        if len(value) == 4:
            return [float(v) for v in value]
        if len(value) == 2:
            lon, lat = (float(v) for v in value)
            return [lon - AROUND_PLACE_DEG, lat - AROUND_PLACE_DEG,
                    lon + AROUND_PLACE_DEG, lat + AROUND_PLACE_DEG]
    raise FetchArgError(f"box on {handle} is [west, south, east, north] or a [lon, lat] point; got {value!r}.")


def _unknown_message(name: str, spec: Any, unknown: list[str]) -> str:
    """The refusal naming each argument the source does not take, the nearest parameter, and every parameter it has."""
    said = []
    for key in unknown:
        near = difflib.get_close_matches(key, list(spec.params), n=1, cutoff=0.5)
        said.append(f"{key!r} is not a parameter of {name}" + (f"; did you mean {near[0]!r}?" if near else ""))
    return "; ".join(said) + f". Its parameters: {params_line(spec)}."


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)
def fetch(
    source: str,
    args: dict | None = None,
    edits: dict | None = None,
    **_extra_ignored: Any,
) -> Any:
    """Get data onto the map: send the request a `find_sources` row holds, by its handle.

    ROUTING: after `find_sources`, `fetch("S1")` sends row S1 exactly as it was shown. To
    change it, `fetch("S1", edits={...})`: `box` [west, south, east, north] and `window` (a
    moment or [opens, closes]) in the row's own words, or a name the row lists under
    "needs" or "also accepts". A name the source does not take, or an edit that moves the
    row out of what it reaches, is refused in one line. Never the first call for a data
    question: find the source first, and never copy a row's fields into args.

    `fetch(source, args)` calls a source by name with its own parameters, for scripting.
    A long animation series: show the user the AOI and the planned frame list first, and
    do not pull every frame on the first turn.

    Returns what the source returns: a layer already on the user's map, or a record.
    """
    name, kwargs = resolve_fetch({"source": source, "args": args, "edits": edits,
                                  **_extra_ignored})
    return TOOL_REGISTRY[name].fn(**kwargs)


fetch.param_descriptions = {  # type: ignore[attr-defined]
    "source": "text, a find_sources row's handle (S1); or a source name with args",
    "args": "object; a named source's own parameters (scripting form only)",
    "edits": "object; changes to the row: box, window, or a name the row lists",
}
