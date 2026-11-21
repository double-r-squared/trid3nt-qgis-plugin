"""Calibration weights: each target's sigma off the accuracy its record cites, and the widening after the fit.

Read on the recorded USGS answers (tests/fixtures/fetcher_fields/README.md) and the recorded glm study
(tests/fixtures/pestpp/README.md).
"""

from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from dataretrieval.nwis import _read_rdb

from trid3nt_server.inputs.observation import Accuracy, Observation, observation
from trid3nt_server.tools.fetchers._router import registration
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import apply_ingest_transforms
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels import hooks as gw_hooks
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges import hooks as nwis_hooks
from trid3nt_server.workflows.pestpp import outputs, weights
from trid3nt_server.workflows.pestpp.authoring.control import TABLES, Observed, Parameter, write_control
from trid3nt_server.workflows.pestpp.errors import PestppError

_FIELDS = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"
_GLM = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp" / "glm"
_TRIAL = Path(__file__).resolve().parents[2] / "workers" / "pestpp" / "telemac_trial.py"
_NWIS, _GW = "fetch_usgs_nwis_gauges", "fetch_usgs_groundwater_levels"
_FT = 0.3048


def _row(source: str, data_class: str):
    return next(r for r in registration.get_spec(source).coverage if r.data_class == data_class)


def _layer(source: str, feats):
    spec = registration.get_spec(source)
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": f["geometry"], "properties": f["properties"]}
        for f in apply_ingest_transforms(feats, spec, {})]}


@pytest.fixture
def hwy_370(monkeypatch):
    """USGS 06610670 (Missouri River at Hwy 370), gage height and site record as NWIS answered them."""
    import dataretrieval.nwis as nwis

    iv = _read_rdb((_FIELDS / "nwis_iv_06610670.rdb").read_text())
    iv = iv.rename(columns={c: c.split("_", 1)[1] for c in iv.columns if c.startswith("271977_")})
    iv.index = pd.DatetimeIndex(pd.to_datetime(iv.index)).tz_localize(dt.timezone(dt.timedelta(hours=-6)))
    site = _read_rdb((_FIELDS / "nwis_site_expanded.rdb").read_text())
    monkeypatch.setattr(nwis, "get_iv", lambda **kw: (iv, None))
    monkeypatch.setattr(nwis, "get_info", lambda **kw: (site, None))
    feats = nwis_hooks.read(registration.get_spec(_NWIS), {"_mode": "instantaneous", "bbox": [-95.95, 41.1, -95.85, 41.3],
                                                           "parameter": "00065"}, timeout_s=5.0)
    return _layer(_NWIS, [f for f in feats if f["properties"]["site_no"] == "06610670"])


def _to_m(found):
    return lambda sigma: sigma * _FT if found.units == "ft" else sigma


def test_a_stage_target_s_sigma_is_the_stage_rule_and_the_gauge_zero_in_quadrature(hwy_370):
    found = observation(hwy_370, fields=_row(_NWIS, "water level series").fields, producer=_NWIS)
    gage_height = found.value - 936.34
    reading = max(0.01, 0.002 * gage_height)
    assert found.accuracy.value == pytest.approx(reading) and "TM 3-A7" in found.accuracy.cite
    assert found.zero_accuracy.value == pytest.approx(0.08) and found.zero_accuracy.cite == f"{_NWIS} field alt_acy_va"
    target = weights.observed("level_1", found.value * _FT, found, to_run=_to_m(found), group="levels")
    assert target.group == "levels" and target.basis == weights.CITED
    assert target.sigma == pytest.approx(math.hypot(reading, 0.08) * _FT)
    assert target.weight == pytest.approx(1.0 / target.sigma)
    assert [member for member, _sigma, _cite in target.parts] == ["accuracy", "above_accuracy"]


def test_a_gauge_whose_zero_states_no_accuracy_starts_at_the_widest_another_site_of_the_set_cites(hwy_370):
    stated = hwy_370["features"][0]
    unstated = {**stated, "properties": {**stated["properties"], "site_no": "06610671", "alt_acy_va": None}}
    layer = {"type": "FeatureCollection", "features": [stated, unstated]}
    found = observation(layer, fields=_row(_NWIS, "water level series").fields, producer=_NWIS, every=True)
    short = next(one for one in found if one.site_id == "06610671")
    assert short.zero_accuracy.value is None and short.zero_accuracy.unread == "FIELD_EMPTY"
    assert short.zero_accuracy.widest == pytest.approx(0.08)
    target = weights.observed("level_2", 0.0, short, to_run=_to_m(short), group="levels")
    assert target.group == weights.NOT_CITED and target.basis == weights.FLOOR
    assert target.sigma == pytest.approx(math.hypot(short.accuracy.value, 0.08) * _FT)


