"""``fetch`` - call one data source by name with the arguments ``find_sources`` handed back.

The model's one way to a source: a call to ``fetch`` is translated at the dispatch entry into a call of the source itself, so
gates, cards, the cache, the reuse guard and emission are the source's own, exactly as for a direct call.
"""
from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import TOOL_REGISTRY, register_tool
from trid3nt_server.tools.search.find_sources.find_sources import params_line
from trid3nt_server.tools.tool_arg_normalizer import _SILENT_DROP, normalize_args

__all__ = ["FETCH", "FetchArgError", "FetchSourceError", "fetch", "resolve_fetch"]

#: The name the registry, the dispatch translation and the turn loop know this tool by.
FETCH = "fetch"

#: Keys every source takes beside its own params: the router pops these before validation, the dispatch pops the refetch trio.
_CONTROL_KEYS = frozenset({"purpose", "visualize", "force_refetch", "refetch", "force"})

_NEAREST = 3


class FetchSourceError(RuntimeError):
    """The source named is not a data source this daemon serves."""

    error_code = "FETCH_SOURCE_UNKNOWN"
    retryable = False


class FetchArgError(RuntimeError):
    """An argument is not a parameter of the source, or the call is not shaped source plus args."""

    error_code = "FETCH_ARG_UNKNOWN"
    retryable = False


_METADATA = AtomicToolMetadata(
    name=FETCH,
    ttl_class="live-no-cache",
    source_class=None,
)


def resolve_fetch(params: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The source a ``fetch`` call names and the arguments to call it with; refuses an unknown source or argument by name."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec, registered_spec_names

    stray = sorted(k for k in params if k not in ("source", "args") and not str(k).startswith("_"))
    if stray:
        raise FetchArgError(
            f"{', '.join(repr(k) for k in stray)} goes inside args: fetch takes source and args "
            "only, e.g. fetch(source='fetch_x', args={...}) with a find_sources row's source and ask.")
    named = str(params.get("source") or "").strip()
    sources = sorted(registered_spec_names())
    name = named if named in sources else f"fetch_{named}"
    if name not in sources:
        near = difflib.get_close_matches(named, sources, n=_NEAREST, cutoff=0.5)
        raise FetchSourceError(
            f"no source named {named!r}; nearest: {', '.join(near) or 'none'}. "
            "Ask find_sources(query=...) for the sources that hold what you want.")
    args = params.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else None
        except ValueError:
            raise FetchArgError(
                f"args must be an object of {name}'s parameters, not text: "
                f"{params_line(get_spec(name))}.") from None
    if args is not None and not isinstance(args, Mapping):
        raise FetchArgError(
            f"args must be an object of {name}'s parameters: {params_line(get_spec(name))}.")
    # The source's own alias table maps the spellings it knows; whatever is left unnamed by the row is refused, not dropped.
    kwargs = normalize_args(name, dict(args or {}), TOOL_REGISTRY[name].fn)
    spec = get_spec(name)
    unknown = sorted(k for k in kwargs if k not in spec.params and k not in _CONTROL_KEYS
                     and k not in _SILENT_DROP)
    if unknown:
        raise FetchArgError(_unknown_message(name, spec, unknown))
    return name, kwargs


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
    **_extra_ignored: Any,
) -> Any:
    """Call ONE data source and put its data on the map - the source and arguments `find_sources` handed back.

    ROUTING: after `find_sources`, to get the data: `fetch(source=<row's source>, args=<row's ask>)`,
    adding any parameter the source needs that the ask lacks. Never the first call for a data
    question: find the source first.

    `source` is a name from a `find_sources` row; `args` is that source's parameters as an
    object (bbox, dates, ids - its row lists them, * marks required). An argument the
    source does not take is refused by name with the nearest parameter.

    Returns what the source returns: a layer already on the user's map, or a record.
    """
    name, kwargs = resolve_fetch({"source": source, "args": args, **_extra_ignored})
    return TOOL_REGISTRY[name].fn(**kwargs)


fetch.param_descriptions = {  # type: ignore[attr-defined]
    "source": "text, a find_sources row's source; the data source to call",
    "args": "object; that source's parameters - the row's ask, plus any it lists (* is required)",
}
