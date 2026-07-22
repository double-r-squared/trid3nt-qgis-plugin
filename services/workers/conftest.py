"""conftest.py for services/workers tests (job-0062).

Installs qgis.* stub modules into sys.modules before pytest collects any test
module. Placing this conftest at services/workers/ ensures it is discovered
and loaded *before* pytest processes the pyqgis sub-package, which would
otherwise trigger the top-level ``from qgis.core import ...`` in
``services/workers/pyqgis/worker.py`` and fail in pure-Python environments
(CI, agent venv) without QGIS installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure the repo root is on sys.path so `services.workers.pyqgis.*` imports
# work correctly. This is necessary because the agent venv only adds the
# agent src and contracts src paths, not the repo root.
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
