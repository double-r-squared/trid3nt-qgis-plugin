"""JOIN-on-key transform (contract sec 2.5) -- first-class, NATE-approved.

The pattern that decides the fold's ceiling (audit surprise #1: census_acs,
lehd_jobs, usgs_gw, usgs_wq, volcano_alerts, openfema). Declarative ``join``
block: fetch geometry (vector-fgb executor), extract the scope set, fetch values
per-scope, left-join on the key, derive the per-feature value, serialize FGB.
Missing value -> ``null`` (NEVER fabricated -- honesty rule).

Pure, offline-testable pieces: ``compute_value`` (both derive kinds) and
``join_on_key`` (left-join + null handling). The network fetches route through
``fetch_geometry`` / ``fetch_values`` which tests monkeypatch.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error, router_upstream_error
from ..executors import vector_fgb

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.transforms.join"
)

__all__ = ["compute_value", "join_on_key", "select_variable", "execute"]


def select_variable(spec: SourceSpec, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve the requested variable's declarative spec from ``join.values``."""
    join_block = spec.join or {}
    variables = (join_block.get("values") or {}).get("variables") or {}
    requested = params.get("variable")
    if requested not in variables:
        raise router_input_error(
            spec.error_code_prefix,
            f"unknown variable {requested!r}; allowed: {sorted(variables)}",
        )
    return requested, variables[requested]


def compute_value(
    var_spec: dict[str, Any],
    rec: dict[str, float | None] | None,
    *,
    null_floor: float | None = None,
) -> float | None:
    """Derive the choropleth value for one feature from its values record.

    - ``kind="value"``: ``rec[code]`` (None -> None, never fabricated).
    - ``kind="pct"``:   ``100 * sum(num) / denom`` (None/<=0 denom -> None).
    Sentinel jam values (<= ``null_floor``) normalize to ``None``.
    """
    if rec is None:
        return None

    def _clean(v: float | None) -> float | None:
        if v is None:
            return None
        if null_floor is not None and v <= null_floor:
            return None
        return v

    kind = var_spec.get("kind", "value")
    if kind == "value":
        return _clean(rec.get(var_spec["code"]))
    if kind == "pct":
        denom = _clean(rec.get(var_spec["denom"]))
        if denom is None or denom <= 0:
            return None
        total = 0.0
        for k in var_spec["num"]:
            v = _clean(rec.get(k))
            if v is None:
                return None
            total += v
        return round(100.0 * total / denom, 2)
    return None


def join_on_key(
    geom_features: list[dict[str, Any]],
    values_by_key: dict[str, dict[str, float | None]],
    join_block: dict[str, Any],
    var_name: str,
    var_spec: dict[str, Any],
    *,
    null_floor: float | None = None,
) -> list[dict[str, Any]]:
    """Left-join ``values_by_key`` onto ``geom_features`` by the join key (pure).

    Missing value -> the feature keeps a ``value: None`` property (honesty rule:
    never fabricated). Returns GeoJSON features ready for ``features_to_fgb_bytes``.
    """
    geom_cfg = join_block.get("geometry") or {}
    key_field = geom_cfg.get("key_field", "GEOID")
    keep = geom_cfg.get("keep", [])
    units = var_spec.get("units")

    out: list[dict[str, Any]] = []
    for feat in geom_features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        props = feat.get("properties") or {}
        key = props.get(key_field)
        rec = values_by_key.get(key) if key is not None else None
        out_props: dict[str, Any] = {
            "geoid": key,
            "variable": var_name,
            "value": compute_value(var_spec, rec, null_floor=null_floor),
            "units": units,
        }
        for k in keep:
            out_props[str(k).lower()] = props.get(k)
        out.append({"type": "Feature", "geometry": geom, "properties": out_props})
    return out


# --------------------------------------------------------------------------- #
# Network fetches (tests monkeypatch these two).
# --------------------------------------------------------------------------- #


