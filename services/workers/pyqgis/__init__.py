"""GRACE-2 PyQGIS worker package — mutates ``.qgs`` projects in GCS.

This package implements the canonical FR-QS-6 round-trip pattern (SRS v0.3
Decision C, Invariant 4 "PyQGIS-worker is the only ``.qgs`` writer"):

    1. Read a ``.qgs`` from GCS (via ``/vsigs/<bucket>/<path>.qgs``)
    2. Mutate it via PyQGIS (append layers, apply QML preset, set temporal)
    3. Write it back to GCS
    4. Publish a completion event to a Pub/Sub topic

For sprint-04 / M2 the only published mutation is
:func:`worker_round_trip` — append a single in-memory FlatGeobuf polygon layer
to the canonical sample ``.qgs`` and notify. Subsequent milestones add the
``update_project_layers`` / ``apply_style_preset`` / ``set_temporal_config``
typed wrappers documented in ``agents/engine.md``.

Public surface
--------------

* :data:`worker_round_trip`  — the entrypoint Cloud Run Job calls
* :data:`WorkerResult`       — typed return shape (also the Pub/Sub envelope)
* :data:`LayerSpec`          — typed parameter for ``layer_to_add``
* :data:`WorkerError`        — typed exception for unrecoverable failures
"""

from __future__ import annotations

from .types import LayerSpec, WorkerError, WorkerResult
from .worker import worker_round_trip

__all__ = [
    "LayerSpec",
    "WorkerError",
    "WorkerResult",
    "worker_round_trip",
]
