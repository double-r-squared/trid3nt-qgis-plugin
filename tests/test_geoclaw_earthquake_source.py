"""Offline tests for the ADR 0226 real-event tsunami source resolver.

The pure ``select_largest_event`` selector + the ``resolve_earthquake_source``
I/O-boundary error paths (geocode / fetch / read) are exercised WITHOUT a live FDSN
call by monkeypatching ``geocode_location`` / ``fetch_usgs_earthquakes`` and the FGB
reader. No network, no MinIO."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.geoclaw import earthquake_source as es


def _feat(mag, depth, lon, lat, eid="x", place="P"):
    return {
        "properties": {"id": eid, "mag": mag, "depth_km": depth, "place": place},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


# --------------------------------------------------------------------------- #
# select_largest_event (pure).
# --------------------------------------------------------------------------- #
def test_select_picks_largest_magnitude():
    feats = [_feat(7.2, 30, -160, 55, "a"), _feat(8.2, 32, -158, 55.4, "b"),
             _feat(6.5, 12, -159, 54, "c")]
    e = es.select_largest_event(feats)
    assert e is not None and e.event_id == "b" and e.magnitude == 8.2


def test_select_tie_prefers_shallower():
    feats = [_feat(8.2, 40, -158, 55, "deep"), _feat(8.2, 10, -158.1, 55.1, "shallow")]
    e = es.select_largest_event(feats)
    assert e.event_id == "shallow" and e.depth_km == 10.0


def test_select_skips_nonfinite_and_missing_geometry():
    feats = [
        {"properties": {"id": "nan", "mag": float("nan"), "depth_km": 10},
         "geometry": {"type": "Point", "coordinates": [-158, 55]}},
        {"properties": {"id": "nogeom", "mag": 9.0, "depth_km": 10}, "geometry": None},
        _feat(7.0, 20, -159, 54, "ok"),
    ]
    e = es.select_largest_event(feats)
    assert e.event_id == "ok"


def test_select_empty_returns_none():
    assert es.select_largest_event([]) is None


def test_resolved_provenance_label_is_honest():
    e = es.ResolvedEarthquake(
        lon=-157.9, lat=55.4, magnitude=8.2, depth_km=32.0,
        event_id="ak0219neiszm", place="Alaska Peninsula", time="2021-07-29T06:15:47Z")
    lbl = e.provenance_label
    assert "ak0219neiszm" in lbl and "M8.2" in lbl and "Alaska Peninsula" in lbl
    assert "depth 32 km" in lbl


# --------------------------------------------------------------------------- #
# resolve_earthquake_source (I/O boundary, monkeypatched).
# --------------------------------------------------------------------------- #
class _Layer:
    uri = "s3://runs/eq.fgb"


def test_resolve_happy_path(monkeypatch):
    monkeypatch.setattr(es, "_region_bbox", lambda region: (-162.0, 54.0, -155.0, 57.0))
    from trid3nt_server.tools import TOOL_REGISTRY

    class _Tool:
        def __init__(self, fn):
            self.fn = fn
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_usgs_earthquakes",
        _Tool(lambda **kw: _Layer()))
    monkeypatch.setattr(
        es, "_read_fgb_features",
        lambda uri: [_feat(7.8, 28, -159, 55, "small"),
                     _feat(8.2, 32, -157.9, 55.4, "big", "Alaska Peninsula")])
    e = es.resolve_earthquake_source("Alaska Peninsula", min_magnitude=7.5)
    assert e.event_id == "big" and e.magnitude == 8.2 and e.depth_km == 32.0


def test_resolve_empty_catalog_raises(monkeypatch):
    monkeypatch.setattr(es, "_region_bbox", lambda region: (-162.0, 54.0, -155.0, 57.0))
    from trid3nt_server.tools import TOOL_REGISTRY

    class _Tool:
        def __init__(self, fn):
            self.fn = fn

    class _NoUri:
        uri = None
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_usgs_earthquakes", _Tool(lambda **kw: _NoUri()))
    with pytest.raises(es.EarthquakeSourceError) as ei:
        es.resolve_earthquake_source("Nowhere", min_magnitude=9.5)
    assert ei.value.error_code == "EARTHQUAKE_CATALOG_EMPTY"


def test_resolve_fetch_failure_is_typed(monkeypatch):
    monkeypatch.setattr(es, "_region_bbox", lambda region: (-162.0, 54.0, -155.0, 57.0))
    from trid3nt_server.tools import TOOL_REGISTRY

    class _Tool:
        def __init__(self, fn):
            self.fn = fn

    def _boom(**kw):
        raise RuntimeError("FDSN 503")
    monkeypatch.setitem(TOOL_REGISTRY, "fetch_usgs_earthquakes", _Tool(_boom))
    with pytest.raises(es.EarthquakeSourceError) as ei:
        es.resolve_earthquake_source("Aleutians", min_magnitude=7.0)
    assert ei.value.error_code == "EARTHQUAKE_CATALOG_FETCH_FAILED"
