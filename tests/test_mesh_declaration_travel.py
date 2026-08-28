"""The MESH declaration travels WHOLE from the template to the mesh it builds.

Offline. A template's mesh step carries the declaration - its mesher, its kind,
every field the router checked and the DECLARED edit chain in its order - rather
than restating parts of it, so a knob or an edit the template asked for cannot go
missing between the declaration and the mesh. What is pinned here:

  1. THE ASK IS ONE VALUE. Every template that declares a mesh step hands that
     step the whole declaration, and the mapping round-trips back to a
     declaration equal to the one the template froze.
  2. A DECLARED EDIT IS PART OF THE ASK. On the river-tracer corridor a declared
     edit reaches the recipe as the chain's PREFIX and reaches the mesh the box
     was asked to triangulate.
  3. RESTART TRUNCATES TO IT, NOT PAST IT. Gate-time edits are thrown away; the
     edits the template declared survive, and the rebuilt mesh is the declared
     one again.
  4. A PATH THAT CANNOT HONOUR AN EDIT REFUSES BY NAME. The catchment generation
     runs its delineation strategy rather than a mesh session, so a declared edit
     there is refused rather than silently dropped.

The corridor build shells a triangulator in its box; the box, the reach fetch and
the staged files are stood in for and everything between them is the shipped
path.
"""

from __future__ import annotations

import importlib
import json

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.meshers import corridor_tin as CT
from trid3nt_server.workflows.mesh.session import MeshSession
from trid3nt_server.workflows.mesh.tool import (
    declaration_from_plan_value,
    declaration_plan_value,
    tool,
)

_REACH = {"name": "Eel River", "slug": "eel", "lon": -124.1, "lat": 40.5,
          "bbox": (-124.2, 40.4, -124.0, 40.6)}
_SEED = {"lon": -124.1, "lat": 40.5, "source": "flowline"}

#: The edge the template DECLARES an edit down to, and the one a gate-time edit
#: moves it to. Far enough apart that the width cap cannot collapse them.
_DECLARED_EDGE_M = 15.0
_GATE_EDGE_M = 25.0


# --------------------------------------------------------------------------- #
# 1. Every template's mesh step carries the whole ask.
# --------------------------------------------------------------------------- #
_TEMPLATES = (
    "trid3nt_server.workflows.telemac.river_dye.river_dye",
    "trid3nt_server.workflows.telemac.do_sag.do_sag",
    "trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid",
)


@pytest.mark.parametrize("dotted", _TEMPLATES)
def test_the_mesh_step_carries_the_declaration_whole(dotted):
    """The step's ask names the mesher, the router-checked fields and the chain."""
    module = importlib.import_module(dotted)
    declared = module.MESH
    ask = declaration_plan_value(declared)

    assert set(ask) == {"mesher", "fields", "edits"}
    assert ask["mesher"] == declared.spec.mesher
    assert set(ask["fields"]) == set(declared.spec.fields)
    assert [e["action"] for e in ask["edits"]] == [e.action for e in declared.edits]


@pytest.mark.parametrize("dotted", _TEMPLATES)
def test_the_declaration_a_step_rebuilds_equals_the_one_it_was_handed(dotted):
    """A round trip through the step's kwargs changes nothing about the ask."""
    module = importlib.import_module(dotted)
    declared = module.MESH
    rebuilt = declaration_from_plan_value(declaration_plan_value(declared))

    assert rebuilt.spec.mesher == declared.spec.mesher
    assert dict(rebuilt.spec.fields) == dict(declared.spec.fields)
    assert [(e.action, dict(e.inputs)) for e in rebuilt.edits] == \
        [(e.action, dict(e.inputs)) for e in declared.edits]


def test_a_declared_edit_survives_the_trip_the_template_sends_it_on():
    """The edit the template declared is in the ask the step is handed."""
    from trid3nt_server.workflows.telemac.river_dye import river_dye as template

    declared = template.MESH.edit("set_resolution", _DECLARED_EDGE_M)
    ask = declaration_plan_value(declared)

    assert ask["edits"] == [{"action": "set_resolution",
                             "inputs": {"edge_length_m": _DECLARED_EDGE_M}}]
    rebuilt = declaration_from_plan_value(
        ask, domain={"reach": dict(_REACH), "seed": dict(_SEED)})
    assert [e.action for e in rebuilt.edits] == ["set_resolution"]
    assert rebuilt.spec.fields["domain"]["reach"]["slug"] == "eel"


