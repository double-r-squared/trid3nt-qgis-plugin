"""conftest.py for the workers tests.

Puts the repo root on ``sys.path`` - the agent venv adds only the agent and
contracts source paths - and installs ``qgis.*`` stubs before collection, so a
worker module that imports them loads where QGIS is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# The agent venv adds the agent and contracts source paths only, so a
# ``workers.*`` import needs the repo root put on the path here.
_REPO_ROOT = Path(__file__).parent.parent
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
