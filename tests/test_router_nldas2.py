"""Offline tests for the NLDAS-2 forcing row, no live calls.

The three things the hook owns: the Earthdata Login check that runs BEFORE the read
(the service answers an unauthenticated request 200 with its sign-in page, which the
library's parser reports as a column count), the variable vocabulary read off the
library's own table, and the AOI mean.
"""

from __future__ import annotations

import sys
import types

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing import hooks as nldas2

_SPEC = compose_specs_from_tree()["fetch_nldas2_forcing"]
_PARAMS = {"bbox": [-95.6, 29.6, -95.2, 30.0], "start_date": "2017-08-26",
           "end_date": "2017-08-27"}


@pytest.fixture
def _logged_in(monkeypatch):
    monkeypatch.setattr(nldas2, "_require_earthdata_login", lambda spec: None)


def _dataset(times, prcp, temp):
    xr = pytest.importorskip("xarray")
    import numpy as np

    return xr.Dataset(
        {
            "prcp": (("time", "y", "x"), np.asarray(prcp, dtype="float64")),
            "temp": (("time", "y", "x"), np.asarray(temp, dtype="float64")),
        },
        coords={"time": np.asarray(times, dtype="datetime64[ns]"),
                "y": [0.0, 1.0], "x": [0.0, 1.0]},
    )


def _stub_library(monkeypatch, ds):
    mod = types.ModuleType("pynldas2")
    mod.get_bygeom = lambda *a, **k: ds
    monkeypatch.setitem(sys.modules, "pynldas2", mod)
    return mod


def test_missing_earthdata_login_refuses_by_name(monkeypatch, tmp_path):
    """A 200 carrying a sign-in page is not data, so the login is checked first."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(RouterUpstreamError) as ei:
        nldas2._require_earthdata_login(_SPEC)
    assert ei.value.error_code == "NLDAS2_UPSTREAM_ERROR"
    assert "urs.earthdata.nasa.gov" in str(ei.value)
    assert str(tmp_path / ".netrc") in str(ei.value)


def test_unknown_variable_is_refused_by_name():
    with pytest.raises(RouterInputError) as ei:
        nldas2._variables(_SPEC, {"variables": ["prcp", "snowfall"]})
    assert ei.value.error_code == "NLDAS2_INPUT_INVALID"
    assert "snowfall" in str(ei.value)


def test_the_vocabulary_is_the_library_s_own():
    """Restating the units here would let them drift from what the service sends."""
    from pynldas2.pynldas2 import NLDAS2_VARS

    assert nldas2._vocabulary() is NLDAS2_VARS
    assert nldas2._variables(_SPEC, {}) == ["prcp", "temp"]


def test_the_record_is_the_aoi_mean_series(monkeypatch, _logged_in):
    ds = _dataset(
        ["2017-08-26T00", "2017-08-26T01"],
        [[[1.0, 2.0], [3.0, 4.0]], [[0.0, 0.0], [0.0, 4.0]]],
        [[[300.0, 300.0], [300.0, 300.0]], [[301.0, 301.0], [301.0, 301.0]]],
    )
    _stub_library(monkeypatch, ds)
    rec = nldas2.build_record(_SPEC, _PARAMS, [])
    assert rec["n_hours"] == 2 and rec["n_cells"] == 4
    assert rec["series"]["prcp"]["values"] == [2.5, 1.0]
    assert rec["series"]["temp"]["values"] == [300.0, 301.0]
    assert rec["series"]["prcp"]["units"] == "kg/m^2"
    assert rec["precip_total_mm"] == 3.5
    assert rec["times"][0].startswith("2017-08-26T00")


def test_a_window_with_no_hours_is_a_typed_empty(monkeypatch, _logged_in):
    import numpy as np

    empty = np.zeros((0, 2, 2), dtype="float64")
    _stub_library(monkeypatch, _dataset(np.array([], dtype="datetime64[ns]"), empty, empty))
    with pytest.raises(Exception) as ei:
        nldas2.build_record(_SPEC, _PARAMS, [])
    assert getattr(ei.value, "error_code", "") == "NLDAS2_NO_HOURS"


def test_a_library_failure_becomes_a_typed_upstream_error(monkeypatch, _logged_in):
    mod = types.ModuleType("pynldas2")

    def boom(*a, **k):
        raise RuntimeError("NLDASServiceError: service is down")

    mod.get_bygeom = boom
    monkeypatch.setitem(sys.modules, "pynldas2", mod)
    with pytest.raises(RouterUpstreamError) as ei:
        nldas2.build_record(_SPEC, _PARAMS, [])
    assert "service is down" in str(ei.value)
