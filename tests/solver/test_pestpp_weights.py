"""Calibration weights: each target's sigma off the accuracy its record cites, and the widening after the fit.

Read on the recorded USGS answers (tests/fixtures/fetcher_fields/README.md) and the recorded glm study
(tests/fixtures/pestpp/README.md).
"""

from __future__ import annotations

import csv
import datetime as dt
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
