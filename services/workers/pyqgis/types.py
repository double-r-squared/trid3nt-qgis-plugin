"""Typed shapes for the PyQGIS worker round-trip.

These are *worker-local* shapes — they are not part of the
``grace2-contracts`` package (no ``AssessmentEnvelope`` / ``ResultLayer`` /
``RunDocument`` surface is touched at M2). When a real run document lands
(M5+ SFINCS), the worker will populate ``schema``-owned contract shapes
through the Appendix D direct-driver path; these M2 shapes will either be
replaced or re-routed through that contract.

Implementation choice: stdlib :mod:`dataclasses` (``frozen=True, slots=True``)
rather than pydantic v2.

Rationale:

* The ``grace2`` conda env (job-0022) does not ship pydantic; adding a
  dependency for one worker-local envelope is more invasive than it is worth
  at M2 — the dataclass equivalent is one import.
* These shapes never cross a contract boundary in M2 (the Pub/Sub topic has
  no subscriber, deferred to M3/M4 per ``reports/sprints/sprint-04.md``).
* When the agent consumer arrives, the schema specialist owns the move to a
  contract-grade shape; this file is the worker-side payload until then.

Surfaced as Open Question OQ-20A in the job-0020 report.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


def _utc_iso_z() -> str:
    """Return an ISO-8601 UTC timestamp with a literal ``Z`` suffix.

    Matches the ``grace2-contracts`` convention (see job-0013 audit):
    ``datetime.now(timezone.utc).isoformat()`` would produce ``+00:00``;
    callers downstream want the canonical ``Z`` form.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True, slots=True)
class LayerSpec:
    """Description of a layer to append during the worker round-trip.

    M2 supports a single layer type: a small in-memory FlatGeobuf polygon
    around a target area. The polygon coordinates are emitted as a
    WKT footprint into a temporary FlatGeobuf file at mutation time.

    Attributes
    ----------
    name:
        Layer name as it will appear in the ``.qgs`` and in any future WMS
        ``GetCapabilities`` response.
    polygon_wkt:
        OGC WKT of a single polygon in ``EPSG:4326`` (lon/lat). Defaults to
        a 1 deg x 1 deg square centred at ``(lon=-100, lat=35)`` (per
        kickoff scope §3 step c).
    crs:
        Authority CRS string of ``polygon_wkt`` — fixed at ``EPSG:4326`` for
        M2.
    """

    name: str
    polygon_wkt: str = (
        "POLYGON((-100.5 34.5, -99.5 34.5, -99.5 35.5, -100.5 35.5, -100.5 34.5))"
    )
    crs: str = "EPSG:4326"


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Typed return value (and Pub/Sub payload) for a worker round-trip.

    Carries the layer-state delta + the Pub/Sub message id + a status flag.
    Maps 1:1 onto the FR-QS-6 step 5 ("notify completion event") envelope.

    Attributes
    ----------
    qgs_uri:
        The input URI the worker was asked to mutate. ``/vsigs/<bucket>/<key>.qgs``
        or a local filesystem path (local-dev only).
    layers_before:
        ``[layer_name, ...]`` extracted via ``QgsProject.mapLayers()`` after
        the initial read, in QGIS' internal iteration order (not sorted).
    layers_after:
        Same after the mutation step.
    notify_message_id:
        The Pub/Sub message id returned by ``publisher.publish().result()``.
        ``None`` when running in ``--no-publish`` mode (local unit test).

        **Important — convention for the published envelope:** when this
        ``WorkerResult`` is serialized into a Pub/Sub message ``data`` field,
        ``notify_message_id`` is **always** ``null`` because the envelope is
        constructed *before* ``publisher.publish().result()`` returns its
        message id (chicken-and-egg). The field is populated only on the
        in-process ``WorkerResult`` returned by ``worker_round_trip`` to the
        in-process caller. Subscribers should rely on the outer Pub/Sub
        ``message.messageId`` for correlation, not on this in-payload field.
        Tracked as OQ-20G — a follow-up may split into two shapes (a
        ``WorkerCompletionEnvelope`` for the published payload that omits
        this field; ``WorkerResult`` for the in-process return that keeps
        it). For M2 the in-payload null is documented behaviour, not a bug.
    status:
        ``"ok"`` for a successful round-trip. ``"error"`` when a wrapped
        external call exhausted its retry budget and the round-trip aborted
        — see the worker's error-handling block. The Cloud Run Job exit
        code is 0 in both cases so the published Pub/Sub message is the
        single source of truth for downstream consumers (NFR-R-1).
    error:
        Human-readable error description when ``status == "error"``.
        ``None`` on success.
    qgs_version:
        ``Qgis.QGIS_VERSION`` of the worker process — pinned by the
        ``grace2`` conda env / production container image. Carried in the
        envelope for downstream debugging (rendering drift surfaces here).
    ts:
        UTC ISO-8601 ``Z`` timestamp captured at publish time.
    """

    qgs_uri: str
    layers_before: list[str]
    layers_after: list[str]
    notify_message_id: str | None
    status: Literal["ok", "error"]
    error: str | None
    qgs_version: str
    ts: str = dataclasses.field(default_factory=_utc_iso_z)
    #: WMS URL for the published raster layer.  Populated only when the
    #: worker was invoked with ``--op publish-raster``; ``None`` for the
    #: polygon round-trip path.  Carried in the Pub/Sub envelope so the
    #: agent caller can retrieve the URL without constructing it independently.
    wms_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain ``dict`` for JSON serialization in the Pub/Sub envelope."""
        d: dict[str, Any] = {
            "qgs_uri": self.qgs_uri,
            "layers_before": list(self.layers_before),
            "layers_after": list(self.layers_after),
            "notify_message_id": self.notify_message_id,
            "status": self.status,
            "error": self.error,
            "qgs_version": self.qgs_version,
            "ts": self.ts,
        }
        if self.wms_url is not None:
            d["wms_url"] = self.wms_url
        return d

    def to_json_bytes(self) -> bytes:
        """UTF-8 JSON bytes for the Pub/Sub ``data`` field."""
        return json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8")


class WorkerError(RuntimeError):
    """Unrecoverable worker failure — raised only for programmer errors.

    The worker's external-call resilience block converts transient failures
    (GCS, Pub/Sub) into a ``WorkerResult(status="error", ...)`` so the
    Cloud Run Job exits cleanly and the consumer sees a structured payload.
    ``WorkerError`` is raised only for setup/programmer errors that cannot
    be expressed inside ``WorkerResult`` (e.g., a malformed ``qgs_uri``).
    """
