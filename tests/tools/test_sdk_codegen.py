"""A generated case records the harness choices and checks its own deck."""

import ast
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from trid3nt import case
from trid3nt.sdk.model import Form
from trid3nt.workflows.runtime.fill import SourceRequest

_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _ROOT / "tests/sdk/test_fill_deck.py"
_SUPPORT = runpy.run_path(str(_HELPER))
_DYE = "telemac_dye_release"


def test_generated_dye_case_runs_without_harness_and_keeps_its_deck(monkeypatch, tmp_path):
    from trid3nt_harness.codegen import generate

    _SUPPORT["_stubbed"](monkeypatch)
    bootstrap = tmp_path / "support"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import runpy, sys, pytest\n"
        "sys.modules['trid3nt_harness'] = None\n"
        "sys.modules['trid3nt_server'] = None\n"
        f"runpy.run_path({str(_HELPER)!r})['_stubbed'](pytest.MonkeyPatch())\n"
    )
    env_path = os.pathsep.join([str(bootstrap), str(_ROOT)])
    monkeypatch.setenv("PYTHONPATH", env_path)
    root = tmp_path / "case"
    c = case(root, strict=True)
    values = _SUPPORT["_ZERO_FETCH"]
    m = c.telemac_dye_release(**values)
    m.steps = {"bed": [("accept", {"code": "BED_WET_HOLE"})]}
    form = m.fill()
    script = generate(_DYE, values, form, case_dir=root)
    assert script.checked and script.refusal is None
    assert script.path == root / "case.py" and script.path.read_text() == script.text
    tree = ast.parse(script.text)
    assert [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))] == ["import trid3nt"]
    assert "# %%" in script.text and "strict=True" in script.text and 'if __name__ == "__main__":' in script.text
    assert "accept" in script.text and "merge_surfaces" not in script.text
    ran = subprocess.run([sys.executable, script.path.name], cwd=root, capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert case(root).lock.data["models"][_DYE]["deck"]["sha256"] == form.deck.sha256


@pytest.mark.parametrize(("values", "code"), [
    ({"spill_duration_s": -1}, "GATE_INPUT_REQUIRED"),
    ({"bed": {"source": "fetch_unregistered"}}, "DATA_NEED_UNMATCHED"),
    ({"bed": {"source": "fetch_dem", "bbox": [0]}}, "DEM_INPUT_ERROR"),
])
def test_invalid_lines_refuse_without_a_script(tmp_path, values, code):
    from trid3nt_harness.codegen import generate

    result = generate(_DYE, values, Form({}, ready=True), case_dir=tmp_path)
    assert result.refusal[0] == code and not result.checked
    assert result.path is None and not (tmp_path / "case.py").exists()


def test_fetched_input_renders_its_named_request_and_rank(monkeypatch, tmp_path):
    import trid3nt_harness.codegen as codegen
    from trid3nt.sdk.model import Staged

    monkeypatch.setattr(codegen, "_check", lambda path: ("from-harness", None))
    options = {"bbox": [-93.6729, 41.6625, -93.6369, 41.6895]}
    form = Form({}, ready=True, deck=Staged("RUN", {}, "from-harness"), checked=True,
                choices={"bed": (SourceRequest("fetch_dem", 2, 3, options),)})
    script = codegen.generate("modflow_water_table", {}, form, case_dir=tmp_path)
    assert script.checked
    assert f"m.bed = {{'source': 'fetch_dem', 'bbox': {options['bbox']!r}}}  # ranked 2 of 3" in script.text
