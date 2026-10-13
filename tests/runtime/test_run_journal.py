"""The RUN JOURNAL: one append-only JSONL line per completed run.

Decoupled from artifacts on purpose - case data is delete-on-whim and run
prefixes come and go, so the record of what was asked, what was resolved and what
came back has to outlive every artifact it describes.

Offline: every write here lands under a tmp persistence dir.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trid3nt_server.inputs import run_notes
from trid3nt_server.workflows.runtime import journal


@pytest.fixture(autouse=True)
def _tmp_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    monkeypatch.delenv(journal.ORIGIN_ENV, raising=False)
    yield


def _record(**overrides):
    base = dict(
        run_id="RUN9", engine="telemac", module="telemac2d",
        inputs={"DURATION": {"value": 7200.0, "origin": "template",
                             "source": "telemac_do_sag"},
                "reach_length_km": {"value": 15.0, "origin": "user",
                                    "source": None}},
        result=SimpleNamespace(mesh_size_m=30.0),
        wall_seconds=91.4, origin="session",
        notes=[],
    )
    base.update(overrides)
    return journal.build_record(**base)


# --- the record SHAPE --------------------------------------------------------- #
def test_a_run_record_carries_the_run_its_engine_and_where_it_came_from():
    rec = _record()
    assert rec["run_id"] == "RUN9"
    # The engine and the MODULE of it that ran are the run's identity; the
    # template it started in is not on the line at all.
    assert rec["engine"] == "telemac" and rec["module"] == "telemac2d"
    assert "template" not in rec
    assert rec["origin"] == "session"
    assert rec["recorded_at"].endswith("+00:00")


def test_the_record_carries_one_map_of_inputs_by_name():
    """A stated value, a keyword of the deck and a supplied or matched row sit in
    ONE map, each with who stated it and the source that names it; the three maps
    a record once split them across are gone."""
    rec = _record(inputs={
        "reach_length_km": {"value": 15.0, "origin": "user", "source": None},
        "LAW_OF_BOTTOM_FRICTION": {"value": 4, "origin": "user", "source": None},
        "domain": {"value": "s3://cache/basin.fgb", "origin": "user",
                   "source": None},
        "bed": {"value": None, "origin": "producer", "source": "fetch_bed"}})
    assert rec["inputs"]["reach_length_km"] == {
        "value": 15.0, "origin": "user", "source": None}
    assert rec["inputs"]["domain"]["value"] == "s3://cache/basin.fgb"
    assert rec["inputs"]["bed"] == {"value": None, "origin": "producer",
                                    "source": "fetch_bed"}
    for gone in ("sheet", "keywords", "supplied", "sources", "fill"):
        assert gone not in rec


def test_the_record_carries_the_mesh_and_one_record_of_an_input():
    rec = _record()
    assert "provenance" not in rec
    assert rec["mesh"] == {"mesh_size_m": 30.0, "key": None, "built_by": None}


def test_the_mesh_field_carries_the_content_key_and_the_run_that_built_it():
    rec = _record(mesh={"key": "54a874e9df6e", "built_by": "RUN1",
                        "artifact": object()})
    assert rec["mesh"] == {"mesh_size_m": 30.0, "key": "54a874e9df6e",
                           "built_by": "RUN1"}
    assert rec["wall_seconds"] == 91.4


def test_the_record_restates_no_core_count_beside_the_decks_own_keyword():
    """The count a run was partitioned across is a keyword of the solved deck,
    so the record's ``inputs`` carry it and no second field does."""
    assert "cores" not in _record()


# --- a long list is summarized, never copied --------------------------------- #
def test_a_long_list_value_is_truncated_to_a_shape_summary():
    """A stage curve is hundreds of points; the journal wants the FACT that there was
    one and its shape, not a second copy of the deck."""
    curve = [float(i) for i in range(500)]
    rec = _record(inputs={"STAGE_CURVE": {"value": curve, "origin": "user"},
                          "DURATION": {"value": 3600, "origin": "user"}})
    summary = rec["inputs"]["STAGE_CURVE"]["value"]
    assert summary == {"length": 500, "head": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
                       "truncated": True}
    assert rec["inputs"]["DURATION"]["value"] == 3600       # the scalar is untouched


def test_a_short_list_value_is_kept_whole():
    rec = _record(inputs={"BBOX": {"value": [-85.0, 29.7, -84.9, 29.8]}})
    assert rec["inputs"]["BBOX"]["value"] == [-85.0, 29.7, -84.9, 29.8]


