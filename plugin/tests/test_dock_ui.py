"""Dock UI regression batch: Qt layout and visibility behaviour.

The pure-python stub tests cannot see any of it, so the checks run in a
SUBPROCESS under the interpreter with ``qgis.PyQt`` and skip honestly when
absent. Covered: a wrapped bubble's full height, whitespace-only deltas leaving
no bubble, the layer fold, the probe panel, gate-card ordering, and markdown."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

import pytest


def _qt_python() -> str | None:
    """First interpreter that can import qgis.PyQt (same probe as
    test_milestone3.TestQtBridgeStart)."""
    candidates = []
    which = shutil.which("python3")
    if which:
        candidates.append(which)
    candidates.append("/usr/bin/python3")
    for py in dict.fromkeys(candidates):
        if not os.path.exists(py):
            continue
        try:
            probe = subprocess.run(
                [py, "-c", "from qgis.PyQt.QtCore import QCoreApplication"],
                capture_output=True,
                timeout=60,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return py
    return None


@pytest.mark.qt_harness_shim
class TestDockUiBatch(unittest.TestCase):
    """One harness subprocess run, shared by the assertions below."""

    _proc: subprocess.CompletedProcess | None = None

    @classmethod
    def setUpClass(cls):
        py = _qt_python()
        if py is None:
            return  # each test skips honestly
        harness = os.path.join(os.path.dirname(__file__), "qt_dock_ui_harness.py")
        cls._proc = subprocess.run(
            [py, "-u", harness],
            capture_output=True,
            timeout=180,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )

    def _stdout(self) -> str:
        if self._proc is None:
            self.skipTest("no interpreter with qgis.PyQt available")
        self.assertEqual(
            self._proc.returncode,
            0,
            "dock ui harness failed (rc="
            f"{self._proc.returncode})\nstdout: {self._proc.stdout}"
            f"\nstderr: {self._proc.stderr}",
        )
        return self._proc.stdout

    def test_dock_ui_fix_batch(self):
        self.assertIn("DOCK-UI-OK", self._stdout())

    def test_markdown_rendering(self):
        """Stream plain -> finalize rich markdown,
        replay rich, tall message unclipped at 320px and 640px widths."""
        out = self._stdout()
        self.assertIn("[markdown] narrow(320px view)", out)
        self.assertIn("[markdown] wide(640px view)", out)
        self.assertIn(
            "[markdown] stream-plain -> finalize-rich, replay rich, "
            "user/thinking plain",
            out,
        )

    def test_persisted_thinking_replay_fold(self):
        """A replayed agent row's persisted thinking folds like the live one.

        Collapsed by default with the answer visible in the same bubble; a row with no
        thinking renders unchanged, with no fold."""
        self.assertIn(
            "[thinking-replay] persisted thinking -> collapsed grey fold in "
            "the answer bubble; plain row unchanged",
            self._stdout(),
        )

    def test_code_exec_approval_card(self):
        """The code-exec-request gate renders an inline approval card.

        A collapsed verbatim code preview, Run and Deny riding the confirm envelope, and
        a lock with a one-line chip - never a silently dropped envelope."""
        self.assertIn("[code-exec] approval card", self._stdout())

    def test_credential_key_entry_card(self):
        """The credential-request renders an inline key-entry card.

        A masked field, Submit sending the two envelopes and Skip declining, the field
        cleared and a provider-named chip; the raw key appears in NO harness output."""
        out = self._stdout()
        self.assertIn("[credential] key-entry card", out)
        # The harness's test key -- must never leak into any log output this
        # test captures from the subprocess.
        self.assertNotIn("harness-firms-key-f00ba4c0ffee", out)
        self.assertNotIn(
            "harness-firms-key-f00ba4c0ffee", self._proc.stderr or ""
        )

    def test_no_tool_turn_mints_no_card(self):
        """F3: a turn with zero tool events must
        leave zero tool cards (the empty stale 'Tools' shell is gone)."""
        self.assertIn("[F3] no-tool turn minted zero tool cards", self._stdout())

    def test_error_notes_wrap_and_fold(self):
        """Error notes wrap like every other chat text and consecutive ones fold.

        A long unbroken URL breaks inside the token rather than widening the dock; a
        single error stays a plain wrapped line; replay folds the same way."""
        self.assertIn("[F7] error notes", self._stdout())

    def test_tool_card_state_border(self):
        """F4: the tool-card border tracks the
        aggregate state -- neutral running, green success, red failure."""
        self.assertIn(
            "[F4] tool-card border: neutral running -> green success -> "
            "red failure",
            self._stdout(),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
