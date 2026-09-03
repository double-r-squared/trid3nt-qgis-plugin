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

from trid3nt_server.workflows.lib import journal


@pytest.fixture(autouse=True)
def _tmp_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    monkeypatch.delenv(journal.ORIGIN_ENV, raising=False)
    yield


def _row(name, value, **kw):
    """A resolved sheet row, in the shape ``ResolvedParams.rows()`` hands over."""
    return SimpleNamespace(name=name, value=value, door=kw.get("door", "scenario"),
                           basis=kw.get("basis", "default_demo"),
                           units=kw.get("units"), consequence=kw.get("consequence",
                                                                    "scenario"),
                           note=kw.get("note", ""), clamped_from=kw.get("clamped_from"),
                           real_source=kw.get("real_source"))


def _record(**overrides):
    base = dict(
        run_id="RUN9", template="telemac_do_sag", engine="telemac2d",
        sheet=[_row("reach_length_km", 15.0, door="user", basis="user", units="km",
                    consequence="physics", real_source="nhd"),
               _row("compute_class", "standard", door="constant",
                    consequence="numerical")],
        answer={"min_do_mgl": 6.1, "layer_uri": "s3://b/RUN9/do.tif"},
        provenance=[SimpleNamespace(param="discharge_cms", value=12.5,
                                    basis="fetched", note="NWM cycle 2026-08-19T00Z",
                                    real_source="national_water_model")],
        result=SimpleNamespace(mesh_size_m=30.0),
        wall_seconds=91.4, origin="session",
        executed=["aoi", "run", "solve"], replayed=[], notes=[],
    )
    base.update(overrides)
    return journal.build_record(**base)


# --- the record SHAPE --------------------------------------------------------- #
def test_a_run_record_carries_the_run_its_template_and_where_it_came_from():
    rec = _record()
    assert rec["run_id"] == "RUN9"
    assert rec["template"] == "telemac_do_sag" and rec["engine"] == "telemac2d"
    assert rec["origin"] == "session"
    assert rec["recorded_at"].endswith("+00:00")
    assert rec["executed"] == ["aoi", "run", "solve"] and rec["replayed"] == []


def test_a_sheet_row_carries_its_door_and_its_basis_not_just_the_number():
    """Which door a value came through is the whole point: a discharge the user
    pinned and one the National Water Model answered are the same number and
    different evidence."""
    row = _record()["sheet"][0]
    assert row == {"name": "reach_length_km", "value": 15.0, "door": "user",
                   "basis": "user", "units": "km", "consequence": "physics",
                   "note": None, "clamped_from": None, "real_source": "nhd"}


def test_the_record_carries_the_answer_the_provenance_the_mesh_and_the_wall_time():
    rec = _record()
    assert rec["answer"]["min_do_mgl"] == 6.1
    assert rec["provenance"] == [{"param": "discharge_cms", "value": 12.5,
                                  "basis": "fetched",
                                  "note": "NWM cycle 2026-08-19T00Z",
                                  "real_source": "national_water_model"}]
    assert rec["mesh"] == {"mesh_size_m": 30.0}
    assert rec["wall_seconds"] == 91.4


def test_the_compute_class_is_lifted_off_the_sheet_row_that_declares_it():
    assert _record()["compute_class"] == "standard"


def test_a_run_with_no_compute_class_row_records_none_rather_than_guessing():
    assert _record(sheet=[_row("reach_length_km", 15.0)])["compute_class"] is None


# --- a long list is summarized, never copied --------------------------------- #
def test_a_long_list_value_is_truncated_to_a_shape_summary():
    """A sag curve is hundreds of points; the journal wants the FACT that there was
    one and its shape, not a second copy of the product."""
    curve = [float(i) for i in range(500)]
    rec = _record(answer={"sag_curve_do_mgl": curve, "min_do_mgl": 6.1})
    summary = rec["answer"]["sag_curve_do_mgl"]
    assert summary == {"length": 500, "head": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
                       "truncated": True}
    assert rec["answer"]["min_do_mgl"] == 6.1        # the scalar is untouched


def test_a_short_list_value_is_kept_whole():
    rec = _record(answer={"bbox": [-85.0, 29.7, -84.9, 29.8]})
    assert rec["answer"]["bbox"] == [-85.0, 29.7, -84.9, 29.8]


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
    token = journal.bind_notes()
    journal.journal_note("42.0% of the centreline is covered")
    assert journal.drain_notes(token) == ["42.0% of the centreline is covered"]


def test_a_note_outside_a_run_only_logs_rather_than_failing():
    """A direct call has no run to record against, and a diagnostic that raised
    would make saying something more dangerous than staying quiet."""
    journal.journal_note("no channel is bound here")


def test_draining_closes_the_channel_so_the_next_run_starts_empty():
    token = journal.bind_notes()
    journal.journal_note("first run")
    journal.drain_notes(token)
    token = journal.bind_notes()
    assert journal.drain_notes(token) == []
