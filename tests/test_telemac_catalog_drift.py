"""The committed TELEMAC catalogs are the image's dictionaries, not a copy of them.

The catalog under ``workflows/telemac/catalog/`` is generated data: the engine's
own keyword dictionaries, extracted in-image and committed so the server can read
a keyword surface without a container round trip. That only holds while the two
agree, so this re-extracts from the image and compares. Without the image there
is nothing to compare against and the check skips saying so - it never passes on
absence.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract_telemac_catalog.py"

#: What the six exposed dictionaries hold together.
_TOTAL_KEYWORDS = 1311


def _extractor():
    """The script, imported by path - ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("extract_telemac_catalog", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_exposed_module_has_a_committed_catalog():
    extractor = _extractor()
    committed = {path.stem: json.loads(path.read_text())
                 for path in extractor.catalog_dir().glob("*.json")}
    assert set(committed) == set(extractor.MODULES)
    assert sum(len(c["keywords"]) for c in committed.values()) == _TOTAL_KEYWORDS
    for module, catalog in committed.items():
        assert catalog["module"] == module
        for slot in catalog["keywords"]:
            assert slot["type"] in ("INTEGER", "REAL", "LOGICAL", "STRING")
            assert slot["help"] and slot["keyword"]
            assert len(slot["rubrique"]) == 3
            assert slot["is_file"] == ("file_role" in slot)


def test_the_committed_catalog_is_what_the_image_says_today(tmp_path):
    extractor = _extractor()
    if not extractor.image_present():
        pytest.skip(f"{extractor.IMAGE} is not on this machine: the dictionaries "
                    "the catalog is extracted from are only in that image")
    extractor.extract_catalogs(tmp_path)
    for module in extractor.MODULES:
        committed = (extractor.catalog_dir() / f"{module}.json").read_text()
        assert (tmp_path / f"{module}.json").read_text() == committed, (
            f"{module}.json has drifted from the image's dictionary; re-run "
            "scripts/extract_telemac_catalog.py and read the diff")
