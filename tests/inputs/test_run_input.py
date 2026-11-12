"""A FINISHED RUN as an input: named by its run id or a handle the case shows, read off its record and staged deck.

Each refusal is the ingestion's own: a run no layer of the case lies under, one with no record or one that
did not end at its own correct end, one of an engine the reader does not take, one whose staged deck or
kept mesh is gone.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server.inputs import run as run_input
from trid3nt_server.inputs.run import FinishedRun, FinishedRunError
from trid3nt_server.inputs.slots import RUN, ingest_slot
from trid3nt_server.render import uri_registry
from trid3nt_server.store import objects as storage
from trid3nt_server.tools.mesh import step
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.runtime import journal

_RUN = "01M76H6GJN2RN18XTTVBSHFBZ1"
_OTHER = "01M76H6GJN2RN18XTTVBSHFBZ9"
_ART = MeshArtifact(mesh_id="M01", name="lattice", mode="om2d", display_uri="s3://cache/mesh/M01/mesh.2dm",
                    crs_authid="EPSG:32610", has_bathymetry=True, node_count=9, element_count=8,
                    bbox=(-122.68, 45.51, -122.66, 45.52), utm_epsg=32610)


def _record(**over):
    row = {"run_id": _RUN, "engine": "modflow", "module": "gwf", "correct_end": True, "stopped": None,
           "check_steps": None, "mesh": {"key": "K1", "built_by": None},
           "outputs": [{"layer_id": f"modflow-head-over-time-{_RUN}", "uri": f"s3://runs/{_RUN}/head.nc"}],
           "inputs": {}}
    row.update(over)
    return row


@pytest.fixture
def case(monkeypatch, tmp_path, fake_s3):
    """A case holding one layer of the run, its record on the journal, its manifest staged and its mesh kept."""
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path))
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "cache")
    storage.set_client(fake_s3)
    storage.set_runs_bucket("runs")
    reg = uri_registry.SessionUriRegistry("S1")
    token = uri_registry.activate_registry(reg)
    uri_registry.observe_published_layer(f"modflow-head-over-time-{_RUN}", f"s3://runs/{_RUN}/head.nc")
    journal.append_record(_record())
    fake_s3.store[f"modflow/{_RUN}/manifest.json"] = json.dumps({
        "case": {"module": "gwf", "steering": "mfsim.nam", "results": ["gwf.hds"]},
        "inputs": [{"gs_uri": f"s3://cache/modflow/{_RUN}/gwf.npf", "dest": "gwf.npf"}],
        "outputs": ["gwf.hds", "gwf.lst"]}).encode()

    async def kept(key):
        return _ART if key == "K1" else None

    monkeypatch.setattr(step, "kept_mesh", kept)
    yield fake_s3
    uri_registry.deactivate_registry(token)
    storage.set_client(None)
    storage.set_runs_bucket(None)


def _read(value, **told):
    return asyncio.run(run_input.run(value, label="run", **told))


def test_a_run_named_by_its_id_is_read_off_its_record_and_staged_deck(case):
    found = _read(_RUN, engines=("modflow",))
    assert isinstance(found, FinishedRun)
    assert (found.run_id, found.engine, found.module) == (_RUN, "modflow", "gwf")
    assert found.case["steering"] == "mfsim.nam" and found.outputs == ("gwf.hds", "gwf.lst")
    assert found.staged == ({"gs_uri": f"s3://cache/modflow/{_RUN}/gwf.npf", "dest": "gwf.npf"},)
    assert found.mesh["artifact"] is _ART and found.bbox == _ART.bbox


def test_a_run_named_by_a_handle_of_one_of_its_layers_is_the_same_run(case):
    assert _read(f"modflow-head-over-time-{_RUN}").run_id == _RUN


def test_the_slot_reads_through_this_ingestion(case):
    assert ingest_slot(RUN, _RUN, label="run", engines=("modflow",)).run_id == _RUN


def test_a_run_no_layer_of_the_case_lies_under_refuses(case):
    with pytest.raises(FinishedRunError, match="no layer this case holds") as refused:
        _read(_OTHER)
    assert refused.value.error_code == "RUN_NOT_IN_CASE"


def test_outside_a_case_no_run_is_one_of_its_own(case):
    held = uri_registry._ACTIVE_REGISTRY.set(None)
    try:
        with pytest.raises(FinishedRunError) as refused:
            _read(_RUN)
    finally:
        uri_registry._ACTIVE_REGISTRY.reset(held)
    assert refused.value.error_code == "RUN_NOT_IN_CASE"


@pytest.mark.parametrize("over, said", [
    ({"stopped": "the user stopped the solve"}, "it was stopped"),
    ({"check_steps": 10}, "check run stopped after 10 engine steps"),
    ({"correct_end": False}, "did not reach its own correct end"),
])
def test_a_run_that_did_not_finish_refuses_saying_why(case, over, said):
    journal.append_record(_record(**over))
    with pytest.raises(FinishedRunError, match=said) as refused:
        _read(_RUN)
    assert refused.value.error_code == "RUN_NOT_FINISHED"


def test_a_run_with_no_record_has_not_finished(case, monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "empty"))
    with pytest.raises(FinishedRunError, match="has no record") as refused:
        _read(_RUN)
    assert refused.value.error_code == "RUN_NOT_FINISHED"


def test_a_run_of_an_engine_the_reader_does_not_take_refuses_naming_them(case):
    journal.append_record(_record(engine="telemac"))
    with pytest.raises(FinishedRunError, match="is a telemac run, and run takes a run of modflow") as refused:
        _read(_RUN, engines=("modflow",))
    assert refused.value.error_code == "RUN_ENGINE_UNTAKEN"


def test_a_run_whose_staged_deck_is_gone_refuses(case):
    del case.store[f"modflow/{_RUN}/manifest.json"]
    with pytest.raises(FinishedRunError, match="no longer held") as refused:
        _read(_RUN)
    assert refused.value.error_code == "RUN_DECK_GONE"


def test_a_run_whose_mesh_is_not_kept_refuses(case):
    journal.append_record(_record(mesh={"key": None}))
    with pytest.raises(FinishedRunError, match="not kept") as refused:
        _read(_RUN)
    assert refused.value.error_code == "RUN_MESH_GONE"
