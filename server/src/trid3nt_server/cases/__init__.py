"""Platform-side ``cases/`` package: case-layer serving to the QGIS plugin.

First tenant of the server-modularization plan's ``cases/`` target (the case
lifecycle absorptions land in later waves). Three seams live here:

- ``build_case_layers_manifest`` (``cases.hydrate_case_layers``) -- the PRIMARY
  case-hydration path: serve a case's persisted layer registry as a lightweight
  manifest so the plugin adds each layer straight from its store URI (the SAME
  by-URI path live-published layers use), no server-side materialization.
- ``hydrate_case_layers`` (``cases.hydrate_case_layers``) -- the REMOTE-mode
  fallback: materialize a case's layers into a self-contained local folder
  (GeoPackage + GeoTIFF + ``.qml`` style sidecars + per-run MDAL mesh
  references) for a client that cannot reach the object store directly. NO QGIS
  project (.qgz/.qgs) is produced.
- ``ingest_user_layer`` / ``upload_layer_file`` / ``register_case_layer``
  (``cases.ingest_user_layer``) -- the reverse seam: bring a plugin-pushed
  vector/raster INTO a case as a first-class input layer.

Only the callables whose names do NOT collide with a submodule name are
re-exported here (``build_case_layers_manifest``, ``upload_layer_file``,
``register_case_layer``). The two module-named functions (``hydrate_case_layers``,
``ingest_user_layer``) and the typed error classes are imported from their
specific submodule -- re-exporting them at the package root would rebind the
same-named submodule attribute and shadow ``import cases.<module>``.
"""

from __future__ import annotations

from .hydrate_case_layers import build_case_layers_manifest
from .ingest_user_layer import register_case_layer, upload_layer_file

__all__ = [
    "build_case_layers_manifest",
    "register_case_layer",
    "upload_layer_file",
]
