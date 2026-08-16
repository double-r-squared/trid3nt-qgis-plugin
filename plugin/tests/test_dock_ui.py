"""Dock UI regression batch (live-feedback 2026-07-12).

Four live-reported dock bugs, all Qt-layout/visibility behavior that the
pure-python stub tests cannot see -- so, like ``TestQtBridgeStart``, the
checks run in a SUBPROCESS under the system interpreter (the one with
``qgis.PyQt``) and skip honestly when absent:

  BUG 1  long user message clipped to one visual line (wrapped-QLabel
         height-for-width never honored by the aligned HBox cell)
  BUG 2  empty grey assistant bubble when qwen3 emits whitespace-only
         text deltas after </think> on a thinking+tool-only turn
  BUG 3a per-layer materialization notes (21 lines on a case open)
         now fold into one default-collapsed "Layers (N)" toggle
  BUG 3b probe output moved out of chat into the pinned panel
  BUG 4  gate card must sit BETWEEN the pre-gate entry and the
         post-decision response (was: response streamed above the card)

Plus the 2026-07-13 markdown feature: assistant answers stream plain,
convert to rendered markdown (header/bold/list/code-block/table) on
turn-complete and on replay, and a tall markdown message must paint at
its full wrapped height at 320px and 640px dock widths.

The harness (``qt_dock_ui_harness.py``) prints the measured 1-line vs
wrapped bubble heights so the fix is quantified, not vibes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest


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
        """Feature 2026-07-13: stream plain -> finalize rich markdown,
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
        """LANE PLUGIN (2026-07-22): a case-reopen agent row carrying the
        persisted "thinking" field replays as the SAME grey collapsible
        thinking fold the live agent-thinking-chunk path shows -- collapsed
        by default ("Thought process" toggle, body hidden, answer visible in
        the same bubble, click expands) -- while a plain agent row (no
        thinking) renders unchanged with no fold."""
        self.assertIn(
            "[thinking-replay] persisted thinking -> collapsed grey fold in "
            "the answer bubble; plain row unchanged",
            self._stdout(),
        )

    def test_code_exec_approval_card(self):
        """Live-feedback 2026-07-21: the code-exec-request confirm gate
        renders an inline approval card (collapsed verbatim code preview,
        Run=proceed / Deny=cancel over tool-payload-confirmation, lock +
        one-line chip) instead of being silently dropped."""
        self.assertIn("[code-exec] approval card", self._stdout())

    def test_credential_key_entry_card(self):
        """LANE K (NATE 2026-07-22): the credential-request JIT key prompt
        renders an inline key-entry card (masked password field,
        Submit=secret-add+credential-provided through the bridge,
        Skip=decline, field cleared, lock + provider-named chip) instead of
        being silently dropped -- and the raw key literal never appears in
        ANY output the harness subprocess produced (stdout or stderr; the
        harness also asserts captured log records and rendered labels)."""
        out = self._stdout()
        self.assertIn("[credential] key-entry card", out)
        # The harness's test key -- must never leak into any log output this
        # test captures from the subprocess.
        self.assertNotIn("harness-firms-key-f00ba4c0ffee", out)
        self.assertNotIn(
            "harness-firms-key-f00ba4c0ffee", self._proc.stderr or ""
        )

    def test_no_tool_turn_mints_no_card(self):
        """F3 (live-feedback 2026-07-21): a turn with zero tool events must
        leave zero tool cards (the empty stale 'Tools' shell is gone)."""
        self.assertIn("[F3] no-tool turn minted zero tool cards", self._stdout())

    def test_error_notes_wrap_and_fold(self):
        """F7 (live-feedback 2026-07-22): error/note lines wrap like every
        other chat text (a long unbroken presigned URL never forces the dock
        wider -- break-anywhere inside long tokens, sizeHint bounded by the
        chat container) and CONSECUTIVE error notes fold into one collapsed
        inline "ERRORS (N)" toggle row (charts-collapse affordance, red
        accent), expanding in place; a single error (N==1) stays a plain
        wrapped red line; persisted-history replay folds the same way."""
        self.assertIn("[F7] error notes", self._stdout())

    def test_tool_card_state_border(self):
        """F4 (live-feedback 2026-07-21): the tool-card border tracks the
        aggregate state -- neutral running, green success, red failure."""
        self.assertIn(
            "[F4] tool-card border: neutral running -> green success -> "
            "red failure",
            self._stdout(),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
