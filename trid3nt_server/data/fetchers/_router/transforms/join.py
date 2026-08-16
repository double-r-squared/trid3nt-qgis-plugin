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
    "trid3nt_server.data.fetchers._router.transforms.join"
)

__all__ = ["compute_value", "join_on_key", "select_variable", "execute", "is_raw_passthrough_code"]


def is_raw_passthrough_code(s: Any) -> bool:
    """True if ``s`` looks like a raw ACS-style estimate code (e.g. ``B19013_001E``).

    Mirrors the twin ``fetch_census_acs._is_raw_acs_code``: a leading ``B``/``C``
    table letter, an ``E`` estimate suffix, and an underscore. This is the
    full-fidelity passthrough NATE chose -- a raw variable code is accepted
    alongside the friendly names, resolved to a ``kind=value`` fetch of that code
    with ``units=count`` (the twin's units for a raw code).
    """
    if not isinstance(s, str) or not s:
        return False
    if s[0].upper() not in ("B", "C"):
        return False
    if not s.upper().endswith("E"):
        return False
    return "_" in s


def select_variable(spec: SourceSpec, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve the requested variable to ``(name, var_spec)`` -- full-fidelity.

    Accepts, exactly as the twin ``_resolve_variable`` did: a friendly name
    (case-insensitive) from ``join.values.variables``, OR a raw ACS estimate code
    (``B19013_001E``) passed through as a ``kind=value`` fetch with ``units=count``.
    An unknown/malformed variable raises the twin-identical typed input error.
    """
    join_block = spec.join or {}
    variables = (join_block.get("values") or {}).get("variables") or {}
    # variable_param: the request param carrying the variable/segment name
    # (default "variable"; lehd_jobs names it "segment"). No-op for census (default).
    variable_param = join_block.get("variable_param", "variable")
    requested = params.get(variable_param)
    if not isinstance(requested, str) or not requested.strip():
        raise router_input_error(
            spec.error_code_prefix,
            f"{variable_param} must be a non-empty string; got {requested!r}",
            spec.input_error_suffix,
        )
    key = requested.strip()
    low = key.lower()
    if low in variables:
        return low, dict(variables[low])
    # Raw ACS estimate-code passthrough (full fidelity, NATE-chosen). allow_raw_code
    # gates it OFF for a closed-vocabulary source (lehd segments), whose
    # unknown value must raise the twin's plain enum error. Default True = census.
    if join_block.get("allow_raw_code", True) and is_raw_passthrough_code(key):
        code = key.upper()
        table = code.split("_", 1)[0]
        return code, {"table": table, "code": code, "kind": "value", "units": "count"}
    raise router_input_error(
        spec.error_code_prefix,
        f"unknown variable {requested!r}; allowed: {sorted(variables)} "
        f"or a raw ACS estimate code like 'B19013_001E'",
        spec.input_error_suffix,
    )


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
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Left-join ``values_by_key`` onto ``geom_features`` by the join key (pure).

    Missing value -> the feature keeps a ``value: None`` property (honesty rule:
    never fabricated). Returns GeoJSON features ready for ``features_to_fgb_bytes``.
    """
    geom_cfg = join_block.get("geometry") or {}
    key_field = geom_cfg.get("key_field", "GEOID")
    keep = geom_cfg.get("keep", [])
    units = var_spec.get("units")
    # value_field: the property carrying the variable/segment label
    # (default "variable"; lehd_jobs -> "segment"). extra_props: static param-echo
    # columns ({prop: {param: <name>}}) the twin stamps beyond the join (lehd year).
    # Both default to census's exact schema (no-op).
    value_field = join_block.get("value_field", "variable")
    extra_props = join_block.get("extra_props") or {}

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
            value_field: var_name,
            "value": compute_value(var_spec, rec, null_floor=null_floor),
            "units": units,
        }
        for k in keep:
            out_props[str(k).lower()] = props.get(k)
        for prop_name, src in extra_props.items():
            if isinstance(src, dict) and "param" in src:
                out_props[str(prop_name)] = (params or {}).get(src["param"])
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
    # values_hook: a source whose values leg is NOT the census Data-API
    # (lehd's per-state bulk gzip-CSV) declares a pure plan+parse hook pair. The
    # transport (the plan GETs) + honesty stay router-owned here; the hooks only
    # build the requests and decode the bytes. No-op for census (no values_hook).
    vhook = values_cfg.get("values_hook")
    if vhook:
        return _fetch_values_via_hook(spec, scope_keys, var_spec, params, vhook)
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


def _fetch_values_via_hook(
    spec: SourceSpec,
    scope_keys: list[tuple[str, ...]],
    var_spec: dict[str, Any],
    params: dict[str, Any],
    vhook: dict[str, Any],
) -> dict[str, dict[str, float | None]]:
    """Values leg via the declared plan+parse hooks; router owns the I/O.

    ``plan(spec, scope_keys, var_spec, params) -> [(scope_key, RequestPlan)]`` is pure;
    the transport GETs each plan (a whole-object gzip, best-effort skipped on failure so
    one bad state never fabricates or aborts the whole join); ``parse(spec,
    {scope_key: bytes}, var_spec, params) -> {join_key: {code: value}}`` decodes.
    """
    from ..executors.http_json import _get
    from ..hooks import resolve_hook

    plan_hook = resolve_hook(vhook["plan"])
    parse_hook = resolve_hook(vhook["parse"])
    plans = plan_hook(spec, scope_keys, var_spec, params)
    bodies: dict[str, bytes] = {}
    for scope_key, plan in plans:
        bodies[scope_key] = _get(spec, plan)
    return parse_hook(spec, bodies, var_spec, params)


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
        null_floor=null_floor, params=params,
    )
    return vector_fgb.features_to_fgb_bytes(joined, spec, params)
