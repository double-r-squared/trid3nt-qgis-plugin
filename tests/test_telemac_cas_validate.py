"""The authored-deck parse gate: what it hands the container, and how it refuses.

The parse itself runs inside the TELEMAC image, against the engine's own
dictionaries, and is proved there. What is proved here is everything on this side
of the mount: which decks the author submits and under which dictionary, that a
deck the authoring did not write is not submitted, and that a parse failure
becomes a refusal naming the deck and the keyword rather than a log line.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.telemac.steps import author as A
from trid3nt_server.workflows.telemac.steps import cas_validate as V

_BED = {"bed_top_m": 100.0, "bed_drop_m": 3.0}
_ORDER = ("outflow", "inflow")
_CENTERLINE = [(x, 0.0) for x in range(0, 1100, 100)]


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _driver(monkeypatch, rows, *, returncode=0):
    """Stand in for the container, recording the config it was handed."""
    seen: dict = {}

    def _run(argv, **_kw):
        rundir = next(a.split(":")[0] for a in argv if a.endswith(":/data"))
        from pathlib import Path

        config = json.loads(
            (Path(rundir) / "telemac_cas_config.json").read_text())
        seen["decks"] = config["decks"]
        seen["argv"] = argv
        (Path(rundir) / "telemac_cas_stats.json").write_text(json.dumps(rows))
        return _Completed(returncode)

    monkeypatch.setattr(V.subprocess, "run", _run)
    return seen


def test_nothing_written_means_nothing_to_parse(tmp_path, monkeypatch):
    """The container is not started for a deck that was never authored."""
    monkeypatch.setattr(V.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("launched with no decks"))
    assert V.validate_authored_decks(tmp_path, {"absent.cas": "telemac2d"}) == {}


def test_only_the_decks_that_exist_are_submitted(tmp_path, monkeypatch):
    """A tracer run writes no WAQTEL steering, so none is offered to the parser."""
    (tmp_path / "t2d_river.cas").write_text("/ deck\n")
    seen = _driver(monkeypatch,
                   {"t2d_river.cas": {"module": "telemac2d", "ok": True,
                                      "keywords": 40}})
    rows = V.validate_authored_decks(
        tmp_path, {"t2d_river.cas": "telemac2d", "t2d_river.waqtel": "waqtel"})
    assert seen["decks"] == {"t2d_river.cas": "telemac2d"}
    assert rows["t2d_river.cas"]["keywords"] == 40
    assert "--network" in seen["argv"] and "none" in seen["argv"]


def test_a_deck_that_does_not_parse_refuses_naming_the_keyword(tmp_path,
                                                               monkeypatch):
    """The refusal carries the deck, its dictionary, and what the parser said."""
    (tmp_path / "t2d_river.cas").write_text("/ deck\n")
    _driver(monkeypatch, {"t2d_river.cas": {
        "module": "telemac2d", "ok": False,
        "error": "TelemacException: Unknown keyword GRAPHIC PRINTOOT PERIOD"}})
    with pytest.raises(V.CasParseError) as excinfo:
        V.validate_authored_decks(tmp_path, {"t2d_river.cas": "telemac2d"})
    message = str(excinfo.value)
    assert "t2d_river.cas" in message and "telemac2d" in message
    assert "GRAPHIC PRINTOOT PERIOD" in message


def test_a_driver_that_could_not_run_at_all_refuses_too(tmp_path, monkeypatch):
    """An unparsed deck and an unrun parser are the same gap: nothing was checked."""
    (tmp_path / "t2d_river.cas").write_text("/ deck\n")
    _driver(monkeypatch, {}, returncode=125)
    with pytest.raises(V.CasParseError):
        V.validate_authored_decks(tmp_path, {"t2d_river.cas": "telemac2d"})


# --------------------------------------------------------------------------- #
# The wiring: every DAMOCLES deck the author wrote reaches the parser.
# --------------------------------------------------------------------------- #
def _authored(monkeypatch, tmp_path, tag="run", **deck):
    submitted: dict = {}
    tmp_path = tmp_path / tag
    tmp_path.mkdir()

    def _record(rundir, decks):
        from pathlib import Path

        submitted.update({name: module for name, module in decks.items()
                          if (Path(rundir) / name).is_file()})
        return {}

    monkeypatch.setattr(A, "validate_authored_decks", _record)
    A.author_reach_deck(
        tmp_path, deck={"name": "reach", "duration_s": 3600.0,
                        "time_step_s": 1.0, **deck},
        geometry="mesh.slf", boundary="mesh.cli", results="r2d.slf",
        cas_name="t2d_river.cas", liquid_boundary_order=_ORDER, bed=_BED,
        source_utm=(500.0, 0.0), centerline_utm=_CENTERLINE)
    return submitted


def test_a_tracer_reach_submits_only_its_own_deck(tmp_path, monkeypatch):
    assert _authored(monkeypatch, tmp_path) == {"t2d_river.cas": "telemac2d"}


def test_a_coupled_reach_submits_the_coupled_modules_steering_too(tmp_path,
                                                                  monkeypatch):
    """Each deck is read against the dictionary of the module that reads it."""
    assert _authored(monkeypatch, tmp_path, tag="o2", substance_class="do_sag",
                     do_sag_effluent_bod_mgl=250.0,
                     do_sag_effluent_q_m3s=1.0, do_sag_effluent_do_mgl=2.0,
                     do_sag_upstream_do_mgl=9.0) == {
        "t2d_river.cas": "telemac2d", A.WAQTEL_FILENAME: "waqtel"}
    assert _authored(monkeypatch, tmp_path, tag="sed",
                     substance_class="sediment") == {
        "t2d_river.cas": "telemac2d", A.GAIA_STEERING_FILENAME: "gaia"}


def test_the_rain_on_grid_deck_reaches_the_parser(tmp_path, monkeypatch):
    submitted: dict = {}
    monkeypatch.setattr(A, "validate_authored_decks",
                        lambda _rundir, decks: submitted.update(decks) or {})
    A.author_rog_deck(
        tmp_path, deck={"name": "creek", "duration_s": 7200.0,
                        "time_step_s": 2.0},
        geometry="rog.slf", boundary="rog.cli", results="r2d_rog.slf",
        cas_name="t2d_rog.cas", cn_map="cn.dat", friction_laws="fr.tbl",
        zones_file="zones.dat", rain_mm_per_day=48.0, runoff_path="native")
    assert submitted == {"t2d_rog.cas": "telemac2d"}
