"""conftest.py for PyQGIS worker package tests (job-0062).

Installs qgis.* stub modules into sys.modules before pytest collects any test
module. Placing this conftest at the package root (not inside tests/) ensures
it is loaded *before* pytest attempts to import ``services.workers.pyqgis``
for collection — which otherwise triggers the top-level
``from qgis.core import ...`` in ``worker.py`` and fails in pure-Python
environments (CI, agent venv) without QGIS installed.
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
