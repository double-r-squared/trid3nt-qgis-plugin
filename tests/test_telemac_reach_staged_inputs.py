"""The reach's manifest is a CASE: an engine, a file, and what must come back.

The worker used to be handed the reach's raw geometry and asked to mesh it. Now
the server authors the steering file against the accepted mesh and stages both,
so what the manifest carries is the ``case`` the worker dispatches on - which
engine, which steering file, which results are the success convention - plus the
``inputs`` the launcher walks into the run directory.

What is pinned here is that contract: the section key the worker reads, the
per-class results and outputs, and the staging refusals.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.telemac.authoring.assembler import (
    _class_files,
    _write_manifest,
)


class _FakeS3:
    def __init__(self) -> None:
        self.put: dict | None = None

    def put_object(self, **kw):  # noqa: ANN001
        self.put = kw


_CASE = {"module": "telemac2d", "steering": "t2d_river.cas",
         "results": ["r2d_river.slf"],
         "server_facts": {"utm_epsg": 32610, "npoin": 812,
                          "bed_source": "3dep"}}

_SOLVE_INPUTS = [
    {"gs_uri": "s3://cache/mesh/M1/mesh.slf", "dest": "river.slf"},
    {"gs_uri": "s3://cache/mesh/M1/mesh.cli", "dest": "river.cli"},
    {"gs_uri": "s3://cache/telemac/RUNTAG/t2d_river.cas", "dest": "t2d_river.cas"},
]


def _stage(monkeypatch, case: dict, *, outputs: list[str] | None = None,
           inputs: list[dict[str, str]] | None = None) -> dict:
    import trid3nt_server.workflows.solver.solver as solver_mod

    fake = _FakeS3()
    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: fake)
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    _write_manifest(case, "RUNTAG", outputs=outputs or ["r2d_river.slf"],
                    inputs=inputs or [], prefix="telemac")
    assert fake.put is not None
    return json.loads(fake.put["Body"])


def test_the_reach_manifest_names_the_case_the_worker_dispatches_on(monkeypatch):
    doc = _stage(monkeypatch, _CASE, inputs=_SOLVE_INPUTS)
    assert doc["case"] == _CASE
    assert "reach" not in doc
    assert [row["dest"] for row in doc["inputs"]] == [
        "river.slf", "river.cli", "t2d_river.cas"]


def test_an_unstaged_manifest_carries_an_empty_inputs_list(monkeypatch):
    """The key is always present: the worker's contract reads it unconditionally."""
    assert _stage(monkeypatch, _CASE)["inputs"] == []


def test_writing_the_manifest_requires_a_cache_bucket(monkeypatch):
    import trid3nt_server.workflows.solver.solver as solver_mod

    from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())
    monkeypatch.delenv("TRID3NT_CACHE_BUCKET", raising=False)
    with pytest.raises(TelemacDyeScenarioError):
        _write_manifest(_CASE, "RUNTAG", outputs=["r2d_river.slf"], inputs=[],
                        prefix="telemac")


# --------------------------------------------------------------------------- #
# What a class must produce, and what comes back.
# --------------------------------------------------------------------------- #
def test_a_plain_tracer_run_must_produce_its_result_and_its_restart():
    results, outputs = _class_files("tracer", dredging=False)
    assert results == ["r2d_river.slf", "restart_river.slf"]
    assert "restart_river.slf" in outputs
    assert "full_listing.log" in outputs
    # the mesh the run was handed comes back with it, so a solved run stays
    # readable from its own prefix
    assert {"river.slf", "river.cli", "t2d_river.cas"} <= set(outputs)


def test_sediment_must_produce_the_gaia_result_and_brings_its_steering_back():
    """A coupled class runs the launcher whole, so it is asked for no restart."""
    results, outputs = _class_files("sediment", dredging=False)
    assert results == ["r2d_river.slf", "gaia_river.slf"]
    assert "restart_river.slf" not in outputs
    assert "gaia_river.cas" in outputs
    assert "nestor.act" not in outputs


def test_a_dredging_run_brings_the_nestor_rule_back():
    _results, outputs = _class_files("sediment", dredging=True)
    assert {"nestor.act", "nestor.pol", "nestor.ref"} <= set(outputs)


def test_oil_must_produce_the_track_the_slick_is_read_from():
    """The slick and the particle snapshots are built on the SERVER off this
    track, so neither is a file the worker is asked to write."""
    results, outputs = _class_files("oil", dredging=False)
    assert results == ["r2d_river.slf", "restart_river.slf", "drogues.txt"]
    assert "oil_spill.txt" in outputs
    assert "slick.geojson" not in outputs and "particles.json" not in outputs


@pytest.mark.parametrize("substance_class", ["decay", "do_sag"])
def test_a_waqtel_run_brings_the_forcing_it_applied_back(substance_class):
    results, outputs = _class_files(substance_class, dredging=False)
    assert results == ["r2d_river.slf"]
    assert "t2d_river.waqtel" in outputs


def test_no_reach_run_declares_a_bed_cog_output():
    """The node-lattice bed COG is dead; nothing may name it as an output."""
    for substance_class in ("tracer", "do_sag", "sediment", "oil"):
        _results, outputs = _class_files(substance_class, dredging=False)
        assert "bed_bathymetry.tif" not in outputs


