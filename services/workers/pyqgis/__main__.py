"""CLI entrypoint for the PyQGIS worker round-trip.

Invoked by the Cloud Run Job container built in job-0021::

    # Polygon path (M2 default):
    python -m services.workers.pyqgis \
        --qgs-uri /vsigs/grace-2-hazard-prod-qgs/grace2-sample.qgs \
        --layer-to-add demo-polygon

    # Raster publish path (job-0062, Option A):
    python -m services.workers.pyqgis \
        --op publish-raster \
        --qgs-uri /vsigs/grace-2-hazard-prod-qgs/grace2-sample.qgs \
        --raster-uri /vsigs/grace-2-hazard-prod-runs/<run_id>/flood_depth_peak.tif \
        --raster-layer-id flood-depth-peak-<run_id> \
        --style-preset-name continuous_flood_depth

Env-var fallbacks (Cloud Run Jobs prefer env over args):

* ``QGS_URI``              → ``--qgs-uri``
* ``LAYER_TO_ADD``         → ``--layer-to-add`` (polygon path)
* ``WORKER_OP``            → ``--op`` (operation discriminator; default: ``add-polygon``)
* ``RASTER_URI``           → ``--raster-uri`` (raster path)
* ``RASTER_LAYER_ID``      → ``--raster-layer-id`` (raster path)
* ``STYLE_PRESET_NAME``    → ``--style-preset-name`` (raster path; default: ``continuous_flood_depth``)
* ``GCP_PROJECT``          → Pub/Sub project override
* ``PUBSUB_TOPIC``         → Pub/Sub topic override

**Operation discriminator** (Option A per job-0062 kickoff):

``--op add-polygon`` (default, backward-compatible):
    Runs the M2 ``worker_round_trip`` polygon path. Reads ``--layer-to-add``
    (or ``LAYER_TO_ADD``). Existing callers that do not pass ``--op`` get this
    path unchanged — regression-safe.

``--op publish-raster``:
    Runs ``publish_raster_round_trip``. Reads ``--raster-uri``,
    ``--raster-layer-id``, and ``--style-preset-name``. Returns a
    ``WorkerResult`` with ``wms_url`` populated in the Pub/Sub envelope.

The process exit code is 0 on both success and recoverable-error paths:
the published Pub/Sub envelope is the single source of truth for downstream
consumers (NFR-R-1). Exit code is non-zero only for setup errors (missing
arg + missing env var).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .types import LayerSpec
from .worker import publish_raster_round_trip, worker_round_trip


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.workers.pyqgis",
        description=(
            "GRACE-2 PyQGIS worker — read a .qgs from GCS, append a layer, "
            "write back, publish completion to Pub/Sub."
        ),
    )
    parser.add_argument(
        "--qgs-uri",
        default=os.environ.get("QGS_URI"),
        help=(
            "Path to the .qgs to mutate. Accepts /vsigs/<bucket>/<key>.qgs, "
            "gs://<bucket>/<key>.qgs, or a local absolute path. "
            "Defaults to env var QGS_URI."
        ),
    )
    # --- Operation discriminator (Option A — minimal disturbance) ---
    parser.add_argument(
        "--op",
        default=os.environ.get("WORKER_OP", "add-polygon"),
        choices=["add-polygon", "publish-raster"],
        help=(
            "Operation to perform. "
            "``add-polygon`` (default): M2 polygon round-trip (backward-compatible). "
            "``publish-raster``: add a COG as a WMS-servable raster layer (job-0062). "
            "Defaults to env var WORKER_OP (falls back to ``add-polygon``)."
        ),
    )
    # --- Polygon path args ---
    parser.add_argument(
        "--layer-to-add",
        default=os.environ.get("LAYER_TO_ADD"),
        help=(
            "Name of the polygon layer the worker will append (add-polygon path). "
            "Defaults to env var LAYER_TO_ADD."
        ),
    )
    # --- Raster publish path args ---
    parser.add_argument(
        "--raster-uri",
        default=os.environ.get("RASTER_URI"),
        help=(
            "GDAL-accessible URI of the COG raster to publish (publish-raster path). "
            "Typically /vsigs/<runs-bucket>/<run_id>/flood_depth_peak.tif. "
            "Defaults to env var RASTER_URI."
        ),
    )
    parser.add_argument(
        "--raster-layer-id",
        default=os.environ.get("RASTER_LAYER_ID"),
        help=(
            "QGIS layer name + WMS LAYERS= value for the published raster "
            "(publish-raster path). E.g. flood-depth-peak-<run_id>. "
            "Defaults to env var RASTER_LAYER_ID."
        ),
    )
    parser.add_argument(
        "--style-preset-name",
        default=os.environ.get("STYLE_PRESET_NAME", "continuous_flood_depth"),
        help=(
            "QML preset filename stem to apply to the raster layer "
            "(publish-raster path). Defaults to ``continuous_flood_depth`` "
            "(env var STYLE_PRESET_NAME)."
        ),
    )
    # --- Shared flags ---
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip Pub/Sub publish (local-dev / unit-test mode).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    if not args.qgs_uri:
        parser.error(
            "--qgs-uri is required (or set QGS_URI env var)."
        )

    if args.op == "publish-raster":
        # --- Raster publish path (job-0062) ---
        if not args.raster_uri:
            parser.error(
                "--raster-uri is required for --op publish-raster (or set RASTER_URI env var)."
            )
        if not args.raster_layer_id:
            parser.error(
                "--raster-layer-id is required for --op publish-raster (or set RASTER_LAYER_ID env var)."
            )

        result = publish_raster_round_trip(
            qgs_uri=args.qgs_uri,
            raster_uri=args.raster_uri,
            layer_id=args.raster_layer_id,
            style_preset_name=args.style_preset_name,
            publish=not args.no_publish,
        )
    else:
        # --- Polygon path (M2 default, backward-compatible) ---
        if not args.layer_to_add:
            parser.error(
                "--layer-to-add is required for --op add-polygon (or set LAYER_TO_ADD env var)."
            )
        spec = LayerSpec(name=args.layer_to_add)
        result = worker_round_trip(
            args.qgs_uri,
            spec,
            publish=not args.no_publish,
        )

    # Emit the result as JSON on stdout so the Cloud Run Job execution log
    # carries the structured envelope (also published to Pub/Sub).
    print(json.dumps(result.to_dict(), indent=2))

    # Exit code policy: 0 on ok + 0 on recoverable error (envelope is the
    # source of truth). The non-zero codes from argparse.parser.error()
    # above cover the unrecoverable arg-missing case.
    return 0


if __name__ == "__main__":
    sys.exit(main())
