"""Qt harness: the key-ask card draws a masked field, stores the typed key and replies without it.

Runs offscreen under the ``qgis.PyQt`` interpreter. The key is NEVER printed."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.PyQt.QtWidgets import QApplication, QLineEdit  # noqa: E402

from plugin.ui import gate  # noqa: E402
from plugin.ui.cards import FormCard  # noqa: E402

_TYPED = "typed-test-key-4471"


def main() -> int:
    app = QApplication(sys.argv)
    payload = {
        "warning_id": "01J000000000000000000000AA", "tool_name": "fetch_airnow_air_quality",
        "options": ["proceed", "narrow_scope", "cancel"], "recommendation": "key",
        "param_sheet": {"workflow": "fetch_airnow_air_quality", "title": "EPA AirNow key is needed",
                        "rows": [{"name": "airnow", "value": None, "secret": "required",
                                  "source_badge": "EPA AirNow"}]},
    }
    warning, sheet = gate.parse_payload_warning(payload), gate.parse_param_sheet(payload)
    decided: list = []
    stored: list = []
    card = FormCard(warning, sheet, lambda *a: decided.append(a), lambda n, v: stored.append((n, v)))
    editor = card._editors["airnow"]
    if not isinstance(editor, QLineEdit) or editor.echoMode() != QLineEdit.EchoMode.Password:
        print("KEY_CARD_FAIL not masked")
        return 1
    editor.setText(_TYPED)
    card._submit()
    app.processEvents()
    if stored != [("airnow", _TYPED)]:
        print("KEY_CARD_FAIL store", len(stored))
        return 1
    if decided != [("01J000000000000000000000AA", "proceed", None)] or _TYPED in repr(decided):
        print("KEY_CARD_FAIL reply", [d[1:] for d in decided])
        return 1
    if editor.text() != "":
        print("KEY_CARD_FAIL field kept the key")
        return 1
    print("KEY_CARD_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
