"""Platform-side ``cases/`` package: case-layer serving to the QGIS plugin.

Only names that do NOT collide with a submodule are re-exported: re-exporting a module-
named function would shadow ``import cases.<module>``.
"""
from __future__ import annotations

from .ingest_user_layer import register_case_layer, upload_layer_file

__all__ = [
    "register_case_layer",
    "upload_layer_file",
]
