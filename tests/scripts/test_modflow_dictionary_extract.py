"""The MODFLOW extractor regenerates keyword rows and keeps the authored sections beside them.

Offline: the DFN read is replaced by fixed rows, and the committed directory is a
temporary one holding an authored ``inputs`` section."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "extract_modflow_dictionary.py"


def _extractor():
    """The script, imported by path - ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("extract_modflow_dictionary", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_regeneration_replaces_the_keywords_and_keeps_the_authored_inputs(
        monkeypatch, tmp_path):
    extractor = _extractor()
    authored = {"modflow_water_table": {"drain_conductance": {
        "desc": "stated by the person", "type": "float"}}}
    (tmp_path / "gwf.json").write_text(json.dumps(
        {"module": "gwf", "keywords": [{"identifier": "STALE"}], "inputs": authored}))
    monkeypatch.setattr(extractor, "module_input_dir", lambda: tmp_path)
    monkeypatch.setattr(extractor, "body_rows",
                        lambda body: [{"identifier": "FRESH"}])

    counts = extractor.extract(tmp_path, bodies=("gwf",))

    written = json.loads((tmp_path / "gwf.json").read_text())
    assert counts == {"gwf": 1}
    assert written["keywords"] == [{"identifier": "FRESH"}]
    assert written["inputs"] == authored
    assert list(written) == ["module", "keywords", "inputs"]
