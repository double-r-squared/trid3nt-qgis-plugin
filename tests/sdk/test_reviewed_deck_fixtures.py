"""Round-trip the typed state carried by the registered templates' offline decks."""

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pytest
from shapely.geometry import box, mapping

from seated import resolve_params, value_inputs
from trid3nt import case
from trid3nt.inputs.bed import bed
from trid3nt.inputs.boundary import BoundaryRun
from trid3nt.inputs.coverage import Coverage, Shortfall, step
from trid3nt.inputs.domain import Domain
from trid3nt.inputs.extent import Extent
from trid3nt.inputs.observation import observation
from trid3nt.inputs.point import Point
from trid3nt.inputs.rain import Rain
from trid3nt.inputs.run import FinishedRun
from trid3nt.inputs.soil import Soil
from trid3nt.inputs.temporal import Series
from trid3nt.inputs.wave import Wave
from trid3nt.inputs.weather import Weather
from trid3nt.sdk.case import Case, _workflow
from trid3nt.sdk.script_run import load_deck, save_deck
from trid3nt.tools.fetchers._router.registration import get_spec
from trid3nt.tools.mesh.artifact import MeshArtifact
from trid3nt.tools.mesh.shared.nodes import read_accepted_mesh_nodes
from trid3nt.workflows.geoclaw.authoring.restart import Previous
from trid3nt.workflows.geoclaw.modules import body_on
from trid3nt.workflows.runtime.fill import ACCEPTED, Fill, SourceRequest, Verdict, production
from trid3nt.workflows.runtime.module import UNSET
from trid3nt.workflows.runtime.sheet import Filled, Provenance
from trid3nt.workflows.runtime.workflow import Deck, Said
from trid3nt_contracts.common import Origin
from trid3nt_contracts.coverage import SourceChoice, SourceOption
from trid3nt_contracts.execution import DemLayerURI, LayerURI

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_AT = datetime.fromisoformat("2026-11-09T00:15:00+00:00")
_POINT = Point(-95.921233757, 41.257659140)
_BBOX = (-95.929416, 41.199796, -95.909204, 41.257726)


@pytest.fixture(scope="module")
def row_values():
    """The Missouri timed forcing and mesh, calibration manifest, and offline weather/rain/soil/wave variants."""
    gauge = _FIXTURES / "usgs_nwis_gauges" / "missouri_hwy370_2026-11-08_09.fgb"
    source = get_spec("fetch_usgs_nwis_gauges")
    fields = next(row.fields for row in source.coverage if row.data_class == "water level series")
    level = observation(str(gauge), fields=fields, at=_AT, window_s=3600, quantity="water level",
                        to_units="m", to_datum="NAVD88")
    assert level.site_id == "06610670" and isinstance(level.series, Series)
    mesh_file = _FIXTURES / "telemac_missouri" / "mesh.2dm"
    points, cells, elevations, _ = read_accepted_mesh_nodes(str(mesh_file), utm_epsg=32615)
    artifact = MeshArtifact("MO", "Missouri", "om2d", str(mesh_file), "EPSG:32615", True,
                            len(points), len(cells), _BBOX, utm_epsg=32615)
    domain = Domain(mapping(box(*_BBOX)), "Missouri", runs=(
        BoundaryRun(Point(_BBOX[0], _BBOX[1]), Point(_BBOX[2], _BBOX[1]), "outflow"),))
    manifest = json.loads((_FIXTURES / "pestpp/calibrate/manifest.json").read_text())
    finished = FinishedRun("01M7FDZZC49BQYYNTFY9SZAE37", "modflow", "gwt", {"correct_end": True},
                           manifest["case"], (), tuple(manifest["outputs"]), {"artifact": artifact}, _BBOX)
    clock = (0.0, 3600.0)
    weather = Weather(MappingProxyType({"air_temp_c": Series(clock, (10, 11), units="degC"),
                                        "cloud_octas": Series(clock, (6, 7), units="octas")}),
                      {}, _AT, "offline station")
    layer = DemLayerURI(layer_id="fixture-bed", name="Bed", layer_type="raster",
                        uri="s3://cache/fixture-bed.tif", vertical_datum="NAVD88")
    return {
        "domain": domain, "bed": bed(4.0), "extent": Extent(_BBOX), "mesh": {"artifact": artifact},
        "level": level, "observe": (level,), "discharge": observation(15.0, to_units="m3/s"),
        "line": {"type": "LineString", "coordinates": [(_POINT.lon, _POINT.lat), (_BBOX[0], _BBOX[1])]},
        "wave": Wave(1.0, 8.0, 270.0), "rain": Rain.every(3600, (0.0, 1.0)),
        "soil": Soil(MappingProxyType({"sand": _FIXTURES / "green_ampt/desmoines_sand.tif"})),
        "weather": weather, "cloud": weather, "run": finished,
    }, {
        "display_uri": mesh_file, "points": points, "cells": cells, "elevations": elevations,
    }, layer


