"""A GeoClaw run carried on from a finished run's checkpoint: the worker's verdict, the previous run it reads, the set it is held to."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.geoclaw import workflow
from trid3nt_server.workflows.geoclaw.authoring import restart
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.geoclaw.templates.tsunami_inundation import tsunami_inundation as template

_WORKER = Path(__file__).resolve().parents[2] / "workers" / "geoclaw" / "entrypoint.py"
_spec = importlib.util.spec_from_file_location("geoclaw_entrypoint_restart", _WORKER)
_entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_entry)

_DONE = " Done integrating to time    600.00000000000000\n"
_CLAW = "\n".join(["2 =: num_dim", "-1.0 -2.0 =: lower", "1.0 2.0 =: upper", "10 10 =: num_cells", "3 =: num_eqn",
                   "1 =: num_aux", "2 =: num_ghost", "0.0 =: t0", "300.0 =: tfinal", "F =: restart"])
_AMR = "\n".join(["4194303 =: memsize", "3 =: amr_levels_max", "2 2 =: refinement_ratios_x", "2 2 =: refinement_ratios_y",
                  "2 2 =: refinement_ratios_t", "1 1 1 =: aux_type"])
_FILES = {"claw.data": _CLAW, "amr.data": _AMR, **{name: f"{name} body" for name in restart._WHOLE}}


class _Bucket:
    def __init__(self, held):
        self.held = held

    def get_paginator(self, _name):
        return SimpleNamespace(paginate=lambda Bucket, Prefix: [
            {"Contents": [{"Key": key} for key in self.held if key.startswith(Prefix)]}])

    def get_object(self, Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: self.held[Key])}


def _run(monkeypatch, *, final=300.0, at=300.0, extra=None, drop=()):
    held = {"geoclaw_metrics.json": json.dumps({"final_time": final}).encode(),
            "fort.chk00007": b"x", "fort.tck00007": f" Checkpoint file at time t =    {at}\n".encode(),
            "gauge00001.txt": b"g", "fgout0001.b0001": b"f", "fgmax0001.txt": b"m",
            **{name: text.encode() for name, text in _FILES.items()}, **(extra or {})}
    for name in drop:
        held.pop(name)
    monkeypatch.setattr(storage, "client", lambda: _Bucket({f"run1/{k}": v for k, v in held.items()}))


def test_the_worker_demands_the_mass_line_of_a_fresh_run_only():
    assert not _entry._verdict(_DONE, 600.0, [], 0)["correct_end"]
    assert _entry._verdict(_DONE, 600.0, [], 0, True)["correct_end"]
    refused = _entry._verdict(" ERROR: no\n", 600.0, [], 0, True)
    assert not refused["correct_end"] and refused["error_code"] == "GEOCLAW_ENGINE_REFUSED"
    assert not _entry._verdict(" Done integrating to time 300.0\n", 600.0, [], 0, True)["correct_end"]


def test_the_worker_reads_restart_off_the_staged_claw_data(tmp_path):
    path = tmp_path / "claw.data"
    path.write_text("T                    =: restart\n")
    assert _entry._staged_restart(path)
    path.write_text("F                    =: restart\n")
    assert not _entry._staged_restart(path)
    path.write_text("10.0 =: tfinal\n")
    assert not _entry._staged_restart(path)


def test_a_checkpoint_run_stages_the_checkpoint_and_its_records(monkeypatch):
    _run(monkeypatch)
    previous = restart.previous_run("s3://runs/run1/fort.chk00007")
    assert previous.time == 300.0 and previous.name == "fort.chk00007"
    dests = {row["dest"] for row in previous.inputs}
    assert dests == {"fort.chk00007", "gauge00001.txt", "fgout0001.b0001"}


@pytest.mark.parametrize("uri,drop,final,code", [
    ("s3://runs/run1/results.slf", (), 300.0, "GEOCLAW_RESTART_FILE"),
    ("s3://runs/run1/fort.chk00007", ("geoclaw_metrics.json",), 300.0, "GEOCLAW_RESTART_NOT_GEOCLAW"),
    ("s3://runs/run1/fort.chk00009", (), 300.0, "GEOCLAW_RESTART_NO_CHECKPOINT"),
    ("s3://runs/run1/fort.chk00007", ("topo.data",), 300.0, "GEOCLAW_RESTART_NO_DATA"),
    ("s3://runs/run1/fort.chk00007", (), 600.0, "GEOCLAW_RESTART_NOT_FINAL"),
])
def test_a_previous_run_that_cannot_be_restarted_from_refuses_by_name(monkeypatch, uri, drop, final, code):
    _run(monkeypatch, final=final, drop=drop)
    with pytest.raises(GeoclawError) as caught:
        restart.previous_run(uri)
    assert caught.value.error_code == code


def _written(tmp_path, **changes):
    files = {**_FILES, **changes}
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_a_set_that_matches_the_checkpoint_passes_and_a_changed_end_or_output_may_differ(monkeypatch, tmp_path):
    _run(monkeypatch)
    previous = restart.previous_run("s3://runs/run1/fort.chk00007")
    restart.check_matches(previous, _written(tmp_path, **{"claw.data": _CLAW.replace("300.0 =: tfinal", "900.0 =: tfinal")}), 900.0)


@pytest.mark.parametrize("changes,tfinal,named", [
    ({"claw.data": _CLAW.replace("10 10 =: num_cells", "20 20 =: num_cells")}, 600.0, "num_cells"),
    ({"amr.data": _AMR.replace("2 2 =: refinement_ratios_x", "4 4 =: refinement_ratios_x")}, 600.0, "refinement_ratios_x"),
    ({"topo.data": "another bed"}, 600.0, "topo.data"),
    ({}, 300.0, "nothing to integrate"),
])
def test_a_set_that_disagrees_with_the_checkpoint_refuses_naming_it(monkeypatch, tmp_path, changes, tfinal, named):
    _run(monkeypatch)
    previous = restart.previous_run("s3://runs/run1/fort.chk00007")
    with pytest.raises(GeoclawError, match=named):
        restart.check_matches(previous, _written(tmp_path, **changes), tfinal)


def test_a_stated_checkpoint_uri_becomes_the_engines_restart_pair_and_a_stated_restart_refuses(monkeypatch):
    _run(monkeypatch)
    me = SimpleNamespace(steering=template.STEERING)
    keywords = {"CLAW_RESTART_FILE": "s3://runs/run1/fort.chk00007", "CLAW_TFINAL": 600.0}
    previous = workflow.GeoclawWorkflow._restarted(me, keywords, template.STEERING)
    assert keywords == {"CLAW_TFINAL": 600.0, "CLAW_RESTART": True, "CLAW_RESTART_FILE": "fort.chk00007"}
    assert previous.name == "fort.chk00007"
    assert workflow.GeoclawWorkflow._restarted(me, {"CLAW_TFINAL": 1.0}, template.STEERING) is None
    with pytest.raises(GeoclawError) as caught:
        workflow.GeoclawWorkflow._restarted(me, {"CLAW_RESTART": True}, template.STEERING)
    assert caught.value.error_code == "GEOCLAW_RESTART_STATED"


def test_a_run_stating_a_checkpoint_uploads_it_with_the_set_and_one_that_states_none_uploads_neither():
    assert restart.checkpoint_outputs({"CLAW_CHECKPT_STYLE": 2}) == ["fort.chk*", "fort.tck*", "*.data"]
    assert restart.checkpoint_outputs({"CLAW_CHECKPT_STYLE": 0}) == [] == restart.checkpoint_outputs({})
