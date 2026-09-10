"""Platform-side ``cases/`` package: case-layer serving to the QGIS plugin.

Only the callables whose names do NOT collide with a submodule name are
re-exported here. Re-exporting the module-named function would rebind the
same-named submodule attribute and shadow ``import cases.<module>``.
"""
from __future__ import annotations

from .ingest_user_layer import register_case_layer, upload_layer_file

__all__ = [
    "register_case_layer",
    "upload_layer_file",
]
