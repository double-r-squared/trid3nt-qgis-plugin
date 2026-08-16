"""Persistence layer: the document-store client seam plus case-lifecycle joins.

``persistence`` holds the ``MCPClientProtocol`` store seam (file-backed by
default); ``case_lifecycle`` builds the per-case QGS project on top of it. This
package re-exports the store surface so ``trid3nt_server.persistence.X`` resolves
unchanged after the module-to-package grouping.
"""

from __future__ import annotations

from .persistence import *  # noqa: F401,F403 -- re-export the documented store API
from .persistence import _default_dev_persistence_dir  # noqa: F401 -- used by dev-dir callers
