"""A calibration by pestpp-glm: the study's method, glm's own outputs read, and the four charts off them.

tests/fixtures/pestpp/glm was recorded from the pestpp-glm study of the Des Moines calibration canary (a fetched
MODFLOW water table, NPF_K and SFR_RHK against 15 published steady head targets, 3 iterations), and
glm_freyberg/pestpp.jcb from a pestpp-glm run over the Freyberg interface (tests/fixtures/pestpp/README.md). The
Jacobian values pinned here are pyEMU 1.7.0's Matrix.from_binary of the same files, read in the worker image.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from trid3nt_server.workflows.pestpp import outputs, staging
from trid3nt_server.workflows.pestpp.charts import fit_chart, parameter_chart, phi_chart, residual_chart
from trid3nt_server.workflows.pestpp.errors import PestppError

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp"
_GLM = _FIXTURES / "glm"


def _rows(payload):
    return payload["spec"]["data"]["values"] if "spec" in payload else payload["vega_lite_spec"]["data"]["values"]


def test_glm_phi_is_its_one_parameter_set_s_per_iteration():
    phi = outputs.read_phi(_GLM, tool="glm")
    assert [row["iteration"] for row in phi] == [0, 1, 2, 3]
    assert [row["total_runs"] for row in phi] == [0, 18, 38, 60]
    assert [row["phi"] for row in phi] == [1776.37, 1743.75, 1704.43, 1658.76]
    assert all(row["measurement"] == row["phi"] and row["regularization"] == 0.0 for row in phi)
    chart = phi_chart(phi, caption="objective function per iteration")
    assert {row["series"] for row in _rows(chart)} == {"parameter set"}
    assert "phi falls from 1776 at iteration 0 to 1659 at iteration 3" in chart["caption"]


def test_glm_s_best_set_is_its_parameter_file_and_its_last_iteration():
    best = outputs.read_parameters(_GLM)
    assert best == {"npf_k": pytest.approx(1.369606665789478), "sfr_rhk": pytest.approx(0.9771589255390473)}
    assert outputs.read_parameters(_GLM, iteration=3) == best
    iterations = outputs.read_parameter_iterations(_GLM)
    assert iterations[0] == {"npf_k": 1.0, "sfr_rhk": 1.0}
    assert iterations[3] == {"npf_k": pytest.approx(1.36961), "sfr_rhk": pytest.approx(0.977159)}


def test_glm_s_residuals_are_its_best_set_s_and_sum_to_its_last_phi():
    residuals = outputs.read_residuals(_GLM, tool="glm")
    assert [row["name"] for row in residuals] == [f"head_{k}" for k in range(1, 16)]
    assert all(row["residual"] == pytest.approx(row["measured"] - row["modelled"]) for row in residuals)
    assert sum(row["weight"] * row["residual"] ** 2 for row in residuals) == pytest.approx(1658.76, rel=1e-5)
    assert outputs.read_residuals(_GLM, iteration=3, tool="glm") == residuals
    (worst,) = [row for row in _rows(residual_chart(residuals, caption="r")) if row["observation"] == "head_11"]
    assert worst["simulated_minus_observed"] == pytest.approx(-24.591062673)
    assert "mean absolute residual 7.65" in fit_chart(residuals, caption="fit")["caption"]


def test_the_jacobian_reads_pest_s_own_layout_and_pestpp_s_long_names():
    jacobian = outputs.read_jacobian(_GLM)
    assert list(jacobian) == [f"head_{k}" for k in range(1, 16)]
    assert jacobian["head_1"] == {"npf_k": pytest.approx(-0.878649218233158),
                                  "sfr_rhk": pytest.approx(-0.018808655898021963)}
    assert jacobian["head_13"]["sfr_rhk"] == pytest.approx(-1.0070919335692075)
    freyberg = outputs.read_jacobian(_FIXTURES / "glm_freyberg")
    assert len(freyberg) == 725
    first = freyberg["oname:hds_otype:lst_usecol:trgw-0-13-10_time:1"]
    assert list(first.values()) == pytest.approx([-0.385028022, -0.0923595181, 0.0, 0.0, 1.71938378])


def test_the_parameter_chart_spans_glm_s_iterations_around_its_best_set():
    best = outputs.read_parameters(_GLM)
    spread = {**{f"iteration {i}": v for i, v in outputs.read_parameter_iterations(_GLM).items()}, "base": best}
    chart = parameter_chart(spread, [SimpleNamespace(name="npf_k", lower=0.1, upper=10.0)], caption="multipliers")
    (row,) = _rows(chart)
    assert (row["lowest"], row["highest"]) == (1.0, pytest.approx(1.369606665789478, rel=1e-5))
    assert row["base"] == pytest.approx(best["npf_k"])


def test_a_glm_study_is_staged_held_to_what_glm_writes(tmp_path, monkeypatch):
    study = tmp_path / "study"
    study.mkdir()
    (study / "pestpp.pst").write_text("pcf\n* model input/output\n")
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(staging, "setting", lambda name: {"CONTAINER_CORES": 3}[name])
    seen = {}

    async def fake_stage_run(rundir, run_id, **kw):
        seen.update(kw)
        return {"run_id": run_id}

    monkeypatch.setattr(staging, "stage_run", fake_stage_run)
    asyncio.run(staging.stage_study(study, tool="glm", control="pestpp.pst", name="glm"))
    assert seen["module"] == "glm"
    assert seen["results"] == ["pestpp.rec", "pestpp.iobj", "pestpp.par", "pestpp.rei", "pestpp.jcb"]


def test_the_study_states_its_method_ies_unstated_and_refuses_another():
    from trid3nt_server.workflows.pestpp.workflow import METHODS, _calibration, _method

    assert (_method(None), _method("GLM"), set(METHODS)) == ("ies", "glm", {"ies", "glm"})
    with pytest.raises(PestppError) as refused:
        _method("sen")
    assert refused.value.error_code == "PESTPP_METHOD_UNKNOWN"
    read = _calibration("glm", _GLM)
    assert read["best"] == outputs.read_parameters(_GLM) and read["spread"]["base"] == read["best"]
    assert set(read["kept"]) == {"iterations"} and read["phi"][-1]["phi"] == 1658.76
    assert set(_calibration("ies", _FIXTURES / "calibrate" / "study")["kept"]) == {"ensemble"}


def test_the_template_states_the_method_and_fill_refuses_one_pestpp_does_not_calibrate_by():
    import trid3nt_server.main as main

    main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["calibrate"].fn.workflow
    (method,) = [row for row in workflow.inputs if row.name == "method"]
    assert method.default == "ies"
    stated = SimpleNamespace(keywords={"noptmax": 2}, carried={}, inputs={"method": SimpleNamespace(value="opt")})
    assert workflow.judged(stated)["method"].code == "PESTPP_METHOD_UNKNOWN"
    glm = SimpleNamespace(keywords={}, carried={}, inputs={"method": SimpleNamespace(value="glm")})
    refused = workflow.judged(glm)
    assert "method" not in refused and refused["noptmax"].code == "PESTPP_ITERATIONS_UNSTATED"
    assert "starting parameters are evaluated" in refused["noptmax"].reason


def test_every_glm_read_refuses_a_study_that_wrote_nothing(tmp_path):
    for read in (outputs.read_parameters, outputs.read_parameter_iterations, outputs.read_jacobian):
        with pytest.raises(PestppError) as err:
            read(tmp_path)
        assert err.value.error_code == "PESTPP_OUTPUT_MISSING"
    for read in (outputs.read_phi, outputs.read_residuals):
        with pytest.raises(PestppError) as err:
            read(tmp_path, tool="glm")
        assert err.value.error_code == "PESTPP_OUTPUT_MISSING"
