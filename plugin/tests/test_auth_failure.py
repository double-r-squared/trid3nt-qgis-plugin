"""Token-expiry classification over a handshake failure.

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
from stub_server import (  # noqa: E402
    EXPIRED_TOKEN,
    StubAgentServer,
)

# --------------------------------------------------------------------------- #
# Token-expiry classification
# --------------------------------------------------------------------------- #


class TestAuthFailureClassification(unittest.TestCase):
    def test_auth_failures_classify_true(self):
        f = tc.is_auth_failure
        # the broker's pre-upgrade rejection (?st= token dead)
        self.assertTrue(f("HandshakeFailed: upgrade rejected: HTTP/1.1 401 Unauthorized"))
        self.assertTrue(f("HandshakeFailed: upgrade rejected: HTTP/1.1 403 Forbidden"))
        # the in-band agent rejection (error envelope folded into the text)
        self.assertTrue(f("ConnectionClosed: connection closed (code=1008 reason='auth required') [AUTH_REQUIRED token expired or invalid]"))
        self.assertTrue(f("something something TOKEN EXPIRED"))

    def test_transport_failures_classify_false(self):
        f = tc.is_auth_failure
        self.assertFalse(f(""))
        self.assertFalse(f("ConnectionClosed: connection closed (code=1011 reason='stub drop')"))
        self.assertFalse(f("OSError: [Errno 111] Connection refused"))
        self.assertFalse(f("ConnectionClosed: read timeout"))
        self.assertFalse(f("HandshakeFailed: upgrade rejected: HTTP/1.1 502 Bad Gateway"))
        # 401/403 appearing OUTSIDE an upgrade rejection does not classify
        self.assertFalse(f("fetched 403 rows from the catalog"))

    def test_expired_token_connect_classifies_as_auth(self):
        """Full stub round trip: dead token -> error envelope + 1008 close ->
        the combined failure text classifies as auth (ladder must stop)."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url, token=EXPIRED_TOKEN)
        self.addCleanup(client.close)
        with self.assertRaises((tc.ConnectionClosed, tc.HandshakeFailed)) as ctx:
            client.connect()
        # the drained error envelope was stashed for classification
        self.assertIsNotNone(client.last_handshake_error)
        self.assertEqual(
            client.last_handshake_error.get("error_code"), "AUTH_REQUIRED"
        )
        combined = (
            f"{type(ctx.exception).__name__}: {ctx.exception} "
            f"[{client.last_handshake_error.get('error_code')} "
            f"{client.last_handshake_error.get('message')}]"
        )
        self.assertTrue(tc.is_auth_failure(combined))

    def test_good_token_still_connects(self):
        """The rejection path must not break the normal token handshake."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url, token="live-token")
        self.addCleanup(client.close)
        client.connect()
        self.assertTrue(client.connected)
        self.assertIsNone(client.last_handshake_error)
