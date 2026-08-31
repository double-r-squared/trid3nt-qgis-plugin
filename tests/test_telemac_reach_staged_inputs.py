"""The reach's manifest is a CASE: an engine, a deck, and what must come back.

The worker used to be handed the reach's raw geometry and asked to mesh it. Now
the server authors the deck against the accepted mesh and stages both, so what
the manifest carries is the ``case`` the worker dispatches on - which engine,
which steering file, which results are the success convention - plus the
``inputs`` the launcher walks into the run directory.

What is pinned here is that contract: the section key the worker reads, the
per-class results and outputs, and the staging refusals.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.telemac.steps.deck import _class_files, stage_manifest


class _FakeS3:
    def __init__(self) -> None:
        self.put: dict | None = None

    def put_object(self, **kw):  # noqa: ANN001
        self.put = kw


_CASE = {"module": "telemac2d", "steering": "t2d_river.cas",
         "results": ["r2d_river.slf"], "family": "reach",
         "echo": {"utm_epsg": 32610, "npoin": 812, "bed_source": "3dep"}}

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
    stage_manifest(case, "RUNTAG", outputs=outputs or ["r2d_river.slf"],
                   inputs=inputs)
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


def test_stage_manifest_requires_cache_bucket(monkeypatch):
    import trid3nt_server.workflows.solver.solver as solver_mod

    from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())
    monkeypatch.delenv("TRID3NT_CACHE_BUCKET", raising=False)
    with pytest.raises(TelemacDyeScenarioError):
        stage_manifest(_CASE, "RUNTAG", outputs=["r2d_river.slf"])


# --------------------------------------------------------------------------- #
# What a class must produce, and what comes back.
# --------------------------------------------------------------------------- #
def test_a_plain_tracer_run_must_produce_its_result_and_nothing_more():
    results, outputs = _class_files("tracer", dredging=False)
    assert results == ["r2d_river.slf"]
    assert "full_listing.log" in outputs
    # the mesh the run was handed comes back with it, so a solved run stays
    # readable from its own prefix
    assert {"river.slf", "river.cli", "t2d_river.cas"} <= set(outputs)


def test_sediment_must_produce_the_gaia_result_and_brings_its_steering_back():
    results, outputs = _class_files("sediment", dredging=False)
    assert results == ["r2d_river.slf", "gaia_river.slf"]
    assert "gaia_river.cas" in outputs
    assert "nestor.act" not in outputs


def test_a_dredging_run_brings_the_nestor_rule_back():
    _results, outputs = _class_files("sediment", dredging=True)
    assert {"nestor.act", "nestor.pol", "nestor.ref"} <= set(outputs)


def test_oil_must_produce_the_track_the_slick_is_read_from():
    """The slick and the particle snapshots are built on the SERVER off this
    track, so neither is a file the worker is asked to write."""
    results, outputs = _class_files("oil", dredging=False)
    assert results == ["r2d_river.slf", "drogues.txt"]
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
def test_the_case_section_names_the_engine_the_deck_and_the_results():
    from trid3nt_server.workflows.telemac.steps import case_section

    case = case_section(
        module="telemac2d", steering="t2d_river.cas",
        results=["r2d_river.slf", "full_listing.log"], family="river_dye",
        echo={"utm_epsg": 32610, "npoin": 812, "bed_source": "3dep"})
    assert case["module"] == "telemac2d"
    assert case["steering"] == "t2d_river.cas"
    assert case["results"] == ["r2d_river.slf", "full_listing.log"]
    assert case["family"] == "river_dye"
    assert case["echo"]["utm_epsg"] == 32610
    # no user fortran was asked for, so the key is ABSENT rather than null: the
    # worker's strict gate reads a present key as a file it must compile.
    assert "user_fortran" not in case
    assert "user_fortran" in case_section(
        module="telemac2d", steering="t2d_river.cas", results=[],
        family="river_dye", echo={}, user_fortran="user_fortran")
    # the coupling reads the same way: an uncoupled case names none, and the
    # worker's runner choice turns on the word being there.
    assert "coupling" not in case
    assert case_section(module="telemac2d", steering="t2d_river.cas",
                        results=[], family="river_dye", echo={},
                        coupling="waqtel")["coupling"] == "waqtel"


def test_a_continued_case_names_the_staged_file_it_restarts_from():
    """Absent on a fresh run, so a present key is always a real continuation."""
    from trid3nt_server.workflows.telemac.steps import case_section

    fresh = case_section(module="telemac2d", steering="t2d_river.cas",
                         results=[], family="river_dye", echo={})
    assert "continue_from" not in fresh
    assert case_section(module="telemac2d", steering="t2d_river.cas", results=[],
                        family="river_dye", echo={},
                        continue_from="previous.slf")["continue_from"] == \
        "previous.slf"


def test_the_classes_that_couple_state_which_module_they_couple_with():
    from trid3nt_server.workflows.telemac.steps.deck import _CLASS_COUPLING

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

    from trid3nt_server.workflows.telemac.steps.deck import write_reach_deck
    from trid3nt_server.workflows.telemac.steps.errors import (
        TelemacDyeScenarioInputError,
    )

    with pytest.raises(TelemacDyeScenarioInputError) as exc:
        asyncio.run(write_reach_deck(
            reach={"slug": "r", "name": "R"}, seed={"lon": -124.0, "lat": 40.0},
            mesh={"min_edge_m": 14.0, "element_count": 10, "artifact": None},
            centerline=None, carrier_discharge={"m3s": 50.0},
            substance=substance,
            continue_from="s3://runs/PREV/r2d_river.slf"))
    assert coupled in str(exc.value)


def test_the_one_writer_stages_every_front_under_its_own_prefix(monkeypatch):
    import trid3nt_server.workflows.solver.solver as solver_mod
    from trid3nt_server.workflows.telemac.steps import stage_telemac_manifest

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