def fetch_geometry(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch the geometry endpoint's features (ArcGIS query via vector-fgb)."""
    join_block = spec.join or {}
    geom_cfg = join_block.get("geometry") or {}
    endpoint_name = geom_cfg.get("endpoint", "geometry")
    endpoint = spec.endpoints.get(endpoint_name)
    if endpoint is None:
        raise router_upstream_error(spec.error_code_prefix, f"missing geometry endpoint {endpoint_name!r}")
    # Reuse the ArcGIS paginated fetch, pointing at the geometry endpoint.
    tmp_spec = spec.model_copy(update={"endpoints": {**spec.endpoints, "data": endpoint}})
    return vector_fgb.fetch_features(tmp_spec, params)


def fetch_values(
    spec: SourceSpec,
    scope_keys: list[tuple[str, ...]],
    var_spec: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, dict[str, float | None]]:
    """Fetch values per scope from the values endpoint; key by the derived key.

    Returns ``{join_key: {code: value, ...}}``. Network. The default shape
    targets the census.gov ACS surface (``get=`` codes, ``for=``/``in=`` scope).
    """
    import httpx

    join_block = spec.join or {}
    values_cfg = join_block.get("values") or {}
    endpoint = spec.endpoints.get(values_cfg.get("endpoint", "values"))
    if endpoint is None:
        raise router_upstream_error(spec.error_code_prefix, "missing values endpoint")
    url = endpoint.url or endpoint.url_template or ""
    null_floor = values_cfg.get("null_sentinel_below")

    # The codes to request for this variable.
    codes: list[str] = []
    if var_spec.get("kind") == "value":
        codes = [var_spec["code"]]
    else:
        codes = list(var_spec.get("num", [])) + [var_spec["denom"]]

    out: dict[str, dict[str, float | None]] = {}
    for scope in scope_keys:
        # scope is e.g. (state, county). Delegate the exact query shape to a
        # per-source hook via ingest.values_query if present; else census-style.
        query = _build_values_query(spec, endpoint, codes, scope, params)
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                resp = client.get(url, params=query, headers={"User-Agent": spec.auth.user_agent})
            resp.raise_for_status()
            rows = resp.json()
        except Exception:  # noqa: BLE001 -- a bad scope never fabricates values
            continue
        _accumulate_values(out, rows, codes, values_cfg, null_floor)
    return out


def _build_values_query(spec, endpoint, codes, scope, params) -> dict[str, str]:
    """census.gov ACS-style values query (get= codes, for/in scope)."""
    q = {"get": ",".join(codes)}
    q.update({str(k): str(v) for k, v in (endpoint.query or {}).items()})
    if len(scope) >= 2:
        q["for"] = "tract:*"
        q["in"] = f"state:{scope[0]} county:{scope[1]}"
    elif scope:
        q["for"] = f"state:{scope[0]}"
    return q


def _accumulate_values(out, rows, codes, values_cfg, null_floor) -> None:
    """Parse a census-style [[header...],[row...]] response into keyed records."""
    if not isinstance(rows, list) or len(rows) < 2:
        return
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    key_field = values_cfg.get("key_field", "geoid11")
    for row in rows[1:]:
        # Derive the 11-digit key: state+county+tract concatenation (census).
        try:
            geo_parts = [row[idx[k]] for k in ("state", "county", "tract") if k in idx]
            key = "".join(geo_parts)
        except (KeyError, IndexError):
            continue
        rec: dict[str, float | None] = {}
        for c in codes:
            if c in idx:
                try:
                    rec[c] = float(row[idx[c]])
                except (TypeError, ValueError):
                    rec[c] = None
        out[key] = rec


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch geometry + values, join on key, serialize FGB (the ``fetch_fn`` body)."""
    join_block = spec.join or {}
    var_name, var_spec = select_variable(spec, params)
    null_floor = (join_block.get("values") or {}).get("null_sentinel_below")

    geom_features = fetch_geometry(spec, params)

    # Extract the scope set (e.g. distinct (STATE, COUNTY)) from geometry props.
    scope_by = (join_block.get("values") or {}).get("scope_by", [])
    scopes: set[tuple[str, ...]] = set()
    for feat in geom_features:
        props = feat.get("properties") or {}
        try:
            scopes.add(tuple(str(props[s]) for s in scope_by))
        except KeyError:
            continue

    values_by_key = fetch_values(spec, sorted(scopes), var_spec, params)
    joined = join_on_key(
        geom_features, values_by_key, join_block, var_name, var_spec,
        null_floor=null_floor,
    )
    return vector_fgb.features_to_fgb_bytes(joined, spec)
