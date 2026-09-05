"""The authored-steering parse gate: what it hands the container, and how it refuses.

The parse itself runs inside the TELEMAC image, against the engine's own
dictionaries, and is proved there. What is proved here is everything on this side
of the mount: which files the author submits and under which dictionary, that a
file the authoring did not write is not submitted, and that a parse failure
becomes a refusal naming the file and the keyword rather than a log line.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.telemac.authoring import cas_validate as V

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
        seen["steering"] = config["steering"]
        seen["argv"] = argv
        (Path(rundir) / "telemac_cas_stats.json").write_text(json.dumps(rows))
        return _Completed(returncode)

    monkeypatch.setattr(V.subprocess, "run", _run)
    return seen


def test_nothing_written_means_nothing_to_parse(tmp_path, monkeypatch):
    """The container is not started for a file that was never authored."""
    monkeypatch.setattr(V.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("launched with nothing"))
    assert V.validate_authored_steering(tmp_path, {"absent.cas": "telemac2d"}) == {}


def test_only_the_files_that_exist_are_submitted(tmp_path, monkeypatch):
    """A tracer run writes no WAQTEL steering, so none is offered to the parser."""
    (tmp_path / "t2d_river.cas").write_text("/ steering\n")
    seen = _driver(monkeypatch,
                   {"t2d_river.cas": {"module": "telemac2d", "ok": True,
                                      "keywords": 40}})
    rows = V.validate_authored_steering(
        tmp_path, {"t2d_river.cas": "telemac2d", "t2d_river.waqtel": "waqtel"})
    assert seen["steering"] == {"t2d_river.cas": "telemac2d"}
    assert rows["t2d_river.cas"]["keywords"] == 40
    assert "--network" in seen["argv"] and "none" in seen["argv"]


def test_a_file_that_does_not_parse_refuses_naming_the_keyword(tmp_path,
                                                               monkeypatch):
    """The refusal carries the file, its dictionary, and what the parser said."""
    (tmp_path / "t2d_river.cas").write_text("/ steering\n")
    _driver(monkeypatch, {"t2d_river.cas": {
        "module": "telemac2d", "ok": False,
        "error": "TelemacException: Unknown keyword GRAPHIC PRINTOOT PERIOD"}})
    with pytest.raises(V.CasParseError) as excinfo:
        V.validate_authored_steering(tmp_path, {"t2d_river.cas": "telemac2d"})
    message = str(excinfo.value)
    assert "t2d_river.cas" in message and "telemac2d" in message
    assert "GRAPHIC PRINTOOT PERIOD" in message


def test_a_driver_that_could_not_run_at_all_refuses_too(tmp_path, monkeypatch):
    """An unparsed file and an unrun parser are the same gap: nothing was checked."""
    (tmp_path / "t2d_river.cas").write_text("/ steering\n")
    _driver(monkeypatch, {}, returncode=125)
    with pytest.raises(V.CasParseError):
        V.validate_authored_steering(tmp_path, {"t2d_river.cas": "telemac2d"})


# --------------------------------------------------------------------------- #
# The wiring: every DAMOCLES file the SERIALIZER wrote reaches the parser.
# --------------------------------------------------------------------------- #
def _submitted(monkeypatch, tmp_path, sheet) -> dict:
    """What the serializer offers the parser, and under which dictionary."""
    from trid3nt_server.workflows.telemac.authoring import serializer as Z

    seen: dict = {}

    def _record(rundir, steering):
        from pathlib import Path

        seen.update({name: module for name, module in steering.items()
                     if (Path(rundir) / name).is_file()})
        return {}

    def _driver(rundir, config, *, what):
        from pathlib import Path

        for name in config["write"]:
            (Path(rundir) / name).write_text("/ written\n")
        (Path(rundir) / "telemac_cas_written.json").write_text(
            json.dumps({name: {} for name in config["write"]}))

    monkeypatch.setattr(Z, "validate_authored_steering", _record)
    monkeypatch.setattr(Z, "run_cas_driver", _driver)
    Z.serialize(sheet, tmp_path, steering="t2d_river.cas")
    return seen


def test_a_tracer_reach_submits_only_its_own_steering(tmp_path, monkeypatch):
    from trid3nt_server.workflows.telemac.modules import T2D, fill

    assert _submitted(monkeypatch, tmp_path, fill(T2D, DURATION=600.0)) == {
        "t2d_river.cas": "telemac2d"}


def test_a_coupled_reach_submits_the_coupled_modules_steering_too(tmp_path,
                                                                  monkeypatch):
    """Each file is read against the dictionary of the module that reads it."""
    from trid3nt_server.workflows.telemac.modules import GAIA, T2D, WAQTEL, fill

    o2 = fill(T2D, coupling=[WAQTEL.o2(water_temp_c=20.0, k1_per_day=0.3,
                                       k2_per_day=0.9, k2_formula=0,
                                       saturation_mgl=9.0)])
    assert _submitted(monkeypatch, tmp_path / "o2", o2) == {
        "t2d_river.cas": "telemac2d", "t2d_river.waqtel": "waqtel"}

    bed = fill(T2D, coupling=[GAIA.erodible(
        geometry="river.slf", boundary="river.cli", d50_um=200.0,
        density=2650.0, thickness_m=5.0, formula=1,
        morphological_factor=10.0)])
    assert _submitted(monkeypatch, tmp_path / "sed", bed) == {
        "t2d_river.cas": "telemac2d", "gaia_river.cas": "gaia"}
