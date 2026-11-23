"""A study pins a finished version and publishes its immutable result."""

import json

import pytest

from trid3nt import case
from trid3nt.errors import DeclarationError
from trid3nt.sdk.study import AppliedStudy


def test_case_study_is_pinned_and_reuses_no_taken_name(tmp_path, monkeypatch):
    c = case(tmp_path / "case")
    base = c.modflow_water_table()
    assert callable(c.study)
    state = c.study(base, name="cal-1", parameters={"NPF_K": [0.1, 10.]}, stages=["calibrate"])
    assert state.name == "cal-1"
    monkeypatch.setattr(state, "_record", lambda: ({"parameters": {"npf_k": {"written": 2.0}}, "prior": {},
                                                   "ensemble": {}, "phi": [], "residuals": []}, {"NPF_K": 2.0}))
    state._keep()
    record = json.loads((c.root / "studies/cal-1/study.json").read_text())
    assert record["base_version"] == base.version
    assert record["values"] == {"NPF_K": 2.0}
    assert "studies/cal-1/study.json" in json.loads((c.root / "trid3nt.lock").read_text())["studies"]["cal-1"]
    with pytest.raises(DeclarationError, match="cal-2") as refusal:
        c.study(base, name="cal-1")
    assert refusal.value.error_code == "STUDY_EXISTS"


def test_apply_is_ordered_and_stale_uses_version_hashes(tmp_path):
    c = case(tmp_path / "case")
    model = c.modflow_water_table()
    base_version = model.version
    model["NPF_K"] = 1.0
    model.strict = True
    current = model.version
    model.apply(AppliedStudy(c.root, "fit", "modflow_water_table", base_version, {"NPF_K": 2.0}))
    assert model["NPF_K"] == 2.0
    assert model._value(*model._held("NPF_K")) == (2.0, "study")
    (warning,) = model._study_warnings()
    assert (warning.code, warning.name, warning.base_version, warning.current_version) == (
        "STUDY_STALE", "fit", base_version, current)
    form = model.fill()
    assert form.refusal == (warning.code, str(warning))
    assert form.deck is None
    model["NPF_K"] = 3.0
    assert model._value(*model._held("NPF_K")) == (3.0, "user")


@pytest.mark.asyncio
async def test_applied_deck_matches_the_recorded_resolve_files(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from trid3nt.inputs.run import FinishedRun
    from trid3nt.sdk.lock import Lock
    from trid3nt.sdk.model import _deck
    from trid3nt.sdk.study import solve_applied, stage_applied
    from trid3nt.sdk.version import hold_finished
    from trid3nt.store.objects import sha256_of
    from trid3nt.workflows.pestpp.authoring import modflow
    from trid3nt.workflows.runtime.fill import Fill, production

    c = case(tmp_path / "case")
    model = c.modflow_water_table()
    fixture = Path(__file__).resolve().parents[1] / "fixtures/pestpp/calibrate"
    manifest = json.loads((fixture / "manifest.json").read_text())
    run_id = "01M849BHJS2NZ7JGQG18KCQFAM"
    source = c.root / "studies" / "fit" / "resolve"
    source.mkdir(parents=True)
    files = {}
    for file in (fixture / "deck").iterdir():
        if file.is_file():
            target = source / file.name
            target.write_bytes(file.read_bytes())
            files[file.name] = sha256_of(target)

    lock = Lock(c.root)
    lock.data["models"]["modflow_water_table"] = {
        "model": model.version,
        "configuration": model._configuration(),
        "template": {"name": model._workflow.name, "path": "called", "version": model._template.sha256},
    }
    with c.bound():
        # Version input data never depends on a live daemon or remote store.
        base = FinishedRun(run_id, "modflow", "gwt", {}, manifest["case"], (), tuple(manifest["outputs"]))
        lock.data["models"]["modflow_water_table"]["finished"] = hold_finished(lock, base)
        lock.save()
        snapshot = (c.root / "versions" / f"{model.version}.lock")
        snapshot.write_bytes(lock.path.read_bytes())
        name = model.version
        applied = c.version(name).model
        assert applied.version == name
        study = SimpleNamespace(case=c.root, name="fit", template="modflow_water_table",
                                base_version=name, values={"NPF_K": 2.0})
        applied.apply(AppliedStudy(study.case, study.name, study.template, study.base_version, study.values, {
            **manifest, "files": files, "result_basename": "gwf.hds",
        }))
        state = Fill(applied._workflow)
        production(state)
        deck = await stage_applied(applied, state, applied._study_deck[0])
        assert _deck(c.root, deck.staged["inputs"])["files"] == files

        async def solve(staged, *_args, **_kwargs):
            return {"run_id": staged["run_id"], "files": _deck(c.root, staged["inputs"])["files"]}

        async def publish(solved):
            return solved["files"]

        monkeypatch.setattr(modflow, "solve_resolved", solve)
        monkeypatch.setattr(modflow, "published", publish)
        assert (await solve_applied(deck)).value == files


def test_split_template_has_no_one_piece_launch():
    from trid3nt.workflows.pestpp.workflow import PestppWorkflow
    from trid3nt.workflows.runtime.workflow import Workflow

    assert PestppWorkflow.launch is Workflow.launch
    assert PestppWorkflow.prepare is not Workflow.prepare
    assert PestppWorkflow.solve is not Workflow.solve


@pytest.mark.asyncio
async def test_a_pinned_check_run_cannot_be_a_finished_base():
    from trid3nt.inputs.run import FinishedRun, FinishedRunError, run

    pinned = FinishedRun("01M849BHJS2NZ7JGQG18KCQFAM", "modflow", "gwf", {"check_steps": 1}, {}, (), ())
    with pytest.raises(FinishedRunError) as refusal:
        await run(pinned, engines=("modflow",))
    assert refusal.value.error_code == "RUN_NOT_FINISHED"


def test_strict_apply_invalidates_a_written_deck_even_for_the_same_values(tmp_path):
    from trid3nt.sdk.model import _Written
    from trid3nt.workflows.runtime.fill import Fill
    from trid3nt.workflows.runtime.workflow import Deck

    c = case(tmp_path / "case", strict=True)
    model = c.modflow_water_table(NPF_K=2.0)
    state = Fill(model._workflow)
    version = model.version
    old = _Written(version, model._row(), state, Deck(state), None)
    object.__setattr__(model, "_written", old)
    assert model._current() is old
    model.apply(AppliedStudy(c.root, "fit", "modflow_water_table", "a" * 64, {"NPF_K": 2.0}))
    assert model.version == version
    assert model.run().code == "STUDY_STALE"
