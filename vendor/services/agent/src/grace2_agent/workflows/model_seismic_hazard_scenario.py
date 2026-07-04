"""OpenQuake probabilistic-seismic-hazard (PSHA) composer (sprint-17).

The OpenQuake analogue of ``model_urban_flood_swmm`` (SWMM) /
``model_groundwater_contamination_scenario`` (MODFLOW) / ``model_flood_scenario``
(SFINCS). A deterministic orchestrator-style workflow (Invariant 2 - no LLM in
the chain) that composes the seismic-hazard engine end-to-end:

    assemble build_spec from OpenQuakeRunArgs (job.ini params + GMPE + G-R source)
      -> stage build_spec.json to S3 (the cache bucket)
      -> run_solver(solver='openquake', model_setup_uri=build_spec) -> Batch
      -> wait_for_completion (poll completion.json over the existing WS)
      -> download the exported hazard-MAP CSV from the Batch output
      -> postprocess_openquake (rasterize site values -> hazard COG + publish)

Unlike SWMM (in-process pyswmm) OpenQuake is CLOUD-ONLY: the engine is RAM-hungry
(~2 GB/thread) and ships as a containerized CLI, so there is NO in-process lane —
the composer always dispatches to the OpenQuake AWS Batch worker
(``services/workers/openquake/entrypoint.py``) through the SAME generic
run_solver / wait_for_completion seam SFINCS/SWMM use, routed to the openquake
job-def via the per-solver ``GRACE2_AWS_BATCH_JOB_DEF_OPENQUAKE`` env knob.

Returns the ``SeismicHazardLayerURI`` directly (a ``LayerURI`` subtype) so the
``emit_tool_call`` ``add_loaded_layer`` gate fires on it - exactly like
``run_modflow_job`` returns a ``PlumeLayerURI``. The hazard map pairs DIRECTLY
with the existing Pelicun impact path: its ground-motion intensity is Pelicun's
fragility input.

Determinism boundary (Invariant 1): every hazard number the agent narrates comes
from the typed ``SeismicHazardLayerURI`` fields the postprocess computed with
plain arithmetic - never free-generated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.execution import LayerURI, LegendClass, LegendKey
from grace2_contracts.openquake_contracts import (
    DEFAULT_SITE_GRID_SPACING_KM,
    OpenQuakeRunArgs,
    SeismicHazardLayerURI,
)

from ..layer_uri_emit import publish_input_layer
from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)
from .postprocess_openquake import (
    PostprocessOpenQuakeError,
    parse_hazard_curve_csv,
    parse_uhs_csv,
    postprocess_openquake,
)

logger = logging.getLogger("grace2_agent.workflows.model_seismic_hazard_scenario")

__all__ = [
    "model_seismic_hazard_scenario",
    "OpenQuakeWorkflowError",
    "OPENQUAKE_SOLVER_NAME",
    "assemble_build_spec",
    "stage_openquake_build_spec",
    "resolve_fault_sources",
    "fault_records_to_feature_collection",
    "make_fault_sources_layer_uri",
    "FAULT_LINE_STYLE_PRESET",
    "REAL_FAULT_SITE_GRID_SPACING_KM",
]

#: The registry key + handle ``solver`` tag for the seismic-hazard engine.
OPENQUAKE_SOLVER_NAME: str = "openquake"

#: task #199: a FINER default site-grid spacing for the real-fault case. The
#: synthetic area-source default (``DEFAULT_SITE_GRID_SPACING_KM`` == 5 km) is a
#: coarse uniform smear; a real-fault hazard map should resolve the sharp
#: gradient AROUND the fault trace, so we drop to 2 km when faults drive the
#: source model AND the caller left the (coarse) default in place. An explicit
#: finer request from the user still wins.
REAL_FAULT_SITE_GRID_SPACING_KM: float = 2.0


class OpenQuakeWorkflowError(RuntimeError):
    """Raised on any build-spec staging / dispatch / postprocess failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame. Codes:

    - ``OQ_PARAMS_INVALID`` — the run args could not be coerced.
    - ``OQ_STAGING_FAILED`` — the build_spec could not be staged to S3.
    - ``OQ_SOLVE_FAILED`` — the Batch solve did not complete.
    - ``OQ_BATCH_OUTPUT_MISSING`` — a completed run produced no hazard-map CSV.
    """

    error_code: str = "OPENQUAKE_WORKFLOW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# build_spec assembly (PURE — unit-tested in isolation).
# --------------------------------------------------------------------------- #
def assemble_build_spec(
    run_args: OpenQuakeRunArgs,
    *,
    fault_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map ``OpenQuakeRunArgs`` -> the build_spec dict the worker reads.

    Pure (no I/O) so the composer arg-assembly unit-tests in isolation. The
    build_spec is exactly the shape ``job_ini.render_openquake_deck`` consumes
    (bbox + IMT + poe + grid spacing + max distance + GMPE + the G-R source
    params) plus the output globs for the worker's upload step.

    task #199 real-fault wiring: when ``fault_sources`` (the records
    ``fetch_fault_sources`` emits for the AOI) is a NON-empty list, it is attached
    to the build_spec under ``"fault_sources"`` so the worker's
    ``render_openquake_deck`` builds a physics-based ``simpleFaultSource`` model
    (hazard PEAKING ON the trace) instead of the synthetic AOI area source. In
    that case the site grid is ALSO refined to ``REAL_FAULT_SITE_GRID_SPACING_KM``
    (2 km) -- BUT ONLY when the caller left the coarse synthetic default
    (``DEFAULT_SITE_GRID_SPACING_KM`` == 5 km) in place; an explicit user request
    for a different spacing is honored unchanged. ADDITIVE: ``fault_sources=None``
    (or an empty list) renders a byte-identical synthetic build_spec, so a run
    with no faults in the AOI behaves exactly like before.

    levers STEP 3: the validated ``advanced_physics`` (truncation_level /
    rupture_mesh_spacing_km / width_of_mfd_bin / area_source_discretization_km)
    is MERGED into the build_spec, and ``uniform_hazard_spectra`` is flipped on
    (the classical run already exports hazard curves; UHS needs the flag). None
    => no keys merged => byte-identical job.ini. Invalid keys raise a typed
    ``OpenQuakeWorkflowError("OQ_PHYSICS_INVALID")``.
    """
    from .physics_registry import (
        PhysicsRegistryError,
        validate_and_resolve_physics,
    )

    try:
        resolved = validate_and_resolve_physics(
            "openquake", getattr(run_args, "advanced_physics", None)
        )
    except PhysicsRegistryError as exc:
        raise OpenQuakeWorkflowError(
            "OQ_PHYSICS_INVALID",
            message=f"invalid advanced_physics: {exc}",
            details={"engine": "openquake", "key": getattr(exc, "key", None)},
        ) from exc

    have_faults = bool(fault_sources)

    # Real-fault case: refine the (coarse synthetic-default) site grid so the map
    # resolves the sharp gradient around the trace -- but never override an
    # explicit user request.
    grid_km = float(run_args.site_grid_spacing_km)
    if have_faults and grid_km == float(DEFAULT_SITE_GRID_SPACING_KM):
        grid_km = REAL_FAULT_SITE_GRID_SPACING_KM

    spec: dict[str, Any] = {
        "bbox": list(run_args.bbox),
        "imt": run_args.imt,
        "poe": float(run_args.poe),
        "investigation_time_years": float(run_args.investigation_time_years),
        "site_grid_spacing_km": grid_km,
        "max_distance_km": float(run_args.max_distance_km),
        "gmpe": run_args.gmpe,
        "a_value": float(run_args.a_value),
        "b_value": float(run_args.b_value),
        "min_magnitude": float(run_args.min_magnitude),
        "max_magnitude": float(run_args.max_magnitude),
        # The OpenQuake CSV exports land under output/; capture them + the
        # rendered deck for provenance.
        "outputs": ["output/*.csv", "*.csv"],
    }
    # Real-fault source model: hand the worker the fetched fault records so it
    # builds simpleFaultSources. Absent/empty => synthetic area source (default).
    if have_faults:
        spec["fault_sources"] = [dict(rec) for rec in fault_sources]  # type: ignore[union-attr]
    # Merge validated physics overrides (the worker render_job_ini reads them).
    spec.update(resolved)
    # levers STEP 3: request UHS export when the registry-quantities flag is on
    # (default OFF -> byte-identical classical job.ini). The agent reads the
    # exported UHS + hazard-curve CSVs into ScalarField metrics in
    # publish_openquake_quantities.
    if os.environ.get("GRACE2_OPENQUAKE_REGISTRY_QUANTITIES", "").lower() in (
        "1", "true", "on", "yes"
    ):
        spec["uniform_hazard_spectra"] = True
    return spec


