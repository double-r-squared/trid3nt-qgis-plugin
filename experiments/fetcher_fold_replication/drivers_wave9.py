"""Replication-parity drivers -- phase-2 wave-9 (multi_url VRT fan-out + gzip_object).

LIVE twin-vs-router: the hand-written TWIN and the spec-driven ROUTER hit the SAME
live source (Meta HRSL VRT on AWS Open Data; UCSB CHC CHIRPS archive) for the SAME
request in one run, so any values/layer/error divergence is attributable to the
fold (the new multi_url + gzip_object access modes). read_through is stubbed (no
MinIO). Twin behavior is the contract. ADR 0055.

Run: ``python drivers_wave9.py`` (needs outbound network to s3.amazonaws.com +
data.chc.ucsb.edu).
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
    raster_stats,
    route_layer,
    _make_stub_read_through,
    _patched,
)

try:
    from trid3nt_server.data.fetchers.socioeconomic.fetch_hrsl_population import (  # noqa: E402
        fetch_hrsl_population as hrsl_mod,
    )
except ImportError:
    hrsl_mod = None
try:
    from trid3nt_server.data.fetchers.climate.fetch_chirps_precipitation import (  # noqa: E402
        fetch_chirps_precipitation as chirps_mod,
    )
except ImportError:
    chirps_mod = None


def _capture_err(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return {"type": None, "error_code": None, "retryable": None, "raised": False}
    except BaseException as exc:  # noqa: BLE001
        d = err_frame(exc)
        d["raised"] = True
        return d


def _cmp_error(res: SourceResult, name: str, tw: dict, rt: dict, *, reported=False) -> None:
    both = tw.get("raised") and rt.get("raised")
    code = tw.get("error_code") == rt.get("error_code")
    retry = tw.get("retryable") == rt.get("retryable")
    ok = bool(both and code and retry)
    note = ""
    if both and not code:
        note = f"error_code diverges: twin={tw['error_code']} router={rt['error_code']}"
    elif not both:
        note = f"raise mismatch: twin={tw.get('raised')} router={rt.get('raised')}"
    res.add(name, ok, tw.get("error_code"), rt.get("error_code"), note=note, reported=reported)


def _cmp_layer(res: SourceResult, tag: str, tl, rl) -> None:
    res.add(f"{tag}.layer.type", tl.layer_type == rl.layer_type, tl.layer_type, rl.layer_type)
    res.add(f"{tag}.layer.style_preset", tl.style_preset == rl.style_preset, tl.style_preset, rl.style_preset)
    res.add(f"{tag}.layer.role", tl.role == rl.role, tl.role, rl.role)
    res.add(f"{tag}.layer.units", tl.units == rl.units, tl.units, rl.units)


def _cmp_values(res: SourceResult, tag: str, tw: dict, rt: dict) -> None:
    for k in ("band_count", "dtype", "crs", "nodata", "bounds", "min", "max", "mean"):
        res.add(f"{tag}.values.{k}", tw[k] == rt[k], tw[k], rt[k])


def _retry(fn, tries=3):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- transient upstream flake
            last = exc
    raise last


def _run_twin(mod, fn_name, kwargs, sink):
    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(mod, "read_through", stub)):
        return getattr(mod, fn_name)(**kwargs)


_FM = (-81.95, 26.30, -81.70, 26.70)      # Fort Myers FL -- dense HRSL coverage
_OCEAN = (-140.0, 20.0, -139.9, 20.1)     # open Pacific -- all-nodata -> EMPTY


def run_hrsl(specs) -> SourceResult:
    res = SourceResult("fetch_hrsl_population")
    spec = specs["fetch_hrsl_population"]
    import rasterio
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(hrsl_mod.fetch_hrsl_population),
                note="spec.docstring == inspect.getdoc(twin)")
        res.add("caveats.reproduced", any("HRSL_EMPTY" in c for c in spec.caveats),
                note="typed empty-caveat present")

        # --- happy: value-identical windowed mosaic over Fort Myers ---
        tw_sink, rt_sink = {}, {}
        tl = _retry(lambda: _run_twin(hrsl_mod, "fetch_hrsl_population", dict(bbox=_FM), tw_sink))
        rl = _retry(lambda: route_layer(spec, dict(bbox=list(_FM)), rt_sink))
        _cmp_values(res, "hrsl", raster_stats(tw_sink["bytes"]), raster_stats(rt_sink["bytes"]))
        _cmp_layer(res, "hrsl", tl, rl)

        # --- empty (ocean all-nodata window): typed EMPTY both ---
        tw_e = _capture_err(_run_twin, hrsl_mod, "fetch_hrsl_population", dict(bbox=_OCEAN), {})
        rt_e = _capture_err(route_layer, spec, dict(bbox=list(_OCEAN)), {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        # --- forced upstream: twin rasterio.open raises; router transport get_bytes raises ---
        from trid3nt_server.data.fetchers._router import transport as _tp
        def _boom_open(*a, **k):
            raise OSError("forced upstream")
        def _boom_bytes(*a, **k):
            raise _tp.TransportUpstreamError("forced upstream")
        with _patched(mock.patch.object(rasterio, "open", _boom_open)):
            tw_u = _capture_err(_run_twin, hrsl_mod, "fetch_hrsl_population", dict(bbox=_FM), {})
        with _patched(mock.patch.object(_tp, "get_bytes", _boom_bytes)):
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_FM)), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- bad bbox (degenerate): both INPUT_INVALID ---
        tw_b = _capture_err(_run_twin, hrsl_mod, "fetch_hrsl_population", dict(bbox=(-81.9, 26.3, -81.9, 26.7)), {})
        rt_b = _capture_err(route_layer, spec, dict(bbox=[-81.9, 26.3, -81.9, 26.7]), {})
        _cmp_error(res, "error.bad_bbox", tw_b, rt_b)

        # --- bbox=None: KNOWN flagged divergence (twin BBOX_REQUIRED unprefixed vs
        #     router HRSL_INPUT_INVALID -- the ADR-0047 unprefixed-error-code schema
        #     gap). Reported, non-gating. ---
        tw_n = _capture_err(_run_twin, hrsl_mod, "fetch_hrsl_population", dict(bbox=None), {})
        rt_n = _capture_err(route_layer, spec, dict(bbox=None), {})
        _cmp_error(res, "error.bbox_none", tw_n, rt_n, reported=True)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


_WG = (72.0, 15.0, 78.0, 21.0)            # Western Ghats -- strong monsoon CHIRPS


def run_chirps(specs) -> SourceResult:
    res = SourceResult("fetch_chirps_precipitation")
    spec = specs["fetch_chirps_precipitation"]
    from trid3nt_server.data.fetchers._router import transport as _tp
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(chirps_mod.fetch_chirps_precipitation),
                note="spec.docstring == inspect.getdoc(twin)")
        res.add("caveats.reproduced", any("CHIRPS_EMPTY" in c for c in spec.caveats),
                note="typed empty-caveat present")

        # --- happy monthly + daily: value-identical ---
        for tag, kw in [("monthly", dict(date="2023-07", period="monthly")),
                        ("daily", dict(date="2022-08-25", period="daily"))]:
            tw_sink, rt_sink = {}, {}
            kt = dict(kw, bbox=_WG); kr = dict(kw, bbox=list(_WG))
            _retry(lambda: _run_twin(chirps_mod, "fetch_chirps_precipitation", kt, tw_sink))
            _retry(lambda: route_layer(spec, kr, rt_sink))
            _cmp_values(res, tag, raster_stats(tw_sink["bytes"]), raster_stats(rt_sink["bytes"]))

        # --- global (bbox=None): value-identical full grid ---
        tw_sink, rt_sink = {}, {}
        tl = _retry(lambda: _run_twin(chirps_mod, "fetch_chirps_precipitation", dict(date="2023-07"), tw_sink))
        rl = _retry(lambda: route_layer(spec, dict(date="2023-07"), rt_sink))
        _cmp_values(res, "global", raster_stats(tw_sink["bytes"]), raster_stats(rt_sink["bytes"]))
        _cmp_layer(res, "global", tl, rl)

        # --- not-available (forced 404): twin CHIRPS_NOT_AVAILABLE / router same ---
        import urllib.error, urllib.request
        def _boom_404(*a, **k):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        def _boom_nf(*a, **k):
            raise _tp.TransportNotFound("forced 404")
        with _patched(mock.patch.object(urllib.request, "urlopen", _boom_404)):
            tw_na = _capture_err(_run_twin, chirps_mod, "fetch_chirps_precipitation", dict(bbox=_WG, date="2023-07"), {})
        with _patched(mock.patch.object(_tp, "get_bytes", _boom_nf)):
            rt_na = _capture_err(route_layer, spec, dict(bbox=list(_WG), date="2023-07"), {})
        _cmp_error(res, "error.not_available", tw_na, rt_na)

        # --- forced upstream (network error): retryable UPSTREAM both ---
        def _boom_url(*a, **k):
            raise urllib.error.URLError("forced upstream")
        def _boom_up(*a, **k):
            raise _tp.TransportUpstreamError("forced upstream")
        with _patched(mock.patch.object(urllib.request, "urlopen", _boom_url)):
            tw_u = _capture_err(_run_twin, chirps_mod, "fetch_chirps_precipitation", dict(bbox=_WG, date="2023-07"), {})
        with _patched(mock.patch.object(_tp, "get_bytes", _boom_up)):
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_WG), date="2023-07"), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- empty (ocean all-nodata): typed EMPTY both ---
        tw_e = _capture_err(_run_twin, chirps_mod, "fetch_chirps_precipitation", dict(bbox=(-140.0, 5.0, -139.5, 5.5), date="2023-07"), {})
        rt_e = _capture_err(route_layer, spec, dict(bbox=[-140.0, 5.0, -139.5, 5.5], date="2023-07"), {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        # --- invalid edges: bad date / future / bad bbox -> INPUT_ERROR both ---
        for ename, kt, kr in [
            ("error.bad_date", dict(bbox=_WG, date="not-a-date"), dict(bbox=list(_WG), date="not-a-date")),
            ("error.future", dict(bbox=_WG, date="2099-01"), dict(bbox=list(_WG), date="2099-01")),
            ("error.bad_bbox", dict(bbox=(72.0, 15.0, 72.0, 21.0), date="2023-07"), dict(bbox=[72.0, 15.0, 72.0, 21.0], date="2023-07")),
        ]:
            tw_x = _capture_err(_run_twin, chirps_mod, "fetch_chirps_precipitation", kt, {})
            rt_x = _capture_err(route_layer, spec, kr, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def run_all() -> list[SourceResult]:
    specs = load_specs()
    out = []
    if hrsl_mod is not None:
        out.append(run_hrsl(specs))
    if chirps_mod is not None:
        out.append(run_chirps(specs))
    return out


def main() -> int:
    results = run_all()
    out = HERE / "results" / "VERDICT_wave9.md"
    out.parent.mkdir(exist_ok=True)
    lines = ["# Replication-parity VERDICT -- phase-2 wave-9 multi_url + gzip_object (ADR 0055)",
             "", "LIVE twin-vs-router over Meta HRSL VRT (AWS) + UCSB CHIRPS archive.", "",
             "| source | verdict | checks |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r.source} | {r.verdict} | {r.n_ok}/{r.n_total} |")
    lines += ["", "## Per-check detail", ""]
    for r in results:
        lines.append(f"### {r.source} -- {r.verdict}")
        if r.error:
            lines.append(f"- ERROR: {r.error}")
        for c in r.checks:
            mark = "ok" if c.ok else ("RP" if c.reported else "XX")
            note = f" -- {c.note}" if c.note else ""
            if c.ok:
                lines.append(f"- [{mark}] {c.name}{note}")
            else:
                lines.append(f"- [{mark}] {c.name}: twin={c.twin!r} router={c.router!r}{note}")
        lines.append("")
    out.write_text("\n".join(lines))
    print("=== wave-9 replication-parity ===")
    for r in results:
        div = [c for c in r.checks if not c.ok and not c.reported]
        tail = "" if not div else " | " + "; ".join(c.note or c.name for c in div)
        print(f"- {r.source}: {r.verdict} ({r.n_ok}/{r.n_total}){tail}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