def test_a_well_whose_procedure_cites_no_accuracy_is_weighed_not_cited_from_the_table_s_widest():
    spec = registration.get_spec(_GW)
    feats = gw_hooks.parse_response(spec, {"state_code": "IA"}, [(_FIELDS / "usgs_field_measurements_des_moines.json").read_bytes()])
    feats = gw_hooks.enrich_merge(spec, {"state_code": "IA"}, feats, {"locations": SimpleNamespace(
        body=(_FIELDS / "usgs_monitoring_locations_des_moines.json").read_bytes())})
    found = observation(_layer(_GW, feats), fields=_row(_GW, "groundwater level").fields, producer=_GW, every=True)
    targets = [weights.observed(f"head_{i}", 0.0, one, to_run=_to_m(one), group="heads") for i, one in enumerate(found)]
    cited = [t for t in targets if t.basis == weights.CITED]
    floored = [t for t in targets if t.basis == weights.FLOOR]
    assert cited and floored and len(cited) + len(floored) == len(targets)
    assert {t.group for t in cited} == {"heads"} and {t.group for t in floored} == {weights.NOT_CITED}
    assert {round(t.sigma, 9) for t in targets} == {round(0.01 * _FT, 9)}
    assert all("TM 1-A1" in t.parts[0][2] for t in targets)


def test_a_source_citing_no_accuracy_starts_its_targets_at_one_in_the_run_s_unit():
    found = Observation(value=3.0, units="m", accuracy=Accuracy(None, unread="FIELD_UNMAPPED"))
    target = weights.observed("head_1", 3.0, found, to_run=lambda sigma: sigma, group="heads")
    assert (target.group, target.basis, target.sigma, target.parts) == (weights.NOT_CITED, weights.UNIT, 1.0, ())


def test_the_sigma_the_user_states_for_a_group_wins_and_an_unknown_group_refuses():
    targets = [(Observed("head_1", 1.0, "heads", 0.003, weights.CITED), 0),
               (Observed("head_2", 2.0, weights.NOT_CITED, 1.0, weights.UNIT), 1)]
    restated = weights.restated(targets, {"not_cited": 0.5})
    assert restated[0] == targets[0]
    assert (restated[1][0].sigma, restated[1][0].basis, restated[1][0].weight) == (0.5, weights.USER, 2.0)
    assert weights.restated(targets, None) == targets
    for stated, code in (({"levels": 0.1}, "PESTPP_SIGMA_GROUP_UNKNOWN"), ({"heads": 0}, "PESTPP_SIGMA_INVALID")):
        with pytest.raises(PestppError) as refused:
            weights.restated(targets, stated)
        assert refused.value.error_code == code


def _unit_and(basis: str) -> list[Observed]:
    return [Observed("head_1", 1.0, "heads" if basis == weights.CITED else weights.NOT_CITED, 0.003, basis),
            Observed("head_2", 2.0, weights.NOT_CITED, 1.0, weights.UNIT)]


@pytest.mark.parametrize("basis", [weights.CITED, weights.FLOOR])
def test_a_unit_start_group_beside_a_cited_or_floor_group_states_the_warning(basis):
    from trid3nt_server.errors import ACCURACY_UNIT_START, unit_start_warning

    assert weights.unit_started(_unit_and(basis)) == 1
    note = unit_start_warning(count=1)
    assert note.startswith(ACCURACY_UNIT_START) and "1 targets" in note


def test_a_study_of_unit_start_groups_only_states_no_unit_start_warning():
    assert weights.unit_started([Observed("a", 1.0, weights.NOT_CITED, 1.0, weights.UNIT)]) == 0


def test_a_unit_start_group_the_user_states_sigma_for_states_no_unit_start_warning():
    targets = [(t, i) for i, t in enumerate(_unit_and(weights.CITED))]
    stated = weights.restated(targets, {"not_cited": 0.5})
    assert weights.unit_started([t for t, _ in stated]) == 0