# --------------------------------------------------------------------------- #
# task #199: real-fault source resolution (the SYNC fetch wrapper).
#
# Calls the ``fetch_fault_sources`` atomic tool for the AOI and returns
# ``(fault_records, narration_note)``. This is a SYNC function (it does network
# I/O via the cache shim) -> the composer runs it OFF the asyncio loop with
# ``asyncio.to_thread`` (the no-sync-blocking norm). The honesty floor lives
# HERE + in the composer: a fetch that returns 0 faults (open ocean, stable
# craton, upstream wobble) yields an EMPTY list -> the composer narrates
# "synthetic-area" and never claims real faults.
# --------------------------------------------------------------------------- #
def resolve_fault_sources(
    bbox: list[float] | tuple[float, float, float, float],
) -> tuple[list[dict[str, Any]], str]:
    """Fetch real active-fault sources for ``bbox`` (sync; run off the loop).

    Returns ``(fault_records, note)``:

      - ``fault_records``: the list ``fetch_fault_sources`` emits (possibly empty).
        Pass straight to ``assemble_build_spec(fault_sources=...)``.
      - ``note``: a short human-readable line for the layer narration.

    NEVER raises for the "no faults / fetch failed" case -- a missing fault source
    is an HONEST fallback to the synthetic area source, not a workflow error (the
    data-source fallback norm). A genuine upstream failure with no cache is logged
    and degraded to the empty-faults synthetic path (we still want a hazard map).
    Only the caller's malformed bbox would surface upstream (already validated by
    ``OpenQuakeRunArgs``), so in practice this always returns cleanly.
    """
    from ..tools.fetch_fault_sources import (
        FaultSourcesError,
        fetch_fault_sources,
    )

    try:
        result = fetch_fault_sources(list(bbox))
    except FaultSourcesError as exc:
        logger.warning(
            "resolve_fault_sources: fault fetch failed bbox=%s (%s); "
            "falling back to the synthetic area source",
            list(bbox),
            exc,
        )
        return [], (
            "Real active-fault sources were unavailable for this AOI "
            f"({exc.error_code}); used the synthetic area source instead."
        )

    # HONESTY FLOOR: the fetcher already drops degenerate traces (it requires a
    # non-collinear/non-coincident >=2-distinct-point trace + slip>0), which is
    # the only realistic way a fetched fault could pass here yet fail the worker's
    # length/moment-balance render gate. So a non-empty list here == faults the
    # worker WILL render into simpleFaultSources -> the real-fault stamp matches
    # what the engine runs. (We do NOT import the worker's job_ini agent-side: it
    # is not in the agent bundle, and an ImportError would wrongly force the
    # synthetic fallback on the deployed agent.)
    # task #207: ``fetch_fault_sources`` now returns a ``FaultSourcesResult``
    # (a renderable ``LayerURI`` subclass) on a NON-empty fetch and a plain dict
    # on the empty degrade -- read the records + note off EITHER shape.
    if isinstance(result, dict):
        faults = list(result.get("faults") or [])
        fetch_note = result.get("note")
    else:
        faults = list(getattr(result, "faults", None) or [])
        fetch_note = getattr(result, "note", None)
    if faults:
        names = ", ".join(
            str(f.get("name") or "fault") for f in faults[:4]
        )
        more = "" if len(faults) <= 4 else f", +{len(faults) - 4} more"
        note = (
            f"Hazard built from {len(faults)} real GEM active-fault source"
            f"{'s' if len(faults) != 1 else ''} ({names}{more}); the hazard "
            "peaks on the actual fault traces."
        )
        return faults, note

    # Empty AOI -> honest synthetic fallback. Surface the fetcher's typed note
    # (read off either shape above).
    note = (
        str(fetch_note)
        if fetch_note
        else (
            "No mapped active fault intersects this AOI; used the synthetic "
            "area source."
        )
    )
    return [], note


