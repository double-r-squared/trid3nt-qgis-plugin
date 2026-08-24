"""fetch_aorc_precip record fold (ADR 0203): the AORC v1.1 hyetograph via the router.

Offline: a synthetic xarray Dataset stands in for the AORC Zarr year store (the
``aorc_precip._open_year`` I/O seam is monkeypatched), and the in-memory read_through
injector caches the record dict -- the real anonymous-s3fs Zarr path is unchanged
(exercised live). Covers the spec shape, the pure-record path (route() -> dict), the
AOI-mean hyetograph math, the coverage NOT_AVAILABLE gates, and the empty-window gate.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterNotAvailableError,
)
from trid3nt_server.tools.fetchers._router.hooks import aorc_precip as ap
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

AORC_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/source.yaml"
)

# Coweeta fork bbox (the ADR 0203 proof AOI).
_BBOX = [-83.48, 35.02, -83.42, 35.08]


def _synthetic_year(year: int, ndays: int = 3) -> Any:
    """A tiny AORC-shaped Dataset: hourly APCP_surface over a small lat/lon grid.

    Cell (i,j) at hour t gets precip = t-th ramp so the AOI-mean is deterministic:
    every cell carries the same 1.0 mm/hr except a 5.0 mm/hr spike at hour 10.
    """
    xr = pytest.importorskip("xarray")
    nt = ndays * 24
    lat = np.array([35.03, 35.04, 35.05, 35.06, 35.07])
    lon = np.array([-83.47, -83.46, -83.45, -83.44, -83.43])
    times = np.array(
        [np.datetime64(f"{year}-06-01T00:00:00") + np.timedelta64(h, "h") for h in range(nt)]
    )
    data = np.ones((nt, lat.size, lon.size), dtype="float32")
    data[10, :, :] = 5.0  # the hyetograph peak hour
    da = xr.DataArray(
        data,
        dims=("time", "latitude", "longitude"),
        coords={"time": times, "latitude": lat, "longitude": lon},
        attrs={"units": "kg/m^2"},
    )
    return xr.Dataset({"APCP_surface": da})


def _inject_read_through(monkeypatch, store: dict[str, bytes]):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path, compute_cache_key, is_cacheable,
    )
    now = _dt.datetime(2026, 8, 8, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def patched(metadata, params, ext, fetch_fn, **kw):
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        source_id = metadata.source_class or metadata.name
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{CACHE_BUCKET}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(router, "read_through", patched)


# --------------------------------------------------------------------------- #
# Registration + shape.
# --------------------------------------------------------------------------- #


def test_aorc_promoted_as_record_spec():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_aorc_precip"]
    assert entry.metadata.source_class == "aorc_precip"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.fn.__module__.endswith("_promoted.fetch_aorc_precip")


def test_aorc_shape_is_record():
    assert AORC_SPEC.shape == "record"
    assert AORC_SPEC.output.layer_type == "record"
    assert AORC_SPEC.output.ext == "json"
    # Pure-record path: no build_request (the record hook owns the Zarr socket).
    assert AORC_SPEC.hooks.build_request is None
    assert AORC_SPEC.hooks.record == "aorc_precip.build_record"


# --------------------------------------------------------------------------- #
# Pure hooks + end-to-end route() -> dict.
# --------------------------------------------------------------------------- #


def test_bbox_subset_snaps_to_nearest_when_empty():
    ds = _synthetic_year(2016)
    da = ds["APCP_surface"]
    # A sub-cell bbox between grid points still yields at least one cell.
    sub = ap._bbox_subset(da, -83.4551, 35.0451, -83.4549, 35.0453)
    assert sub.sizes["latitude"] >= 1 and sub.sizes["longitude"] >= 1


def test_route_returns_hyetograph_record(monkeypatch):
    _inject_read_through(monkeypatch, {})
    monkeypatch.setattr(ap, "_open_year", lambda year: _synthetic_year(year, ndays=3))

    result = router.route(
        AORC_SPEC, {"bbox": _BBOX, "start_date": "2016-06-01", "end_date": "2016-06-03"}
    )
    assert isinstance(result, dict)  # record shape -> a dict, never a LayerURI
    assert result["variable"] == "APCP_surface"
    assert result["units"] == "mm"
    assert result["n_hours"] == 72  # 3 days x 24 h
    assert result["n_cells"] == 25  # 5 x 5 synthetic grid
    assert len(result["times"]) == 72 and len(result["precip_mm"]) == 72
    # 71 hours at 1.0 mm + one 5.0 mm peak hour = 76.0 mm AOI-mean accumulation.
    assert result["total_mm"] == pytest.approx(76.0, abs=1e-3)
    assert result["peak_mm_per_hr"] == pytest.approx(5.0, abs=1e-3)
    assert result["peak_time"].startswith("2016-06-01T10")
    assert result["cell_accumulation_mm"]["max"] == pytest.approx(76.0, abs=1e-3)


def test_route_multi_year_window(monkeypatch):
    _inject_read_through(monkeypatch, {})
    monkeypatch.setattr(ap, "_open_year", lambda year: _synthetic_year(year, ndays=3))
    # The synthetic store only has June; a Dec->Jan window still spans two year
    # opens without error and returns the (possibly empty-per-year) union.
    with pytest.raises(RouterEmptyError):
        # 2015-12/2016-01 has no June hours in the synthetic store -> honest empty.
        router.route(
            AORC_SPEC,
            {"bbox": _BBOX, "start_date": "2015-12-28", "end_date": "2016-01-02"},
        )


def test_route_before_coverage_not_available(monkeypatch):
    _inject_read_through(monkeypatch, {})
    monkeypatch.setattr(ap, "_open_year", lambda year: _synthetic_year(year))
    with pytest.raises(RouterNotAvailableError) as exc:
        router.route(
            AORC_SPEC, {"bbox": _BBOX, "start_date": "1970-01-01", "end_date": "1970-01-02"}
        )
    assert exc.value.error_code == "AORC_PRECIP_NOT_AVAILABLE"


def test_route_within_lag_not_available(monkeypatch):
    _inject_read_through(monkeypatch, {})
    monkeypatch.setattr(ap, "_open_year", lambda year: _synthetic_year(year))
    today = _dt.date.today()
    with pytest.raises(RouterNotAvailableError):
        router.route(
            AORC_SPEC,
            {
                "bbox": _BBOX,
                "start_date": today.isoformat(),
                "end_date": today.isoformat(),
            },
        )


def test_bbox_over_cap_rejected():
    with pytest.raises(RouterInputError):
        router.validate_params(
            AORC_SPEC,
            {"bbox": [-90.0, 30.0, -80.0, 40.0], "start_date": "2016-06-01", "end_date": "2016-06-02"},
        )


def test_window_over_range_days_rejected():
    with pytest.raises(RouterInputError):
        router.validate_params(
            AORC_SPEC,
            {"bbox": _BBOX, "start_date": "2016-01-01", "end_date": "2016-12-31"},
        )


def test_record_dict_json_roundtrips(monkeypatch):
    _inject_read_through(monkeypatch, {})
    monkeypatch.setattr(ap, "_open_year", lambda year: _synthetic_year(year, ndays=1))
    result = router.route(
        AORC_SPEC, {"bbox": _BBOX, "start_date": "2016-06-01", "end_date": "2016-06-01"}
    )
    # The record executor json.dumps the dict; assert it is serializable.
    assert json.loads(json.dumps(result))["n_hours"] == 24
