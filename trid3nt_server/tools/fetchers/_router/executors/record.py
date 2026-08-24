"""Record-return executor: the bare-JSON-dict output shape.

Selected when a spec declares ``shape: record`` / ``output.layer_type: record``.
The source's result is a STRUCTURED DICT (a discovery record, a summary), NOT a
renderable LayerURI -- so the router does not build a LayerURI or serialize a
COG/FGB; it produces JSON bytes the read-through caches and ``route()`` returns as
the parsed dict envelope.

The router owns the transport (fetching the ``hooks.build_request`` plans through
the shared pooled client + retry authority) and the cache; the PURE ``hooks.record``
hook shapes the fetched body/bodies into the dict. The plans are walked IN ORDER and
the executor STOPS at the first plan whose record hook returns a non-None dict -- the
wfigs Current->YearToDate best-feature short-circuit (a recently-contained fire the
live feed dropped resolves against the all-incidents sibling). If EVERY plan yields
None the source's typed empty/not-found error raises (honesty floor: the hook never
fabricates a success dict, and a bad body still raises a typed upstream error via the
shared factories -- so a "no such record" is an honest typed dead-end, never a silent
empty the caller could narrate as a hit).

A record spec that declares no ``build_request`` (a pure dict builder needing no
fetch) calls the record hook once with an empty body list.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error
from ..hooks import resolve_hook
from .http_json import _get

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.record"
)

__all__ = ["execute"]


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch the build plan(s) and shape the record dict to JSON bytes (fetch_fn body).

    Returns the ``json.dumps`` of the first non-None ``hooks.record`` dict, walking
    the build plans in order (first-usable-record short-circuit). Raises the source's
    typed empty/not-found error when every plan yields None.
    """
    record = resolve_hook(spec.hooks.record)  # type: ignore[union-attr]
    build_name = spec.hooks.build_request if spec.hooks is not None else None
    if not build_name:
        # Pure record: no fetch, the hook builds the dict from params alone.
        result = record(spec, params, [])
        if result is None:
            raise router_empty_error(
                spec.error_code_prefix,
                f"{spec.name}: record hook produced no result",
                spec.empty_error_suffix,
            )
        return json.dumps(result, separators=(",", ":")).encode("utf-8")

    build = resolve_hook(build_name)
    plans = build(spec, params)
    for plan in plans:
        body = _get(spec, plan)
        result = record(spec, params, [body])
        if result is not None:
            return json.dumps(result, separators=(",", ":")).encode("utf-8")
    raise router_empty_error(
        spec.error_code_prefix,
        f"{spec.name}: no record matched across {len(plans)} endpoint(s)",
        spec.empty_error_suffix,
    )
