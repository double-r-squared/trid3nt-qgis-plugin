"""The system model is checked against the tree, or it rots like every model does.

Two gates per seam, both offline. The checker validates the modeled seam
contracts, the requirement-to-test allocations and the block dependency rules
against the live code; the view is asserted to be a regeneration of the model
rather than a drawing somebody kept up to date by hand.

Every ``docs/model/*.sysml`` is a seam and every seam is gated, so a model added
for a new seam is checked by being written rather than by editing this file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "model_check.py"
MODELS = sorted((REPO_ROOT / "docs" / "model").glob("*.sysml"))


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECKER), *args],
                          cwd=str(REPO_ROOT), capture_output=True, text=True,
                          timeout=120)


@pytest.mark.parametrize("model", MODELS, ids=lambda p: p.stem)
def test_the_model_conforms_to_the_tree(model):
    """Every hop's two ends name what it carries, every law has a live verifier.

    This test IS the verification the dependency requirements name: the forbid
    rules are checked here against import edges the checker computes for the
    modeled modules, so a worker that reached into the server package fails the
    suite rather than a review.
    """
    done = _run("--model", str(model))
    assert done.returncode == 0, done.stdout + done.stderr


#: The five ways python names one module, and the source line for each. A
#: dependency rule that any of them slips past is a rule with a spelling.
_IMPORT_FORMS = {
    "plain": "import pkg.sub.leaf",
    "aliased": "import pkg.sub.leaf as leaf",
    "submodule_from_package": "from pkg.sub import leaf",
    "name_from_module": "from pkg.sub.leaf import thing",
    "relative": "from .sub import leaf",
}

_FORBID_MODEL = """
package FiveForms {
    part def Importer {
        doc /* the one module the rule is written about */
    }

    part importer : Importer {
        doc /*
        code: pkg/importer.py
        */
    }

    requirement def NeverTheLeaf {
        doc /*
        forbid: pkg.importer -> pkg.sub.leaf
        */
    }

    satisfy requirement NeverTheLeaf by importer;
    verify requirement NeverTheLeaf by
        "tests/test_five_forms.py::test_the_leaf_is_never_imported";
}
"""


def _five_forms_tree(tmp_path: Path, source: str) -> Path:
    """A one-block tree the rule is checked against -> its model path."""
    (tmp_path / "docs" / "model").mkdir(parents=True)
    model = tmp_path / "docs" / "model" / "five-forms.sysml"
    model.write_text(_FORBID_MODEL, encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "importer.py").write_text(source + "\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_five_forms.py").write_text(
        "def test_the_leaf_is_never_imported():\n    pass\n", encoding="utf-8")
    return model


@pytest.mark.parametrize("form", sorted(_IMPORT_FORMS), ids=lambda f: f)
def test_every_import_form_fires_against_the_rule_that_forbids_it(form, tmp_path):
    """``forbid:`` is unevadable by spelling.

    ``from pkg.sub import leaf`` is an import OF ``pkg.sub.leaf``; recorded as an
    import of ``pkg.sub`` it passes a rule that names the leaf, and the rule reads
    as enforced while the edge it forbids is in the tree.
    """
    model = _five_forms_tree(tmp_path, _IMPORT_FORMS[form])
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "DEPENDENCY_VIOLATION" in done.stdout
    assert "pkg.importer -> pkg.sub.leaf" in done.stdout


def test_a_neighbouring_module_is_not_read_as_the_forbidden_one(tmp_path):
    """The full path is matched on a dot boundary, not as a text prefix."""
    model = _five_forms_tree(tmp_path, "from pkg.sub import leafy")
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("model", MODELS, ids=lambda p: p.stem)
def test_the_view_is_derived_rather_than_drawn(model, tmp_path):
    view = model.with_name(f"{model.stem}-view.md")
    regenerated = tmp_path / "view.md"
    done = _run("--model", str(model), "--view", str(regenerated))
    assert done.returncode == 0, done.stdout + done.stderr
    if regenerated.read_text(encoding="utf-8") != view.read_text(encoding="utf-8"):
        pytest.fail(
            f"{view.relative_to(REPO_ROOT)} is stale against "
            f"{model.relative_to(REPO_ROOT)}; regenerate it with "
            f"'python scripts/model_check.py --model {model.relative_to(REPO_ROOT)} "
            "--view'")
