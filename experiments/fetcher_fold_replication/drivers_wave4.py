"""Replication-parity drivers -- phase-2 wave-4 station family (ADR 0045).

Self-contained per-wave driver (the wave-3 pattern). The CO-OPS currents TWIN
uses ``urllib`` (``_http_get``) while the ROUTER uses ``httpx``; the two speak
different HTTP stacks, so -- like wave-3 -- there is no single mockable upstream.
The gate is a LIVE twin-vs-router comparison: both hit the SAME real CO-OPS
endpoints for the SAME request in the same run, so any values/schema/layer/error
divergence is attributable to the fold (the ``coops_currents`` snapshot transform
+ the ``emit: snapshot`` router mode), not to data drift.

Robustness note: OBSERVED currents update every ~6 min, so the exact latest
scalar can drift by one timestep between the twin and router calls (seconds
apart). The GATING value check is therefore the STATION-ID SET (deterministic:
same catalog + same bbox -> identical set); the scalar speed/direction is an
``info.*`` (non-gating) spotcheck recorded honestly.

Twin behavior is the contract. Run: ``python drivers_wave4.py`` (needs outbound
network to CO-OPS; read_through is stubbed, no MinIO).
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

# Twin module (present until promotion; this gate runs BEFORE the cut).
from trid3nt_server.agent.tools.fetchers.ocean.fetch_noaa_coops_currents import (  # noqa: E402
    fetch_noaa_coops_currents as cur_mod,
)

_SF_BAY = (-123.0, 37.4, -122.0, 38.2)   # 4 CO-OPS current stations
_OCEAN = (-140.0, 20.0, -139.9, 20.1)    # no current stations -> typed EMPTY


def _run_twin(kwargs: dict, sink: dict):
    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(cur_mod, "read_through", stub)):
        return cur_mod.fetch_noaa_coops_currents(**kwargs)


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
        except Exception as exc:  # noqa: BLE001 -- CO-OPS datagetter flakes transiently
            last = exc
    raise last


def _ids(info) -> set[str]:
    g = info["gdf"]
    return set(str(x) for x in g["station_id"]) if "station_id" in g.columns else set()


def _happy(res: SourceResult, spec, product: str, prefix: str) -> None:
    """Live twin-vs-router happy path for one product."""
    tw_sink, rt_sink = {}, {}
    tl = _retry(lambda: _run_twin(dict(bbox=_SF_BAY, product=product), tw_sink))
    rl = _retry(lambda: route_layer(spec, dict(bbox=list(_SF_BAY), product=product), rt_sink))
    tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
    res.add(f"{prefix}.values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
    res.add(f"{prefix}.values.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
    res.add(f"{prefix}.values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
    res.add(f"{prefix}.schema.columns", tw["columns"] == rt["columns"], sorted(tw["columns"]), sorted(rt["columns"]))
    tw_ids, rt_ids = _ids(tw), _ids(rt)
    res.add(f"{prefix}.values.station_id_set", tw_ids == rt_ids, sorted(tw_ids), sorted(rt_ids),
            note="" if tw_ids == rt_ids else f"station set diverges (twin-only {sorted(tw_ids-rt_ids)}, router-only {sorted(rt_ids-tw_ids)})")
    # non-gating scalar spotcheck (observed drifts by a timestep between calls).
    def _sp(info):
        g = info["gdf"]
        return sorted(round(float(v), 3) for v in g["speed_kn"]) if "speed_kn" in g.columns and len(g) else []
    res.add(f"info.{prefix}.speed_spotcheck", _sp(tw) == _sp(rt), _sp(tw), _sp(rt),
            note="non-gating: observed speeds may drift 1 timestep between twin/router calls")
    _cmp_layer(res, tl, rl)


def run_currents(specs) -> SourceResult:
    res = SourceResult("fetch_noaa_coops_currents")
    spec = specs["fetch_noaa_coops_currents"]
    import httpx
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(cur_mod.fetch_noaa_coops_currents),
                note="spec.docstring == inspect.getdoc(twin)")

        # --- happy: observed + predictions, live twin(urllib) vs router(httpx) ---
        _happy(res, spec, "currents", "obs")
        _happy(res, spec, "currents_predictions", "pred")

        res.add("caveats.reproduced", any("COOPS_CURRENTS_EMPTY" in c for c in spec.caveats),
                note="typed COOPS_CURRENTS_EMPTY caveat present")

        # --- empty (ocean bbox): both raise COOPS_CURRENTS_EMPTY ---
        tw_e = _capture_err(_run_twin, dict(bbox=_OCEAN, product="currents"), {})
        rt_e = _capture_err(route_layer, spec, dict(bbox=list(_OCEAN), product="currents"), {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        # --- forced upstream (each side's HTTP layer raises) ---
        err = cur_mod.COOPSCurrentsUpstreamError("forced upstream")
        with _patched(mock.patch.object(cur_mod, "_http_get", mock.Mock(side_effect=err)),
                      mock.patch.object(cur_mod, "read_through", _make_stub_read_through({}))):
            tw_u = _capture_err(lambda: cur_mod.fetch_noaa_coops_currents(bbox=_SF_BAY, product="currents"))

        def _boom(self, *a, **k):
            raise httpx.ConnectError("forced upstream")
        with _patched(mock.patch.object(httpx.Client, "get", _boom)):
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_SF_BAY), product="currents"), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges (pre-network) ---
        for ename, tk, rp in [
            ("error.bad_bbox", dict(bbox=(-122.5, 37.4, -122.5, 38.2), product="currents"),
             dict(bbox=[-122.5, 37.4, -122.5, 38.2], product="currents")),
            ("error.bad_product", dict(bbox=_SF_BAY, product="water_level"),
             dict(bbox=list(_SF_BAY), product="water_level")),
        ]:
            tw_x = _capture_err(_run_twin, tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def run_all() -> list[SourceResult]:
    specs = load_specs()
    return [run_currents(specs)]


def main() -> int:
    results = run_all()
    out = HERE / "results" / "VERDICT_wave4.md"
    out.parent.mkdir(exist_ok=True)
    lines = ["# Replication-parity VERDICT -- phase-2 wave-4 station family (ADR 0045)",
             "",
             "LIVE twin(urllib) vs router(httpx, `emit: snapshot` + `coops_currents`",
             "transform) over the same real CO-OPS endpoints. Twin behavior is the",
             "contract. Gating value check = station-id SET (observed scalars drift a",
             "timestep between calls, recorded as info.*).",
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
    print("=== wave-4 replication-parity ===")
    for r in results:
        div = [c for c in r.checks if not c.ok]
        tail = "" if not div else " | " + "; ".join(c.note or c.name for c in div)
        print(f"- {r.source}: {r.verdict} ({r.n_ok}/{r.n_total}){tail}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
