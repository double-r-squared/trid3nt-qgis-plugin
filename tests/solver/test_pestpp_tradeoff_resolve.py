"""The trade-off authoring, the selection of one candidate, and the charts a trade-off publishes."""

import asyncio

import pytest

from trid3nt.workflows.pestpp.authoring.tradeoff import author_tradeoff
from trid3nt.workflows.pestpp.errors import PestppError
from trid3nt.workflows.pestpp.tradeoff import TradeoffResult, _publish_charts, _selected


def _deck(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "flow.nam").write_text(
        "BEGIN PACKAGES\n  DIS6 grid.dis\n  WEL6 flow.wel Wells_A\n  SFR6 flow.sfr River\n"
        "  OBS6 original.obs\nEND PACKAGES\n")
    (root / "flow.wel").write_text("BEGIN OPTIONS\n  BOUNDNAMES\nEND OPTIONS\n")
    (root / "flow.sfr").write_text("BEGIN OPTIONS\nEND OPTIONS\n")
    (root / "mfsim.nam").write_text(
        "BEGIN TIMING\nTDIS6 clock.tdis\nEND TIMING\nBEGIN MODELS\nGWF6 flow.nam aquifer\nEND MODELS\n")
    (root / "clock.tdis").write_text("BEGIN PERIODDATA\n1 1 1\n2 2 1\n3 1 1\nEND PERIODDATA\n")
    (root / "flow.wel").write_text(
        "BEGIN OPTIONS\nBOUNDNAMES\nEND OPTIONS\n"
        "BEGIN PERIOD 1\n 1 2 3 -2 well\nEND PERIOD\n"
        "BEGIN PERIOD 2\n 1 2 3 -3 well\nEND PERIOD\n")
    (root / "original.obs").write_text("BEGIN CONTINUOUS FILEOUT original.csv\n  old head 1 2 3\nEND CONTINUOUS\n")


def test_authoring_binds_objectives_with_senses_and_the_search(tmp_path):
    _deck(tmp_path)
    selection = dict(period=2, step=2, units="m3/day", source="published model")
    control, bound = author_tradeoff(
        tmp_path, model="aquifer", generations=3, population=12, generator="de", seed=7,
        decisions=[dict(name="pump", package="Wells_A", period=2, cellid=[1, 2, 3],
                        lower=-5.0, upper=0.0, source="published bounds")],
        objectives=[dict(selection, name="pump_flux", type="wel", id=[1, 2, 3], package="Wells_A", sense="min"),
                    dict(selection, name="spring_flow", type="sfr", id=["HEADWATER"], package="River", sense="max")],
        constraints=[dict(selection, name="exchange", type="sfr", id=["TAILWATER"], package="River",
                          sense="<=", limit=-1.0)])
    text = control.read_text()
    assert "mou_objectives pump_flux,spring_flow" in text
    assert "mou_generator de" in text and "rand_seed 7" in text and "noptmax 3" in text
    assert [row["name"] for row in bound["objectives"]] == ["pump_flux", "spring_flow"]
    assert [row["sense"] for row in bound["objectives"]] == ["min", "max"]
    assert bound["constraints"][0]["limit"] == -1.0 and bound["decisions"][0]["start"] == -3
    instruction = "".join(path.read_text() for path in tmp_path.glob("tradeoff_observations*.csv.ins"))
    assert "!pump_flux!" in instruction and "!spring_flow!" in instruction and "!exchange!" in instruction


@pytest.mark.parametrize(("overrides", "code", "name"), [
    ({"objectives": [{"name": "obj_1", "type": "wel", "id": [1, 2, 3], "units": "m3/day",
                      "period": 1, "step": 1, "source": "source"}]}, "PESTPP_MOU_SENSE", "obj_1"),
    ({"decisions": [{"name": "pump", "package": "Wells_A", "period": 2, "cellid": [1, 2, 3],
                     "upper": 0.0, "source": "source"}]}, "PESTPP_MOU_BOUNDS_UNSTATED", "pump"),
])
def test_authoring_names_the_missing_choice(tmp_path, overrides, code, name):
    _deck(tmp_path)
    selection = dict(period=1, step=1, units="m3/day", source="source")
    values = {
        "model": "aquifer", "generations": 2, "population": 8, "generator": "de", "seed": 1,
        "decisions": [dict(name="pump", package="Wells_A", period=1, cellid=[1, 2, 3],
                           lower=-5.0, upper=0.0, source="source")],
        "objectives": [dict(selection, name="pump_flux", type="wel", id=[1, 2, 3], package="Wells_A", sense="min")],
        "constraints": [],
    }
    with pytest.raises(PestppError, match=name) as refused:
        author_tradeoff(tmp_path, **{**values, **overrides})
    assert refused.value.error_code == code


def _result(candidates=None, outcome="feasible"):
    return TradeoffResult(
        run_id="trade", uri="s3://runs/trade/pestpp.rec", manifest_uri="s3://runs/trade/manifest.json",
        outcome=outcome, candidates=candidates or [], objectives=[{"name": "obj_1", "sense": "min"},
                                                                  {"name": "obj_2", "sense": "min"}],
        constraints=[{"name": "stream", "sense": "<=", "limit": -1.0}],
        bindings={"decisions": [{"name": "pump", "lower": -5.0, "upper": 0.0, "start": -2.0}]})


def test_a_candidate_is_selected_by_its_id_and_no_other():
    result = _result([{"id": "r1", "decisions": {"pump": -2.0}},
                      {"id": "r2", "decisions": {"pump": -3.5}}])
    assert _selected(result, "r2")["decisions"] == {"pump": -3.5}
    with pytest.raises(PestppError) as unknown:
        _selected(result, "r9")
    assert unknown.value.error_code == "PESTPP_MOU_CANDIDATE"
    with pytest.raises(PestppError) as empty:
        _selected(_result([], outcome="empty"), "r1")
    assert empty.value.error_code == "PESTPP_MOU_INFEASIBLE"


def test_a_tradeoff_publishes_charts_and_no_raster_or_mesh(monkeypatch):
    published = []

    async def capture(**kwargs):
        published.extend(kwargs["items"])

    monkeypatch.setattr("trid3nt.render.formats.publish", capture)
    result = _result([{"id": "r1", "decisions": {"pump": -2.0}, "objectives": {"obj_1": 1.0, "obj_2": 4.0},
                       "feasible": True, "front": 1, "rank": 1, "constraints": []},
                      {"id": "r2", "decisions": {"pump": -3.0}, "objectives": {"obj_1": 2.0, "obj_2": 3.0},
                       "feasible": True, "front": 1, "rank": 1, "constraints": []}])
    asyncio.run(_publish_charts(result))
    from trid3nt.render.formats import Chart

    assert published and all(isinstance(item.product, Chart) for item in published)
