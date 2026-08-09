"""Offline registration tests for the telemac_rain_on_grid template (ADR 0196 C3).

Live end-to-end (mesh acquisition + solve + depth COG) is proven on Coweeta Creek
NC by scripts/sandbox/telemac/rog_coweeta_live.py (docs/proof/templates/
telemac_rain_on_grid*.png); the worker RoG deck THROUGH the image by
scripts/sandbox/telemac/rog_offline_smoke.py. These tests pin the offline
registration surface only (no network / no solver)."""

from __future__ import annotations

import pytest


def test_registered_as_telemac_template():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    assert "telemac_rain_on_grid" in TOOL_REGISTRY
    md = TOOL_REGISTRY["telemac_rain_on_grid"].metadata
    assert md.engine == "telemac"
    assert md.tier == "template"
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"
    assert md.source_class == "workflow_dispatch"


def test_docstring_carries_the_godara_envelope():
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    doc = telemac_rain_on_grid.__doc__ or ""
    # the applicability envelope must be baked into the routing docstring.
    assert "RAIN" in doc and "watershed" in doc.lower()
    assert "hydrograph" in doc.lower()
    assert "SCS" in doc or "curve-number" in doc.lower() or "curve number" in doc.lower()


def test_fetch_hyetograph_blocks_builds_hourly_blocks(monkeypatch):
    """ADR 0206: an MRMS/AORC window -> one 3600-s gross-mm block per hour, and
    the sim length is at least the hyetograph span."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.workflows.telemac.rain_on_grid import rain_on_grid as ROG

    class _Stub:
        fn = staticmethod(lambda **kw: {
            "times": ["2015-12-23T18:00", "2015-12-23T19:00", "2015-12-23T20:00"],
            "precip_mm": [3.0, 12.5, 0.0]})

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_aorc_precip", _Stub())
    blocks, mm, sim_s = ROG._fetch_hyetograph_blocks(
        (-83.47, 35.02, -83.42, 35.06), "2015-12-23/2015-12-24", 0.0)
    assert mm == [3.0, 12.5, 0.0]
    assert blocks == [[3600.0, 3.0], [7200.0, 12.5], [10800.0, 0.0]]
    assert sim_s == 3 * 3600.0  # hyetograph span dominates the 0 hint


def test_fetch_hyetograph_blocks_rejects_bad_window(monkeypatch):
    from trid3nt_server.agent.workflows.telemac.rain_on_grid import rain_on_grid as ROG
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid import (
        TelemacRainOnGridError,
    )
    with pytest.raises(TelemacRainOnGridError):
        ROG._fetch_hyetograph_blocks((-83.4, 35.0, -83.3, 35.1), "no-separator", 0.0)


def test_amc_word_maps_to_scs_condition():
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid import _AMC

    assert _AMC["dry"] == 1 and _AMC["normal"] == 2 and _AMC["wet"] == 3
    assert _AMC["i"] == 1 and _AMC["iii"] == 3


def test_utm_epsg_from_coweeta_pour_point():
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid import (
        _guess_utm_epsg,
    )

    # Coweeta Creek NC -> UTM zone 17 N -> EPSG:32617.
    assert _guess_utm_epsg((-83.40402, 35.05746)) == 32617


def test_corpus_yaml_present_and_routes():
    import yaml
    from pathlib import Path
    import trid3nt_server.agent.workflows.telemac.rain_on_grid as pkg

    corpus = Path(pkg.__file__).parent / "corpus.yaml"
    assert corpus.exists()
    data = yaml.safe_load(corpus.read_text())
    assert "telemac_rain_on_grid" in data
    assert any("runoff" in q.lower() for q in data["telemac_rain_on_grid"])


def test_category_mapping():
    from trid3nt_server.agent.categories import PRIMARY_CATEGORY

    assert PRIMARY_CATEGORY.get("telemac_rain_on_grid") == "simulation_modeling"


def test_aoi_from_pour_point_buffers_the_outlet():
    """The pour-point AOI is a generous buffer around the OUTLET, kept under the
    0.3-deg watershed-primitive D8 clamp (bug 1: a town bbox clips the basin)."""
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid import (
        _aoi_from_pour_point,
        _ROG_POUR_BUFFER_DEG,
    )

    pp = (-83.40402, 35.05746)
    aoi = _aoi_from_pour_point(pp)
    # centered on the pour point.
    assert aoi[0] < pp[0] < aoi[2] and aoi[1] < pp[1] < aoi[3]
    # each side under the 0.3-deg D8 clamp.
    assert (aoi[2] - aoi[0]) <= 0.3 and (aoi[3] - aoi[1]) <= 0.3
    assert abs((aoi[2] - aoi[0]) - 2 * _ROG_POUR_BUFFER_DEG) < 1e-9


def test_pour_point_supplied_derives_aoi_from_it_not_geocode(monkeypatch):
    """When a pour point is supplied the analysis AOI must come FROM the pour
    point, NOT the geocoded place bbox (the ADR 0196 live bug: 'Otto, NC'
    geocodes to a town box that does not contain the upstream Coweeta catchment).
    geocode_location must not even be consulted on this path."""
    import asyncio

    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.workflows.telemac.rain_on_grid import (
        mesh_acquisition as MA,
    )
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid import (
        _aoi_from_pour_point,
        model_telemac_rain_on_grid,
    )

    pp = (-83.40402, 35.05746)

    def exploding_geocode(*_a, **_k):
        raise AssertionError("geocode_location must NOT be called when a pour "
                             "point is supplied")

    import dataclasses

    real_entry = TOOL_REGISTRY["geocode_location"]
    monkeypatch.setitem(
        TOOL_REGISTRY, "geocode_location",
        dataclasses.replace(real_entry, fn=exploding_geocode))

    captured: dict = {}

    def capture_mesh(*_a, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after AOI resolution")

    monkeypatch.setattr(MA, "acquire_watershed_mesh", capture_mesh, raising=True)

    with pytest.raises(RuntimeError, match="stop after AOI resolution"):
        asyncio.run(model_telemac_rain_on_grid(
            location="Otto, North Carolina", bbox=None, pour_point=pp,
            curve_number=None, antecedent_moisture="normal",
            design_storm_mm_per_hr=25.0, storm_duration_hr=6.0,
            sim_duration_hr=None, mrms_window=None, observed_gauge_id=None,
            mesh_uri=None, compute_class="medium",
        ))
    assert tuple(captured["bbox"]) == _aoi_from_pour_point(pp)
    assert tuple(captured["pour_point"]) == pp


def test_location_only_dispatch_matches_geocode_location_signature(monkeypatch):
    """Regression for the geocode_location(location=...) TypeError: the real
    registered fn's first required positional is ``query``, not ``location``.
    Stub signature is asserted against the real fn so any future drift in the
    real signature fails this test loudly instead of silently re-breaking the
    call site."""
    import inspect

    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.tools.fetchers.socioeconomic.geocode_location.geocode_location import (
        geocode_location as real_geocode_location,
    )

    def stub_geocode_location(query: str, **_extra_ignored):
        return {"bbox": [-83.42, 35.04, -83.38, 35.08]}

    # Fail loudly if the real registered tool's signature ever drifts away
    # from what this stub (and the call site) assume.
    real_sig = inspect.signature(real_geocode_location)
    stub_sig = inspect.signature(stub_geocode_location)
    assert list(real_sig.parameters)[0] == list(stub_sig.parameters)[0] == "query"

    class _Sentinel(Exception):
        pass

    def stub_mesh_acquisition(*_a, **_k):
        raise _Sentinel("reached mesh acquisition")

    import dataclasses

    real_entry = TOOL_REGISTRY["geocode_location"]
    stub_entry = dataclasses.replace(real_entry, fn=stub_geocode_location)
    monkeypatch.setitem(TOOL_REGISTRY, "geocode_location", stub_entry)

    from trid3nt_server.agent.workflows.telemac.rain_on_grid import (
        mesh_acquisition as MA,
    )
    monkeypatch.setattr(
        MA, "acquire_watershed_mesh", stub_mesh_acquisition, raising=True)

    from trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid import (
        model_telemac_rain_on_grid,
    )

    import asyncio

    with pytest.raises(_Sentinel):
        asyncio.run(model_telemac_rain_on_grid(
            location="Coweeta Creek NC", bbox=None, pour_point=None,
            curve_number=None, antecedent_moisture="normal",
            design_storm_mm_per_hr=25.0, storm_duration_hr=6.0,
            sim_duration_hr=None, mrms_window=None, observed_gauge_id=None,
            mesh_uri=None, compute_class="medium",
        ))
