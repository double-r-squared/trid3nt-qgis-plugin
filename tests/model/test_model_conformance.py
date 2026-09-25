"""The system model is checked against the tree, or it rots like every model does.

Two gates per seam, both offline: the checker validates the modeled contracts,
the requirement-to-test allocations and the block dependency rules against live
code, and the view is asserted to be a regeneration of the model. Every
``docs/model/*.sysml`` is a seam and every seam is gated."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "model_check.py"
MODELS = sorted((REPO_ROOT / "docs" / "model").glob("*.sysml"))


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECKER), *args],
                          cwd=str(REPO_ROOT), capture_output=True, text=True,
                          timeout=120)


@pytest.mark.parametrize("model", MODELS, ids=lambda p: p.stem)
def test_the_model_conforms_to_the_tree(model):
    """Every hop's two ends name what it carries, every law has a live verifier.

    The forbid rules are checked here against the import edges the checker computes,
    so a forbidden edge fails the suite rather than a review."""
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

_FORBID_MODEL = """// plane: workflow | system: solver
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
    import of ``pkg.sub`` it would pass a rule that names the leaf."""
    model = _five_forms_tree(tmp_path, _IMPORT_FORMS[form])
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "DEPENDENCY_VIOLATION" in done.stdout
    assert "pkg.importer -> pkg.sub.leaf" in done.stdout


def test_a_seam_that_states_no_plane_refuses(tmp_path):
    """A seam nobody placed cannot be indexed, and its view reads as the whole.

    A model without the plane header is a parse failure rather than a seam quietly
    missing from the index."""
    model = _five_forms_tree(tmp_path, "import pkg.sub.leafy")
    model.write_text(_FORBID_MODEL.partition("\n")[2], encoding="utf-8")
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "MODEL_PARSE" in done.stdout and "plane:" in done.stdout


def test_a_neighbouring_module_is_not_read_as_the_forbidden_one(tmp_path):
    """The full path is matched on a dot boundary, not as a text prefix."""
    model = _five_forms_tree(tmp_path, "from pkg.sub import leafy")
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr


_SWEEP_MODEL = _FORBID_MODEL.replace(
    "forbid: pkg.importer -> pkg.sub.leaf", "forbid: pkg -> pkg.engine")


@pytest.mark.parametrize("seed", ["from ..engine import solve",
                                  "from pkg.engine import solve"])
def test_a_forbid_sweeps_the_modules_the_model_never_binds(seed, tmp_path):
    """A forbidden import planted in an UNBOUND module under the rule's path fires.

    Only ``pkg/importer.py`` is bound; the seed sits two packages down in a module
    no block names, spelled relative and absolute."""
    model = _five_forms_tree(tmp_path, "import json")
    model.write_text(_SWEEP_MODEL, encoding="utf-8")
    shared = tmp_path / "pkg" / "shared"
    shared.mkdir()
    (shared / "__init__.py").write_text("", encoding="utf-8")
    (shared / "nodes.py").write_text(seed + "\n", encoding="utf-8")
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "pkg.shared.nodes -> pkg.engine" in done.stdout


def test_a_relative_import_in_a_package_init_resolves_from_that_package(tmp_path):
    """A package's ``__init__`` IS the package, so ``..`` climbs one level from it."""
    model = _five_forms_tree(tmp_path, "import json")
    model.write_text(_SWEEP_MODEL, encoding="utf-8")
    shared = tmp_path / "pkg" / "shared"
    shared.mkdir()
    (shared / "__init__.py").write_text("from ..engine import solve\n",
                                        encoding="utf-8")
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "pkg.shared -> pkg.engine" in done.stdout


def test_a_forbid_whose_importer_names_no_module_refuses(tmp_path):
    """A rule over a path with nothing under it holds vacuously, so it says so."""
    model = _five_forms_tree(tmp_path, "import json")
    model.write_text(_FORBID_MODEL.replace("forbid: pkg.importer",
                                           "forbid: pkg.gone"), encoding="utf-8")
    done = _run("--model", str(model), "--root", str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FORBID_SWEEPS_NOTHING" in done.stdout and "pkg.gone" in done.stdout


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
