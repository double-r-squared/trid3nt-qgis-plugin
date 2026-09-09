"""Record-return executor: the bare-JSON-dict output shape.

The source's result is a STRUCTURED DICT, not a renderable LayerURI, so the router
serializes no COG or FGB: it produces JSON bytes the read-through caches, and the
pure ``hooks.record`` hook shapes the fetched bodies into that dict."""

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
    """Fetch the build plan(s) and shape the record dict to JSON bytes: the first
    non-None ``hooks.record`` dict, walking the plans in order. Every plan yielding
    None raises the source's typed empty/not-found error, never a fabricated hit."""
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
