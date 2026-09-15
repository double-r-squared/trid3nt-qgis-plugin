"""The raw ABI archive fetcher: bands are a parameter, nothing is computed.

Covered: registration and the promoted signature; the step selection over the
archive's own cadence; the per-frame cache params, naming and validity windows;
the band description contract the derives read back; and the typed inputs and the
honesty floor. No network. ASCII only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
from trid3nt_server.tools.fetchers.imagery import _goes_archive_core as core
from trid3nt_server.tools.fetchers.imagery.fetch_goes_abi import hooks as ABI

_BBOX = [-108.0, 45.2, -107.2, 45.9]
_SPEC = reg.get_spec("fetch_goes_abi")
_WINDOW = ("2026-09-14T20:20:00Z", "2026-09-14T21:20:00Z")


def _scan(minute_offset: int) -> tuple[datetime, str]:
    """One archived CONUS scan, at the product's own five-minute cadence."""
    t = datetime(2026, 9, 14, 20, 20, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minute_offset)
    tag = t.strftime("%Y%j%H%M%S")
    return t, f"ABI-L2-MCMIPC/2026/257/20/OR_ABI-L2-MCMIPC-M6_G18_s{tag}0_e_c.nc"


@pytest.fixture()
def _stub_archive(monkeypatch):
    """Thirteen in-window scans at five minutes, and a cached frame per plan."""
    scans = [_scan(m) for m in range(0, 65, 5)]
    monkeypatch.setattr(
        core, "_list_archive_keys_in_window", lambda sat, s, e, session=None: scans)

    class _Result:
        def __init__(self, uri):
            self.uri = uri
            self.data = b"COG"

    def fake_read_through(metadata, params, ext, fetch_fn):
        return _Result(
            f"s3://trid3nt-cache/cache/dynamic-1h/{metadata.source_class}/"
            f"{params['ts_start']}.tif")

    monkeypatch.setattr(EX, "read_through", fake_read_through)
    return scans


def test_registered_on_the_frames_shape():
    assert "fetch_goes_abi" in TOOL_REGISTRY
    assert _SPEC is not None
    assert _SPEC.shape == "animation_frames"
    assert _SPEC.source_class == "goes_abi"
    assert _SPEC.error_code_prefix == "GOES_ABI"
    assert TOOL_REGISTRY["fetch_goes_abi"].fn.__annotations__["return"] is list


def test_signature_is_the_addressing_params_only():
    import inspect

    params = inspect.signature(TOOL_REGISTRY["fetch_goes_abi"].fn).parameters
    assert list(params) == [
        "bbox", "bands", "satellite", "start_utc", "end_utc", "step_minutes",
        "_extra_ignored",
    ]


def test_no_product_or_threshold_param_survives():
    """A band is addressed; a product, a stretch and a threshold are computed."""
    assert not ({"band", "product", "bt_c07_min_k", "bt_diff_min_k",
                 "true_color_res_deg"} & set(_SPEC.params))


def test_step_keeps_one_scan_per_step_instant(_stub_archive):
    layers = TOOL_REGISTRY["fetch_goes_abi"].fn(
        bbox=_BBOX, bands=[7, 14], start_utc=_WINDOW[0], end_utc=_WINDOW[1],
        step_minutes=20)
    assert [l.name.split(" step ")[1].split(" ")[1] for l in layers] == [
        "2026-09-14T20:20:00Z", "2026-09-14T20:40:00Z",
        "2026-09-14T21:00:00Z", "2026-09-14T21:20:00Z",
    ]


def test_step_finer_than_the_cadence_returns_the_native_scans(_stub_archive):
    layers = TOOL_REGISTRY["fetch_goes_abi"].fn(
        bbox=_BBOX, bands=[7], start_utc=_WINDOW[0], end_utc=_WINDOW[1],
        step_minutes=1)
    assert len(layers) == len(_stub_archive)


def test_frames_declare_a_playable_sequence(_stub_archive):
    layers = TOOL_REGISTRY["fetch_goes_abi"].fn(
        bbox=_BBOX, bands=[7, 14], start_utc=_WINDOW[0], end_utc=_WINDOW[1],
        step_minutes=20)
    windows = [(l.valid_from, l.valid_to) for l in layers]
    assert all(None not in w for w in windows)
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:]))


def test_cache_params_carry_the_bands_and_the_key_stays_out_of_them(_stub_archive):
    plans = ABI.frames_plan(
        _SPEC, {"bbox": _BBOX, "bands": [1.0, 2.0, 3.0], "satellite": "goes-18",
                "start_utc": _WINDOW[0], "end_utc": _WINDOW[1], "step_minutes": 20})
    first = plans[0]
    assert first.cache_params["bands"] == [1, 2, 3]
    assert first.cache_params["satellite"] == "goes-18"
    assert "key" not in first.cache_params
    assert first.fetch_context["key"].endswith(".nc")
    assert first.fetch_context["bands"] == (1, 2, 3)


def test_band_descriptions_state_number_wavelength_and_quantity():
    assert ABI.band_description(2) == "ABI band 2 0.64 um reflectance factor"
    assert ABI.band_description(14) == "ABI band 14 11.2 um brightness temperature"
    assert ABI.band_units(2) == "1"
    assert ABI.band_units(14) == "K"


def test_an_unknown_band_number_is_refused_pre_network():
    with pytest.raises(RouterInputError) as caught:
        TOOL_REGISTRY["fetch_goes_abi"].fn(bbox=_BBOX, bands=[7, 17])
    assert caught.value.error_code == "GOES_ABI_INPUT_INVALID"
    assert caught.value.retryable is False


def test_a_degenerate_bbox_is_refused_pre_network():
    with pytest.raises(RouterInputError) as caught:
        TOOL_REGISTRY["fetch_goes_abi"].fn(
            bbox=[-100.0, 40.0, -100.0, 41.0], bands=[7])
    assert caught.value.error_code == "GOES_ABI_INPUT_INVALID"


def test_a_backwards_window_is_refused(monkeypatch):
    monkeypatch.setattr(
        core, "_list_archive_keys_in_window", lambda *a, **k: [_scan(0)])
    with pytest.raises(RouterInputError):
        TOOL_REGISTRY["fetch_goes_abi"].fn(
            bbox=_BBOX, bands=[7], start_utc=_WINDOW[1], end_utc=_WINDOW[0])


def test_an_empty_window_raises_the_typed_empty_error(monkeypatch):
    from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError

    monkeypatch.setattr(core, "_list_archive_keys_in_window", lambda *a, **k: [])
    with pytest.raises(RouterEmptyError) as caught:
        TOOL_REGISTRY["fetch_goes_abi"].fn(
            bbox=_BBOX, bands=[7], start_utc=_WINDOW[0], end_utc=_WINDOW[1])
    assert caught.value.error_code == "GOES_ABI_EMPTY"


def test_every_corpus_phrasing_is_present():
    assert len(_SPEC.corpus) >= 6
    assert all(isinstance(q, str) and q.strip() for q in _SPEC.corpus)


def test_observation_tags_carry_the_instant_and_the_subpoint(tmp_path):
    """The file states no solar or view angle of its own, so the layer carries the
    two values the angles are computed from instead."""
    import netCDF4

    path = tmp_path / "scan.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.time_coverage_start = "2026-09-14T20:41:17.0Z"
        ds.createVariable("nominal_satellite_subpoint_lon", "f4")[...] = -137.0
    assert ABI.observation_tags(str(path)) == {
        "scan_time_utc": "2026-09-14T20:41:17.0Z",
        "satellite_subpoint_lon": "-137.0",
    }
