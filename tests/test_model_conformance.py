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