# --------------------------------------------------------------------------- #
# 2/3. The declared edit reaches the recipe and the box; restart truncates to it.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def corridor_box(monkeypatch, tmp_path):
    """The corridor build with its box, its reach fetch and its files stood in.

    Returns the list of asks the box was handed, in build order - the mesh size on
    each one is what the chain actually asked to triangulate.
    """
    from trid3nt_server.workflows.telemac.steps import reach as reach_steps

    asks: list[dict] = []

    async def _river(**_kw):
        return {"inputs": [],
                "provenance": {"seed_lon": -124.1, "seed_lat": 40.5,
                               "seed_rung": "position-named-flowline",
                               "centerline_sha256": "0" * 64,
                               "centerline_comids": [1]}}

    async def _triangulate(ask, run_tag, inputs):
        asks.append(dict(ask))
        return f"RUN{len(asks)}", {"utm_epsg": 32610, "npoin": 4,
                                   "bbox4326": (-124.2, 40.4, -124.0, 40.6),
                                   "domain_mode": "water-polygon",
                                   "n_islands": 0, "water_coverage_frac": 0.9,
                                   "n_inflow_nodes": 2, "n_outflow_nodes": 2,
                                   "bank_source": "nhd_area"}

    def _stage(run_id):
        rundir = tmp_path / run_id
        rundir.mkdir(parents=True, exist_ok=True)
        for name in ("river.slf", "river.cli", "river_mesh.npz"):
            (rundir / name).write_bytes(b"")
        return {"slf_uri": str(rundir / "river.slf"),
                "cli_uri": str(rundir / "river.cli"),
                "topology_uri": str(rundir / "river_mesh.npz")}

    def _geometry(_slf_path):
        points = np.array([[0.0, 0.0], [30.0, 0.0], [30.0, 20.0], [0.0, 20.0]])
        return points, np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)

    monkeypatch.setattr(reach_steps, "resolve_reach_river", _river)
    monkeypatch.setattr(CT, "_triangulate", _triangulate)
    monkeypatch.setattr(CT, "_stage_outputs", _stage)
    monkeypatch.setattr(CT, "_read_geometry", _geometry)
    return asks


def _corridor_session(tmp_path, *, edits=((("set_resolution"), _DECLARED_EDGE_M),)):
    """A session over the river-tracer ask, bound, with the declared edits on it."""
    from trid3nt_server.workflows.telemac.river_dye import river_dye as template

    declaration = tool.build_mesh(
        mesher=template.MESH.spec.mesher, kind=template.MESH.spec.kind,
        domain={"reach": dict(_REACH), "seed": dict(_SEED)},
        extent_km=6.0, width_m=60.0, banks="nhd_area",
        refine={"edge_length": 30.0, "mode": "auto"})
    for action, value in edits:
        declaration = declaration.edit(action, value)
    return MeshSession(declaration, workdir=tmp_path / "session")


def test_a_declared_edit_lands_in_the_recipe_and_the_mesh(corridor_box, tmp_path):
    session = _corridor_session(tmp_path)
    built = session.mesh

    lines = [json.loads(ln) for ln in
             session.recipe_path.read_text().splitlines() if ln.strip()]
    assert lines[0]["spec"]["mesher"] == "corridor_tin"
    assert [ln.get("edit") for ln in lines[1:]] == ["set_resolution"]
    assert lines[1]["edge_length_m"] == pytest.approx(_DECLARED_EDGE_M)

    # The box was asked to triangulate at the DECLARED edge, and the mesh says so.
    assert corridor_box[-1]["mesh_size_m"] == pytest.approx(_DECLARED_EDGE_M)
    assert built.meta["artifact"]["provenance"]["mesh_size_m"] == pytest.approx(
        _DECLARED_EDGE_M)


def test_restart_truncates_to_the_declared_edit_and_not_past_it(corridor_box,
                                                                tmp_path):
    session = _corridor_session(tmp_path)
    session.edit("set_resolution", _GATE_EDGE_M)
    assert [e.action for e in session.chain] == ["set_resolution", "set_resolution"]
    assert corridor_box[-1]["mesh_size_m"] == pytest.approx(_GATE_EDGE_M)

    session.restart()

    assert [(e.action, dict(e.inputs)) for e in session.chain] == [
        ("set_resolution", {"edge_length_m": _DECLARED_EDGE_M})]
    assert corridor_box[-1]["mesh_size_m"] == pytest.approx(_DECLARED_EDGE_M)
    assert session.mesh.meta["artifact"]["provenance"]["mesh_size_m"] == \
        pytest.approx(_DECLARED_EDGE_M)


def test_a_declaration_with_no_edits_builds_the_spec_line_alone(corridor_box,
                                                                tmp_path):
    """The unchanged ask is unchanged: one spec line, no edit line, no re-ask."""
    session = _corridor_session(tmp_path, edits=())
    session.mesh

    lines = [json.loads(ln) for ln in
             session.recipe_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1 and "edit" not in lines[0]
    # One ask, at the edge the SPEC declared - which is what makes the edited
    # runs above a measurement rather than a coincidence.
    assert len(corridor_box) == 1
    assert corridor_box[0]["mesh_size_m"] == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# 4. A path with no chain to prefix refuses the edit rather than dropping it.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_catchment_path_refuses_a_declared_edit_by_name():
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        build_catchment_mesh,
    )

    declaration = tool.build_mesh(
        mesher="watershed", kind="unstructured_tri",
        extent={"bbox": (-83.5, 35.0, -83.4, 35.09), "name": "Cataloochee",
                "slug": "cataloochee", "pour_point": (-83.43, 35.06)},
    ).edit("set_edge_band", min_edge_length_m=30.0)

    with pytest.raises(MeshToolError) as excinfo:
        await build_catchment_mesh(mesh=declaration_plan_value(declaration),
                                   supplied=None, bed_dem={}, rivers=None)
    assert excinfo.value.error_code == "MESH_DECLARED_EDIT_UNSUPPORTED"
    assert "set_edge_band" in str(excinfo.value)
