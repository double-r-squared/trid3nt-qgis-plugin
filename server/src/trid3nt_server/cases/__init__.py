"""Platform-side ``cases/`` package: case-layer serving to the QGIS plugin.

First tenant of the server-modularization plan's ``cases/`` target (the case
lifecycle absorptions land in later waves). One seam lives here:

- ``ingest_user_layer`` / ``upload_layer_file`` / ``register_case_layer``
  (``cases.ingest_user_layer``) -- the reverse seam: bring a plugin-pushed
  vector/raster INTO a case as a first-class input layer. (A case's layers are
  RESTORED to the plugin over the WS case-open replay and STREAM in place from
  the object store via GDAL ``/vsicurl/`` so there is no
  server-side materialize/download seam.)

Only the callables whose names do NOT collide with a submodule name are
re-exported here (``upload_layer_file``, ``register_case_layer``). The
module-named function (``ingest_user_layer``) and the typed error classes are
imported from their specific submodule -- re-exporting them at the package root
would rebind the same-named submodule attribute and shadow
``import cases.<module>``.
"""

from __future__ import annotations

from .ingest_user_layer import register_case_layer, upload_layer_file

__all__ = [
    "register_case_layer",
    "upload_layer_file",
]
