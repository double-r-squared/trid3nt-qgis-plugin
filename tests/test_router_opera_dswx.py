"""Router coverage for fetch_opera_dswx -- OPERA surface water extent.

The V1 DSWx products are served from an Earthdata-Login-gated bucket, so the
live read cannot run until ``~/.netrc`` carries a machine entry for
``urs.earthdata.nasa.gov``. The offline tests below pin the spec identity, the
param surface and the REFUSAL that names the file and the host; the live cell
SKIPS with that same reason until the credential exists.
"""

from __future__ import annotations

import netrc
import os

import pytest

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_EDL_HOST = "urs.earthdata.nasa.gov"
#: Peace River / Charlotte Harbor, FL -- a US basin the DSWx-HLS tiles cover.
_BBOX = [-82.2, 26.8, -81.8, 27.2]


def _netrc_has_edl() -> bool:
    try:
        return netrc.netrc(
            os.path.join(os.path.expanduser("~"), ".netrc")
        ).authenticators(_EDL_HOST) is not None
    except (OSError, netrc.NetrcParseError):
        return False


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


@pytest.mark.skipif(
    not _netrc_has_edl(),
    reason=f"no machine entry for {_EDL_HOST} in ~/.netrc "
           "(an Earthdata Login account is an interactive step)",
)
def test_live_dswx_fetch_over_a_us_basin(spec):
    params = router.validate_params(
        spec, {"bbox": _BBOX, "sensor": "hls",
               "start_date": "2026-07-01", "end_date": "2026-09-08"})
    data = router.select_executor(spec)(spec, params)
    import numpy as np
    from rasterio.io import MemoryFile

    with MemoryFile(data) as m, m.open() as src:
        assert src.count == 1 and src.dtypes[0] == "uint8"
        assert src.nodata == 255
        assert str(src.crs) == "EPSG:4326"
        assert bool((np.asarray(src.read(1)) != 255).any())
