"""Router-fold test parity for ``fetch_noaa_nwm_streamflow`` (ADR 0112).

Migrated from ``test_fetch_noaa_nwm_streamflow.py`` when the coded twin -- the LAST
coded data-fetcher, a MULTI-SOURCE COMPOSITE (NWM S3 channel_rt netCDF -> a
``{feature_id: streamflow}`` lookup + an NLDI 5x5 bbox sample -> COMIDs + per-reach
geometry + a ``feature_id`` JOIN -> point FGB) -- was folded onto the router (a
``library_delegate`` vector-fgb spec + the ``nwm_streamflow.*`` hooks + the fetch-time
provenance channel). Follows the ``test_router_storm_tracks.py`` / ``test_router_topobathy.py``
migrated-test style: pure hook/helper tests offline, plus end-to-end drives through the
promoted router closure (``TOOL_REGISTRY``) with the delegate's network leaves
(``nwm_streamflow._resolve_nwm_key`` / ``_http_get`` / ``_load_streamflow_by_feature`` /
``_discover_comids_in_bbox`` / ``_nldi_get_reach_geometry``) monkeypatched and the S3 cache
faked (``fake_s3``). Proves:

- registry shape + typed-error envelope + payload estimator;
- the CONUS-intersect + short_range-forecast_hour + valid_time-parse gates
  (``nwm_streamflow.validate``);
- the composite JOIN (streamflow lookup x NLDI geometry -> point features) inside the
  delegate, including the flow-missing / geometry-missing skips + honest-empty raises;
- the NWM key resolver (analysis_assim + short_range matchers over a mocked S3 listing);
- END-TO-END via the promoted router closure: NWMStreamflowLayerURI fields populated,
  FlatGeobuf round-trips via geopandas with the feature_id/streamflow_cms/valid_time/product
  schema, honest-empty raises the typed EMPTY code;
- THE CHANNEL: a cache-hit REPLAYS the reference_time/reach_count/nldi provenance fields
  identically (no re-fetch).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from trid3nt_contracts.execution import NWMStreamflowLayerURI
from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.fetchers._fetch_common import FetchError
from trid3nt_server.data.fetchers._router.hooks import nwm_streamflow as ns
from trid3nt_server.data.fetchers._router.hooks.nwm_streamflow import (
    NWMStreamflowEmptyError,
    NWMStreamflowError,
    NWMStreamflowInputError,
    NWMStreamflowNotAvailableError,
    NWMStreamflowUpstreamError,
    estimate_payload_mb,
)

# Fort Myers / Caloosahatchee bbox (~1.5deg wide for plenty of NLDI hits).
_FORT_MYERS_BBOX: tuple[float, float, float, float] = (-82.0, 26.4, -81.7, 26.7)
_PINNED_VT = _dt.datetime(2026, 6, 9, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _fetch_nwm(**kw: Any) -> Any:
    return TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn(**kw)


# --------------------------------------------------------------------------- #
# Registry shape.
# --------------------------------------------------------------------------- #


def test_nwm_registered_with_expected_metadata() -> None:
    assert "fetch_noaa_nwm_streamflow" in TOOL_REGISTRY
    md = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].metadata
    assert md.ttl_class == "dynamic-1h"
    assert md.source_class == "nwm_streamflow"
    assert md.cacheable is True
    assert getattr(md, "supports_global_query", None) is False
    assert getattr(md, "payload_mb_estimator_name", None) == "estimate_payload_mb"


# --------------------------------------------------------------------------- #
# Typed-error envelope (base = FetchError so library_delegate passes it through).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cls, code, retryable",
    [
        (NWMStreamflowError, "NWM_STREAMFLOW_ERROR", True),
        (NWMStreamflowInputError, "NWM_STREAMFLOW_INPUT_ERROR", False),
        (NWMStreamflowUpstreamError, "NWM_STREAMFLOW_UPSTREAM_ERROR", True),
        (NWMStreamflowNotAvailableError, "NWM_STREAMFLOW_NOT_AVAILABLE", False),
        (NWMStreamflowEmptyError, "NWM_STREAMFLOW_EMPTY", False),
    ],
)
def test_typed_error_envelope(cls: type, code: str, retryable: bool) -> None:
    err = cls("boom")
    assert err.error_code == code
    assert err.retryable is retryable
    assert isinstance(err, RuntimeError)
    assert isinstance(err, FetchError)  # library_delegate.invoke passes the code through
    assert issubclass(cls, NWMStreamflowError)


def test_estimate_payload_mb_shape() -> None:
    val = estimate_payload_mb(bbox=_FORT_MYERS_BBOX)
    assert isinstance(val, float) and val > 0
    small = estimate_payload_mb(bbox=(-82.0, 26.4, -81.9, 26.5))
    large = estimate_payload_mb(bbox=(-100.0, 27.0, -94.0, 33.0))
    assert large >= small
    assert estimate_payload_mb(bbox=None) >= 0.0


# --------------------------------------------------------------------------- #
# Input validation (``nwm_streamflow.validate``, pre-cache).
# --------------------------------------------------------------------------- #


def test_validate_hook_out_of_conus_raises() -> None:
    with pytest.raises(NWMStreamflowInputError, match="CONUS"):
        ns.validate_nwm_streamflow(None, {"bbox": (10.0, 45.0, 12.0, 47.0)})


def test_validate_hook_conus_bbox_passes() -> None:
    ns.validate_nwm_streamflow(None, {"bbox": _FORT_MYERS_BBOX})  # no raise


def test_validate_hook_short_range_requires_nonzero_fhour() -> None:
    with pytest.raises(NWMStreamflowInputError, match="short_range"):
        ns.validate_nwm_streamflow(
            None,
            {"bbox": _FORT_MYERS_BBOX, "product": "short_range", "forecast_hour": 0},
        )


def test_validate_hook_bad_valid_time_raises() -> None:
    with pytest.raises(NWMStreamflowInputError):
        ns.validate_nwm_streamflow(
            None, {"bbox": _FORT_MYERS_BBOX, "valid_time": "not-an-iso-date"}
        )


def test_parse_valid_time_zulu_to_utc() -> None:
    dt = ns._parse_valid_time("2025-01-01T12:00:00Z")
    assert dt == _dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    assert ns._parse_valid_time(None) is None


# --------------------------------------------------------------------------- #
# NWM key resolution (S3 listing mocked).
# --------------------------------------------------------------------------- #


def test_resolve_nwm_key_analysis_assim_latest(monkeypatch) -> None:
    def _fake_list(prefix: str, max_keys: int = 1000) -> list[str]:
        if prefix.endswith("analysis_assim/"):
            return [
                "nwm.20260609/analysis_assim/nwm.t00z.analysis_assim.channel_rt.tm00.conus.nc",
                "nwm.20260609/analysis_assim/nwm.t12z.analysis_assim.channel_rt.tm00.conus.nc",
            ]
        return ["nwm.20260609/"]  # latest-date probe

    monkeypatch.setattr(ns, "_list_s3_keys", _fake_list)
    monkeypatch.setattr(ns, "_latest_nwm_date", lambda: "20260609")
    key, dt = ns._resolve_nwm_key("analysis_assim", None, 0)
    assert key.endswith("nwm.t12z.analysis_assim.channel_rt.tm00.conus.nc")  # latest cycle
    assert dt == _dt.datetime(2026, 6, 9, 12, tzinfo=_dt.timezone.utc)


def test_resolve_nwm_key_not_available_raises(monkeypatch) -> None:
    monkeypatch.setattr(ns, "_latest_nwm_date", lambda: "20260609")
    monkeypatch.setattr(ns, "_list_s3_keys", lambda prefix, max_keys=1000: [])
    with pytest.raises(NWMStreamflowNotAvailableError):
        ns._resolve_nwm_key("analysis_assim", None, 0)


# --------------------------------------------------------------------------- #
# netCDF -> streamflow lookup: the real datetime64[ns] round-trip (ADR 0309).
# --------------------------------------------------------------------------- #


def test_load_streamflow_reads_the_real_nc_time_coordinate(tmp_path) -> None:
    """A real NWM channel_rt file's 'time' coord is datetime64[ns]; ``.item(0)``
    on that dtype degrades to a plain int (no ``.astype``), which used to make
    the old ``hasattr(t0, "astype")`` guard silently skip the whole block and
    fall through to the ``datetime.now()`` fallback - so a HISTORICAL
    ``event_time`` request always reported "now" as its resolved cycle. No
    network: a real xarray Dataset round-tripped through netcdf4 reproduces the
    exact dtype a downloaded NWM file carries.
    """
    import numpy as np
    import xarray as xr

    ds = xr.Dataset(
        {"streamflow": (["feature_id"], np.array([1.5, 2.5], dtype="float64"))},
        coords={"feature_id": np.array([100, 200], dtype="int64"),
               "time": np.array(["2026-08-19T00:00:00"], dtype="datetime64[ns]")},
    )
    nc_path = str(tmp_path / "channel_rt.nc")
    ds.to_netcdf(nc_path, engine="netcdf4")
    ds.close()

    flows, valid_time = ns._load_streamflow_by_feature(nc_path)
    assert flows == {100: 1.5, 200: 2.5}
    assert valid_time == _dt.datetime(2026, 8, 19, 0, 0, 0, tzinfo=_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# The composite fetch + join (network leaves mocked).
# --------------------------------------------------------------------------- #


def _install_composite_mocks(monkeypatch, *, comids, flows, geom_none=()) -> None:
    monkeypatch.setattr(
        ns, "_resolve_nwm_key",
        lambda product, vt, fh: ("nwm.20260609/analysis_assim/x.nc", _PINNED_VT),
    )
    monkeypatch.setattr(ns, "_http_get", lambda url, timeout: b"NETCDF")
    monkeypatch.setattr(
        ns, "_load_streamflow_by_feature", lambda path: (dict(flows), _PINNED_VT)
    )
    monkeypatch.setattr(ns, "_discover_comids_in_bbox", lambda bbox: list(comids))

    def _geom(comid: int):
        if comid in geom_none:
            return None
        return [(-81.9, 26.5), (-81.85, 26.55), (-81.8, 26.6)]

    monkeypatch.setattr(ns, "_nldi_get_reach_geometry", _geom)


def test_fetch_features_join_skips_missing_flow_and_geometry(monkeypatch) -> None:
    # 101/202/303 have flow; 404 has no flow (skip); 303 has no geometry (skip).
    _install_composite_mocks(
        monkeypatch,
        comids=[101, 202, 303, 404],
        flows={101: 12.5, 202: 3.0, 303: 0.4},
        geom_none=(303,),
    )
    feats, vt, n_comids = ns._fetch_nwm_features(
        _FORT_MYERS_BBOX, "analysis_assim", None, 0
    )
    assert n_comids == 4
    assert vt == _PINNED_VT
    ids = sorted(f["properties"]["feature_id"] for f in feats)
    assert ids == [101, 202]  # 303 dropped (no geom), 404 dropped (no flow)
    for f in feats:
        assert f["geometry"]["type"] == "Point"
        p = f["properties"]
        assert set(p) == {"feature_id", "streamflow_cms", "valid_time", "product"}
        assert p["product"] == "analysis_assim"
        assert p["valid_time"] == _PINNED_VT.isoformat()


def test_fetch_features_no_comids_raises_empty(monkeypatch) -> None:
    _install_composite_mocks(monkeypatch, comids=[], flows={})
    with pytest.raises(NWMStreamflowEmptyError):
        ns._fetch_nwm_features(_FORT_MYERS_BBOX, "analysis_assim", None, 0)


def test_fetch_features_no_matched_tuples_raises_empty(monkeypatch) -> None:
    # COMIDs discovered but none carry both flow AND geometry.
    _install_composite_mocks(
        monkeypatch, comids=[101, 202], flows={999: 1.0}, geom_none=()
    )
    with pytest.raises(NWMStreamflowEmptyError):
        ns._fetch_nwm_features(_FORT_MYERS_BBOX, "analysis_assim", None, 0)


# --------------------------------------------------------------------------- #
# Pure envelope hook (layer_id / name / provenance replay).
# --------------------------------------------------------------------------- #


def test_envelope_hook_layer_id_and_name_analysis() -> None:
    params = {
        "bbox": [-82.0, 26.4, -81.7, 26.7],
        "product": "analysis_assim",
        "forecast_hour": 0,
    }
    out = ns.envelope_nwm_streamflow(
        None, params, None, None,
        provenance={
            "reference_time": _PINNED_VT.isoformat(),
            "reach_count": 7,
            "nldi_comids_discovered": 11,
        },
    )
    assert out["layer_id"].startswith("nwm-streamflow-analysis_assim-")
    assert out["name"] == "NWM streamflow -- analysis_assim (latest)"
    assert out["product"] == "analysis_assim"
    assert out["reference_time"] == _PINNED_VT.isoformat()
    assert out["reach_count"] == 7
    assert out["nldi_comids_discovered"] == 11


def test_envelope_hook_short_range_name_carries_fhour() -> None:
    params = {
        "bbox": [-82.0, 26.4, -81.7, 26.7],
        "product": "short_range",
        "forecast_hour": 6,
        "valid_time": "2025-01-01T12:00:00Z",
    }
    out = ns.envelope_nwm_streamflow(None, params, None, None, provenance=None)
    assert out["name"] == (
        "NWM streamflow -- short_range (2025-01-01T12:00:00Z +f006)"
    )
    # pre-channel cache object -> declared defaults hold.
    assert out["reference_time"] is None
    assert out["reach_count"] == 0
    assert out["nldi_comids_discovered"] == 0


# --------------------------------------------------------------------------- #
# END-TO-END via the promoted router closure (network leaves mocked + fake_s3).
# --------------------------------------------------------------------------- #


def test_end_to_end_happy_path_roundtrip(monkeypatch, fake_s3) -> None:
    pytest.importorskip("geopandas")
    _install_composite_mocks(
        monkeypatch,
        comids=[101, 202, 303],
        flows={101: 230.6, 202: 12.0, 303: 5.5},
    )

    res = _fetch_nwm(bbox=_FORT_MYERS_BBOX)

    assert isinstance(res, NWMStreamflowLayerURI)
    assert res.layer_type == "vector"
    assert res.role == "primary"
    assert res.style_preset == "nwm_streamflow"
    assert res.units == "m^3/s"
    assert res.uri is not None and res.uri.endswith(".fgb")
    assert res.layer_id.startswith("nwm-streamflow-analysis_assim-")
    assert res.name == "NWM streamflow -- analysis_assim (latest)"
    assert res.product == "analysis_assim"
    assert res.reference_time == _PINNED_VT.isoformat()
    assert res.reach_count == 3
    assert res.nldi_comids_discovered == 3

    import tempfile as _tf

    import geopandas as gpd

    fgb_key = next(k for k in fake_s3.store if k.endswith(".fgb"))
    with _tf.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fake_s3.store[fgb_key])
        f.flush()
        gdf = gpd.read_file(f.name)
    assert len(gdf) == 3
    assert set(gdf.geometry.geom_type) == {"Point"}
    for col in ("feature_id", "streamflow_cms", "valid_time", "product"):
        assert col in gdf.columns
    assert gdf["streamflow_cms"].max() == pytest.approx(230.6)
    assert set(gdf["product"]) == {"analysis_assim"}
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326


def test_end_to_end_honest_empty_raises(monkeypatch, fake_s3) -> None:
    _install_composite_mocks(monkeypatch, comids=[], flows={})
    with pytest.raises(NWMStreamflowEmptyError) as ei:
        _fetch_nwm(bbox=_FORT_MYERS_BBOX)
    assert ei.value.error_code == "NWM_STREAMFLOW_EMPTY"


def test_end_to_end_out_of_conus_raises_pre_network(monkeypatch, fake_s3) -> None:
    # An out-of-CONUS bbox is rejected by the validate hook BEFORE any fetch leaf.
    def _boom(*a, **k):
        raise AssertionError("network leaf must not be called for an out-of-CONUS bbox")

    monkeypatch.setattr(ns, "_resolve_nwm_key", _boom)
    with pytest.raises(NWMStreamflowInputError) as ei:
        _fetch_nwm(bbox=(10.0, 45.0, 12.0, 47.0))
    assert ei.value.error_code == "NWM_STREAMFLOW_INPUT_ERROR"


def test_end_to_end_not_available_propagates(monkeypatch, fake_s3) -> None:
    def _raise_na(product, vt, fh):
        raise NWMStreamflowNotAvailableError("cycle not published")

    monkeypatch.setattr(ns, "_resolve_nwm_key", _raise_na)
    with pytest.raises(NWMStreamflowNotAvailableError) as ei:
        _fetch_nwm(bbox=_FORT_MYERS_BBOX)
    assert ei.value.error_code == "NWM_STREAMFLOW_NOT_AVAILABLE"


# --------------------------------------------------------------------------- #
# THE CHANNEL: cache-hit replay of reference_time / reach_count / nldi count.
# --------------------------------------------------------------------------- #


def test_cache_hit_replays_provenance_identically(monkeypatch, fake_s3) -> None:
    """A second call over the same params is a CACHE HIT that never re-fetches, yet
    reference_time / reach_count / nldi_comids_discovered REPLAY IDENTICAL from the
    provenance sidecar (ADR 0110) -- the fact a pre-channel cache object would lose."""
    calls = {"n": 0}

    def _counting_resolve(product, vt, fh):
        calls["n"] += 1
        return ("nwm.20260609/analysis_assim/x.nc", _PINNED_VT)

    _install_composite_mocks(
        monkeypatch, comids=[101, 202, 303], flows={101: 230.6, 202: 12.0, 303: 5.5}
    )
    monkeypatch.setattr(ns, "_resolve_nwm_key", _counting_resolve)

    r1 = _fetch_nwm(bbox=_FORT_MYERS_BBOX)
    assert calls["n"] == 1
    fields1 = (r1.reference_time, r1.reach_count, r1.nldi_comids_discovered)

    r2 = _fetch_nwm(bbox=_FORT_MYERS_BBOX)
    assert calls["n"] == 1, "cache hit must NOT re-fetch"
    fields2 = (r2.reference_time, r2.reach_count, r2.nldi_comids_discovered)
    assert fields1 == fields2
    assert fields1 == (_PINNED_VT.isoformat(), 3, 3)