# --- append + read round-trip ------------------------------------------------- #
def test_append_and_read_round_trip_through_the_persistence_dir():
    first = journal.append_record(_record(run_id="RUN1"))
    second = journal.append_record(_record(run_id="RUN2"))
    assert first == second == journal.journal_path()
    assert first.name == "run_journal.jsonl"

    records = journal.read_records()
    assert [r["run_id"] for r in records] == ["RUN1", "RUN2"]   # oldest first


def test_reading_a_journal_that_does_not_exist_yet_is_empty_not_an_error():
    assert journal.read_records() == []


def test_a_malformed_line_is_skipped_rather_than_fatal():
    """A half-written line must not cost the reader every record around it."""
    journal.append_record(_record(run_id="RUN1"))
    path = journal.journal_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n\n")
    journal.append_record(_record(run_id="RUN2"))

    assert [r["run_id"] for r in journal.read_records()] == ["RUN1", "RUN2"]


def test_a_journal_write_that_cannot_land_never_fails_the_run(tmp_path, monkeypatch):
    """The run already happened and its products already exist; refusing to hand
    the caller its answer because a log line could not be written would be the
    failure-retracts-something anti-pattern."""
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("this is a file, so nothing can be created under it")
    monkeypatch.setattr(journal, "journal_path",
                        lambda: blocked / "run_journal.jsonl")
    assert journal.append_record(_record()) is None


def test_every_record_is_one_json_line():
    journal.append_record(_record(run_id="RUN1"))
    journal.append_record(_record(run_id="RUN2"))
    lines = journal.journal_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["run_id"] for line in lines] == ["RUN1", "RUN2"]


# --- the origin label --------------------------------------------------------- #
def test_a_declared_origin_label_beats_the_live_session_guess(monkeypatch):
    """A canary and a person asking a question produce the same shaped record, and
    telemetry that learned a default from a canary would be learning from a
    fixture - so a driver gets to say what it is."""
    monkeypatch.setenv(journal.ORIGIN_ENV, "canary:flood")
    assert journal.run_origin(live_session=True) == "canary:flood"
    assert journal.run_origin(live_session=False) == "canary:flood"


def test_without_a_label_the_origin_is_the_session_or_headless():
    assert journal.run_origin(live_session=True) == "session"
    assert journal.run_origin(live_session=False) == "headless"


def test_a_blank_origin_label_falls_back_rather_than_recording_nothing(monkeypatch):
    monkeypatch.setenv(journal.ORIGIN_ENV, "   ")
    assert journal.run_origin(live_session=False) == "headless"


# --- the note channel a producer says something on ----------------------------- #
def test_a_note_written_inside_the_channel_drains_out_of_it():
    """What a step MEASURED and has no result field to say in still reaches the
    reader: the channel is opened per run and drained at the end of it."""
    token = run_notes.bind_notes()
    run_notes.journal_note("42.0% of the centreline is covered")
    assert run_notes.drain_notes(token) == ["42.0% of the centreline is covered"]


def test_a_note_outside_a_run_only_logs_rather_than_failing():
    """A direct call has no run to record against, and a diagnostic that raised
    would make saying something more dangerous than staying quiet."""
    run_notes.journal_note("no channel is bound here")


def test_draining_closes_the_channel_so_the_next_run_starts_empty():
    token = run_notes.bind_notes()
    run_notes.journal_note("first run")
    run_notes.drain_notes(token)
    token = run_notes.bind_notes()
    assert run_notes.drain_notes(token) == []


def test_the_solver_s_correct_end_is_read_off_the_solve_step_and_recorded():
    """A solve says whether it reached its own correct end; the line carries the
    flag it said, and nothing where the solve step's metrics state none."""
    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

    ended = SimpleNamespace(results={"solve": {"metrics": {"correct_end": True}}})
    silent = SimpleNamespace(results={"solve": {"metrics": {}}})
    assert TelemacWorkflow._correct_end(TelemacWorkflow, ended) is True
    assert TelemacWorkflow._correct_end(TelemacWorkflow, silent) is None
    assert _record(correct_end=True)["correct_end"] is True
    assert _record()["correct_end"] is None


