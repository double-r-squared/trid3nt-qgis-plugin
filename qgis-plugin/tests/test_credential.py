"""Credential-request key-entry card tests (LANE K, NATE directive 2026-07-22).

The agent's ``credential-request`` envelope (contracts ``secrets.py``) had
ZERO plugin handling -- the exact gap the code-exec card closed: it fell
through the client's classifier as kind="raw", the dock dropped it, and the
agent's paused keyed tool (AirNow, FIRMS, ...) waited out its server-side TTL
and failed with the original auth error.

Coverage here (offline -- pure parse logic + stub_server round trips; the Qt
card itself is covered at the qt harness level, see ``qt_dock_ui_harness.py``):

* ``gate.parse_credential_request`` field mapping + malformed-envelope
  honesty (no request_id / no provider_id -> None, never a crash).
* the wire round trip against the stub's paused "need-key" turn. Submit is
  TWO envelopes in Decision-F order: ``secret-add`` (the ONLY transport that
  ever carries the raw key -- the server vault-writes it) THEN
  ``credential-provided`` (request_id echo, ``secret_id=None``,
  ``provided=True``; NO key material). Skip is ``credential-provided`` with
  ``provided=False`` alone -- the contract's real negative path (the server
  then re-raises the tool's original typed error; agent narrates honestly).
* key hygiene on the wire: the raw key appears in the ``secret-add``
  envelope and NOWHERE else -- not in ``credential-provided``, not in any
  other frame the stub recorded.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.net import trid3nt_client as tc  # noqa: E402
from trid3nt.ui import gate  # noqa: E402
from stub_server import (  # noqa: E402
    CREDENTIAL_REQUEST_ROW,
    STUB_CREDENTIAL_REQUEST_ID,
    StubAgentServer,
)

# Distinctive so a leak into any other envelope / field is unambiguous.
TEST_KEY = "stub-firms-key-a1b2c3d4e5f6"


class TestCredentialParsing(unittest.TestCase):
    def test_parse_fields(self):
        req = gate.parse_credential_request(CREDENTIAL_REQUEST_ROW)
        self.assertEqual(req.request_id, STUB_CREDENTIAL_REQUEST_ID)
        self.assertEqual(req.provider_id, "firms")
        self.assertEqual(req.provider_label, "NASA FIRMS")
        self.assertEqual(req.secret_key_name, "FIRMS_MAP_KEY")
        self.assertEqual(req.message, CREDENTIAL_REQUEST_ROW["message"])
        self.assertEqual(req.tool_name, "fetch_active_fires")
        self.assertEqual(
            req.signup_url, "https://firms.modaps.eosdis.nasa.gov/api/map_key/"
        )
        self.assertIs(req.raw, CREDENTIAL_REQUEST_ROW)
        # The chip/title label is the server's label verbatim -- the client
        # never hardcodes a provider->label table.
        self.assertEqual(req.display_label, "NASA FIRMS")

    def test_parse_malformed_is_none(self):
        # No request_id -> nothing to correlate the reply against.
        self.assertIsNone(
            gate.parse_credential_request({"provider_id": "firms"})
        )
        # No provider_id -> nothing to scope the secret-add under (a key
        # saved to the wrong scope is one the retry can never re-resolve).
        self.assertIsNone(
            gate.parse_credential_request(
                {"request_id": STUB_CREDENTIAL_REQUEST_ID}
            )
        )
        self.assertIsNone(gate.parse_credential_request("not-a-dict"))
        self.assertIsNone(gate.parse_credential_request(None))

    def test_parse_defensive_optional_fields(self):
        # Everything but the two reply-critical ids degrades honestly.
        req = gate.parse_credential_request(
            {
                "request_id": STUB_CREDENTIAL_REQUEST_ID,
                "provider_id": "generic",
                "provider_label": 42,
                "secret_key_name": None,
                "message": ["not", "a", "string"],
                "tool_name": 7,
                "signup_url": "",
            }
        )
        self.assertEqual(req.provider_label, "")
        self.assertEqual(req.secret_key_name, "")
        self.assertEqual(req.message, "")
        self.assertEqual(req.tool_name, "")
        # Empty-string signup_url normalizes to None (no link rendered; the
        # client NEVER fabricates a URL -- name-only generic card contract).
        self.assertIsNone(req.signup_url)
        # Label falls back to the provider_id, never an empty chip.
        self.assertEqual(req.display_label, "generic")

    def test_note_lines(self):
        req = gate.parse_credential_request(CREDENTIAL_REQUEST_ROW)
        lines = gate.credential_note_lines(req)
        self.assertIn("Key name: FIRMS_MAP_KEY", lines)
        self.assertIn("Waiting tool: fetch_active_fires", lines)
        # A name-only generic card omits absent fields instead of rendering
        # empty "Key name:" stubs.
        bare = gate.parse_credential_request(
            {"request_id": STUB_CREDENTIAL_REQUEST_ID, "provider_id": "generic"}
        )
        self.assertEqual(gate.credential_note_lines(bare), [])


class TestCredentialRoundTrip(unittest.TestCase):
    """The wire round trip against the stub's paused 'need-key' turn --
    mirrors TestCodeExecRoundTrip for the credential seam."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("credential test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_request_classified_not_raw(self):
        # The regression itself: the envelope must surface as its own kind,
        # never the dropped-on-the-floor "raw" fallthrough.
        self.client.send_chat("the fires tool says it will need-key access")
        ev = self._await_kind("credential-request")
        self.assertEqual(ev.data["request_id"], STUB_CREDENTIAL_REQUEST_ID)
        self.assertEqual(ev.data["provider_id"], "firms")
        self.assertEqual(ev.data["secret_key_name"], "FIRMS_MAP_KEY")

    def test_submit_round_trip(self):
        self.client.send_chat("the fires tool says it will need-key access")
        ev = self._await_kind("credential-request")
        req = gate.parse_credential_request(ev.data)
        self.client.submit_credential(req.request_id, req.provider_id, TEST_KEY)
        chunk = self._await_kind("chunk")
        self.assertIn("Key accepted", chunk.data["delta"])
        done = self._await_kind("turn-complete")
        self.assertFalse(done.data.get("cancelled"))

        # secret-add: the EXACT SecretAddEnvelopePayload wire shape (the live
        # server validates extra="forbid" -- any stray key would 400).
        self.assertEqual(len(self.server.secret_adds), 1)
        sa = self.server.secret_adds[0]
        self.assertEqual(
            sa,
            {
                "provider": "firms",
                "case_id": self.client.case_id,
                "key_value": TEST_KEY,
            },
        )
        # credential-provided: the EXACT CredentialProvidedEnvelopePayload
        # wire shape; secret_id None (contract-Optional -- the server's
        # resume path re-resolves the vault record itself) and NO key.
        self.assertEqual(len(self.server.credential_replies), 1)
        self.assertEqual(
            self.server.credential_replies[0],
            {
                "request_id": STUB_CREDENTIAL_REQUEST_ID,
                "secret_id": None,
                "provided": True,
            },
        )
        # Decision-F ORDER on the wire: the vault write (secret-add) must
        # land BEFORE the retry signal, so the server's sequential per-
        # connection consumption has the key saved when the tool retries.
        types = [e.get("type") for e in self.server.received]
        self.assertIn("secret-add", types)
        self.assertIn("credential-provided", types)
        self.assertLess(
            types.index("secret-add"), types.index("credential-provided")
        )
        # Key hygiene: the raw key rides the secret-add envelope and NOWHERE
        # else in anything the client ever sent.
        for env in self.server.received:
            if env.get("type") == "secret-add":
                continue
            self.assertNotIn(TEST_KEY, json.dumps(env))

    def test_skip_round_trip(self):
        self.client.send_chat("the fires tool says it will need-key access")
        ev = self._await_kind("credential-request")
        self.client.decline_credential(ev.data["request_id"])
        chunk = self._await_kind("chunk")
        self.assertIn("No key provided", chunk.data["delta"])
        self._await_kind("turn-complete")
        # Skip is the contract's REAL negative path: credential-provided with
        # provided=False and NO secret-add at all (nothing was saved).
        self.assertEqual(self.server.secret_adds, [])
        self.assertEqual(
            self.server.credential_replies,
            [
                {
                    "request_id": STUB_CREDENTIAL_REQUEST_ID,
                    "secret_id": None,
                    "provided": False,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
