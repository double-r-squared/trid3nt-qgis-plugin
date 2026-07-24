"""Offline unit tests for ``fetch_high_water_marks``.

Network is fully mocked: ``_fetch_events`` + ``_fetch_filtered_hwms`` are
patched to return a COMMITTED real capture (a trimmed
``FilteredHWMs.json?Event=287`` response for Hurricane Michael 2018, captured
live once and stored under ``fixtures/validation/stn/``), and ``read_through``
is patched with an in-memory store. No test hits the network.
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.hydrology import fetch_high_water_marks as hwm_mod
from trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks import (
    HighWaterMarksLayerURI,
    HwmEventNotFoundError,
    HwmInputError,
    HwmNoMarksError,
    estimate_payload_mb,
    fetch_high_water_marks,
)

_FIXTURE = (
    pathlib.Path(__file__).parent
    / "fixtures" / "validation" / "stn" / "michael_2018_filtered_hwms.json"
)
_RECORDS = json.loads(_FIXTURE.read_text())
# Full-extent bbox around the fixture (all 10 marks fall inside).
_BBOX = (-86.0, 29.0, -83.0, 30.5)
_EVENTS = [{"event_id": 287, "event_name": "2018 Michael"}]


def _make_read_through_injector(store):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path,
        compute_cache_key as ck, is_cacheable,
    )

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = ck(source_id, params, metadata.ttl_class)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


def _read_stored_fgb(store: dict[str, bytes]) -> gpd.GeoDataFrame:
    """Read the single FGB the injector stored (uri is a fake s3://)."""
    import tempfile

    data = next(iter(store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        path = f.name
    return gpd.read_file(path)


def _patch(monkeypatch, records, events=_EVENTS) -> dict[str, bytes]:
    store: dict[str, bytes] = {}
    monkeypatch.setattr(hwm_mod, "read_through", _make_read_through_injector(store))
    monkeypatch.setattr(hwm_mod, "_fetch_events", lambda: events)

    def _fake_filtered(event_id=None, states=None):
        return records

    monkeypatch.setattr(hwm_mod, "_fetch_filtered_hwms", _fake_filtered)
    return store


# ---------------------------------------------------------------------------
# Registration + input validation.
# ---------------------------------------------------------------------------


def test_registered() -> None:
    entry = TOOL_REGISTRY["fetch_high_water_marks"]
    assert entry.fn is fetch_high_water_marks
    m = entry.metadata
    assert m.cacheable is True
    assert m.ttl_class == "semi-static-7d"
    assert m.source_class == "usgs_stn_hwm"
    assert m.supports_global_query is False
    assert m.open_world_hint is True


def test_missing_bbox_raises() -> None:
    with pytest.raises(HwmInputError):
        fetch_high_water_marks(bbox=None)


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(HwmInputError):
        fetch_high_water_marks(bbox=(-86.0, 30.0, -86.0, 30.0))


def test_payload_estimate() -> None:
    assert estimate_payload_mb(bbox=_BBOX, event="Michael") > 0
    assert estimate_payload_mb() > 0


# ---------------------------------------------------------------------------
# Event-scoped happy path (real fixture).
# ---------------------------------------------------------------------------


def test_event_scoped_happy(monkeypatch) -> None:
    store = _patch(monkeypatch, _RECORDS)
    layer = fetch_high_water_marks(bbox=_BBOX, event="Michael")

    assert isinstance(layer, HighWaterMarksLayerURI)
    assert layer.layer_type == "vector"
    assert layer.style_preset == "usgs_high_water_marks"
    assert layer.uri.startswith("s3://")
    assert layer.event == "2018 Michael"
    assert layer.n_marks == len(_RECORDS)
    assert sum(layer.quality_breakdown.values()) == len(_RECORDS)
    # The fixture spans all six STN quality ratings.
    assert "Excellent: +/- 0.05 ft" in layer.quality_breakdown
    assert "Unknown/Historical" in layer.quality_breakdown
    assert layer.datum_summary.get("NAVD88") == len(_RECORDS)

    # Caveats: the datum-reconciliation warning + the historical-quality note.
    joined = " ".join(layer.caveats)
    assert "VERTICAL DATUM" in joined
    assert "Unknown/Historical" in joined

    # The FGB carries the pairing-ready columns.
    gdf = _read_stored_fgb(store)
    assert len(gdf) == len(_RECORDS)
    for col in ("hwm_id", "elev_ft", "vertical_datum", "quality", "hwm_type"):
        assert col in gdf.columns


def test_bbox_clip_drops_outside(monkeypatch) -> None:
    _patch(monkeypatch, _RECORDS)
    # A bbox that splits the fixture cluster east/west.
    clip = (-86.0, 29.0, -84.5, 30.5)
    expected = sum(
        1 for r in _RECORDS
        if clip[0] <= r["longitude"] <= clip[2] and clip[1] <= r["latitude"] <= clip[3]
    )
    assert 0 < expected < len(_RECORDS)  # fixture guarantees a real split
    layer = fetch_high_water_marks(bbox=clip, event="Michael")
    assert layer.n_marks == expected


def test_state_scoped_no_event(monkeypatch) -> None:
    _patch(monkeypatch, _RECORDS)
    layer = fetch_high_water_marks(bbox=_BBOX)  # no event
    assert layer.event is None
    assert layer.n_marks == len(_RECORDS)


# ---------------------------------------------------------------------------
# Honest error paths.
# ---------------------------------------------------------------------------


def test_no_marks_raises(monkeypatch) -> None:
    _patch(monkeypatch, [])  # both event + state fetch return nothing
    with pytest.raises(HwmNoMarksError):
        fetch_high_water_marks(bbox=_BBOX, event="Michael")


def test_event_not_found_raises(monkeypatch) -> None:
    _patch(monkeypatch, _RECORDS, events=[{"event_id": 1, "event_name": "1999 Floyd"}])
    with pytest.raises(HwmEventNotFoundError):
        fetch_high_water_marks(bbox=_BBOX, event="Nonexistent Storm")


def test_bbox_outside_us_no_event_raises(monkeypatch) -> None:
    _patch(monkeypatch, _RECORDS)
    with pytest.raises(HwmInputError):
        fetch_high_water_marks(bbox=(10.0, 10.0, 11.0, 11.0))  # Africa, no US state


def test_null_elev_preserved(monkeypatch) -> None:
    rec = dict(_RECORDS[0])
    rec["elev_ft"] = None  # honest: missing elevation kept as null
    store = _patch(monkeypatch, [rec])
    layer = fetch_high_water_marks(bbox=_BBOX, event="Michael")
    gdf = _read_stored_fgb(store)
    assert layer.n_marks == 1
    assert gdf["elev_ft"].isna().all()


def test_multi_datum_caveat(monkeypatch) -> None:
    a = dict(_RECORDS[0])
    b = dict(_RECORDS[1])
    b["verticalDatumName"] = "NGVD29"
    _patch(monkeypatch, [a, b])
    layer = fetch_high_water_marks(bbox=_BBOX, event="Michael")
    assert set(layer.datum_summary) == {"NAVD88", "NGVD29"}
    assert any("multiple vertical datums" in c for c in layer.caveats)
