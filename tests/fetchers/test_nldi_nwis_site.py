"""A gauge's reach is read off NLDI's own NWIS index, never off a snap of its point.

Offline: NLDI's recorded answers for its source catalogue and for nwissite USGS-02395000
(Etowah River near Kingston), and the index's 404 for a site it does not hold."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError
from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hydrology.fetch_nhdplus_nldi_navigate import hooks

_NLDI = Path(__file__).resolve().parents[1] / "fixtures" / "nhdplus_nldi"
_KINGSTON = _NLDI / "nwissite_USGS-02395000.json"
_SOURCES = _NLDI / "linked_data_sources.json"


@pytest.fixture
def nldi(monkeypatch):
    import dataretrieval.nldi as nldi
    from dataretrieval.exceptions import HTTPError

    asked: list[str] = []

    def _query(url, query_params):
        asked.append(url)
        if url.endswith("/linked-data/"):
            return json.loads(_SOURCES.read_text())
        if url.endswith("/nwissite/USGS-02395000"):
            return json.loads(_KINGSTON.read_text())
        if "/nwissite/" in url:
            raise HTTPError(f"HTTP 404 Not Found (URL: {url})", status_code=404)
        if url.endswith("/comid/6498944/navigation/DM/flowlines"):
            return {"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"nhdplus_comid": "6498944"},
                 "geometry": {"type": "LineString", "coordinates": [[-84.98, 34.21], [-84.97, 34.20]]}}]}
        raise AssertionError(url)

    monkeypatch.setattr(nldi, "_query_nldi", _query)
    monkeypatch.setattr(nldi, "_AVAILABLE_DATA_SOURCES", None)
    return asked


def test_a_gauge_starts_at_the_reach_the_index_lists_it_on(nldi):
    found = hooks.features(get_spec("fetch_nhdplus_nldi_navigate"),
                           {"nwis_site": "02395000", "direction": "DM", "distance_km": 0.0}, timeout_s=30.0)
    assert [f["properties"]["nhdplus_comid"] for f in found] == [6498944]
    assert not any("/comid/position" in url for url in nldi)


def test_a_site_the_index_does_not_hold_is_the_typed_empty(nldi):
    with pytest.raises(RouterEmptyError) as refused:
        hooks.features(get_spec("fetch_nhdplus_nldi_navigate"),
                       {"nwis_site": "9439221", "direction": "DM", "distance_km": 0.0}, timeout_s=30.0)
    assert refused.value.error_code == "NHDPLUS_NLDI_EMPTY"
    assert "USGS-9439221" in str(refused.value)
