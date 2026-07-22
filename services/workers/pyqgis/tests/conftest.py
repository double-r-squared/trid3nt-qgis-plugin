"""conftest.py for PyQGIS worker tests (job-0062).

Installs qgis.* stub modules into sys.modules before pytest collects any test
module. This is required because ``services/workers/pyqgis/__init__.py``
eagerly imports from ``worker.py``, which has a top-level
``from qgis.core import ...`` — the import fails in pure-Python environments
(CI, the agent venv) that do not have QGIS installed.

The stubs are MagicMock objects, so any attribute access or call on them
returns another MagicMock — sufficient for the unit tests that patch the
relevant symbols with precise fakes via ``importlib.reload``.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

_QGIS_STUBS = {
    "qgis": MagicMock(),
    "qgis.core": MagicMock(),
    "qgis.PyQt": MagicMock(),
    "qgis.PyQt.QtCore": MagicMock(),
}

# Install stubs immediately (at conftest import time = before collection).
for _mod_name, _stub in _QGIS_STUBS.items():
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _stub
