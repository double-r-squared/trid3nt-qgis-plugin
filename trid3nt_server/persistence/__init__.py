"""Persistence layer: the document-store client seam.

``persistence`` holds the ``MCPClientProtocol`` store seam (file-backed by
default). This package re-exports the store surface so
``trid3nt_server.persistence.X`` resolves unchanged.
"""

from __future__ import annotations

from .persistence import *  # noqa: F401,F403 -- re-export the documented store API
from .persistence import _default_dev_persistence_dir  # noqa: F401 -- used by dev-dir callers
