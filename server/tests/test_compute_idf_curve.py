"""Unit tests for ``compute_idf_curve`` (no network).

The PFDS fetch is monkeypatched with the verbatim Fort Myers Atlas 14 CSV
capture (the same fixture ``test_data_fetch`` uses for
``lookup_precip_return_period``), and ``read_through`` is replaced with a
pass-through stub, so the full pipeline (quantize -> fetch -> parse ->
190-row Vega-Lite chart payload) runs offline.

Coverage:
1.  ``test_registered`` -- tool in TOOL_REGISTRY, cacheable static-30d.
2.  ``test_intensity_chart_payload`` -- chart-emission payload shape, log x,
    one series per ARI, and the hand-checked 100-yr 24-hr intensity cell.
3.  ``test_depth_mode`` -- depth y axis carries the raw Atlas 14 inches.
4.  ``test_bbox_center_accepted`` -- a 4-element bbox resolves to its center.
5.  ``test_out_of_area_raises_no_coverage`` -- the PFDS "not within a project
    area" answer surfaces as the typed ``IdfCurveNoCoverageError``.
6.  ``test_network_failure_raises_upstream`` -- a non-coverage upstream die
    stays retryable ``IdfCurveUpstreamError``.
7.  ``test_bad_location_raises`` / ``test_bad_y_axis_raises`` -- typed input
    validation.
8.  ``test_category_and_corpus`` -- primary category + routing-corpus presence.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing import compute_idf_curve as idf_mod
from trid3nt_server.tools.fetchers.climate import lookup_precip_return_period as df_mod
from trid3nt_server.tools.processing.charts_common import is_chart_emission_result
from trid3nt_server.tools.processing.compute_idf_curve import (
    IdfCurveInputError,
    IdfCurveNoCoverageError,
    IdfCurveUpstreamError,
    compute_idf_curve,
)

# Verbatim Atlas 14 PFDS response for the Fort Myers center (captured
# 2026-06-07; same capture test_data_fetch.py uses).
_ATLAS14_FIXTURE = b"""Point precipitation frequency estimates (inches)
NOAA Atlas 14 Volume 9 Version 2
Data type: Precipitation depth
Time series type: Partial duration
Project area: Southeastern States
Latitude: 26.6 Degree
Longitude: -81.9 Degree


