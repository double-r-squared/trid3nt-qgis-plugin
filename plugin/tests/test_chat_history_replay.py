"""Chat-history replay extraction: session_state rows -> the dock's replay.

No QGIS required except where a case names the bridge; the WS stub needs
``websockets``.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402

# ITEM B: chat-history replay extraction --
# ``session_state.chat_history`` (contracts ``case.py`` CaseChatMessage) ->
# plain role/content rows for the dock's case-open chat replay.


class TestParseChatHistory(unittest.TestCase):
    def test_present_user_and_agent_rows_survive(self):
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    {"role": "user", "content": "how deep does it flood?"},
                    {"role": "agent", "content": "up to 1.2 m near the river"},
                ]
            }
        )
        # LANE PLUGIN: an agent row with no persisted thinking
        # surfaces an honest thinking=None (plain rows replay unchanged).
        self.assertEqual(
            rows,
            [
                {"role": "user", "content": "how deep does it flood?"},
                {
                    "role": "agent",
                    "content": "up to 1.2 m near the river",
                    "thinking": None,
                },
            ],
        )

    def test_agent_row_thinking_surfaces(self):
        # LANE PLUGIN: the persisted "thinking" field (Lane
        # CORE row-model addition) rides through on agent rows so the dock
        # replays the collapsed thinking fold on case reopen.
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    {
                        "role": "agent",
                        "content": "the answer",
                        "thinking": "reasoning about the depth raster",
                    },
                ]
            }
        )
        self.assertEqual(
            rows,
            [
                {
                    "role": "agent",
                    "content": "the answer",
                    "thinking": "reasoning about the depth raster",
                }
            ],
        )

    def test_agent_row_thinking_defensive_defaults_to_none(self):
        # Absent, non-string, and blank thinking values all default to an
        # honest None -- never raised on, never a fabricated fold. A user
        # row never carries the key (thinking is an agent-row field).
        for bad in ({}, {"thinking": None}, {"thinking": 42},
                    {"thinking": ["not", "a", "string"]}, {"thinking": "   "}):
            rows = tc.parse_chat_history(
                {"chat_history": [{"role": "agent", "content": "hi", **bad}]}
            )
            self.assertEqual(
                rows,
                [{"role": "agent", "content": "hi", "thinking": None}],
                f"thinking variant {bad!r} did not default to None",
            )
        rows = tc.parse_chat_history(
            {"chat_history": [{"role": "user", "content": "hi",
                               "thinking": "never on user rows"}]}
        )
        self.assertEqual(rows, [{"role": "user", "content": "hi"}])

    def test_absent_chat_history_yields_empty_list(self):
        self.assertEqual(tc.parse_chat_history({}), [])
        self.assertEqual(tc.parse_chat_history({"chat_history": None}), [])
        self.assertEqual(tc.parse_chat_history({"chat_history": "not-a-list"}), [])

    def test_malformed_rows_are_skipped_not_raised(self):
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    "not-a-dict",
                    {"role": "user"},  # no content
                    {"content": "no role"},
                    {"role": "user", "content": 42},  # non-string content
                    {"role": "user", "content": ""},  # empty content
                    {"role": "system", "content": "tool bookkeeping"},
                    {"role": "tool"},  # tool row with NO tool_card and NO content
                    {"role": "tool", "content": "{...}"},
                    {"role": "bogus", "content": "hi"},
                    {"role": "user", "content": "the one good row"},
                ]
            }
        )
        # Item H: a tool row with a usable content
        # twin SURFACES (tool_card rides along, None here); a tool row with
        # NEITHER a tool_card dict NOR content is skipped like every other
        # malformed row -- never raised on.
        self.assertEqual(
            rows,
            [
                {"role": "tool", "tool_card": None, "content": "{...}"},
                {"role": "user", "content": "the one good row"},
            ],
        )

    def test_capped_at_replay_max_keeping_the_tail(self):
        many = [
            {"role": "user" if i % 2 == 0 else "agent", "content": f"msg {i}"}
            for i in range(tc.CHAT_HISTORY_REPLAY_MAX + 10)
        ]
        rows = tc.parse_chat_history({"chat_history": many})
        self.assertEqual(len(rows), tc.CHAT_HISTORY_REPLAY_MAX)
        # the TAIL survives (most recent conversation), not the head
        self.assertEqual(rows[0]["content"], "msg 10")
        self.assertEqual(rows[-1]["content"], f"msg {tc.CHAT_HISTORY_REPLAY_MAX + 9}")

    def test_parse_case_open_surfaces_chat_messages(self):
        info = tc.parse_case_open(
            {
                "session_state": {
                    "case": {"case_id": "01OK", "title": "Asheville flood"},
                    "loaded_layers": [],
                    "chat_history": [
                        {"role": "user", "content": "start a flood sim"},
                        {
                            "role": "tool",
                            "tool_card": {"name": "run_flood_sim", "state": "ok"},
                            "content": "{tool_card}",
                        },
                        {"role": "agent", "content": "here is the result"},
                    ],
                }
            }
        )
        self.assertIsNotNone(info)
        # Item H: tool rows are SURFACED with their
        # typed tool_card dict (tool-call chain replay on reopen), in order,
        # inline between the user and agent bubbles.
        self.assertEqual(
            info.chat_messages,
            [
                {"role": "user", "content": "start a flood sim"},
                {
                    "role": "tool",
                    "tool_card": {"name": "run_flood_sim", "state": "ok"},
                    "content": "{tool_card}",
                },
                {"role": "agent", "content": "here is the result",
                 "thinking": None},
            ],
        )

    def test_parse_case_open_without_chat_history_is_empty(self):
        info = tc.parse_case_open(
            {"session_state": {"case": {"case_id": "01OK"}, "loaded_layers": []}}
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.chat_messages, [])
