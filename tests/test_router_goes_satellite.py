"""Router-fold test parity for ``fetch_goes_satellite`` (ADR 0111).

Migrated from ``test_fetch_goes_satellite.py`` when the coded twin was folded
onto the router (a ``library_delegate`` raster-cog spec + the ``goes_satellite.*``
hooks + the fetch-time provenance channel). Follows the ``test_router_topobathy.py``
/ ``test_router_dem.py`` migrated-test style: pure hook/helper tests offline, plus
end-to-end drives through the promoted router closure (``TOOL_REGISTRY``) with the
delegate's network seams (``goes_satellite._list_recent_keys`` /
``_download_to_tempfile`` / ``_reproject_and_clip``) monkeypatched and the S3 cache
faked (``fake_s3``) -- no real netCDF is read. Proves:

- registry shape + typed-error envelope + payload estimator;
- ``goes_satellite.validate``: BBOX_REQUIRED, band/satellite/target_res_deg input
  gates, and the CONUS-sector fast-reject (honest GOES_EMPTY pre-network);
- ``goes_satellite.resolve``: satellite canon + 15-min ``valid_time`` cache
  rounding (``_round_valid_time``, ``_scan_time_iso``, ``_pick_most_recent_key``);
- END-TO-END via the promoted router closure: GOESSatelliteLayerURI fields
  populated, the twin's exact em-dash display name preserved byte-identical, a
  real COG written through the shared writer;
- typed-error passthrough for GOES_UPSTREAM_ERROR / GOES_EMPTY raised inside the
  delegate socket;
- THE CHANNEL: a cache-hit REPLAYS the satellite/band/scan_time provenance fields
  identically (no re-fetch) -- ``scan_time`` is otherwise unrecoverable from the
  COG on a cache hit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pytest
import rasterio
import rasterio.transform as _rt

from trid3nt_contracts.execution import GOESSatelliteLayerURI
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._fetch_common import FetchError
from trid3nt_server.tools.fetchers._router.hooks import goes_satellite as gs
from trid3nt_server.tools.fetchers.imagery._goes_common import (
    GOESBboxRequiredError,
    GOESEmptyError,
    GOESError,
    GOESInputError,
    GOESUpstreamError,
)

# Florida coastal bbox - inside the CONUS sector.
_FL_BBOX = (-82.0, 26.0, -80.0, 28.0)

# Europe - entirely outside the GOES CONUS sector.
_EUROPE_BBOX = (10.0, 45.0, 11.0, 46.0)


def _fetch_goes(**kw: Any) -> Any:
    return TOOL_REGISTRY["fetch_goes_satellite"].fn(**kw)


def _synth_array_transform(bbox: tuple[float, float, float, float]) -> tuple[Any, Any]:
    arr = np.linspace(0.0, 0.8, 8 * 8, dtype="float32").reshape(8, 8)
    tr = _rt.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], 8, 8)
    return arr, tr


def _install_fake_delegate_socket(
    monkeypatch, *, keys: list[str] | None = None, array_transform=None
) -> None:
    """Patch the three delegate network seams: listing, download, reproject."""
    default_key = (
        "ABI-L2-MCMIPC/2024/180/12/"
        "OR_ABI-L2-MCMIPC-M6_G19_s20241801201176_e20241801203560_c20241801204045.nc"
    )
    monkeypatch.setattr(gs, "_list_recent_keys", lambda *_a, **_k: list(keys or [default_key]))
    monkeypatch.setattr(gs, "_download_to_tempfile", lambda *_a, **_k: "/tmp/does-not-exist.nc")

    def _fake_reproject(nc_path, variable, bbox, target_res_deg=0.02):
        arr, tr = array_transform or _synth_array_transform(bbox)
        return arr, tr, "EPSG:4326"

    monkeypatch.setattr(gs, "_reproject_and_clip", _fake_reproject)


# --------------------------------------------------------------------------- #
# Registry shape.
# --------------------------------------------------------------------------- #


def test_goes_satellite_registered_with_expected_metadata() -> None:
    assert "fetch_goes_satellite" in TOOL_REGISTRY
    md = TOOL_REGISTRY["fetch_goes_satellite"].metadata
    assert md.ttl_class == "dynamic-1h"
    assert md.source_class == "goes_satellite"
    assert md.cacheable is True
    assert getattr(md, "supports_global_query", None) is False
    assert getattr(md, "payload_mb_estimator_name", None) == "estimate_payload_mb"


# --------------------------------------------------------------------------- #
# Typed-error envelope.
# --------------------------------------------------------------------------- #


def test_typed_error_hierarchy() -> None:
    assert issubclass(GOESInputError, GOESError)
    assert issubclass(GOESBboxRequiredError, GOESError)
    assert issubclass(GOESUpstreamError, GOESError)
    assert issubclass(GOESEmptyError, GOESError)


@pytest.mark.parametrize(
    "cls, code, retryable",
    [
        (GOESError, "GOES_SATELLITE_ERROR", True),
        (GOESBboxRequiredError, "BBOX_REQUIRED", False),
        (GOESInputError, "GOES_INPUT_INVALID", False),
        (GOESUpstreamError, "GOES_UPSTREAM_ERROR", True),
        (GOESEmptyError, "GOES_EMPTY", False),
    ],
)
def test_typed_error_envelope(cls: type, code: str, retryable: bool) -> None:
    err = cls("boom")
    assert err.error_code == code
    assert err.retryable is retryable
    assert isinstance(err, RuntimeError)
    assert isinstance(err, FetchError)  # library_delegate.invoke passes the code through


def test_estimate_payload_mb() -> None:
    assert gs.estimate_payload_mb(bbox=None) == 5.0
    small = gs.estimate_payload_mb(bbox=(-82.0, 26.0, -81.99, 26.01))
    big = gs.estimate_payload_mb(bbox=_FL_BBOX)
    assert 0.0 < small < big
    assert gs.estimate_payload_mb(bbox=(-82.0, 26.0, -82.0, 26.0)) >= 0.05  # floor


# --------------------------------------------------------------------------- #
# Input validation (``goes_satellite.validate``, pre-cache).
# --------------------------------------------------------------------------- #


def test_validate_hook_bbox_none_raises_bbox_required() -> None:
    with pytest.raises(GOESBboxRequiredError) as ei:
        gs.validate_goes_satellite(None, {"bbox": None})
    assert ei.value.error_code == "BBOX_REQUIRED"


@pytest.mark.parametrize(
    "bad_bbox",
    [
        (1.0, 2.0, 3.0),  # wrong arity
        (-82.0, 26.0, 270.0, 28.0),  # lon out of range
        (float("nan"), 26.0, -80.0, 28.0),  # non-finite
        (-82.0, 26.0, -82.0, 26.0),  # degenerate
    ],
)
def test_validate_hook_bad_bbox_shape(bad_bbox: tuple[float, ...]) -> None:
    with pytest.raises(GOESInputError) as ei:
        gs.validate_goes_satellite(None, {"bbox": bad_bbox})
    assert ei.value.error_code == "GOES_INPUT_INVALID"


def test_validate_hook_unknown_band() -> None:
    with pytest.raises(GOESInputError, match="unknown band"):
        gs.validate_goes_satellite(None, {"bbox": _FL_BBOX, "band": "ultraviolet"})


def test_validate_hook_unknown_satellite() -> None:
    with pytest.raises(GOESInputError, match="unknown satellite"):
        gs.validate_goes_satellite(None, {"bbox": _FL_BBOX, "satellite": "himawari-9"})


@pytest.mark.parametrize("bad_res", [0.0, -1.0, float("nan"), float("inf")])
def test_validate_hook_bad_target_res_deg(bad_res: float) -> None:
    with pytest.raises(GOESInputError, match="target_res_deg"):
        gs.validate_goes_satellite(None, {"bbox": _FL_BBOX, "target_res_deg": bad_res})


def test_validate_hook_off_conus_bbox_raises_empty_pre_gate() -> None:
    with pytest.raises(GOESEmptyError) as ei:
        gs.validate_goes_satellite(None, {"bbox": _EUROPE_BBOX})
    assert ei.value.error_code == "GOES_EMPTY"
    assert ei.value.retryable is False


def test_validate_hook_healthy_conus_bbox_passes() -> None:
    gs.validate_goes_satellite(None, {"bbox": _FL_BBOX})  # no raise


# --------------------------------------------------------------------------- #
# pre_resolve (satellite canon + 15-min valid_time rounding).
# --------------------------------------------------------------------------- #


def test_resolve_hook_normalizes_satellite() -> None:
    out = gs.resolve_goes_satellite(None, {"satellite": "GOES-18"})
    assert out["satellite"] == "goes-18"
    assert out["valid_time"].endswith("Z")


def test_resolve_hook_default_satellite() -> None:
    out = gs.resolve_goes_satellite(None, {})
    assert out["satellite"] == "goes-19"


def test_round_valid_time_rounds_down_to_15min_boundary() -> None:
    pin = datetime(2026, 6, 8, 12, 7, 30, tzinfo=timezone.utc)
    assert gs._round_valid_time(pin) == "2026-06-08T12:00:00Z"

    pin2 = datetime(2026, 6, 8, 12, 17, 30, tzinfo=timezone.utc)
    assert gs._round_valid_time(pin2) == "2026-06-08T12:15:00Z"

    pin3 = datetime(2026, 6, 8, 12, 45, 0, tzinfo=timezone.utc)
    assert gs._round_valid_time(pin3) == "2026-06-08T12:45:00Z"


def test_round_valid_time_handles_naive_datetime_as_utc() -> None:
    pin = datetime(2026, 6, 8, 12, 7, 30)  # naive
    assert gs._round_valid_time(pin) == "2026-06-08T12:00:00Z"


def test_scan_time_iso_parses_start_token() -> None:
    token = "2024180120117"  # 13 digits: YYYY JJJ HH MM SS
    got = gs._scan_time_iso(token)
    expected = (
        datetime(2024, 1, 1, tzinfo=timezone.utc)
        + timedelta(days=180 - 1, hours=12, minutes=1, seconds=17)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert got == expected


def test_scan_time_iso_rejects_short_or_empty_token() -> None:
    assert gs._scan_time_iso("") is None
    assert gs._scan_time_iso("123") is None


def test_pick_most_recent_key_picks_largest_start_time() -> None:
    keys = [
        "ABI-L2-MCMIPC/2024/180/12/OR_ABI-L2-MCMIPC-M6_G16_s20241801201176_e..._c....nc",
        "ABI-L2-MCMIPC/2024/180/12/OR_ABI-L2-MCMIPC-M6_G16_s20241801206176_e..._c....nc",
        "ABI-L2-MCMIPC/2024/180/12/OR_ABI-L2-MCMIPC-M6_G16_s20241801211176_e..._c....nc",
    ]
    chosen = gs._pick_most_recent_key(keys)
    assert "s20241801211176" in chosen


def test_pick_most_recent_key_empty_returns_empty_string() -> None:
    assert gs._pick_most_recent_key([]) == ""
    assert gs._pick_most_recent_key(["no_start_time_substring.nc"]) == ""


def test_list_recent_keys_returns_first_nonempty_probe(monkeypatch) -> None:
    calls: list[str] = []

    def _fake_list(bucket, prefix):
        calls.append(prefix)
        if len(calls) < 2:
            return []
        return ["ABI-L2-MCMIPC/x/OR_ABI-L2-MCMIPC-M6_G19_s20241801201176_e..._c....nc"]

    monkeypatch.setattr(gs, "_list_keys_for_prefix", _fake_list)
    keys = gs._list_recent_keys("goes-19", now=datetime(2024, 6, 28, 12, 5, tzinfo=timezone.utc))
    assert len(keys) == 1
    assert len(calls) == 2  # first probe empty, second (hour-back) hit


def test_list_recent_keys_raises_empty_when_all_probes_empty(monkeypatch) -> None:
    monkeypatch.setattr(gs, "_list_keys_for_prefix", lambda bucket, prefix: [])
    with pytest.raises(GOESEmptyError, match="no MCMIPC keys"):
        gs._list_recent_keys("goes-19", lookback_hours=1)


def test_list_recent_keys_propagates_upstream_error_when_never_recovers(monkeypatch) -> None:
    def _always_fails(bucket, prefix):
        raise GOESUpstreamError(f"listing failed for {prefix}")

    monkeypatch.setattr(gs, "_list_keys_for_prefix", _always_fails)
    with pytest.raises(GOESUpstreamError):
        gs._list_recent_keys("goes-19", lookback_hours=1)


# --------------------------------------------------------------------------- #
# Pure envelope hook (layer_id / the em-dash name / provenance replay).
# --------------------------------------------------------------------------- #


def test_envelope_hook_layer_id_and_em_dash_name() -> None:
    params = {"band": "visible", "satellite": "goes-19", "bbox": _FL_BBOX}
    out = gs.envelope_goes_satellite(
        None, params, None, None,
        provenance={"satellite": "goes-19", "band": "visible", "scan_time": "2024-06-28T12:01:17Z"},
    )
    assert out["layer_id"] == "goes-goes-19-visible--82.0000-26.0000"
    assert out["name"] == "GOES Satellite — Visible (Band 2) (GOES-19)"
    assert "—" in out["name"]
    assert "-" not in out["name"].split("—")[0]  # no ASCII hyphen stand-in before the dash
    assert out["satellite"] == "goes-19"
    assert out["band"] == "visible"
    assert out["scan_time"] == "2024-06-28T12:01:17Z"


def test_envelope_hook_pre_channel_defaults() -> None:
    params = {"band": "ir_window", "satellite": "goes-18", "bbox": _FL_BBOX}
    out = gs.envelope_goes_satellite(None, params, None, None, provenance=None)
    assert out["satellite"] == "goes-18"
    assert out["band"] == "ir_window"
    assert out["scan_time"] is None
    assert "IR Window (Band 13)" in out["name"]


# --------------------------------------------------------------------------- #
# END-TO-END via the promoted router closure (no real netCDF; fake_s3).
# --------------------------------------------------------------------------- #


def test_end_to_end_visible_band_happy_path(monkeypatch, fake_s3) -> None:
    _install_fake_delegate_socket(monkeypatch)

    res = _fetch_goes(bbox=_FL_BBOX)

    assert isinstance(res, GOESSatelliteLayerURI)
    assert res.layer_type == "raster"
    assert res.role == "context"
    assert res.style_preset == "goes_satellite"
    assert res.units == "reflectance"
    assert res.uri is not None and res.uri.endswith(".tif")
    assert res.layer_id == "goes-goes-19-visible--82.0000-26.0000"
    assert res.name == "GOES Satellite — Visible (Band 2) (GOES-19)"
    assert res.satellite == "goes-19"
    assert res.band == "visible"
    assert res.scan_time == "2024-06-28T12:01:17Z"

    tif_key = next(k for k in fake_s3.store if k.endswith(".tif"))
    assert "/dynamic-1h/goes_satellite/" in tif_key
    cog_bytes = fake_s3.store[tif_key]
    assert len(cog_bytes) > 0
    with rasterio.MemoryFile(cog_bytes) as mem, mem.open() as ds:
        assert ds.crs is not None and ds.crs.to_epsg() == 4326
        assert str(ds.dtypes[0]) == "float32"
        assert ds.count == 1


def test_end_to_end_ir_window_band(monkeypatch, fake_s3) -> None:
    _install_fake_delegate_socket(monkeypatch)
    res = _fetch_goes(bbox=_FL_BBOX, band="ir_window")
    assert res.units == "K"
    assert res.band == "ir_window"
    assert "IR Window (Band 13)" in res.name


def test_end_to_end_water_vapor_band(monkeypatch, fake_s3) -> None:
    _install_fake_delegate_socket(monkeypatch)
    res = _fetch_goes(bbox=_FL_BBOX, band="water_vapor")
    assert res.units == "K"
    assert "Water Vapor (Band 8)" in res.name


# --------------------------------------------------------------------------- #
# Typed errors via the full router drive.
# --------------------------------------------------------------------------- #


def test_end_to_end_bbox_none_raises_bbox_required(fake_s3) -> None:
    with pytest.raises(GOESBboxRequiredError) as ei:
        _fetch_goes(bbox=None)
    assert ei.value.error_code == "BBOX_REQUIRED"
    assert fake_s3.store == {}


def test_end_to_end_unknown_band_raises_input_error(fake_s3) -> None:
    with pytest.raises(GOESInputError) as ei:
        _fetch_goes(bbox=_FL_BBOX, band="ultraviolet")
    assert ei.value.error_code == "GOES_INPUT_INVALID"


def test_end_to_end_unknown_satellite_raises_input_error(fake_s3) -> None:
    with pytest.raises(GOESInputError) as ei:
        _fetch_goes(bbox=_FL_BBOX, satellite="himawari-9")
    assert ei.value.error_code == "GOES_INPUT_INVALID"


def test_end_to_end_degenerate_bbox_raises_input_error(fake_s3) -> None:
    """A degenerate-but-present bbox is caught by the router's OWN generic bbox
    gate (``RouterInputError``, source-stamped) before the delegate_validate hook
    ever runs -- still the pinned GOES_INPUT_INVALID code, just a different class
    than the hook's own ``GOESInputError`` (that class is proven directly by
    ``test_validate_hook_bad_bbox_shape`` above)."""
    with pytest.raises(FetchError) as ei:
        _fetch_goes(bbox=(-82.0, 26.0, -82.0, 26.0))
    assert ei.value.error_code == "GOES_INPUT_INVALID"
    assert ei.value.retryable is False


def test_end_to_end_off_conus_bbox_raises_empty_pre_gate(fake_s3) -> None:
    with pytest.raises(GOESEmptyError) as ei:
        _fetch_goes(bbox=_EUROPE_BBOX)
    assert ei.value.error_code == "GOES_EMPTY"
    assert fake_s3.store == {}


def test_end_to_end_upstream_listing_failure_propagates(monkeypatch, fake_s3) -> None:
    def _boom(*_a, **_k):
        raise GOESUpstreamError("S3 listing timed out")

    monkeypatch.setattr(gs, "_list_recent_keys", _boom)
    with pytest.raises(GOESUpstreamError) as ei:
        _fetch_goes(bbox=_FL_BBOX)
    assert ei.value.error_code == "GOES_UPSTREAM_ERROR"
    assert ei.value.retryable is True
    assert fake_s3.store == {}


def test_end_to_end_empty_window_propagates_from_delegate(monkeypatch, fake_s3) -> None:
    """Simulates the all-NaN-window branch inside ``_reproject_and_clip`` (which
    itself needs a real netCDF to exercise) by having the delegate raise the same
    typed error it would raise there; proves the router passthrough is intact."""
    default_key = (
        "ABI-L2-MCMIPC/2024/180/12/"
        "OR_ABI-L2-MCMIPC-M6_G19_s20241801201176_e20241801203560_c20241801204045.nc"
    )
    monkeypatch.setattr(gs, "_list_recent_keys", lambda *_a, **_k: [default_key])
    monkeypatch.setattr(gs, "_download_to_tempfile", lambda *_a, **_k: "/tmp/does-not-exist.nc")

    def _empty(*_a, **_k):
        raise GOESEmptyError("bbox produces no valid pixels")

    monkeypatch.setattr(gs, "_reproject_and_clip", _empty)
    with pytest.raises(GOESEmptyError) as ei:
        _fetch_goes(bbox=_FL_BBOX)
    assert ei.value.error_code == "GOES_EMPTY"
    assert ei.value.retryable is False


# --------------------------------------------------------------------------- #
# THE CHANNEL: cache-hit replay of satellite / band / scan_time.
# --------------------------------------------------------------------------- #


def test_cache_hit_replays_scan_provenance_identically(monkeypatch, fake_s3) -> None:
    """A second call with identical params is a CACHE HIT that never re-fetches,
    yet satellite/band/scan_time REPLAY IDENTICAL from the provenance sidecar
    (ADR 0110) -- scan_time is otherwise unrecoverable from the COG alone."""
    calls = {"n": 0}
    default_key = (
        "ABI-L2-MCMIPC/2024/180/12/"
        "OR_ABI-L2-MCMIPC-M6_G19_s20241801201176_e20241801203560_c20241801204045.nc"
    )
    monkeypatch.setattr(gs, "_list_recent_keys", lambda *_a, **_k: [default_key])
    monkeypatch.setattr(gs, "_download_to_tempfile", lambda *_a, **_k: "/tmp/does-not-exist.nc")

    def _counting_reproject(nc_path, variable, bbox, target_res_deg=0.02):
        calls["n"] += 1
        arr, tr = _synth_array_transform(bbox)
        return arr, tr, "EPSG:4326"

    monkeypatch.setattr(gs, "_reproject_and_clip", _counting_reproject)

    r1 = _fetch_goes(bbox=_FL_BBOX)
    assert calls["n"] == 1
    fields1 = (r1.satellite, r1.band, r1.scan_time)
    assert fields1 == ("goes-19", "visible", "2024-06-28T12:01:17Z")

    r2 = _fetch_goes(bbox=_FL_BBOX)
    assert calls["n"] == 1, "cache hit must NOT re-fetch"
    fields2 = (r2.satellite, r2.band, r2.scan_time)
    assert fields1 == fields2