# --------------------------------------------------------------------------- #
# ONE manifest writer, and the CASE section it carries.
# --------------------------------------------------------------------------- #
def test_the_case_section_names_the_engine_the_file_and_the_results():
    from trid3nt_server.workflows.telemac.authoring.open_water import case_section

    case = case_section(
        module="telemac2d", steering="t2d_river.cas",
        results=["r2d_river.slf", "full_listing.log"],
        server_facts={"utm_epsg": 32610, "npoin": 812, "bed_source": "3dep"})
    assert case["module"] == "telemac2d"
    assert case["steering"] == "t2d_river.cas"
    assert case["results"] == ["r2d_river.slf", "full_listing.log"]
    assert case["server_facts"]["utm_epsg"] == 32610
    # WHICH family a run belongs to is nobody's question here: the section
    # carried the word to the worker, the worker copied it into a report no
    # reader ever opened it from, and the solver name is what a run listing is
    # read by.
    assert "family" not in case
    # no user fortran was asked for, so the key is ABSENT rather than null: the
    # worker's strict gate reads a present key as a file it must compile.
    assert "user_fortran" not in case
    assert "user_fortran" in case_section(
        module="telemac2d", steering="t2d_river.cas", results=[],
        server_facts={}, user_fortran="user_fortran")
    # the coupling reads the same way: an uncoupled case names none, and the
    # worker's runner choice turns on the word being there.
    assert "coupling" not in case
    assert case_section(module="telemac2d", steering="t2d_river.cas",
                        results=[], server_facts={},
                        coupling="waqtel")["coupling"] == "waqtel"


def test_a_continued_case_names_the_staged_file_it_restarts_from():
    """Absent on a fresh run, so a present key is always a real continuation."""
    from trid3nt_server.workflows.telemac.authoring.open_water import case_section

    fresh = case_section(module="telemac2d", steering="t2d_river.cas",
                         results=[], server_facts={})
    assert "continue_from" not in fresh
    assert case_section(module="telemac2d", steering="t2d_river.cas", results=[],
                        server_facts={},
                        continue_from="previous.slf")["continue_from"] == \
        "previous.slf"


def test_the_continuation_starts_where_the_restart_file_says_it_does(monkeypatch,
                                                                    tmp_path):
    """The instant is READ, never computed from the ask.

    The engine writes its restart at its own last time step, which is neither
    the graphic period the results file lands on nor the duration that was
    asked for, so a server that derived the instant would author the extended
    scenario over the wrong stretch of clock.
    """
    import trid3nt_server.workflows.telemac.result_reader as reader
    from trid3nt_server.workflows.telemac.authoring.assembler import (
        _continuation_state,
    )
    from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError

    previous = tmp_path / "restart_river.slf"
    previous.write_bytes(b"selafin")
    monkeypatch.setattr(reader, "read_selafin", lambda path: {
        "times": [0.0, 104.2, 600.192], "npoin": 3,
        "varnames": ["WATER DEPTH", "FREE SURFACE"],
        "data": {"WATER DEPTH": [[1.0, 1.0, 1.0], [1.0, 0.5, 0.0],
                                 [0.7, 0.0, 0.0]],
                 "FREE SURFACE": [[9.0] * 3] * 3}})
    state = _continuation_state(str(previous))
    assert state["start_s"] == 600.192
    # the state a release is settled against is the LAST frame's own wet/dry
    assert list(state["wet"]) == [True, False, False]

    monkeypatch.setattr(reader, "read_selafin", lambda path: {"times": []})
    with pytest.raises(TelemacDyeScenarioError) as exc:
        _continuation_state(str(previous))
    assert exc.value.error_code == "TELEMAC_CONTINUATION_UNREADABLE"


def test_the_classes_that_couple_state_which_module_they_couple_with():
    from trid3nt_server.workflows.telemac.authoring.assembler import _CLASS_COUPLING

    assert _CLASS_COUPLING == {"decay": "waqtel", "do_sag": "waqtel",
                               "sediment": "gaia"}
    assert "tracer" not in _CLASS_COUPLING and "oil" not in _CLASS_COUPLING


@pytest.mark.parametrize("substance,coupled", [("sewage", "WAQTEL"),
                                               ("sand", "GAIA")])
def test_a_coupled_reach_refuses_to_be_continued_and_says_why(substance, coupled):
    """The couplings run the engine's own launcher, whole-process, unstepped.

    The refusal is server-side, before anything is authored or staged: the run
    that would come back is a fresh one wearing a continuation's name.
    """
    import asyncio

    from trid3nt_server.workflows.telemac.authoring.assembler import assemble_reach
    from trid3nt_server.workflows.telemac.helpers.errors import (
        TelemacDyeScenarioInputError,
    )

    with pytest.raises(TelemacDyeScenarioInputError) as exc:
        asyncio.run(assemble_reach(
            reach={"slug": "r", "name": "R"}, seed={"lon": -124.0, "lat": 40.0},
            mesh={"min_edge_m": 14.0, "element_count": 10, "artifact": None},
            centerline=None, carrier_discharge={"m3s": 50.0},
            substance=substance,
            continue_from="s3://runs/PREV/r2d_river.slf"))
    assert coupled in str(exc.value)


def test_the_one_writer_stages_every_front_under_its_own_prefix(monkeypatch):
    import trid3nt_server.workflows.solver.solver as solver_mod
    from trid3nt_server.workflows.telemac.authoring.open_water import stage_telemac_manifest

    fake = _FakeS3()
    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: fake)
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    uri = stage_telemac_manifest(
        section="agitation", config={"name": "harbour"}, run_tag="RUNTAG",
        outputs=["res.slf"], prefix="artemis")
    assert uri == "s3://test-cache/artemis/RUNTAG/manifest.json"
    doc = json.loads(fake.put["Body"])
    assert doc["agitation"] == {"name": "harbour"}
    assert doc["telemac_args"] == [] and doc["inputs"] == []
