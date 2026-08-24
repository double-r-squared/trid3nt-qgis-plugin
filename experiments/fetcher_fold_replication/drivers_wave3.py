"""Replication-parity drivers -- phase-2 wave-3 USGS water-data family (ADR 0040).

Unlike the wave-1/2 offline harness (one synthetic upstream both the twin and the
router consume via a shared ``httpx`` patch), this wave DELEGATES the router to the
official USGS ``dataretrieval`` client while the twin uses raw ``urllib`` + bespoke
parsing -- the two paths speak different HTTP stacks, so there is no single
mockable upstream. The gate is therefore a LIVE twin-vs-router comparison: both
hit the SAME real USGS endpoints for the SAME request, and any values/schema/layer
divergence is attributable to the fold (bespoke parse vs dataretrieval), not to
data drift (both read the same live data in the same run). The router path's
MECHANICS are validated OFFLINE first (mocked dataretrieval, scratch smoke) per the
offline-first rule; this file is the LIVE gate the recipe names.

Twin behavior is the contract. A divergence rooted in a TWIN defect (not the
router) is flagged-not-copied; any other divergence closes to twin behavior.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any, Callable
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from harness import (  # noqa: E402
    SourceResult,
    err_frame,
    load_specs,
    route_layer,
    vector_info,
    _make_stub_read_through,
    _patched,
)

# Twin modules (present until promotion; this gate runs BEFORE the cut).
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_water_quality import (  # noqa: E402
    fetch_usgs_water_quality as wq_mod,
)
from trid3nt_server.tools.fetchers.hydrology.fetch_nhdplus_nldi_navigate import (  # noqa: E402
    fetch_nhdplus_nldi_navigate as nldi_mod,
)


def _run_twin(mod, fn_name: str, kwargs: dict, sink: dict):
    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(mod, "read_through", stub)):
        return getattr(mod, fn_name)(**kwargs)


def _capture_err(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return {"type": None, "error_code": None, "retryable": None, "raised": False}
    except BaseException as exc:  # noqa: BLE001
        d = err_frame(exc)
        d["raised"] = True
        return d


def _cmp_error(res: SourceResult, name: str, tw: dict, rt: dict) -> None:
    both = tw.get("raised") and rt.get("raised")
    code = tw.get("error_code") == rt.get("error_code")
    retry = tw.get("retryable") == rt.get("retryable")
    ok = bool(both and code and retry)
    note = ""
    if both and not code:
        note = f"error_code diverges: twin={tw['error_code']} router={rt['error_code']}"
    elif not both:
        note = f"raise mismatch: twin={tw.get('raised')} router={rt.get('raised')}"
    res.add(name, ok, tw.get("error_code"), rt.get("error_code"), note=note)


def _cmp_layer(res: SourceResult, tl, rl) -> None:
    res.add("layer.type", tl.layer_type == rl.layer_type, tl.layer_type, rl.layer_type)
    res.add("layer.style_preset", tl.style_preset == rl.style_preset, tl.style_preset, rl.style_preset)
    res.add("layer.role", tl.role == rl.role, tl.role, rl.role)
    res.add("layer.units", tl.units == rl.units, tl.units, rl.units)
    tb, rb = tl.bbox is not None, rl.bbox is not None
    res.add("layer.bbox_present", tb == rb, tb, rb)


def _retry(fn, tries=4):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- USGS OGC/WQP endpoints flake (transient 400/5xx)
            last = exc
    raise last


# --------------------------------------------------------------------------- #
# WQP water quality
# --------------------------------------------------------------------------- #


def run_wqp(specs) -> SourceResult:
    res = SourceResult("fetch_usgs_water_quality")
    spec = specs["fetch_usgs_water_quality"]
    name = "fetch_usgs_water_quality"
    # a small Iowa ag watershed with known nitrate sites (probe-verified 5/5).
    bbox_t = (-93.30, 41.90, -93.10, 42.10)
    bbox_l = list(bbox_t)
    char = "nitrate"
    try:
        res.add("schema.docstring_verbatim", spec.docstring == inspect.getdoc(getattr(wq_mod, name)),
                note="spec.docstring == inspect.getdoc(twin)")

        # --- happy path: same real WQP request, twin(urllib) vs router(dataretrieval) ---
        tw_sink, rt_sink = {}, {}
        tl = _retry(lambda: _run_twin(wq_mod, name, dict(bbox=bbox_t, characteristic=char), tw_sink))
        rl = _retry(lambda: route_layer(spec, dict(bbox=bbox_l, characteristic=char), rt_sink))
        tw = vector_info(tw_sink["bytes"])
        rt = vector_info(rt_sink["bytes"])
        res.add("values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
        res.add("values.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
        res.add("values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
        res.add("schema.columns", tw["columns"] == rt["columns"], sorted(tw["columns"]), sorted(rt["columns"]))
        # value spotcheck: the SET of site_ids must be identical (same endpoint).
        tw_ids = set(tw["gdf"]["site_id"]) if "site_id" in tw["gdf"].columns else set()
        rt_ids = set(rt["gdf"]["site_id"]) if "site_id" in rt["gdf"].columns else set()
        res.add("values.site_id_set", tw_ids == rt_ids, len(tw_ids), len(rt_ids),
                note="" if tw_ids == rt_ids else f"site_id set diverges (twin-only {sorted(tw_ids-rt_ids)[:3]}, router-only {sorted(rt_ids-tw_ids)[:3]})")
        # latest-value spotcheck: sorted non-null value multiset.
        def _vals(info):
            import math
            return sorted(round(float(v), 6) for v in info["gdf"]["value"] if v is not None and not (isinstance(v, float) and math.isnan(v)))
        res.add("values.value_spotcheck", _vals(tw) == _vals(rt), _vals(tw)[:5], _vals(rt)[:5],
                note="sorted latest-per-site numeric values")
        _cmp_layer(res, tl, rl)
        res.add("caveats.reproduced", any("WQP_NO_SITES" in c for c in spec.caveats),
                note="typed WQP_NO_SITES empty caveat present")

        # --- empty (ocean bbox): both raise WQP_NO_SITES ---
        ocean = (-40.0, 10.0, -39.9, 10.1)
        tw_e = _capture_err(_run_twin, wq_mod, name, dict(bbox=ocean, characteristic="Nitrate"), {})
        rt_e = _capture_err(route_layer, spec, dict(bbox=list(ocean), characteristic="Nitrate"), {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        # --- bad characteristic (live HTTP 400 both sides) ---
        tw_c = _capture_err(_run_twin, wq_mod, name, dict(bbox=bbox_t, characteristic="NotARealCharacteristicXYZ"), {})
        rt_c = _capture_err(route_layer, spec, dict(bbox=bbox_l, characteristic="NotARealCharacteristicXYZ"), {})
        _cmp_error(res, "error.bad_characteristic", tw_c, rt_c)

        # --- forced upstream failure (each side's HTTP layer raises) ---
        import dataretrieval.wqp as wqp
        from dataretrieval.exceptions import ServiceUnavailable
        tw_u = _capture_err(
            lambda: _run_twin_patched_http(wq_mod, name, dict(bbox=bbox_t, characteristic="Nitrate"))
        )
        with _patched(mock.patch.object(wqp, "what_sites", mock.Mock(side_effect=ServiceUnavailable("down")))):
            rt_u = _capture_err(route_layer, spec, dict(bbox=bbox_l, characteristic="Nitrate"), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges (pre-network) ---
        for ename, tk, rp in [
            ("error.bad_bbox", dict(bbox=(-93.1, 41.9, -93.1, 42.1), characteristic="Nitrate"),
             dict(bbox=[-93.1, 41.9, -93.1, 42.1], characteristic="Nitrate")),
            ("error.bbox_too_large", dict(bbox=(-120.0, 30.0, -100.0, 45.0), characteristic="Nitrate"),
             dict(bbox=[-120.0, 30.0, -100.0, 45.0], characteristic="Nitrate")),
        ]:
            tw_x = _capture_err(_run_twin, wq_mod, name, tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def _run_twin_patched_http(mod, name, kwargs):
    """Force the twin's HTTP layer to raise its upstream error (offline-forced)."""
    err = mod.WqpUpstreamError("forced upstream")
    with _patched(mock.patch.object(mod, "_http_get", mock.Mock(side_effect=err)),
                  mock.patch.object(mod, "read_through", _make_stub_read_through({}))):
        return getattr(mod, name)(**kwargs)