def _verdict(value, origin, source=""):
    from trid3nt_contracts.common import Origin
    from trid3nt_server.workflows.runtime.fill import ACCEPTED, Verdict

    return Verdict(ACCEPTED, value, origin=Origin(origin), source=source)


def test_the_run_s_own_line_carries_every_input_and_the_correct_end_its_solve_step_said(
        monkeypatch):
    """The workflow's one journal seam folds the solved deck's keywords and what
    the fill answered into the one map: a keyword the caller stated under its
    spaced spelling is the deck's own row, a supplied row records what was handed
    in and a matched one names its source."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.workflow import RunResult

    written: list[dict] = []
    monkeypatch.setattr(journal, "append_record", written.append)
    wf = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    run = RunResult(value=None, results={"solve": {
        "module": "telemac2d", "metrics": {"correct_end": False},
        "sheet": {"filled": {
            "TIME_STEP": {"value": 0.5,
                          "provenance": "derived: settled.time_step_s"},
            "LAW_OF_BOTTOM_FRICTION": {"value": 4, "provenance": "user"}}}}},
        inputs={"spill_fraction": _verdict(0.4, "user"),
                "LAW OF BOTTOM FRICTION": _verdict(4, "user"),
                "domain": _verdict(object(), "user"),
                "bed": _verdict(object(), "producer", "fetch_bed")})
    inputs = wf._inputs(run, {"domain": "s3://cache/reach.fgb"})
    wf._journal("RUN1", run, SimpleNamespace(mesh_size_m=None), 1.0, [], inputs)
    line = written[0]
    assert line["correct_end"] is False
    # An optional value nobody stated is on the same map: no value, and the
    # sentence saying what the run derived in its place as its source.
    absent = {name: line["inputs"].pop(name) for name, row
              in list(line["inputs"].items())
              if row["origin"] == "derived" and row["value"] is None}
    assert absent["event_time"] == {
        "value": None, "origin": "derived",
        "source": wf.input("event_time").absent_note}
    assert "provenance" not in line
    assert line["inputs"] == {
        "TIME_STEP": {"value": 0.5, "origin": "derived",
                      "source": "settled.time_step_s"},
        "LAW_OF_BOTTOM_FRICTION": {"value": 4, "origin": "user", "source": None},
        "spill_fraction": {"value": 0.4, "origin": "user", "source": None},
        "domain": {"value": "s3://cache/reach.fgb", "origin": "user",
                   "source": None},
        "bed": {"value": None, "origin": "producer", "source": "fetch_bed"}}


def test_a_keyword_stated_on_a_coupled_body_keeps_its_own_entry():
    """Two bodies may spell one identifier: the carrier's is the deck's row, and
    the one a caller stated on a coupled body stays under the name it was stated."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.workflow import RunResult
    from trid3nt_server.workflows.telemac.workflow import run_bodies

    wf = TOOL_REGISTRY["telemac_do_sag"].fn.workflow
    carrier, coupled = run_bodies(wf.steering)[:2]
    shared = next(name for name in coupled.MODULE_INPUT
                  if name in carrier.MODULE_INPUT)
    stated = f"{coupled.MODULE}: {coupled.MODULE_INPUT[shared].keyword}"
    assert not wf.input(stated).carried and wf.input(stated).name == shared
    run = RunResult(value=None, results={"solve": {"sheet": {"filled": {
        shared: {"value": 1, "provenance": "template: t"}}}}},
        inputs={stated: _verdict(2, "user")})
    inputs = wf._inputs(run, {})
    assert inputs[shared]["value"] == 1
    assert inputs[stated] == {"value": 2, "origin": "user", "source": None}


def test_the_run_journals_the_mesh_its_mesh_step_returned(monkeypatch):
    from trid3nt_server.workflows.runtime.workflow import RunResult, Workflow

    lines: list = []
    monkeypatch.setattr(journal, "append_record", lines.append)
    skeleton = SimpleNamespace(engine="telemac", _module=lambda run: None,
                               _correct_end=lambda run: None)
    run = RunResult(value=None, results={"mesh": {"key": "5f4d1a42f53e",
                                                  "built_by": "RUN1"}})
    Workflow._journal(skeleton, "RUN2", run,
                      SimpleNamespace(mesh_size_m=30.0), 1.0, [], {})
    assert lines[0]["mesh"] == {"mesh_size_m": 30.0, "key": "5f4d1a42f53e",
                                "built_by": "RUN1"}
