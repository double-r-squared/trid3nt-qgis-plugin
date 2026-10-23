"""The published USGS model K in the MODFLOW templates: frames paired by layer id, and the K pair or transmissivity.

Offline: the frame ids are the ones the two fetch rows state; the Des Moines frames are the fetchers' own over the ring excerpt.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules.gwf import _paired
from trid3nt_server.workflows.runtime.sheet import fill

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usgs_model_k"
_DM_BBOX = [-93.6729, 41.6625, -93.6369, 41.6895]
_DM_PROBE = (-93.655, 41.676)


def _k(n):
    return {"layer_id": f"model-k-des_moines-l{n}--93.673-41.663", "name": f"k{n}"}


def _surface(n):
    stem = "top-l1" if n == 0 else f"bot-l{n}"
    return {"layer_id": f"model-layers-des_moines-{stem}--93.673-41.663", "name": f"s{n}"}


def test_frames_pair_by_layer_id_never_by_position():
    got = _paired([_k(4), _k(1), _k(2)], [_surface(n) for n in (3, 0, 4, 1, 2)], names=("k", "s"))
    assert [k["name"] if isinstance(k, dict) else k for k in got["layers"][:2]] == ["k1", "k2"]
    assert math.isnan(got["layers"][2]) and got["layers"][3]["name"] == "k4"
    assert [s["name"] for s in got["surfaces"]] == ["s0", "s1", "s2", "s3", "s4"]


def test_a_layer_or_surface_with_no_frame_is_unmeasured():
    got = _paired([_k(1), _k(3)], [_surface(0), _surface(1), _surface(3)], names=("k", "s"))
    assert math.isnan(got["layers"][1]) and len(got["layers"]) == 3
    assert math.isnan(got["surfaces"][2]) and len(got["surfaces"]) == 4


def test_a_frame_that_states_no_layer_refuses_by_name():
    with pytest.raises(ModflowError) as refused:
        _paired([{"layer_id": "elsewhere"}], [_surface(0), _surface(1)], names=("k", "s"))
    assert "elsewhere" in str(refused.value)


def test_a_list_of_frames_that_holds_none_refuses():
    with pytest.raises(ModflowError):
        _paired([], [_surface(0)], names=("k", "s"))


def _sheet(template, frames, transmissivity, water_table=None, water_table_depth=2.0):
    workflow = TOOL_REGISTRY[template].fn.workflow
    run = {"conductivity_layers": [_k(1), _k(2)] if frames else None,
           "layer_surfaces": [_surface(n) for n in range(3)] if frames else None,
           "transmissivity": transmissivity, "saturated_thickness": 30.0, "water_table": water_table,
           "water_table_depth": water_table_depth, "layers": 2,
           "until": "1994-10-31", "time_steps": 2, "event_time": "1994-01-01T00:00:00+00:00"}
    return fill(workflow.steering, template=template, produced=run, params={})


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_inside_a_published_grid_the_sheet_states_conductivity_by_depth_and_no_transmissivity(template):
    statement = _sheet(template, True, None).filled["NPF_K"].value
    assert statement["cells"] == "by_depth"
    assert [k["name"] for k in statement["of"]["layers"]] == ["k1", "k2"]
    assert [s["name"] for s in statement["of"]["surfaces"]] == ["s0", "s1", "s2"]


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_outside_every_grid_the_sheet_states_transmissivity_as_before(template):
    statement = _sheet(template, False, 100.0).filled["NPF_K"].value
    assert statement["cells"] == "quotient" and statement["of"][0] == 100.0


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_inside_a_published_grid_the_sheet_states_the_head_as_an_elevation_and_no_depth(template):
    filled = _sheet(template, True, None, water_table=12.0, water_table_depth=None).filled
    assert filled["IC_STRT"].value == {"cells": "elevation", "of": 12.0}
    assert filled["DISV_BOTM"].value == {"cells": "elevation", "of": [12.0, 30.0]}


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_outside_every_grid_the_sheet_states_the_depth_below_the_top_as_before(template):
    filled = _sheet(template, False, 100.0).filled
    assert filled["IC_STRT"].value == {"cells": "below_top", "of": 2.0}
    assert filled["DISV_BOTM"].value == {"cells": "below_top", "of": [2.0, 30.0]}


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_a_deck_stating_an_elevation_and_a_depth_refuses_rather_than_choosing(template):
    with pytest.raises(ModflowError) as refused:
        _sheet(template, True, None, water_table=12.0)
    assert "both water_table and water_table_depth" in str(refused.value)


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_a_deck_stating_a_conductivity_pair_and_transmissivity_refuses_rather_than_choosing(template):
    with pytest.raises(ModflowError) as refused:
        _sheet(template, True, 100.0)
    assert "both conductivity and transmissivity" in str(refused.value)


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_a_deck_stating_neither_refuses(template):
    with pytest.raises(ModflowError) as refused:
        _sheet(template, False, None)
    assert "neither conductivity nor transmissivity" in str(refused.value)


class _Held:
    def __init__(self, uri, data=b""):
        self.uri, self.data = uri, data


def _ring_frames(monkeypatch):
    """The Des Moines fetchers' own frames over the ring excerpt, offline, with each frame's COG bytes by uri."""
    import trid3nt_server.tools.cache as cache_mod
    from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
    from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_k import hooks as MK
    from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_layers import hooks as ML

    archive = (_FIXTURES / "dm_ring_excerpt.zip").read_bytes()
    inner, held = {}, {}

    def get_bytes(client, url, *, headers=None, params=None):
        return archive, "application/zip", url

    def inner_rt(metadata, params, ext, fetch_fn, **kw):
        inner.setdefault(str(params), fetch_fn())
        return _Held(f"s3://cache-in/{len(inner)}", inner[str(params)])

    def outer_rt(metadata, params, ext, fetch_fn, **kw):
        data = fetch_fn()
        uri = f"s3://cache/{metadata.name}-{params.get('layer', params.get('surface'))}"
        held[uri] = data
        return _Held(uri, data)

    monkeypatch.setattr(MK, "get_bytes", get_bytes)
    monkeypatch.setattr(ML, "get_bytes", get_bytes)
    monkeypatch.setattr(cache_mod, "read_through", inner_rt)
    monkeypatch.setattr(EX, "read_through", outer_rt)
    return (TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_DM_BBOX),
            TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_DM_BBOX), held)


def test_des_moines_pairs_by_id_across_its_gap_and_places_the_layers_around_it(monkeypatch, tmp_path):
    """Whatever layers the frames drop at the probe, each K lands on its own layer and surfaces; layers 1 and 2 place the published values."""
    import rasterio
    from pyproj import Transformer

    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place
    from trid3nt_server.workflows.modflow.modules import GWF

    k_frames, surface_frames, held = _ring_frames(monkeypatch)

    def staged(frame, units):
        path = tmp_path / f"{frame.layer_id}.tif"
        path.write_bytes(held[frame.uri])
        return {"uri": str(path), "name": frame.name, "units": units, "vertical_datum": frame.vertical_datum,
                "layer_id": frame.layer_id}

    k_by = {int(re.search(r"-l(\d+)-", f.layer_id).group(1)): staged(f, "m/day") for f in k_frames}
    run = {"kf": [staged_ for staged_ in k_by.values()],
           "sf": [staged(f, "m") for f in surface_frames], "vertical_frame": "NAVD88"}
    top, botm = 245.0, [243.0, 234.0]
    sheet = fill(GWF, produced=run, NPF_ICELLTYPE=0, DISV_LENGTH_UNITS="meters", TDIS_TIME_UNITS="days", TDIS_NPER=1,
                 TDIS_PERIODDATA=[[10.0, 1, 1.0]], DISV_TOP=top,
                 Aquifer={"thickness": top - botm[-1], "water_table_depth": 1.0, "layers": 2,
                          "conductivity": {"layers": "kf", "surfaces": "sf"}})
    paired = sheet.filled["NPF_K"].value["of"]
    deepest = max(k_by)
    assert len(paired["layers"]) == deepest and len(paired["surfaces"]) == deepest + 1
    for number, item in enumerate(paired["layers"], start=1):
        assert (item is k_by[number]) if number in k_by else math.isnan(item)
    for number, item in enumerate(paired["surfaces"]):
        stem = "-top-l1-" if number == 0 else f"-bot-l{number}-"
        assert math.isnan(item) if isinstance(item, float) else stem in item["layer_id"]

    with rasterio.open(Path(k_by[1]["uri"])) as ds:
        lon, lat = ds.xy(*ds.index(*_DM_PROBE))
    x, y = Transformer.from_crs(4326, 32615, always_xy=True).transform(lon, lat)
    got = place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot, nlay=2, run=run,
                points=[(x - 1.0, y - 1.0), (x + 1.0, y - 1.0), (x, y + 2.0)], cells=[(0, 1, 2)], utm_epsg=32615,
                frame=frame_of(sheet), top=[top], botm=[[b] for b in botm])
    assert got[0] == pytest.approx([28.973], rel=1e-3)
    assert got[1] == pytest.approx([10.289], rel=1e-3)