# --------------------------------------------------------------------------- #
# task #207: surface the resolved fault traces as a renderable INPUT layer.
#
# The fault sources are resolved in-memory (lon/lat traces) and baked into the
# OpenQuake XML, then DISCARDED -- no artifact was kept, so the user could never
# SEE the fault lines the hazard peaks on. We now serialize the records to a
# GeoJSON FeatureCollection of LineStrings (carrying name / slip-rate / slip-type
# for click-inspect), upload it next to the run, and emit it as a role="input"
# vector so the fault traces render under the hazard COG.
# --------------------------------------------------------------------------- #

#: Style-preset label for the surfaced fault-trace vector. Semantic name (future-
#: proof for a dedicated web/QGIS preset); today the web renders an unknown LINE
#: preset in its geometry-family colour, so the traces draw as a distinct line.
FAULT_LINE_STYLE_PRESET = "fault_line"


def fault_records_to_feature_collection(
    fault_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Serialize resolved fault records to a GeoJSON ``FeatureCollection``.

    Each record's ``geometry`` is the flattened ``[[lon, lat], ...]`` trace the
    fetcher produced (``trace_coords`` already collapses MultiLineString to one
    ordered vertex list). A record becomes a ``LineString`` feature carrying the
    click-inspect properties ``name`` / ``net_slip_rate_mm_yr`` / ``slip_type``
    (plus ``catalog_name`` when present). Records with fewer than 2 vertices are
    SKIPPED (a degenerate trace is not a drawable line) -- this mirrors the
    fetcher's own >=2-distinct-vertex gate, so in practice every resolved record
    yields a feature.

    Pure dict work (no I/O, no reproject -- the traces are already EPSG:4326
    lon/lat). Returns a valid (possibly empty) FeatureCollection.
    """
    features: list[dict[str, Any]] = []
    for rec in fault_records or []:
        coords = rec.get("geometry") or []
        # Coerce to a clean [[lon, lat], ...] list of >=2 vertices.
        line: list[list[float]] = []
        for p in coords:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                line.append([float(p[0]), float(p[1])])
        if len(line) < 2:
            continue
        props: dict[str, Any] = {
            "name": str(rec.get("name") or "fault"),
            "net_slip_rate_mm_yr": rec.get("net_slip_rate_mm_yr"),
            "slip_type": rec.get("slip_type"),
        }
        if rec.get("catalog_name"):
            props["catalog_name"] = str(rec.get("catalog_name"))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": line},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def make_fault_sources_layer_uri(
    fault_records: list[dict[str, Any]],
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> LayerURI | None:
    """Build the fault-trace ``FeatureCollection`` + UPLOAD it to S3 -> LayerURI.

    Mirrors :func:`make_swmm_mesh_layer_uri`: serialize the records, upload to the
    DURABLE runs bucket at ``s3://<runs_bucket>/<run_id>/fault_sources.geojson``
    (so ``add_loaded_layer`` can re-inline the s3:// vector on every reconnect,
    exactly like the mesh), and return a ``role="input"`` vector ``LayerURI`` with
    ``bbox=None`` (an input must not emit a competing zoom-to). Carries a
    categorical ``LegendKey`` so the surfaced traces get a legend swatch.

    Returns ``None`` (best-effort, never fatal) when there are no drawable
    features OR the S3 upload fails. SYNC compute + boto3 upload -- the caller
    wraps it in ``asyncio.to_thread`` (never run sync boto3 on the asyncio loop).
    """
    fc = fault_records_to_feature_collection(fault_records)
    n_features = len(fc.get("features") or [])
    if n_features <= 0:
        logger.info(
            "make_fault_sources_layer_uri: no drawable fault traces -> no input "
            "layer (run_id=%s)",
            run_id,
        )
        return None

    # Upload to the DURABLE runs bucket via the SHARED solver S3 seam (the SAME
    # boto3 instance-role + bucket convention the mesh layer + every run artifact
    # uses). A put failure -> the input is simply absent, never breaks the solve.
    try:
        from ..tools.solver import _get_runs_bucket, _get_s3_client

        bucket = runs_bucket or _get_runs_bucket()
        key = f"{run_id}/fault_sources.geojson"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(fc).encode("utf-8"),
            ContentType="application/geo+json",
        )
        s3_uri = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - best-effort; S3 put failure non-fatal
        logger.warning(
            "make_fault_sources_layer_uri: fault_sources.geojson S3 upload failed "
            "(non-fatal, fault input absent; run_id=%s): %s",
            run_id,
            exc,
        )
        return None

    plural = "trace" if n_features == 1 else "traces"
    return LayerURI(
        layer_id=f"fault-sources-{run_id}",
        name=f"Active fault {plural} ({n_features})",
        layer_type="vector",
        uri=s3_uri,
        style_preset=FAULT_LINE_STYLE_PRESET,
        role="input",
        bbox=None,
        legend=LegendKey(
            kind="categorical",
            classes=[
                LegendClass(value="fault", color="#FF6A00", label="Active fault trace")
            ],
            label="Active faults (GEM)",
        ),
    )


# --------------------------------------------------------------------------- #
# build_spec staging (S3) — mirror of stage_swmm_manifest.
# --------------------------------------------------------------------------- #
def stage_openquake_build_spec(
    run_args: OpenQuakeRunArgs,
    run_id: str,
    *,
    fault_sources: list[dict[str, Any]] | None = None,
) -> str:
    """Upload the build_spec JSON to S3; return its ``s3://`` URI.

    Mirrors ``run_swmm.stage_swmm_manifest`` EXACTLY (no new client): uses the
    same ``cache.storage_scheme()`` scheme + the same ``solver._get_s3_client()``
    boto3 client + the same ``GRACE2_CACHE_BUCKET`` staging bucket. Feed the
    returned URI STRAIGHT to ``run_solver(solver='openquake',
    model_setup_uri=<this>, ...)``.

    task #199: ``fault_sources`` (when non-empty) is threaded into
    ``assemble_build_spec`` so the staged build_spec carries the real-fault source
    model. ``None`` => synthetic area source (unchanged).

    Raises:
        OpenQuakeWorkflowError("OQ_STAGING_FAILED"): the upload could not complete.
    """
    from ..tools.cache import storage_scheme
    from ..tools.solver import _get_s3_client

    scheme = storage_scheme()  # "s3" on AWS
    cache_bucket = os.environ.get("GRACE2_CACHE_BUCKET", "grace-2-hazard-prod-cache")
    prefix = f"cache/static-30d/openquake_setup/{run_id}/"
    spec_key = f"{prefix}build_spec.json"
    spec_uri = f"{scheme}://{cache_bucket}/{spec_key}"

    build_spec = assemble_build_spec(run_args, fault_sources=fault_sources)
    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=cache_bucket,
            Key=spec_key,
            Body=json.dumps(build_spec, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise OpenQuakeWorkflowError(
            "OQ_STAGING_FAILED",
            message=f"failed to stage OpenQuake build_spec to {spec_uri}: {exc}",
            details={"run_id": run_id, "build_spec_uri": spec_uri},
        ) from exc

    logger.info("stage_openquake_build_spec run_id=%s -> %s", run_id, spec_uri)
    return spec_uri


# --------------------------------------------------------------------------- #
# Batch hazard-map download — mirror of _download_batch_swmm_outputs.
# --------------------------------------------------------------------------- #
def _pick_hazard_map_uri(output_uris: list[str]) -> str | None:
    """Pick the hazard-MAP CSV from the uploaded output URIs (agent-side mirror
    of the worker's ``resolve_hazard_map_csv``, so the agent never imports the
    worker package). Prefer a ``hazard_map`` CSV, fall back to any ``hazard``
    CSV, else None."""
    csvs = [u for u in output_uris if u.lower().endswith(".csv")]
    for u in csvs:
        base = u.rsplit("/", 1)[-1].lower()
        if "hazard_map" in base or "hazard-map" in base:
            return u
    for u in csvs:
        if "hazard" in u.rsplit("/", 1)[-1].lower():
            return u
    return None


def _download_batch_hazard_csv(run_result: Any, run_id: str) -> str:
    """Download the exported hazard-MAP CSV produced by the Batch worker.

    The OpenQuake Batch worker uploads the engine's CSV exports under
    ``s3://<runs_bucket>/<run_id>/output/`` and records the hazard-map URI in
    completion.json (``hazard_map_uri``, with the full ``output_uris`` list as a
    fallback). We re-read completion.json (small, already on S3) to find the
    hazard-map key, download it via the SAME boto3 client the solver dispatch
    uses, and return the local CSV TEXT.

    Raises:
        OpenQuakeWorkflowError("OQ_BATCH_OUTPUT_MISSING"): the completed run did
            not produce a downloadable hazard-map CSV.
    """
    from ..tools.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    manifest = _try_get_completion_s3(runs_bucket, run_id)
    hazard_uri: str | None = None
    if isinstance(manifest, dict):
        hazard_uri = manifest.get("hazard_map_uri") or _pick_hazard_map_uri(
            [str(u) for u in (manifest.get("output_uris") or [])]
        )

    if not hazard_uri:
        raise OpenQuakeWorkflowError(
            "OQ_BATCH_OUTPUT_MISSING",
            message=(
                "OpenQuake Batch solve completed but produced no hazard-map CSV "
                f"(runs_bucket={runs_bucket} run_id={run_id})"
            ),
            details={"run_id": run_id, "output_uri": getattr(run_result, "output_uri", None)},
        )

    try:
        _scheme, _bucket, key = _split_object_uri(hazard_uri)
    except Exception as exc:  # noqa: BLE001
        raise OpenQuakeWorkflowError(
            "OQ_BATCH_OUTPUT_MISSING",
            message=f"hazard_map_uri unparseable: {hazard_uri!r}: {exc}",
            details={"run_id": run_id},
        ) from exc

    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise OpenQuakeWorkflowError(
            "OQ_BATCH_OUTPUT_MISSING",
            message=f"hazard-map CSV download failed s3://{runs_bucket}/{key}: {exc}",
            details={"run_id": run_id},
        ) from exc


# --------------------------------------------------------------------------- #
# task-198: download the NON-RASTER curve products (hazard CURVE + UHS) for the
# chart producers. Best-effort: these are charts, not the headline layer - a
# missing/unreadable curve CSV yields no chart (NOT a workflow failure).
# --------------------------------------------------------------------------- #
def _pick_csv_by_token(output_uris: list[str], *tokens: str) -> str | None:
    """Pick the first CSV whose basename contains ANY of ``tokens`` (lowercased).

    OpenQuake exports ``hazard_curve-mean-<IMT>_*.csv`` and (when UHS is on)
    ``hazard_uhs-mean_*.csv`` alongside the map CSV; we select by filename token.
    """
    for u in output_uris:
        base = u.rsplit("/", 1)[-1].lower()
        if base.endswith(".csv") and any(t in base for t in tokens):
            return u
    return None


def _download_batch_curve_csvs(
    run_id: str,
) -> tuple[str | None, str | None]:
    """Download the hazard-CURVE + UHS CSV TEXT from the Batch run (best-effort).

    Re-reads completion.json's ``output_uris`` (the same manifest
    ``_download_batch_hazard_csv`` reads), selects the curve / UHS CSVs by
    filename token, and downloads them via the SAME boto3 client. Returns
    ``(hazard_curve_text|None, uhs_text|None)`` - a None entry means the product
    was not exported / not readable (no chart for it). NEVER raises: a curve
    download wobble must not fail the hazard run (the map layer already landed)."""
    try:
        from ..tools.solver import (
            _get_runs_bucket,
            _get_s3_client,
            _split_object_uri,
            _try_get_completion_s3,
        )

        runs_bucket = _get_runs_bucket()
        s3 = _get_s3_client()
        manifest = _try_get_completion_s3(runs_bucket, run_id)
        if not isinstance(manifest, dict):
            return None, None
        output_uris = [str(u) for u in (manifest.get("output_uris") or [])]
        curve_uri = _pick_csv_by_token(output_uris, "hazard_curve", "hazard-curve")
        uhs_uri = _pick_csv_by_token(output_uris, "hazard_uhs", "hazard-uhs", "_uhs")

        def _get_text(uri: str | None) -> str | None:
            if not uri:
                return None
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
                resp = s3.get_object(Bucket=runs_bucket, Key=key)
                return resp["Body"].read().decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("curve CSV download failed %s: %s", uri, exc)
                return None

        return _get_text(curve_uri), _get_text(uhs_uri)
    except Exception as exc:  # noqa: BLE001 - charts are non-fatal
        logger.warning("curve/UHS CSV resolution failed run_id=%s: %s", run_id, exc)
        return None, None


async def _emit_oq_curve_charts(
    run_id: str,
    *,
    imt: str,
    poe: float,
    investigation_time_years: float,
    source_layer_uri: str | None,
) -> None:
    """Build + side-emit the hazard-curve (and UHS) charts (best-effort, no-op safe).

    Downloads the curve / UHS CSVs off the loop, parses them with the EXISTING
    ``parse_hazard_curve_csv`` / ``parse_uhs_csv`` (real engine output, no LLM),
    builds Vega-Lite line charts via ``chart_tools``, and emits them through the
    live pipeline emitter. Each builder returns None (emits nothing) when its
    series is absent - so a classical-only run (no UHS) emits only the curve."""
    from ..tools.chart_tools import build_hazard_curve_chart, build_uhs_chart

    curve_text, uhs_text = await asyncio.to_thread(
        _download_batch_curve_csvs, run_id
    )

    charts: list[dict[str, Any]] = []
    if curve_text:
        curve = parse_hazard_curve_csv(curve_text)
        chart = build_hazard_curve_chart(
            imls_g=curve.get("hazard_curve_imls_g") or [],
            mean_poe=curve.get("hazard_curve_mean_poe") or [],
            imt=imt,
            investigation_time_years=investigation_time_years,
            n_sites=curve.get("hazard_curve_n_sites"),
            source_layer_uri=source_layer_uri,
        )
        if chart is not None:
            charts.append(chart)
    if uhs_text:
        uhs = parse_uhs_csv(uhs_text)
        chart = build_uhs_chart(
            periods_s=uhs.get("uhs_periods_s") or [],
            mean_sa_g=uhs.get("uhs_mean_sa_g") or [],
            poe=poe,
            n_sites=uhs.get("uhs_n_sites"),
            source_layer_uri=source_layer_uri,
        )
        if chart is not None:
            charts.append(chart)

    if charts:
        await emit_chart_payloads(charts)


# --------------------------------------------------------------------------- #
# Composer.
# --------------------------------------------------------------------------- #
async def model_seismic_hazard_scenario(
    run_args: OpenQuakeRunArgs,
    *,
    compute_class: str = "standard",
) -> SeismicHazardLayerURI:
    """Run a classical-PSHA OpenQuake hazard calculation end-to-end on AWS Batch.

    Stages a build_spec, dispatches the OpenQuake Batch worker through the
    generic run_solver / wait_for_completion seam, downloads the exported
    hazard-map CSV, and postprocesses it into a published ``SeismicHazardLayerURI``.

    Args:
        run_args: the validated ``OpenQuakeRunArgs``.
        compute_class: FR-CE-3 compute class for the Batch sizing bucket.
            Default ``"standard"`` (OpenQuake is RAM-hungry, so it should size up
            for a larger site grid).

    Returns:
        ``SeismicHazardLayerURI`` (a ``LayerURI`` subtype) — the emitter appends
        it to ``session-state.loaded_layers`` and the map renders the hazard COG.

    Raises:
        OpenQuakeWorkflowError: any staging / dispatch / postprocess step failed.
    """
    from ..tools.solver import run_solver, wait_for_completion

    run_id = new_ulid()
    logger.info(
        "model_seismic_hazard_scenario run_id=%s bbox=%s imt=%s poe=%.4g "
        "inv_time=%.0fyr grid=%.1fkm gmpe=%s compute_class=%s",
        run_id,
        run_args.bbox,
        run_args.imt,
        run_args.poe,
        run_args.investigation_time_years,
        run_args.site_grid_spacing_km,
        run_args.gmpe,
        compute_class,
    )

    # Declare the planned child count up front so the parent card's live
    # breadcrumb can render "k/5" (faults -> build_spec -> solve -> download ->
    # publish). No-op when no emitter is bound (verify/CI direct-call path).
    begin_substeps(current_emitter(), 5)

    # 0) task #199: resolve REAL active-fault sources for the AOI (sync fetch off
    #    the loop). Non-empty => build the fault source model + narrate
    #    "real-fault"; empty => honest synthetic-area fallback. NEVER fails the
    #    run (resolve_fault_sources degrades to [] on any fetch error).
    async with substep(current_emitter(), "resolve_fault_sources"):
        fault_sources, source_model_note = await asyncio.to_thread(
            resolve_fault_sources, list(run_args.bbox)
        )
    used_real_faults = bool(fault_sources)
    source_model_kind = "real-fault" if used_real_faults else "synthetic-area"
    logger.info(
        "model_seismic_hazard_scenario run_id=%s source_model_kind=%s "
        "(fault_count=%d): %s",
        run_id,
        source_model_kind,
        len(fault_sources),
        source_model_note,
    )

    # 0.5) task #207: surface the resolved fault traces as a renderable INPUT
    #      layer so the user can SEE the fault lines the hazard peaks on (they
    #      were previously baked into the OpenQuake XML and discarded). GATED on
    #      ``used_real_faults`` (no traces -> nothing to draw). The serialize +
    #      S3 upload is SYNC boto3, OFFLOADED off the loop; the emit is BEST-
    #      EFFORT (publish_input_layer never raises) so a failure to surface the
    #      faults can NEVER fail the solve. role="input" + bbox=None: the traces
    #      render under the hazard COG and do not fight the AOI camera.
    # GATED additionally on an emitter being bound: there is no point serializing
    # + uploading the fault GeoJSON when nothing can surface it (the verify/CI
    # direct-call path), and skipping it keeps that path free of boto3.
    if used_real_faults and current_emitter() is not None:
        try:
            fault_layer = await asyncio.to_thread(
                make_fault_sources_layer_uri, fault_sources, run_id=run_id
            )
            await publish_input_layer(current_emitter(), fault_layer)
        except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
            logger.warning(
                "model_seismic_hazard_scenario: fault-trace input surface failed "
                "(non-fatal) run_id=%s: %s",
                run_id,
                exc,
            )

    # 1) Stage the build_spec (sync boto3 off the loop). Thread the resolved
    #    fault sources so a real-fault AOI stages the simpleFaultSource model.
    async with substep(current_emitter(), "stage_openquake_build_spec"):
        build_spec_uri = await asyncio.to_thread(
            lambda: stage_openquake_build_spec(
                run_args, run_id, fault_sources=fault_sources or None
            )
        )

    # 2) Dispatch through the generic run_solver / wait_for_completion seam.
    #    Surface the dispatch + Batch wait as a single "Solved (Batch ...)" child
    #    row; the live Batch readout stays owned by the two-card Sim observability
    #    inside run_solver / wait_for_completion (mint_dispatch_and_sim_cards).
    async with substep(current_emitter(), "run_solver"):
        handle = run_solver(
            solver=OPENQUAKE_SOLVER_NAME,
            model_setup_uri=build_spec_uri,
            compute_class=compute_class,
        )
        run_result = await wait_for_completion(handle)

        # Honesty floor: a non-complete Batch result raises INSIDE the substep so
        # the solve child reads red (failed), not a silent green. The raise re-
        # raises through the substep wrapper unchanged (caller control flow).
        if run_result.status != "complete":
            raise OpenQuakeWorkflowError(
                "OQ_SOLVE_FAILED",
                message=(
                    "OpenQuake Batch solve did not complete "
                    f"(status={run_result.status}, error_code={run_result.error_code}): "
                    f"{run_result.error_message or run_result.cancellation_reason or ''}"
                ),
                details={
                    "run_id": run_id,
                    "output_uri": run_result.output_uri,
                },
            )

    # --- Register-only branch (worker postprocess offload) -------------------
    # If the worker wrote a publish_manifest.json (schema_version==1), read +
    # schema-gate it and SHORT-CIRCUIT the on-box heavy tail (no CSV download,
    # no postprocess_openquake). Degrades cleanly to legacy path when absent.
    from .register_published_manifest import (
        read_publish_manifest,
        register_manifest_layers,
    )

    batch_run_id = getattr(run_result, "run_id", None) or run_id
    _oq_manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if _oq_manifest is not None:
        logger.info(
            "model_seismic_hazard_scenario: REGISTER-ONLY path (worker postprocess "
            "offload) run_id=%s engine=%s layers=%d",
            batch_run_id, _oq_manifest.engine, len(_oq_manifest.layers),
        )
        async with substep(current_emitter(), "publish_layer"):
            _oq_reg = register_manifest_layers(
                _oq_manifest, run_id=batch_run_id,
                bbox=tuple(run_args.bbox) if run_args.bbox else None,
            )
        _oq_primary_layers = [lyr for lyr in _oq_reg.layers if lyr.role == "primary"]
        if not _oq_primary_layers:
            raise OpenQuakeWorkflowError(
                "OQ_NO_LAYERS",
                message="worker publish_manifest produced no primary layer (empty solve?)",
            )
        _oq_prim = _oq_primary_layers[0]
        _oq_m = _oq_reg.metrics
        _poe = float(run_args.poe)
        _inv = float(run_args.investigation_time_years)
        try:
            _rp = -_inv / math.log(1.0 - _poe)
        except (ValueError, ZeroDivisionError):
            _rp = 0.0
        _oq_layer = SeismicHazardLayerURI(
            uri=_oq_prim.uri,
            layer_type=_oq_prim.layer_type,
            layer_id=_oq_prim.layer_id,
            name=_oq_prim.name,
            style_preset=_oq_prim.style_preset,
            bbox=tuple(run_args.bbox) if run_args.bbox else None,
            role=_oq_prim.role,
            imt=run_args.imt,
            poe=_poe,
            investigation_time_years=_inv,
            return_period_years=_rp,
            max_hazard_value=float(_oq_m.get("max_pga_g", 0.0)),
            hazard_area_km2=float(_oq_m.get("hazard_area_km2", 0.0)),
            n_sites=int(_oq_m.get("n_sites", 0)),
            source_model_kind=source_model_kind,
            source_model_note=source_model_note,
        )
        return _oq_layer

    # 3) Download the hazard-map CSV from the worker's run_id prefix (the Batch
    #    dispatch mints a fresh run_id; the worker writes under run_result.run_id,
    #    NOT the composer's run_id — mirror the SWMM/SFINCS Batch lesson).
    async with substep(current_emitter(), "_download_batch_hazard_csv"):
        hazard_csv_text = await asyncio.to_thread(
            _download_batch_hazard_csv, run_result, batch_run_id
        )

    # 4) Postprocess: rasterize site values -> hazard COG + publish.
    try:
        async with substep(current_emitter(), "postprocess_openquake"):
            layer = await asyncio.to_thread(
                postprocess_openquake,
                hazard_csv_text,
                run_id=batch_run_id,
                imt=run_args.imt,
                poe=float(run_args.poe),
                investigation_time_years=float(run_args.investigation_time_years),
            )
    except PostprocessOpenQuakeError as exc:
        raise OpenQuakeWorkflowError(
            exc.error_code,
            message=str(exc),
            details={"run_id": batch_run_id, **getattr(exc, "details", {})},
        ) from exc

    # task #199 honesty floor: stamp the source-model provenance onto the typed
    # layer so the agent narrates real-vs-fallback from a TYPED field, never a
    # free claim. ``source_model_kind`` reflects the path THIS run actually took
    # (postprocess builds the layer with the synthetic-area default; we flip it to
    # real-fault ONLY when fault sources were actually staged).
    layer = layer.model_copy(
        update={
            "source_model_kind": source_model_kind,
            "source_model_note": source_model_note,
        }
    )

    # task-198: wire the NON-RASTER PSHA products (hazard CURVE + UHS) to charts.
    # The documented FOLLOW-UP (postprocess_openquake.py:524-531): the chart
    # producers consume the parsed curve arrays, not a layer URI. Best-effort -
    # a missing curve emits no chart (the honesty floor); never fails the run.
    await _emit_oq_curve_charts(
        batch_run_id,
        imt=run_args.imt,
        poe=float(run_args.poe),
        investigation_time_years=float(run_args.investigation_time_years),
        source_layer_uri=layer.uri,
    )

    logger.info(
        "model_seismic_hazard_scenario complete run_id=%s layer_id=%s "
        "source_model_kind=%s max_hazard=%.6g hazard_area_km2=%.6g n_sites=%d "
        "uri=%s",
        batch_run_id,
        layer.layer_id,
        layer.source_model_kind,
        layer.max_hazard_value,
        layer.hazard_area_km2,
        layer.n_sites,
        layer.uri,
    )
    return layer
