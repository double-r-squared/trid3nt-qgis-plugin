"""The key-ask card, driven offscreen in the Qt interpreter: masked field, key to the store, reply without it."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from test_provider_config import _qt_python


class TestKeyCard(unittest.TestCase):
    def test_a_typed_key_goes_to_the_store_and_never_into_the_reply(self):
        py = _qt_python()
        if py is None:
            self.skipTest("no interpreter with qgis.PyQt")
        harness = os.path.join(os.path.dirname(__file__), "qt_key_card_harness.py")
        proc = subprocess.run([py, harness], capture_output=True, timeout=120,
                              env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        out = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, msg=f"{out}\n{proc.stderr.decode('utf-8', 'replace')}")
        self.assertIn("KEY_CARD_OK", out)
        self.assertNotIn("typed-test-key-4471", out)


if __name__ == "__main__":
    unittest.main()