# --------------------------------------------------------------------------- #
# NLDI navigate
# --------------------------------------------------------------------------- #


def run_nldi(specs) -> SourceResult:
    res = SourceResult("fetch_nhdplus_nldi_navigate")
    spec = specs["fetch_nhdplus_nldi_navigate"]
    name = "fetch_nhdplus_nldi_navigate"
    comid = 15334434  # twin docstring example (Caloosahatchee region)
    seed = (-81.85, 26.55)
    try:
        res.add("schema.docstring_verbatim", spec.docstring == inspect.getdoc(getattr(nldi_mod, name)),
                note="spec.docstring == inspect.getdoc(twin)")

        # --- happy path (comid navigate): twin(urllib) vs router(dataretrieval) ---
        tw_sink, rt_sink = {}, {}
        tl = _retry(lambda: _run_twin(nldi_mod, name, dict(comid=comid, direction="DM", distance_km=50.0), tw_sink))
        rl = _retry(lambda: route_layer(spec, dict(comid=comid, direction="DM", distance_km=50.0), rt_sink))
        tw = vector_info(tw_sink["bytes"]); rt = vector_info(rt_sink["bytes"])
        res.add("values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
        res.add("values.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
        res.add("values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
        res.add("schema.columns", tw["columns"] == rt["columns"], sorted(tw["columns"]), sorted(rt["columns"]))
        tw_c = set(int(x) for x in tw["gdf"]["nhdplus_comid"] if x is not None) if "nhdplus_comid" in tw["gdf"].columns else set()
        rt_c = set(int(x) for x in rt["gdf"]["nhdplus_comid"] if x is not None) if "nhdplus_comid" in rt["gdf"].columns else set()
        res.add("values.comid_set", tw_c == rt_c, len(tw_c), len(rt_c),
                note="" if tw_c == rt_c else f"comid set diverges (twin-only {sorted(tw_c-rt_c)[:3]}, router-only {sorted(rt_c-tw_c)[:3]})")
        _cmp_layer(res, tl, rl)
        res.add("caveats.reproduced", any("NHDPLUS_NLDI_EMPTY" in c for c in spec.caveats),
                note="typed NHDPLUS_NLDI_EMPTY caveat present")

        # --- seed_point path: both snap to the same COMID, same navigate ---
        tw_s2, rt_s2 = {}, {}
        _retry(lambda: _run_twin(nldi_mod, name, dict(seed_point=seed, direction="DM", distance_km=50.0), tw_s2))
        _retry(lambda: route_layer(spec, dict(seed_point=list(seed), direction="DM", distance_km=50.0), rt_s2))
        tw2 = vector_info(tw_s2["bytes"]); rt2 = vector_info(rt_s2["bytes"])
        res.add("values.seed_snap_n", tw2["n"] == rt2["n"], tw2["n"], rt2["n"],
                note="seed_point snap->navigate feature count parity")

        # --- empty navigate (terminus / stub -> zero flowlines): forced zero both
        # sides (a live terminus comid is unstable to pin), tests the zero-flowlines
        # -> NHDPLUS_NLDI_EMPTY mapping on twin + router deterministically. ---
        import dataretrieval.nldi as nldi
        empty_fc = {"type": "FeatureCollection", "features": []}
        with _patched(mock.patch.object(nldi_mod, "_navigate_flowlines", mock.Mock(return_value=[])),
                      mock.patch.object(nldi_mod, "read_through", _make_stub_read_through({}))):
            tw_e = _capture_err(lambda: getattr(nldi_mod, name)(comid=comid))
        with _patched(mock.patch.object(nldi, "get_flowlines", mock.Mock(return_value=empty_fc))):
            rt_e = _capture_err(route_layer, spec, dict(comid=comid), {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        # --- forced upstream (each side's HTTP layer raises) ---
        import dataretrieval.nldi as nldi
        from dataretrieval.exceptions import ServiceUnavailable
        err = nldi_mod.NHDPlusNLDIUpstreamError("forced")
        with _patched(mock.patch.object(nldi_mod, "_http_get", mock.Mock(side_effect=err)),
                      mock.patch.object(nldi_mod, "read_through", _make_stub_read_through({}))):
            tw_u = _capture_err(lambda: getattr(nldi_mod, name)(comid=comid))
        with _patched(mock.patch.object(nldi, "get_flowlines", mock.Mock(side_effect=ServiceUnavailable("down")))):
            rt_u = _capture_err(route_layer, spec, dict(comid=comid), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges (pre-network) ---
        for ename, tk, rp in [
            ("error.both_seeds", dict(seed_point=seed, comid=comid), dict(seed_point=list(seed), comid=comid)),
            ("error.neither_seed", dict(direction="DM"), dict(direction="DM")),
            ("error.bad_direction", dict(comid=comid, direction="XX"), dict(comid=comid, direction="XX")),
            ("error.bad_distance", dict(comid=comid, distance_km=5000.0), dict(comid=comid, distance_km=5000.0)),
            ("error.seed_outside_conus", dict(seed_point=(-100.0, 10.0)), dict(seed_point=[-100.0, 10.0])),
        ]:
            tw_x = _capture_err(_run_twin, nldi_mod, name, tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def run_all() -> list[SourceResult]:
    specs = load_specs()
    return [run_wqp(specs), run_nldi(specs)]


def main() -> int:
    results = run_all()
    out = HERE / "results" / "VERDICT_wave3.md"
    out.parent.mkdir(exist_ok=True)
    lines = ["# Replication-parity VERDICT -- phase-2 wave-3 USGS water-data family (ADR 0040)",
             "",
             "LIVE twin(urllib+bespoke) vs router(dataretrieval) over the same real USGS",
             "endpoints. Twin behavior is the contract.",
             "",
             "| source | verdict | checks |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r.source} | {r.verdict} | {r.n_ok}/{r.n_total} |")
    lines += ["", "## Per-check detail", ""]
    for r in results:
        lines.append(f"### {r.source} -- {r.verdict}")
        if r.error:
            lines.append(f"- ERROR: {r.error}")
        for c in r.checks:
            mark = "ok" if c.ok else "XX"
            note = f" -- {c.note}" if c.note else ""
            if c.ok:
                lines.append(f"- [{mark}] {c.name}{note}")
            else:
                lines.append(f"- [{mark}] {c.name}: twin={c.twin!r} router={c.router!r}{note}")
        lines.append("")
    out.write_text("\n".join(lines))
    print("=== wave-3 replication-parity ===")
    for r in results:
        div = [c for c in r.checks if not c.ok]
        tail = "" if not div else " | " + "; ".join(c.note or c.name for c in div)
        print(f"- {r.source}: {r.verdict} ({r.n_ok}/{r.n_total}){tail}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