def test_the_widening_brings_each_group_s_phi_to_its_count_and_never_narrows_a_cited_sigma():
    residuals = outputs.read_residuals(_GLM, tool="glm")
    observed = [Observed(row["name"], row["measured"], row["group"], 1.0 / row["weight"], weights.CITED)
                for row in residuals]
    widening = weights.widened(observed, residuals)
    phi = sum((row["weight"] * row["residual"]) ** 2 for row in residuals)
    assert widening["phi"] == pytest.approx(phi) and widening["observations"] == len(residuals)
    assert phi > len(residuals) and widening["widened_phi"] == pytest.approx(len(residuals))
    assert widening["sigma"]["head_1"] == pytest.approx(math.sqrt(phi / len(residuals)))
    assert "parameters" not in widening
    tight = [Observed(o.name, o.value, o.group, 1e6, weights.CITED) for o in observed]
    tight_residuals = [{**row, "weight": 1e-6} for row in residuals]
    held = weights.widened(tight, tight_residuals)
    assert held["groups"]["heads"]["factor"] == 1.0 and held["widened_phi"] < len(residuals)
    unit = [Observed(o.name, o.value, o.group, 1e6, weights.UNIT) for o in observed]
    assert weights.widened(unit, tight_residuals)["widened_phi"] == pytest.approx(len(residuals))


def _write(directory, observations, **kwargs):
    directory.mkdir()
    return write_control(directory, name="pestpp.pst", parameters=[Parameter("k", 1.0, 0.1, 10.0, "k")],
                         observations=observations, templates=[("m.tpl", "m.in")], instructions=[("m.ins", "m.out")],
                         command="mf6", **kwargs)


def test_the_weight_written_is_one_over_sigma_and_balancing_is_off_unless_asked(tmp_path):
    observations = [Observed("head_1", 1.0, "heads", 0.25, weights.CITED),
                    Observed("head_2", 2.0, weights.NOT_CITED, 0.5, weights.FLOOR)]
    plain = _write(tmp_path / "plain", observations)
    with (plain.parent / TABLES["observation data"]).open() as fh:
        assert [(row["obsnme"], float(row["weight"]), row["obgnme"]) for row in csv.DictReader(fh)] == [
            ("head_1", 4.0, "heads"), ("head_2", 2.0, "not_cited")]
    assert "ies_phi_factor_file" not in plain.read_text() and not (plain.parent / TABLES["phi factors"]).exists()
    balanced = _write(tmp_path / "balanced", observations, balanced=True)
    assert f"ies_phi_factor_file {TABLES['phi factors']}" in balanced.read_text().splitlines()
    assert (balanced.parent / TABLES["phi factors"]).read_text() == "heads,0.5\nnot_cited,0.5\n"


def test_a_cited_gauge_zero_is_a_datum_parameter_and_leaves_sigma_counted_once(hwy_370, tmp_path):
    from trid3nt_server.workflows.pestpp.authoring import telemac
    from trid3nt_server.workflows.pestpp.authoring.control import prior

    found = observation(hwy_370, fields=_row(_NWIS, "water level series").fields, producer=_NWIS)
    both = weights.observed("level_1", found.value * _FT, found, to_run=_to_m(found), group="levels")
    held = weights.observed("level_1", found.value * _FT, found, to_run=_to_m(found), group="levels", datum=True)
    reading = max(0.01, 0.002 * (found.value - 936.34)) * _FT
    assert (held.group, held.basis) == ("levels", weights.CITED)
    assert held.sigma == pytest.approx(reading) and [m for m, _s, _c in held.parts] == ["accuracy"]
    assert held.datum == (pytest.approx(0.08 * _FT), f"{_NWIS} field alt_acy_va")
    # the zero is counted once: out of sigma by exactly what the datum's prior holds
    assert held.sigma ** 2 + held.datum[0] ** 2 == pytest.approx(both.sigma ** 2)
    assert both.datum is None
    parameters = telemac.datums(tmp_path, [(held, 412)])
    (datum,) = parameters
    assert (datum.name, datum.group, datum.transform, datum.sd) == ("datum_1", "datum_1", "none", held.datum[0])
    assert datum.value * datum.scale + datum.offset == 0.0 and datum.lower > 0.0
    stated = prior(parameters)["datum_1"]
    assert stated["mean"] == pytest.approx(0.0) and stated["sd"] == held.datum[0]
    assert (stated["lower"], stated["upper"]) == (pytest.approx(-2.0 * held.datum[0]), pytest.approx(2.0 * held.datum[0]))
    assert "alt_acy_va" in stated["sd_source"]
    assert (tmp_path / telemac.DATUMS).read_text() == "OBS_1 0.0\n"
    assert (tmp_path / f"{telemac.HELD}.ins").read_text() == "pif ~\nl1 w !level_1!\n"
    control = write_control(tmp_path, name="pestpp.pst", parameters=parameters, observations=[held],
                            templates=[(f"{telemac.DATUMS}.tpl", telemac.DATUMS)],
                            instructions=[(f"{telemac.HELD}.ins", telemac.HELD)], command="python trial")
    assert "parcov study_prior.unc" in control.read_text()
    assert (tmp_path / "study_prior.unc").read_text().splitlines() == [
        "START STANDARD_DEVIATION", f"datum_1 {held.datum[0]!r}", "END STANDARD_DEVIATION"]
    assert not (tmp_path / "study_prior.cov").exists()
    with (tmp_path / TABLES["observation data"]).open() as fh:
        assert float(next(csv.DictReader(fh))["weight"]) == pytest.approx(1.0 / reading)


