"""``fetch_opera_dswx``: the surface-water-extent products.

The bucket is login-gated, so these pin the spec identity, the param surface and the
REFUSAL that names the credential file and the host it is wanted for."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_EDL_HOST = "urs.earthdata.nasa.gov"
#: Peace River / Charlotte Harbor, FL -- a US basin the DSWx-HLS tiles cover.
_BBOX = [-82.2, 26.8, -81.8, 27.2]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_opera_dswx"]


def test_spec_identity(spec):
    assert spec.name == "fetch_opera_dswx"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "OPERA_DSWX"
    assert spec.empty_error_suffix == "NO_OBSERVATION"
    assert spec.ingest["access"] == "stac"
    assert spec.ingest["render"] == "mosaic"
    assert spec.ingest["native_cell_m"] == 30.0
    assert spec.vertical_datum.startswith("n/a")
    assert spec.output.style["kind"] == "classed"


def test_both_collections_are_declared(spec):
    m = spec.ingest["stac"]["collection_by_param"]["map"]
    assert m["hls"] == "OPERA_L3_DSWX-HLS_V1_1.0"
    assert m["s1"] == "OPERA_L3_DSWX-S1_V1_1.0"
    # the water band's asset key is numbered per product, so it is matched on
    # the suffix the two share
    assert spec.ingest["stac"]["asset_suffix"] == "_B01_WTR"


def test_sensor_aliases_normalize(spec):
    assert router.validate_params(spec, {"bbox": _BBOX, "sensor": "SAR"})["sensor"] == "s1"
    assert router.validate_params(spec, {"bbox": _BBOX, "sensor": "optical"})["sensor"] == "hls"
    assert router.validate_params(spec, {"bbox": _BBOX})["sensor"] == "hls"


def test_bad_sensor_is_typed_input_error(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": _BBOX, "sensor": "lidar"})
    assert getattr(ei.value, "error_code", "") == "OPERA_DSWX_SENSOR_INVALID"


def test_missing_earthdata_credential_refuses_by_name(spec, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    params = router.validate_params(spec, {"bbox": _BBOX})
    with pytest.raises(Exception) as ei:
        router.select_executor(spec)(spec, params)
    msg = str(ei.value)
    assert _EDL_HOST in msg and ".netrc" in msg
    assert "interactive" in msg
    assert getattr(ei.value, "error_code", "") == "OPERA_DSWX_UPSTREAM_ERROR"
