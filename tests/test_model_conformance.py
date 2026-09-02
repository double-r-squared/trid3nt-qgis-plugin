"""The system model is checked against the tree, or it rots like every model does.

Two gates, both offline. The checker validates the modeled seam contracts, the
requirement-to-test allocations and the block dependency rules against the live
code; the view is asserted to be a regeneration of the model rather than a
drawing somebody kept up to date by hand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "model_check.py"
MODEL = REPO_ROOT / "docs" / "model" / "solve-seam.sysml"
VIEW = REPO_ROOT / "docs" / "model" / "solve-seam-view.md"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECKER), *args],
                          cwd=str(REPO_ROOT), capture_output=True, text=True,
                          timeout=120)


def test_the_solve_seam_model_conforms():
    """Every modeled item is written and read, every law has a live verifier.

    This test IS the verification the two dependency requirements name: the
    forbid rules are checked here against the measured import graph, so a
    worker that reached into the server package fails the suite rather than a
    review.
    """
    done = _run()
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_view_is_derived_rather_than_drawn(tmp_path):
    regenerated = tmp_path / "view.md"
    done = _run("--view", str(regenerated))
    assert done.returncode == 0, done.stdout + done.stderr
    if regenerated.read_text(encoding="utf-8") != VIEW.read_text(encoding="utf-8"):
        pytest.fail(
            f"{VIEW.relative_to(REPO_ROOT)} is stale against "
            f"{MODEL.relative_to(REPO_ROOT)}; regenerate it with "
            "'python scripts/model_check.py --view'")
