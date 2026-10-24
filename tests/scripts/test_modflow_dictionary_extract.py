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


_PRP = """\
block options
name release_times
type keyword
optional true
removed 6.6.0

block options
name dev_forceternary
type keyword
optional false
longname force ternary tracking method

block options
name exit_solve_tolerance
type double precision
optional true
default_value 1e-5

block dimensions
name nreleasepts
type integer
optional false

block dimensions
name nreleasetimes
type integer
optional false

block period
name perioddata
type recarray releasesetting
shape

block period
name releasesetting
type keystring all first
in_record true
"""

_TDIS = """\
block dimensions
name nper
type integer
optional false
"""


def test_the_prt_body_has_the_clock_alone_and_drops_what_mf6_removed_or_runs_without(monkeypatch):
    extractor = _extractor()
    monkeypatch.setattr(extractor, "_dfn_names", lambda: ("prt-prp", "gwf-npf"))
    monkeypatch.setattr(extractor, "fetch_dfn", lambda stem: {"prt-prp": _PRP, "sim-tdis": _TDIS}[stem])

    rows = {row["identifier"]: row for row in extractor.body_rows("prt")}

    assert set(rows) == {"PRP_DEV_FORCETERNARY", "PRP_EXIT_SOLVE_TOLERANCE", "PRP_NRELEASEPTS", "PRP_NRELEASETIMES",
                         "PRP_PERIODDATA", "TDIS_NPER"}
    assert {name for name, row in rows.items() if row["mandatory"]} == {"PRP_NRELEASEPTS", "TDIS_NPER"}
    assert extractor.BODIES[-1] == "prt"
