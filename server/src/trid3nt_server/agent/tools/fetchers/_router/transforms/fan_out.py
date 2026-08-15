"""Declarative fan-out transform.

A named transform WRAPPING the vector-fgb executor for the multi-query-per-value
shape: a ``float_list`` param drives one query PER value against a per-value
endpoint (``url_template`` + a ``value_map`` substitution), each fetched feature
is stamped with per-value output columns, and all values' features MERGE into one
FlatGeobuf (slr_scenarios: one query per ``scenario_ft`` level, ``slr_ft`` +
``scenario_label`` stamped, dissolved polygons merged in sorted-level order). This
composes the vector executor N times (analysis-is-composition), never a new
executor. Strictly no-op for prior specs: only reached when ``ingest.fan_out`` is
declared, which no prior spec sets.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.source_spec import EndpointSpec, SourceSpec

from ..errors import router_input_error
from ..executors import vector_fgb

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.transforms.fan_out"
)

__all__ = ["fan_value_key", "build_stamp_props", "execute"]


def fan_value_key(v: float) -> str:
    """The ``value_map`` lookup key for a fan-out value (``1.0`` -> ``"1.0"``).

    ``str(float)`` gives the twin's naming input verbatim (``0.5`` -> ``"0.5"``,
    ``10.0`` -> ``"10.0"``); the spec's ``value_map`` is keyed by these strings.
    """
    return str(float(v))


def build_stamp_props(
    stamp: dict[str, Any], value: float, src_props: dict[str, Any]
) -> dict[str, Any]:
    """Build one feature's stamped output props (ordered per ``stamp``).

    Each ``stamp`` entry (in declaration order) resolves a source column:
    ``value`` -> the fan-out value; ``value_template`` -> ``template.format(value=)``
    (the twin's ``scenario_label``); ``prop`` -> a source property (``from``) with
    an optional ``kind`` (int/float/passthrough) + ``default`` when absent.
    """
    props: dict[str, Any] = {}
    for col, rule in stamp.items():
        source = rule.get("source")
        if source == "value":
            props[str(col)] = value
        elif source == "value_template":
            props[str(col)] = str(rule.get("template", "{value}")).format(value=value)
        elif source == "prop":
            present = rule.get("from") in src_props
            raw = src_props.get(rule.get("from")) if present else rule.get("default")
            kind = rule.get("kind", "passthrough")
            if kind == "int":
                try:
                    props[str(col)] = int(raw) if raw is not None else rule.get("default")
                except (TypeError, ValueError):
                    props[str(col)] = rule.get("default")
            elif kind == "float":
                try:
                    props[str(col)] = float(raw) if raw is not None else rule.get("default")
                except (TypeError, ValueError):
                    props[str(col)] = rule.get("default")
            else:
                props[str(col)] = raw
        else:
            props[str(col)] = rule.get("default")
    return props


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """One vector query PER fan-out value, stamped + merged -> FGB bytes."""
    fan = (spec.ingest or {}).get("fan_out") or {}
    pname = fan.get("param")
    values = params.get(pname) or []
    base_ep = spec.endpoints.get(fan.get("endpoint", "data")) or next(
        iter(spec.endpoints.values())
    )
    url_template = base_ep.url_template or base_ep.url or ""
    value_map = fan.get("value_map") or {}
    stamp = fan.get("stamp") or {}

    all_features: list[dict[str, Any]] = []
    for v in values:  # already sorted + deduped by validate_params
        service = value_map.get(fan_value_key(v))
        if service is None:
            raise router_input_error(
                spec.error_code_prefix,
                f"{pname}={v!r} has no fan-out endpoint mapping",
                spec.input_error_suffix,
            )
        url = url_template.replace("{service}", str(service))
        endpoint = EndpointSpec(url=url, query=dict(base_ep.query or {}))
        feats = vector_fgb._fetch_from_endpoint(spec, endpoint, params)
        for feat in feats:
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry")
            if geom is None:
                continue
            props = build_stamp_props(stamp, v, feat.get("properties") or {})
            all_features.append({"type": "Feature", "geometry": geom, "properties": props})

    logger.info(
        "router.fan_out: %d value(s) -> %d feature(s) (source=%s)",
        len(values), len(all_features), spec.source_class,
    )
    return vector_fgb.features_to_fgb_bytes(all_features, spec, params)