def _same_state(before, after, original, pinned, seen):
    """Compare the typed graph while checking the declarations that must rebind."""
    if before is original:
        assert after is pinned
        return
    if isinstance(before, type) and hasattr(before, "MODULE_INPUT"):
        body = pinned.steering
        assert after is (body if before.MODULE == body.MODULE else body_on(body, before.MODULE))
        return
    if before is UNSET:
        assert after is UNSET
        return
    assert type(before) is type(after)
    pair = id(before), id(after)
    if pair in seen:
        return
    seen.add(pair)
    if isinstance(before, Mapping):
        assert before.keys() == after.keys()
        for name, item in before.items():
            _same_state(item, after[name], original, pinned, seen)
    elif is_dataclass(before):
        for field in fields(before):
            _same_state(getattr(before, field.name), getattr(after, field.name), original, pinned, seen)
    elif isinstance(before, (list, tuple)):
        assert len(before) == len(after)
        for item, restored in zip(before, after, strict=True):
            _same_state(item, restored, original, pinned, seen)
    elif isinstance(before, np.ndarray):
        assert before.dtype == after.dtype
        np.testing.assert_array_equal(before, after)
    else:
        assert before == after


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize(("template", "sibling"), [
    *((name, False) for name in Case.templates()),
    ("geoclaw_tsunami_inundation", True),
    ("geoclaw_storm_surge", True),
])
@pytest.mark.asyncio
async def test_the_registered_template_deck_state_survives_the_reviewed_handoff(tmp_path, row_values, template, sibling, portable):
    original = _workflow(template)
    pinned = getattr(case(tmp_path / "child"), template)()._workflow
    rows, mesh, layer = row_values
    supplied = {row.name: (_POINT.lon, _POINT.lat) for row in value_inputs(original) if row.type is Point}
    if sibling:
        supplied["module"] = "geoclaw_bouss"
    if template == "calibrate":
        supplied.update(parameters={"NPF_K": [0.1, 10.0]}, stages=["screen"],
                        parameter_data={"npf_k": {"PARVAL1": 0.8}}, sigma={"heads": 1.0})
    inputs = await resolve_params(value_inputs(original), supplied)
    state = Fill(original, inputs=inputs, stated=supplied, settings={"LOCAL_CORES": 1})
    env = production(state)
    for name, row in env.data.items():
        value = rows[row.slot.name] if row.slot else LayerURI(
            layer_id=f"fixture-{name}", name=name, layer_type="vector", uri="s3://cache/fixture.fgb")
        env.run[name] = value
        state.inputs[name] = Verdict(ACCEPTED, value, origin=Origin.PRODUCER)
    if "bed" in env.data:
        env.supplied["bed"] = layer
        remedy = step("interpolate_surface", max_distance=50)
        state.coverage["bed"] = Coverage("bed", unmeasured=0.5, steps=(remedy,),
                                        shortfall=Shortfall("BED_UNPAINTED", "bed", 2, 0.5, 1, (remedy,)))
        env.choices = {"bed": [SourceRequest("fixture", 1, 1, MappingProxyType({"bbox": _BBOX}))]}
    held = {}
    if original.engine != "pestpp":
        env.run.setdefault("mesh", rows["mesh"])
        env.run["moment"] = {"at": _AT, "lon": _POINT.lon, "lat": _POINT.lat}
        body = body_on(original.steering, "geoclaw_bouss") if sibling else original.steering
        keyword = next(slot for slot in body.MODULE_INPUT.values() if slot.engine_default is UNSET)
        filled = Filled(keyword, "fixture", Provenance(Origin.PRODUCER))
        env.run["sheet"] = body.sheet({keyword.identifier: filled}, {"mesh": mesh}, dict(env.run))
    if original.engine == "geoclaw":
        held["previous"] = Previous("s3://runs/earlier/fort.chk00001", "fort.chk00001", 60.0, (),
                                    MappingProxyType({"claw.data": "recorded data"}))
    choice = SourceChoice(slot="bed", need="terrain", rows=[SourceOption(fetcher="fixture")])
    deck = Deck(state, run_id="FIXTURE", staged={"case": {"module": original.engine}}, held=held,
                said=Said(choices=[choice]))
    if portable:
        from trid3nt.sdk.deck_state import decode, encode

        received = decode(json.loads(json.dumps(encode(deck, original, str))), pinned, Path)
    else:
        received = load_deck(save_deck(deck), pinned)
    _same_state(deck, received, original, pinned, set())
    assert received.state.env.inputs is received.state.inputs
    for name in received.state.env.data:
        assert received.state.inputs[name].value is received.state.env.run[name]
    if "level" in received.state.env.data:
        timed = received.state.env.run["level"].series
        with pytest.raises(AttributeError, match="frozen"):
            timed.times_s = ()