def test_a_gauge_zero_citing_no_accuracy_stays_in_sigma_and_is_no_parameter(hwy_370, tmp_path):
    from trid3nt_server.workflows.pestpp.authoring import telemac

    stated = hwy_370["features"][0]
    unstated = {**stated, "properties": {**stated["properties"], "site_no": "06610671", "alt_acy_va": None}}
    found = observation({"type": "FeatureCollection", "features": [stated, unstated]},
                        fields=_row(_NWIS, "water level series").fields, producer=_NWIS, every=True)
    short = next(one for one in found if one.site_id == "06610671")
    target = weights.observed("level_2", 0.0, short, to_run=_to_m(short), group="levels", datum=True)
    assert target.datum is None and target.basis == weights.FLOOR
    assert target.sigma == pytest.approx(math.hypot(short.accuracy.value, 0.08) * _FT)
    assert telemac.datums(tmp_path, [(target, 7)]) == [] and not (tmp_path / telemac.DATUMS).exists()


def _hwy_370_window():
    """Hwy 370's recorded day of gage height (15 min, CST), on its NAVD88 zero 936.34 ft, in metres on a clock from its
    first reading."""
    from trid3nt_server.inputs.temporal import Series

    rows = [line.split("\t") for line in (_FIELDS / "nwis_iv_06610670.rdb").read_text().splitlines()
            if line.startswith("USGS\t")]
    stamps = [dt.datetime.fromisoformat(row[2]) for row in rows]
    return Series([(t - stamps[0]).total_seconds() for t in stamps],
                  [(float(row[4]) + 936.34) * _FT for row in rows], units="m")


def _missouri_held_at(directory, found, *, zero=True):
    """The Missouri reach's recorded steering file, its outflow held at Hwy 370's measured window: the level the slot
    ingested (on metres) as the deck's PRESCRIBED ELEVATIONS entry and its window as the liquid boundaries file's SL(1)
    column, written by the product's own writer; and the finished run's record of that level input."""
    import shutil

    from trid3nt_server.workflows.runtime.journal import build_record
    from trid3nt_server.workflows.runtime.workflow import _accuracies
    from trid3nt_server.workflows.telemac.authoring.boundaries import liquid_boundaries_file
    from trid3nt_server.workflows.telemac.authoring.staged_check import read_steering

    missouri = Path(__file__).resolve().parents[1] / "fixtures" / "telemac_missouri"
    steering = "telemac2d_telemac_dye_release.cas"
    shutil.copy(missouri / steering, directory / steering)
    window = _hwy_370_window()
    (directory / "river_boundaries.txt").write_text(liquid_boundaries_file(
        [("SL(1)", "m", window)], start_s=window.times_s[0], until_s=window.times_s[-1], tail_s=60.0))
    stated = {**read_steering(directory / steering), "LIQUID BOUNDARIES FILE": ["river_boundaries.txt"],
              "PRESCRIBED ELEVATIONS": [f"{found.value:.3f}", "0.0"], "INITIAL ELEVATION": [f"{found.value:.3f}"]}
    accuracies = _accuracies(found)
    if not zero:
        accuracies["zero_accuracy"] = {**accuracies["zero_accuracy"], "value": None, "unread": "FIELD_EMPTY"}
    record = build_record(run_id="r", engine="telemac", module="telemac2d", result=None, wall_seconds=1.0,
                          origin="telemac_dye_release", notes=[], inputs={"level": {
                              "value": "$outflow_stage", "origin": "user", "source": _NWIS, **accuracies}})
    return stated, SimpleNamespace(record=record)


def test_the_run_records_the_measured_level_s_accuracy_and_its_gauge_zero_s(hwy_370):
    from trid3nt_server.workflows.runtime.workflow import _accuracies

    found = observation(hwy_370, fields=_row(_NWIS, "water level series").fields, producer=_NWIS, to_units="m")
    held = _accuracies(found)
    assert held["accuracy"]["value"] == pytest.approx(max(0.01, 0.002 * (found.value / _FT - 936.34)) * _FT)
    assert "TM 3-A7" in held["accuracy"]["cite"]
    assert held["zero_accuracy"] == {"value": pytest.approx(0.08 * _FT), "cite": f"{_NWIS} field alt_acy_va",
                                     "unread": "", "widest": None}
    assert _accuracies(4.2) == {} and _accuracies(Observation(value=4.2)) == {}


