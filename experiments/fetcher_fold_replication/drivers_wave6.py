"""Replication-parity drivers -- phase-2 wave-6 VECTOR/ZIP family (ADR 0052).

LIVE twin-vs-router: both the hand-written TWIN and the spec-driven ROUTER hit the
SAME real endpoints for the SAME request in one run, so any values/schema/layer/
error divergence is attributable to the fold (fetch_noaa_slr_scenarios = the new
declarative fan-out mode; fetch_usace_levees = endpoint_by_param sub-layer routing
+ properties_by_param). Both twin and router speak httpx here, so the forced-
upstream edge patches ``httpx.Client.get`` once for each side. read_through is
stubbed (no MinIO). Twin behavior is the contract; a divergence is REPORTED.

Run: ``python drivers_wave6.py`` (needs outbound network to NOAA OCM + USACE NLD).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
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

# Twins are DELETED same-commit as promotion; each parity gate runs BEFORE its
# twin's deletion. Import defensively so a run after a twin cut still exercises the
# remaining (pre-cut) sources rather than hard-failing at import.
try:
    from trid3nt_server.tools.fetchers.ocean.fetch_noaa_slr_scenarios import (  # noqa: E402
        fetch_noaa_slr_scenarios as slr_mod,
    )
except ImportError:
    slr_mod = None
try:
    from trid3nt_server.tools.fetchers.hazard.fetch_usace_levees import (  # noqa: E402
        fetch_usace_levees as lev_mod,
    )
except ImportError:
    lev_mod = None
try:
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_epa_ejscreen import (  # noqa: E402
        fetch_epa_ejscreen as ej_mod,
    )
except ImportError:
    ej_mod = None

_SLR_COAST = (-82.02, 26.44, -81.90, 26.56)   # coastal Lee County FL -- SLR footprint
_SLR_INLAND = (-98.02, 38.50, -97.98, 38.54)  # interior Kansas -- no SLR coverage -> empty
_NOLA = (-90.14, 29.90, -90.00, 30.00)        # New Orleans -- NLD leveed areas
_OCEAN = (-140.0, 20.0, -139.9, 20.1)         # open ocean -> empty


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


def _retry(fn, tries=3):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- hosted FeatureServers flake transiently
            last = exc
    raise last


def _run_twin(mod, fn_name, kwargs, sink):
    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(mod, "read_through", stub)):
        return getattr(mod, fn_name)(**kwargs)


def _cmp_values(res: SourceResult, prefix: str, tw: dict, rt: dict, id_col: str | None) -> None:
    res.add(f"{prefix}.values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
    res.add(f"{prefix}.values.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
    res.add(f"{prefix}.values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
    res.add(f"{prefix}.schema.columns", tw["columns"] == rt["columns"], sorted(tw["columns"]), sorted(rt["columns"]))
    if id_col and id_col in tw["gdf"].columns and id_col in rt["gdf"].columns:
        tw_ids = set(str(x) for x in tw["gdf"][id_col])
        rt_ids = set(str(x) for x in rt["gdf"][id_col])
        res.add(f"{prefix}.values.{id_col}_set", tw_ids == rt_ids, len(tw_ids), len(rt_ids),
                note="" if tw_ids == rt_ids else f"id set diverges (twin-only {len(tw_ids-rt_ids)}, router-only {len(rt_ids-tw_ids)})")


def run_slr(specs) -> SourceResult:
    res = SourceResult("fetch_noaa_slr_scenarios")
    spec = specs["fetch_noaa_slr_scenarios"]
    import httpx
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(slr_mod.fetch_noaa_slr_scenarios),
                note="spec.docstring == inspect.getdoc(twin)")
        res.add("caveats.reproduced", any("NOAA_SLR_SCENARIOS_EMPTY" in c for c in spec.caveats),
                note="typed empty-caveat present")

        # --- happy: single scenario + default 3-scenario fan-out ---
        for tag, kw_t, kw_r in [
            ("single", dict(bbox=_SLR_COAST, scenario_ft=1.0), dict(bbox=list(_SLR_COAST), scenario_ft=1.0)),
            ("default", dict(bbox=_SLR_COAST), dict(bbox=list(_SLR_COAST))),
        ]:
            tw_sink, rt_sink = {}, {}
            tl = _retry(lambda: _run_twin(slr_mod, "fetch_noaa_slr_scenarios", kw_t, tw_sink))
            rl = _retry(lambda: route_layer(spec, kw_r, rt_sink))
            tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
            _cmp_values(res, tag, tw, rt, None)
            # slr_ft value set (the fan-out stamp) must match
            if "slr_ft" in tw["gdf"].columns and "slr_ft" in rt["gdf"].columns:
                tset = sorted(set(float(x) for x in tw["gdf"]["slr_ft"]))
                rset = sorted(set(float(x) for x in rt["gdf"]["slr_ft"]))
                res.add(f"{tag}.values.slr_ft_set", tset == rset, tset, rset)
            _cmp_layer(res, tl, rl)

        # --- empty (inland bbox): header-only FGB, no raise ---
        tw_sink, rt_sink = {}, {}
        tl = _retry(lambda: _run_twin(slr_mod, "fetch_noaa_slr_scenarios", dict(bbox=_SLR_INLAND), tw_sink))
        rl = _retry(lambda: route_layer(spec, dict(bbox=list(_SLR_INLAND)), rt_sink))
        tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
        res.add("empty.values.n", tw["n"] == rt["n"], tw["n"], rt["n"],
                note=f"twin={tw['n']} router={rt['n']} (parity; header-only when 0)")
        res.add("empty.schema.columns", tw["columns"] == rt["columns"], sorted(tw["columns"]), sorted(rt["columns"]))

        # --- forced upstream: patch httpx.Client.get for both sides ---
        def _boom(self, *a, **k):
            raise httpx.ConnectError("forced upstream")
        with _patched(mock.patch.object(httpx.Client, "get", _boom)):
            tw_u = _capture_err(_run_twin, slr_mod, "fetch_noaa_slr_scenarios", dict(bbox=_SLR_COAST, scenario_ft=1.0), {})
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_SLR_COAST), scenario_ft=1.0), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges (pre-network) ---
        for ename, tk, rp in [
            ("error.bad_bbox", dict(bbox=(-82.0, 26.4, -82.0, 26.5)), dict(bbox=[-82.0, 26.4, -82.0, 26.5])),
            ("error.bad_scenario", dict(bbox=_SLR_COAST, scenario_ft=11.0), dict(bbox=list(_SLR_COAST), scenario_ft=11.0)),
        ]:
            tw_x = _capture_err(_run_twin, slr_mod, "fetch_noaa_slr_scenarios", tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def run_levees(specs) -> SourceResult:
    res = SourceResult("fetch_usace_levees")
    spec = specs["fetch_usace_levees"]
    import httpx
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(lev_mod.fetch_usace_levees),
                note="spec.docstring == inspect.getdoc(twin)")

        # --- happy per sub-layer (leveed_areas + system_routes) ---
        for layer in ("leveed_areas", "system_routes"):
            tw_sink, rt_sink = {}, {}
            tl = _retry(lambda: _run_twin(lev_mod, "fetch_usace_levees", dict(bbox=_NOLA, layer=layer), tw_sink))
            rl = _retry(lambda: route_layer(spec, dict(bbox=list(_NOLA), layer=layer), rt_sink))
            tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
            _cmp_values(res, layer, tw, rt, "SYSTEM_ID")
            _cmp_layer(res, tl, rl)

        # --- empty (ocean bbox): header-only FGB ---
        tw_sink, rt_sink = {}, {}
        _retry(lambda: _run_twin(lev_mod, "fetch_usace_levees", dict(bbox=_OCEAN, layer="leveed_areas"), tw_sink))
        _retry(lambda: route_layer(spec, dict(bbox=list(_OCEAN), layer="leveed_areas"), rt_sink))
        tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
        res.add("empty.values.n", tw["n"] == rt["n"] == 0, tw["n"], rt["n"])
        res.add("empty.schema.columns", tw["columns"] == rt["columns"], sorted(tw["columns"]), sorted(rt["columns"]))

        # --- forced upstream ---
        def _boom(self, *a, **k):
            raise httpx.ConnectError("forced upstream")
        with _patched(mock.patch.object(httpx.Client, "get", _boom)):
            tw_u = _capture_err(_run_twin, lev_mod, "fetch_usace_levees", dict(bbox=_NOLA, layer="leveed_areas"), {})
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_NOLA), layer="leveed_areas"), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges ---
        for ename, tk, rp in [
            ("error.bad_bbox", dict(bbox=(-90.1, 29.9, -90.1, 30.0), layer="leveed_areas"),
             dict(bbox=[-90.1, 29.9, -90.1, 30.0], layer="leveed_areas")),
            ("error.bad_layer", dict(bbox=_NOLA, layer="not_a_layer"),
             dict(bbox=list(_NOLA), layer="not_a_layer")),
        ]:
            tw_x = _capture_err(_run_twin, lev_mod, "fetch_usace_levees", tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


_HOUSTON = (-95.14, 29.72, -95.06, 29.78)     # Houston Ship Channel -- EJScreen block groups


def run_ejscreen(specs) -> SourceResult:
    res = SourceResult("fetch_epa_ejscreen")
    spec = specs["fetch_epa_ejscreen"]
    import httpx
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(ej_mod.fetch_epa_ejscreen),
                note="spec.docstring == inspect.getdoc(twin)")

        # --- happy: default pm25 + a routed indicator (diesel) ---
        for tag, ind in [("pm25", None), ("diesel", "diesel")]:
            kw_t = dict(bbox=_HOUSTON) if ind is None else dict(bbox=_HOUSTON, indicator=ind)
            kw_r = dict(bbox=list(_HOUSTON)) if ind is None else dict(bbox=list(_HOUSTON), indicator=ind)
            tw_sink, rt_sink = {}, {}
            tl = _retry(lambda: _run_twin(ej_mod, "fetch_epa_ejscreen", kw_t, tw_sink))
            rl = _retry(lambda: route_layer(spec, kw_r, rt_sink))
            tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
            _cmp_values(res, tag, tw, rt, "bg_id")
            # value column parity (the from_param-selected percentile) + indicator echo
            if "value" in tw["gdf"].columns and "value" in rt["gdf"].columns and len(tw["gdf"]):
                tv = sorted(round(float(x), 4) for x in tw["gdf"]["value"].dropna())
                rv = sorted(round(float(x), 4) for x in rt["gdf"]["value"].dropna())
                res.add(f"{tag}.values.value_col", tv == rv, len(tv), len(rv),
                        note="" if tv == rv else "selected-indicator percentile diverges")
                res.add(f"{tag}.values.indicator_echo",
                        set(tw["gdf"]["indicator"]) == set(rt["gdf"]["indicator"]),
                        set(tw["gdf"]["indicator"]), set(rt["gdf"]["indicator"]))
            _cmp_layer(res, tl, rl)

        # --- empty (ocean bbox): header-only FGB with the full 21-col schema ---
        tw_sink, rt_sink = {}, {}
        _retry(lambda: _run_twin(ej_mod, "fetch_epa_ejscreen", dict(bbox=_OCEAN), tw_sink))
        _retry(lambda: route_layer(spec, dict(bbox=list(_OCEAN)), rt_sink))
        tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
        res.add("empty.values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
        res.add("empty.schema.columns", tw["columns"] == rt["columns"], sorted(tw["columns"]), sorted(rt["columns"]))

        # --- forced upstream ---
        def _boom(self, *a, **k):
            raise httpx.ConnectError("forced upstream")
        with _patched(mock.patch.object(httpx.Client, "get", _boom)):
            tw_u = _capture_err(_run_twin, ej_mod, "fetch_epa_ejscreen", dict(bbox=_HOUSTON), {})
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_HOUSTON)), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges ---
        for ename, tk, rp in [
            ("error.bad_bbox", dict(bbox=(-95.1, 29.7, -95.1, 29.8)), dict(bbox=[-95.1, 29.7, -95.1, 29.8])),
            ("error.bad_indicator", dict(bbox=_HOUSTON, indicator="not_real"), dict(bbox=list(_HOUSTON), indicator="not_real")),
        ]:
            tw_x = _capture_err(_run_twin, ej_mod, "fetch_epa_ejscreen", tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def run_all() -> list[SourceResult]:
    specs = load_specs()
    out = []
    if slr_mod is not None:
        out.append(run_slr(specs))
    if lev_mod is not None:
        out.append(run_levees(specs))
    if ej_mod is not None:
        out.append(run_ejscreen(specs))
    return out


def main() -> int:
    results = run_all()
    out = HERE / "results" / "VERDICT_wave6.md"
    out.parent.mkdir(exist_ok=True)
    lines = ["# Replication-parity VERDICT -- phase-2 wave-6 VECTOR/ZIP family (ADR 0052)",
             "",
             "LIVE twin-vs-router over the same real NOAA OCM SLR + USACE NLD endpoints.",
             "slr = declarative fan-out; levees = endpoint_by_param + properties_by_param.",
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
    print("=== wave-6 replication-parity ===")
    for r in results:
        div = [c for c in r.checks if not c.ok]
        tail = "" if not div else " | " + "; ".join(c.note or c.name for c in div)
        print(f"- {r.source}: {r.verdict} ({r.n_ok}/{r.n_total}){tail}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
