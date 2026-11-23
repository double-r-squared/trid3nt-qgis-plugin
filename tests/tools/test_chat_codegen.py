"""The daemon validates script lines without filling or publishing another deck."""

import pytest

from trid3nt.sdk.model import Form, Staged

_DYE = "telemac_dye_release"
_READY = Form({}, True, deck=Staged("RUN", {}, "reviewed"), checked=True)


def test_chat_line_check_neither_fills_nor_publishes(tmp_path, monkeypatch):
    import trid3nt_harness.codegen as codegen

    def no_check(path):
        pytest.fail("chat attempted a second fill")

    monkeypatch.setattr(codegen, "_check", no_check)
    draft = codegen.check_lines(_DYE, {"spill_duration_s": 300}, _READY, case_dir=tmp_path)
    assert not draft.checked and draft.check_code == "CODEGEN_LINE_CHECKED"
    assert draft.refusal is None and draft.path is None
    assert "m.spill_duration_s = 300" in draft.text
    assert not (tmp_path / "case.py").exists()


def test_chat_line_check_returns_the_models_typed_refusal(tmp_path):
    from trid3nt_harness.codegen import check_lines

    draft = check_lines(_DYE, {"spill_duration_s": -1}, _READY, case_dir=tmp_path)
    assert draft.refusal[0] == "GATE_INPUT_REQUIRED"
    assert not draft.text and not draft.checked and draft.path is None


def test_reviewed_study_script_names_its_base_choices_and_stage_order(tmp_path):
    from trid3nt_harness.codegen import check_study_lines

    version = "b" * 64
    plan = ("screen", "calibrate", "resolve")
    draft = check_study_lines(version, "cal-1", {"stages": list(plan), "keywords": {"noptmax": 3}},
                              _READY, stages=plan, case_dir=tmp_path)
    assert draft.refusal is None
    compile(draft.text, "calibrate.py", "exec")
    assert f"base = c.version({version!r}).model" in draft.text
    assert "stages=['screen', 'calibrate', 'resolve']" in draft.text
    assert "keywords={'NOPTMAX': 3}" in draft.text
    assert [line.strip() for line in draft.text.splitlines() if line.startswith("    s.")] == [
        f"s.{stage}()" for stage in plan]
