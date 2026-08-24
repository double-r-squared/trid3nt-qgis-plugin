"""Replication-parity drivers -- phase-2 wave-7 RASTER imageserver_export (ADR 0053).

LIVE twin-vs-router: both the hand-written TWIN and the spec-driven ROUTER hit the
SAME LANDFIRE LF2022 ImageServer exportImage endpoint for the SAME request in one
run, so any values/layer/error divergence is attributable to the fold (the new
imageserver_export access mode + style_preset_by_param + units_by_param). The twin
speaks ``requests``; the router speaks the httpx transport -- the forced-upstream
edge patches BOTH. read_through is stubbed (no MinIO). Twin behavior is the contract.

Run: ``python drivers_wave7.py`` (needs outbound network to lfps.usgs.gov).
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
    from trid3nt_server.tools.fetchers.hazard.fetch_landfire_fuels import (  # noqa: E402
        fetch_landfire_fuels as lf_mod,
    )
except ImportError:
    lf_mod = None
try:
    from trid3nt_server.tools.fetchers.hazard.fetch_usfs_canopy_fuels import (  # noqa: E402
        fetch_usfs_canopy_fuels as us_mod,
    )
except ImportError:
    us_mod = None
try:
    from trid3nt_server.tools.fetchers.climate.fetch_modis_lst import (  # noqa: E402
        fetch_modis_lst as modis_mod,
    )
except ImportError:
    modis_mod = None

_AZ = (-112.02, 34.50, -111.96, 34.56)   # north-central Arizona forest -- CONUS coverage


def _all_nodata_tiff() -> bytes:
    """A synthetic all-(-32768) S16 GeoTIFF -- the all-nodata coverage the live
    ImageServer never returns (it resamples data even over water). Feeding the
    SAME bytes to twin + router proves the EMPTY-gate parity deterministically."""
    import io
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    arr = np.full((32, 32), -32768, dtype="int16")
    prof = dict(driver="GTiff", height=32, width=32, count=1, dtype="int16",
                crs="EPSG:4326", nodata=-32768,
                transform=from_bounds(-112.02, 34.50, -111.96, 34.56, 32, 32))
    buf = io.BytesIO()
    with rasterio.open(buf, "w", **prof) as dst:
        dst.write(arr, 1)
    return buf.getvalue()


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


def _cmp_layer(res: SourceResult, tag: str, tl, rl) -> None:
    res.add(f"{tag}.layer.type", tl.layer_type == rl.layer_type, tl.layer_type, rl.layer_type)
    res.add(f"{tag}.layer.style_preset", tl.style_preset == rl.style_preset, tl.style_preset, rl.style_preset)
    res.add(f"{tag}.layer.role", tl.role == rl.role, tl.role, rl.role)
    res.add(f"{tag}.layer.units", tl.units == rl.units, tl.units, rl.units)
    tb, rb = tl.bbox is not None, rl.bbox is not None
    res.add(f"{tag}.layer.bbox_present", tb == rb, tb, rb)


def _cmp_values(res: SourceResult, tag: str, tw: dict, rt: dict) -> None:
    for k in ("band_count", "dtype", "crs", "nodata", "bounds", "min", "max", "mean"):
        res.add(f"{tag}.values.{k}", tw[k] == rt[k], tw[k], rt[k])


def _retry(fn, tries=3):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- ImageServer flakes transiently
            last = exc
    raise last


def _run_twin(mod, fn_name, kwargs, sink):
    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(mod, "read_through", stub)):
        return getattr(mod, fn_name)(**kwargs)


def _run_source(specs, mod, fn_name, spec_name, layers, empty_code) -> SourceResult:
    res = SourceResult(fn_name)
    spec = specs[spec_name]
    import httpx
    import requests
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(getattr(mod, fn_name)),
                note="spec.docstring == inspect.getdoc(twin)")
        res.add("caveats.reproduced", any(empty_code in c for c in spec.caveats),
                note="typed empty-caveat present")

        # --- happy per layer ---
        for layer in layers:
            tw_sink, rt_sink = {}, {}
            tl = _retry(lambda: _run_twin(mod, fn_name, dict(bbox=_AZ, layer=layer), tw_sink))
            rl = _retry(lambda: route_layer(spec, dict(bbox=list(_AZ), layer=layer), rt_sink))
            tw, rt = raster_stats(tw_sink["bytes"]), raster_stats(rt_sink["bytes"])
            _cmp_values(res, layer, tw, rt)
            _cmp_layer(res, layer, tl, rl)

        # --- empty (synthetic all-nodata TIFF fed to BOTH sides): typed EMPTY ---
        from trid3nt_server.tools.fetchers._router import transport as _tp
        nod = _all_nodata_tiff()

        class _FakeResp:
            status_code = 200
            content = nod
            headers = {"Content-Type": "image/tiff"}
        with _patched(mock.patch.object(requests, "get", lambda *a, **k: _FakeResp())):
            tw_e = _capture_err(_run_twin, mod, fn_name, dict(bbox=_AZ, layer=layers[0]), {})
        with _patched(mock.patch.object(_tp, "get_bytes", lambda *a, **k: (nod, "image/tiff", "x"))):
            rt_e = _capture_err(route_layer, spec, dict(bbox=list(_AZ), layer=layers[0]), {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        # --- forced upstream: patch requests.get (twin) + httpx.Client.get (router) ---
        def _boom_req(*a, **k):
            raise requests.ConnectionError("forced upstream")
        def _boom_httpx(self, *a, **k):
            raise httpx.ConnectError("forced upstream")
        with _patched(mock.patch.object(requests, "get", _boom_req)):
            tw_u = _capture_err(_run_twin, mod, fn_name, dict(bbox=_AZ, layer=layers[0]), {})
        with _patched(mock.patch.object(httpx.Client, "get", _boom_httpx)):
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_AZ), layer=layers[0]), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges (pre-network) ---
        for ename, bad in [
            ("error.bad_bbox", dict(bbox=(-112.0, 34.5, -112.0, 34.6), layer=layers[0])),
            ("error.bad_layer", dict(bbox=_AZ, layer="not_a_layer")),
        ]:
            tk = dict(bad)
            rp = dict(bad); rp["bbox"] = list(rp["bbox"])
            tw_x = _capture_err(_run_twin, mod, fn_name, tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


_PHX = (-112.30, 33.30, -111.80, 33.60)   # Phoenix metro desert -- strong LST, CONUS
_OCEAN2 = (-140.0, 20.0, -139.9, 20.1)    # open Pacific -- all-fill LST -> NO_DATA


def run_modis(specs) -> SourceResult:
    res = SourceResult("fetch_modis_lst")
    spec = specs["fetch_modis_lst"]
    import pystac_client
    try:
        res.add("schema.docstring_verbatim",
                spec.docstring == inspect.getdoc(modis_mod.fetch_modis_lst),
                note="spec.docstring == inspect.getdoc(twin)")
        res.add("caveats.reproduced", any("MODIS_LST_NO_DATA" in c for c in spec.caveats),
                note="typed no-data caveat present")

        # --- happy: default 11A2 day + a routed (21A2? night) variant ---
        for tag, kw in [("day", dict(bbox=_PHX)), ("night", dict(bbox=_PHX, daynight="night"))]:
            kw_t = dict(kw); kw_r = dict(kw); kw_r["bbox"] = list(kw["bbox"])
            tw_sink, rt_sink = {}, {}
            tl = _retry(lambda: _run_twin(modis_mod, "fetch_modis_lst", kw_t, tw_sink))
            rl = _retry(lambda: route_layer(spec, kw_r, rt_sink))
            tw, rt = raster_stats(tw_sink["bytes"]), raster_stats(rt_sink["bytes"])
            _cmp_values(res, tag, tw, rt)
            _cmp_layer(res, tag, tl, rl)

        # --- no-data (ocean all-fill): both raise the typed NO_DATA error ---
        tw_e = _capture_err(_run_twin, modis_mod, "fetch_modis_lst", dict(bbox=_OCEAN2), {})
        rt_e = _capture_err(route_layer, spec, dict(bbox=list(_OCEAN2)), {})
        _cmp_error(res, "error.no_data", tw_e, rt_e)

        # --- forced upstream: patch pystac_client.Client.open for both ---
        def _boom_open(*a, **k):
            raise ConnectionError("forced upstream")
        with _patched(mock.patch.object(pystac_client.Client, "open", staticmethod(_boom_open))):
            tw_u = _capture_err(_run_twin, modis_mod, "fetch_modis_lst", dict(bbox=_PHX), {})
            rt_u = _capture_err(route_layer, spec, dict(bbox=list(_PHX)), {})
        _cmp_error(res, "error.upstream", tw_u, rt_u)

        # --- invalid-param edges (pre-network) ---
        for ename, tk, rp in [
            ("error.bad_bbox", dict(bbox=(-112.0, 33.3, -112.0, 33.6)), dict(bbox=[-112.0, 33.3, -112.0, 33.6])),
            ("error.over_area", dict(bbox=(-115.0, 30.0, -108.0, 36.0)), dict(bbox=[-115.0, 30.0, -108.0, 36.0])),
            ("error.bad_product", dict(bbox=_PHX, product="not_real"), dict(bbox=list(_PHX), product="not_real")),
            ("error.bad_daynight", dict(bbox=_PHX, daynight="dusk"), dict(bbox=list(_PHX), daynight="dusk")),
        ]:
            tw_x = _capture_err(_run_twin, modis_mod, "fetch_modis_lst", tk, {})
            rt_x = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw_x, rt_x)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def run_all() -> list[SourceResult]:
    specs = load_specs()
    out = []
    if modis_mod is not None:
        out.append(run_modis(specs))
    if lf_mod is not None:
        out.append(_run_source(specs, lf_mod, "fetch_landfire_fuels",
                               "fetch_landfire_fuels", ["fbfm40", "cbh"], "LANDFIRE_FUELS_EMPTY"))
    if us_mod is not None:
        out.append(_run_source(specs, us_mod, "fetch_usfs_canopy_fuels",
                               "fetch_usfs_canopy_fuels", ["cbh", "cbd"], "USFS_CANOPY_FUELS_EMPTY"))
    return out


def main() -> int:
    results = run_all()
    out = HERE / "results" / "VERDICT_wave7.md"
    out.parent.mkdir(exist_ok=True)
    lines = ["# Replication-parity VERDICT -- phase-2 wave-7 RASTER imageserver_export (ADR 0053)",
             "", "LIVE twin-vs-router over the same LANDFIRE LF2022 ImageServer exportImage.", "",
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
    print("=== wave-7 replication-parity ===")
    for r in results:
        div = [c for c in r.checks if not c.ok]
        tail = "" if not div else " | " + "; ".join(c.note or c.name for c in div)
        print(f"- {r.source}: {r.verdict} ({r.n_ok}/{r.n_total}){tail}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
