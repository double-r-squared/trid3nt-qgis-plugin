"""Tool-selection picker tests (ADR 0018 auto/ask modes -- Stage 3, 2026-07-22).

The agent's ``tool-candidates`` envelope (contracts ws.ToolCandidatesPayload)
surfaces the retrieval-ranked tool candidates for a step as an inline picker
card; the reply is ONE ``tool-choice`` envelope (request_id echo + verbatim
pick XOR free-text guidance, or both None = let the agent decide). Fail-open
by contract: unanswered, the SERVER times out (``timeout_s``) and proceeds
with its own top pick -- the picker can only ever ADD a one-click error-kill,
never block a turn. The Auto/Ask mode itself rides every user-message as
``tool_choice_mode`` (the show_thinking settings-carrier pattern).

Coverage here (offline -- pure parse logic + stub_server round trips; the Qt
card itself is covered at the qt harness level, ``qt_tool_picker_harness.py``,
run as a subprocess by ``TestToolPickerQt`` below):

* ``gate.parse_tool_candidates`` field mapping + malformed-envelope honesty
  (no request_id -> None; junk candidate rows skipped, never a crash) and
  the ``gate.resolve_tool_choice`` three-shape normalization.
* the wire round trips against the stub's paused "which-tool" turn: pick a
  tool -> the EXACT ToolChoicePayload shape; the free-text path; the
  let-agent-decide path; and the unanswered fail-open twin ("which-tool-
  timeout": the turn proceeds with NO tool-choice ever sent).
* the mode toggle on the wire: ``tool_choice_mode="ask"`` rides the
  user-message payload; the default send OMITS the key (byte-identical to
  the pre-field payload) -- and neither trips the stub's extra=forbid
  contract gate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.net import trid3nt_client as tc  # noqa: E402
from trid3nt.ui import gate  # noqa: E402
from stub_server import (  # noqa: E402
    STUB_TOOL_CANDIDATES_REQUEST_ID,
    TOOL_CANDIDATES_ROW,
    StubAgentServer,
)


class TestToolCandidatesParsing(unittest.TestCase):
    def test_parse_fields(self):
        req = gate.parse_tool_candidates(TOOL_CANDIDATES_ROW)
        self.assertEqual(req.request_id, STUB_TOOL_CANDIDATES_REQUEST_ID)
        self.assertEqual(req.stage_label, "Data step")
        self.assertEqual(req.reason, "ambiguity")
        self.assertEqual(req.timeout_s, 60.0)
        self.assertIs(req.raw, TOOL_CANDIDATES_ROW)
        # Ranked order preserved verbatim (the server ranks; the client
        # never re-sorts).
        self.assertEqual(
            [c.tool_name for c in req.candidates],
            ["spatial_query", "assess_building_damage", "fetch_landcover"],
        )
        self.assertEqual(
            req.candidates[0].summary,
            "Query/summarize features of a loaded layer",
        )
        self.assertEqual(req.candidates[0].score, 0.62)
        # The honest reason note keys off the CLOSED contract enum.
        self.assertIn("nearly tied", req.reason_note)
        ask = gate.parse_tool_candidates(
            dict(TOOL_CANDIDATES_ROW, reason="ask_mode")
        )
        self.assertIn("Ask mode", ask.reason_note)

    def test_parse_malformed_is_none(self):
        # No request_id -> nothing to correlate the reply against.
        self.assertIsNone(gate.parse_tool_candidates({"stage_label": "x"}))
        self.assertIsNone(gate.parse_tool_candidates("not-a-dict"))
        self.assertIsNone(gate.parse_tool_candidates(None))

    def test_parse_defensive_candidates(self):
        """Junk candidate rows are SKIPPED (never a crash); an empty surviving
        list is legal -- the card still offers free-text + let-agent-decide."""
        req = gate.parse_tool_candidates(
            {
                "request_id": STUB_TOOL_CANDIDATES_REQUEST_ID,
                "stage_label": 42,
                "candidates": [
                    "not-a-dict",
                    {"summary": "no name"},
                    {"tool_name": ""},
                    {"tool_name": "ok_tool", "summary": 7, "score": "high"},
                ],
                "reason": None,
                "timeout_s": "soon",
            }
        )
        self.assertEqual(req.stage_label, "")
        self.assertEqual(req.reason, "")
        self.assertEqual(req.timeout_s, 0.0)
        self.assertEqual(req.reason_note, "")
        self.assertEqual(len(req.candidates), 1)
        self.assertEqual(req.candidates[0].tool_name, "ok_tool")
        self.assertEqual(req.candidates[0].summary, "")
        self.assertEqual(req.candidates[0].score, 0.0)
        # Candidates missing entirely -> empty list, same degrade.
        bare = gate.parse_tool_candidates(
            {"request_id": STUB_TOOL_CANDIDATES_REQUEST_ID}
        )
        self.assertEqual(bare.candidates, [])

    def test_resolve_three_shapes(self):
        """The contract's three reply shapes, normalized: pick wins outright
        (both never sent), else stripped guidance, else both-None."""
        self.assertEqual(
            gate.resolve_tool_choice("spatial_query", "stray text"),
            ("spatial_query", None),
        )
        self.assertEqual(
            gate.resolve_tool_choice(None, "  use landcover  "),
            (None, "use landcover"),
        )
        self.assertEqual(gate.resolve_tool_choice(None, "   "), (None, None))
        self.assertEqual(gate.resolve_tool_choice(None, None), (None, None))
        self.assertEqual(gate.resolve_tool_choice("", None), (None, None))

    def test_chip_summaries(self):
        self.assertEqual(
            gate.tool_choice_summary("spatial_query", None),
            "picked spatial_query",
        )
        self.assertEqual(
            gate.tool_choice_summary(None, "guidance"),
            "sent guidance to the agent",
        )
        self.assertEqual(gate.tool_choice_summary(None, None), "agent decided")


class TestToolChoiceRoundTrip(unittest.TestCase):
    """The wire round trips against the stub's paused 'which-tool' turn --
    mirrors TestCredentialRoundTrip for the picker seam."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("tool picker test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_request_classified_not_raw(self):
        # The envelope must surface as its own kind, never the
        # dropped-on-the-floor "raw" fallthrough (the code-exec/credential
        # lesson: a raw fallthrough silently wastes the pick window).
        self.client.send_chat("not sure which-tool fits here")
        ev = self._await_kind("tool-candidates")
        self.assertEqual(ev.data["request_id"], STUB_TOOL_CANDIDATES_REQUEST_ID)
        self.assertEqual(ev.data["stage_label"], "Data step")
        self.assertEqual(
            [c["tool_name"] for c in ev.data["candidates"]],
            ["spatial_query", "assess_building_damage", "fetch_landcover"],
        )

    def test_pick_round_trip(self):
        self.client.send_chat("not sure which-tool fits here")
        ev = self._await_kind("tool-candidates")
        req = gate.parse_tool_candidates(ev.data)
        tool, text = gate.resolve_tool_choice(req.candidates[0].tool_name, None)
        self.client.send_tool_choice(req.request_id, tool_name=tool, free_text=text)
        chunk = self._await_kind("chunk")
        self.assertIn("Running spatial_query", chunk.data["delta"])
        done = self._await_kind("turn-complete")
        self.assertFalse(done.data.get("cancelled"))
        # The EXACT ToolChoicePayload wire shape (the live server validates
        # extra="forbid" -- any stray key would 400; both keys always
        # present, None-valued when unused).
        self.assertEqual(
            self.server.tool_choices,
            [
                {
                    "request_id": STUB_TOOL_CANDIDATES_REQUEST_ID,
                    "tool_name": "spatial_query",
                    "free_text": None,
                }
            ],
        )

    def test_free_text_round_trip(self):
        self.client.send_chat("not sure which-tool fits here")
        ev = self._await_kind("tool-candidates")
        tool, text = gate.resolve_tool_choice(
            None, "  summarize the building layer instead  "
        )
        self.client.send_tool_choice(
            ev.data["request_id"], tool_name=tool, free_text=text
        )
        chunk = self._await_kind("chunk")
        self.assertIn("Taking your guidance", chunk.data["delta"])
        self._await_kind("turn-complete")
        self.assertEqual(
            self.server.tool_choices,
            [
                {
                    "request_id": STUB_TOOL_CANDIDATES_REQUEST_ID,
                    "tool_name": None,
                    "free_text": "summarize the building layer instead",
                }
            ],
        )

    def test_let_agent_decide_round_trip(self):
        self.client.send_chat("not sure which-tool fits here")
        ev = self._await_kind("tool-candidates")
        self.client.send_tool_choice(ev.data["request_id"])
        chunk = self._await_kind("chunk")
        self.assertIn("Agent decided", chunk.data["delta"])
        self._await_kind("turn-complete")
        self.assertEqual(
            self.server.tool_choices,
            [
                {
                    "request_id": STUB_TOOL_CANDIDATES_REQUEST_ID,
                    "tool_name": None,
                    "free_text": None,
                }
            ],
        )

    def test_unanswered_fail_open(self):
        """The fail-open twin: the server emits the picker and the turn moves
        on with NO tool-choice ever sent -- the client just watches the turn
        proceed (the dock folds the card to 'agent proceeded'; Qt-level
        coverage in the harness)."""
        self.client.send_chat("which-tool-timeout please")
        self._await_kind("tool-candidates")
        chunk = self._await_kind("chunk")
        self.assertIn("proceeding with spatial_query", chunk.data["delta"])
        self._await_kind("turn-complete")
        self.assertEqual(self.server.tool_choices, [])

    def test_mode_toggle_on_the_wire(self):
        # Default send: the key is OMITTED (byte-identical to the pre-field
        # payload -- the aoi_bbox/show_thinking omit convention). "auto"
        # explicitly also omits: the server default IS auto.
        self.client.send_chat("hello there")
        self._await_kind("turn-complete")
        self.client.send_chat("hello again", tool_choice_mode="auto")
        self._await_kind("turn-complete")
        self.assertEqual(self.server.user_message_tool_choice_modes, [])
        # Ask mode rides the payload -- and passes the stub's extra=forbid
        # user-message contract gate (a misspelled key would be recorded as
        # a protocol violation and the turn would error instead).
        self.client.send_chat("hello in ask mode", tool_choice_mode="ask")
        self._await_kind("turn-complete")
        self.assertEqual(self.server.user_message_tool_choice_modes, ["ask"])
        self.assertEqual(self.server.protocol_violations, [])


def _qt_python() -> "str | None":
    """First interpreter that can import qgis.PyQt (same probe as
    test_dock_ui / test_milestone3.TestQtBridgeStart)."""
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


class TestToolPickerQt(unittest.TestCase):
    """One harness subprocess run covering the Qt card behavior: render,
    single-answer lock + chip folds, free-text radio selection, empty-Confirm
    honesty, the unanswered 'agent proceeded' fold, the malformed-envelope
    note, and the Settings Auto/Ask toggle -> send-path stamping."""

    _proc: "subprocess.CompletedProcess | None" = None

    @classmethod
    def setUpClass(cls):
        py = _qt_python()
        if py is None:
            raise unittest.SkipTest("no interpreter with qgis.PyQt available")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_tool_picker_harness.py"
        )
        cls._proc = subprocess.run(
            [py, "-u", harness],
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )

    def test_harness_green(self):
        proc = self._proc
        self.assertIsNotNone(proc)
        self.assertEqual(
            proc.returncode,
            0,
            "tool-picker harness failed (rc="
            f"{proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn("TOOL-PICKER-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