PRECIPITATION FREQUENCY ESTIMATES
by duration for ARI (years):, 1,2,5,10,25,50,100,200,500,1000
5-min:, 0.553,0.620,0.731,0.822,0.950,1.05,1.15,1.25,1.38,1.48
10-min:, 0.810,0.908,1.07,1.20,1.39,1.54,1.68,1.83,2.02,2.17
15-min:, 0.988,1.11,1.30,1.47,1.70,1.87,2.05,2.23,2.47,2.65
30-min:, 1.60,1.79,2.11,2.37,2.74,3.02,3.31,3.60,3.99,4.28
60-min:, 2.14,2.38,2.79,3.13,3.62,4.00,4.38,4.78,5.32,5.74
2-hr:, 2.69,2.98,3.47,3.90,4.49,4.97,5.46,5.97,6.66,7.20
3-hr:, 2.92,3.25,3.81,4.30,4.99,5.54,6.11,6.71,7.53,8.17
6-hr:, 3.23,3.70,4.50,5.18,6.16,6.94,7.75,8.60,9.76,10.7
12-hr:, 3.49,4.18,5.35,6.36,7.79,8.94,10.1,11.3,13.0,14.3
24-hr:, 4.01,4.76,6.09,7.28,9.05,10.5,12.1,13.7,16.1,18.0
2-day:, 4.94,5.57,6.77,7.94,9.80,11.4,13.3,15.3,18.2,20.7
3-day:, 5.43,6.22,7.68,9.02,11.1,12.9,14.8,16.9,19.8,22.3
4-day:, 5.83,6.78,8.43,9.92,12.1,14.0,15.9,18.0,20.9,23.3
7-day:, 7.08,8.10,9.87,11.4,13.7,15.5,17.5,19.5,22.4,24.6
10-day:, 8.28,9.30,11.0,12.6,14.8,16.6,18.5,20.4,23.2,25.4
20-day:, 11.7,12.9,14.8,16.4,18.7,20.4,22.1,23.8,26.1,27.8
30-day:, 14.5,15.9,18.2,20.0,22.4,24.2,25.9,27.5,29.5,30.9
45-day:, 18.0,19.9,22.7,24.9,27.7,29.6,31.4,33.0,34.9,36.2
60-day:, 21.0,23.3,26.6,29.2,32.4,34.6,36.6,38.3,40.3,41.5
"""

LOCATION = (26.6, -81.9)


@pytest.fixture()
def offline_pfds(monkeypatch):
    """Canned PFDS fetch + pass-through read_through (no S3, no network)."""
    fetch_calls: list[tuple[float, float]] = []

    def _canned(lat: float, lon: float) -> bytes:
        fetch_calls.append((lat, lon))
        return _ATLAS14_FIXTURE

    monkeypatch.setattr(idf_mod, "_fetch_pfds_matrix_bytes", _canned)
    monkeypatch.setattr(
        idf_mod,
        "read_through",
        lambda metadata, params, ext, fetch_fn, **kw: SimpleNamespace(
            uri="cache://idf-test.csv", data=fetch_fn(), hit=False
        ),
    )
    return fetch_calls


def _rows(payload: dict) -> list[dict]:
    return payload["vega_lite_spec"]["data"]["values"]


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_idf_curve"]
    assert entry.fn is compute_idf_curve
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "idf_curve"
    assert entry.metadata.open_world_hint is True  # NOAA PFDS API


def test_intensity_chart_payload(offline_pfds) -> None:
    payload = compute_idf_curve(location=LOCATION)

    # House chart-emission contract (the agent loop's detection predicate).
    assert is_chart_emission_result(payload)
    assert payload["envelope_type"] == "chart-emission"

    spec = payload["vega_lite_spec"]
    assert spec["encoding"]["x"]["scale"] == {"type": "log"}
    assert spec["encoding"]["y"]["scale"] == {"type": "log"}
    assert spec["encoding"]["color"]["field"] == "return_period"

    rows = _rows(payload)
    # 19 durations x 10 ARIs, every fixture cell positive.
    assert len(rows) == 190
    assert len({r["return_period"] for r in rows}) == 10
    assert len({r["duration_hr"] for r in rows}) == 19

    # Hand-checked cell: 100-yr 24-hr = 12.1 inches -> 12.1/24 in/hr.
    cell = [
        r for r in rows if r["ari_years"] == 100 and r["duration_hr"] == 24.0
    ]
    assert len(cell) == 1
    assert cell[0]["value"] == pytest.approx(12.1 / 24.0, rel=1e-4)

    # Provenance rides in the caption.
    assert "Volume 9" in payload["caption"]
    assert "Southeastern States" in payload["caption"]


def test_depth_mode(offline_pfds) -> None:
    payload = compute_idf_curve(location=LOCATION, y_axis="depth")
    spec = payload["vega_lite_spec"]
    assert spec["encoding"]["y"]["title"] == "Depth (inches)"
    assert "scale" not in spec["encoding"]["y"]  # linear y for depth (DDF)
    cell = [
        r
        for r in _rows(payload)
        if r["ari_years"] == 100 and r["duration_hr"] == 24.0
    ]
    assert cell[0]["value"] == pytest.approx(12.1)


def test_bbox_center_accepted(offline_pfds) -> None:
    # bbox centered on the Fort Myers point: center = (lat 26.6, lon -81.9).
    payload = compute_idf_curve(location=(-82.0, 26.5, -81.8, 26.7))
    assert is_chart_emission_result(payload)
    # The fetch was issued at the snapped bbox center.
    (lat, lon) = offline_pfds[0]
    assert lat == pytest.approx(26.6, abs=1 / 120)
    assert lon == pytest.approx(-81.9, abs=1 / 120)


def test_out_of_area_raises_no_coverage(monkeypatch) -> None:
    def _out_of_area(lat: float, lon: float) -> bytes:
        raise df_mod.UpstreamAPIError(
            f"NOAA Atlas 14 PFDS returned no precip-frequency data for "
            f"(lat={lat}, lon={lon}) -- point may be outside the Atlas 14 "
            "project areas."
        )

    monkeypatch.setattr(idf_mod, "_fetch_pfds_matrix_bytes", _out_of_area)
    monkeypatch.setattr(
        idf_mod,
        "read_through",
        lambda metadata, params, ext, fetch_fn, **kw: SimpleNamespace(
            uri=None, data=fetch_fn(), hit=False
        ),
    )
    with pytest.raises(IdfCurveNoCoverageError):
        compute_idf_curve(location=(46.325, -122.733))  # Toutle / PNW


def test_network_failure_raises_upstream(monkeypatch) -> None:
    def _boom(lat: float, lon: float) -> bytes:
        raise df_mod.UpstreamAPIError("connection reset by peer")

    monkeypatch.setattr(idf_mod, "_fetch_pfds_matrix_bytes", _boom)
    monkeypatch.setattr(
        idf_mod,
        "read_through",
        lambda metadata, params, ext, fetch_fn, **kw: SimpleNamespace(
            uri=None, data=fetch_fn(), hit=False
        ),
    )
    with pytest.raises(IdfCurveUpstreamError):
        compute_idf_curve(location=LOCATION)


def test_bad_location_raises() -> None:
    with pytest.raises(IdfCurveInputError):
        compute_idf_curve(location=(1.0, 2.0, 3.0))  # type: ignore[arg-type]
    with pytest.raises(IdfCurveInputError):
        compute_idf_curve(location=(200.0, 0.0))  # lat out of range
    with pytest.raises(IdfCurveInputError):
        compute_idf_curve(location="Houston")  # type: ignore[arg-type]


def test_bad_y_axis_raises(offline_pfds) -> None:
    with pytest.raises(IdfCurveInputError):
        compute_idf_curve(location=LOCATION, y_axis="volume")


def test_category_and_corpus() -> None:
    import yaml

    from trid3nt_server import categories
    from trid3nt_server.tools.discovery import search_tools as dd

    assert categories.PRIMARY_CATEGORY["compute_idf_curve"] == "hydrology"
    corpus_path = pathlib.Path(dd._default_corpus_path())
    corpus = yaml.safe_load(corpus_path.read_text())
    assert len(corpus.get("compute_idf_curve", [])) >= 5
