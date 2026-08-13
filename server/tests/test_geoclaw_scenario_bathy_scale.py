"""Offline tests for the ADR 0230 follow-up: the SCENARIO-scale bathymetry mode.

A basin-scale scenario (a full-margin Slab2 rupture domain) threads a DECLARED
coarse bathymetry target into ``_fetch_topo_for_geoclaw`` -> ``fetch_topobathy``:
the domain-wide topo floors to the ~1 arcminute ETOPO deep-water class and SKIPS
the fine CUDEM 1/9" nearshore composite LOUDLY (the 0224 precedent). None keeps the
native full-resolution fetch. All offline (the registry ``fetch_topobathy`` closure
is swapped for a kwargs-capturing stub -- no network, no MinIO, no clawpack).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.workflows.geoclaw.inundation import inundation as I


def _install_capture(monkeypatch) -> dict:
    """Swap the registry ``fetch_topobathy`` for a stub that records its kwargs and
    returns a minimal layer with a ``uri``. Returns the capture dict."""
    captured: dict = {}

    def _stub(bbox=None, **kw):
        captured["bbox"] = bbox
        captured["kw"] = dict(kw)
        return SimpleNamespace(uri="s3://trid3nt-cache/stub/topo.tif")

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_topobathy", SimpleNamespace(fn=_stub)
    )
    return captured


_BBOX = (-127.8, 41.9, -122.1, 48.1)  # a full-margin Cascadia domain


def test_native_fetch_never_skips_cudem(monkeypatch):
    """target_resolution_m=None -> the native fetch: no min_pixel_m floor, CUDEM
    is NOT skipped (byte-identical to the pre-follow-up default)."""
    cap = _install_capture(monkeypatch)
    uri, label = I._fetch_topo_for_geoclaw(_BBOX, force_bathy_base=True)
    assert uri == "s3://trid3nt-cache/stub/topo.tif"
    assert "skip_cudem" not in cap["kw"]
    assert "min_pixel_m" not in cap["kw"]
    assert cap["kw"]["force_bathy_base"] is True
    # the native label still advertises the CUDEM seamless composite.
    assert "CUDEM" in label and "skipped" not in label.lower()


def test_scenario_scale_target_skips_cudem_and_floors(monkeypatch):
    """A basin-scale coarse target floors the composite AND skips CUDEM, and the
    source label honestly names the coarse ETOPO-only column."""
    cap = _install_capture(monkeypatch)
    res = I._SCENARIO_BATHY_TARGET_RES_M
    uri, label = I._fetch_topo_for_geoclaw(
        _BBOX, force_bathy_base=True, target_resolution_m=res
    )
    assert cap["kw"]["skip_cudem"] is True
    # min_pixel_m carries the true basin floor (no upper bound); resolution_m only
    # sets the 3DEP land leg and is clamped to the fetch_topobathy spec max (1000 m).
    assert cap["kw"]["min_pixel_m"] == pytest.approx(res)
    assert cap["kw"]["resolution_m"] == min(int(res), 1000)
    assert cap["kw"]["resolution_m"] <= 1000
    assert cap["kw"]["force_bathy_base"] is True
    # label surfaces the honest coarse column (rides into the input-layer name).
    assert "ETOPO" in label and "CUDEM skipped" in label
    assert f"{res:.0f}" in label


def test_below_threshold_target_floors_but_keeps_cudem(monkeypatch):
    """A fine explicit target still floors the composite but KEEPS CUDEM (below the
    0224 skip threshold CUDEM materially refines the nearshore)."""
    cap = _install_capture(monkeypatch)
    fine = I._GEOCLAW_CUDEM_SKIP_RES_M - 100.0  # below the skip threshold
    _uri, label = I._fetch_topo_for_geoclaw(
        _BBOX, force_bathy_base=True, target_resolution_m=fine
    )
    assert cap["kw"]["min_pixel_m"] == pytest.approx(fine)
    assert "skip_cudem" not in cap["kw"]
    assert "CUDEM" in label and "skipped" not in label.lower()


def test_scenario_default_is_at_or_above_skip_threshold():
    """The declared scenario default MUST skip CUDEM (>= the 0224 threshold) and sit
    in the ~1-2 km arcminute basin class the follow-up justifies."""
    assert I._SCENARIO_BATHY_TARGET_RES_M >= I._GEOCLAW_CUDEM_SKIP_RES_M
    assert 1000.0 <= I._SCENARIO_BATHY_TARGET_RES_M <= 2000.0


def test_cudem_skip_is_loud(monkeypatch, caplog):
    """The CUDEM skip is LOUD: a basin-scale target logs an INFO line naming the
    skip + the deep-water-only column so the status machinery is honest."""
    _install_capture(monkeypatch)
    with caplog.at_level(logging.INFO):
        I._fetch_topo_for_geoclaw(
            _BBOX, force_bathy_base=True,
            target_resolution_m=I._SCENARIO_BATHY_TARGET_RES_M,
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "CUDEM" in msg and "SKIPPED" in msg
    assert "deep-water" in msg