def test_a_measured_outflow_level_citing_its_accuracy_is_an_offset_parameter(hwy_370, tmp_path):
    from trid3nt_server.workflows.pestpp.authoring import telemac
    from trid3nt_server.workflows.pestpp.authoring.control import prior

    found = observation(hwy_370, fields=_row(_NWIS, "water level series").fields, producer=_NWIS, to_units="m")
    stated, finished = _missouri_held_at(tmp_path, found)
    assert set(finished.record["inputs"]["level"]) == {"value", "origin", "source", "accuracy", "zero_accuracy"}
    level, unheld = telemac._measured_level(tmp_path, stated, finished)
    sd = math.hypot(finished.record["inputs"]["level"]["accuracy"]["value"], 0.08 * _FT)
    assert unheld == "" and (level["boundary"], level["column"], level["initial"]) == (0, "SL(1)", True)
    assert level["sd"] == pytest.approx(sd) and "TM 3-A7" in level["cite"] and "alt_acy_va" in level["cite"]
    the_deck = telemac.Deck(steering="telemac2d_telemac_dye_release.cas", stated=stated, module="telemac2d",
                            result="r2d.slf", utm_epsg=32615, level=level)
    held, notes = telemac.supplied(the_deck)
    assert held == [("outflow_level", level["sd"], level["cite"])] and "its bed and stated values" in notes[0]
    parameter = telemac.outflow_offset(tmp_path, the_deck=the_deck)
    assert (parameter.name, parameter.transform, parameter.sd) == ("outflow_level", "none", level["sd"])
    assert parameter.value * parameter.scale + parameter.offset == 0.0 and parameter.lower > 0.0
    stated_prior = prior([parameter])["outflow_level"]
    assert stated_prior["mean"] == pytest.approx(0.0)
    assert (stated_prior["lower"], stated_prior["upper"]) == (pytest.approx(-2.0 * sd), pytest.approx(2.0 * sd))
    assert (tmp_path / telemac.OUTFLOW).read_text() == "OUTFLOW 0.0\n"
    assert "~outflow_level" in (tmp_path / f"{telemac.OUTFLOW}.tpl").read_text()
    table = json.loads((tmp_path / telemac.LEVELS).read_text())
    assert (table["level"], table["column"], table["unmoved"]) == (level["value"], "SL(1)", "river_boundaries.txt.unmoved")
    # the calibrated run and a trial move the level the same: the steering entry, the opening and every instant
    before = (tmp_path / "river_boundaries.txt").read_text()
    values = telemac._offset_level(tmp_path, the_deck, 0.02)
    assert values == {"PRESCRIBED ELEVATIONS": [round(level["value"] + 0.02, 4), 0.0],
                      "INITIAL ELEVATION": round(level["value"] + 0.02, 4)}
    moved = (tmp_path / "river_boundaries.txt").read_text()
    spec = importlib.util.spec_from_file_location("telemac_trial", _TRIAL)
    trial = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trial)
    trial.shift_column(str(tmp_path / table["unmoved"]), str(tmp_path / "trial.txt"), "SL(1)", 0.02)
    assert (tmp_path / "trial.txt").read_text() == moved
    rows = [line.split() for line in before.splitlines()[3:]]
    after = [line.split() for line in moved.splitlines()[3:]]
    assert len(rows) == len(_hwy_370_window().values) + 1
    assert all(float(b[1]) == pytest.approx(float(a[1]) + 0.02, abs=1e-6) and b[0] == a[0] for a, b in zip(rows, after))


def test_a_measured_outflow_level_whose_zero_cites_none_is_no_parameter(hwy_370, tmp_path):
    from trid3nt_server.workflows.pestpp.authoring import telemac

    found = observation(hwy_370, fields=_row(_NWIS, "water level series").fields, producer=_NWIS, to_units="m")
    stated, finished = _missouri_held_at(tmp_path, found, zero=False)
    assert telemac._measured_level(tmp_path, stated, finished) == (None, "uncited")
    assert telemac._measured_level(tmp_path, stated, SimpleNamespace(record={"inputs": {}})) == (None, "unmeasured")
    the_deck = telemac.Deck(steering="s.cas", stated=stated, module="telemac2d", result="r", utm_epsg=32615)
    held, notes = telemac.supplied(the_deck)
    assert held == [] and "its bed, boundary levels and stated values" in notes[0]
