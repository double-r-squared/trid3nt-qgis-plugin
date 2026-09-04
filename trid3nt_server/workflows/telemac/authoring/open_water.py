"""The OPEN-WATER front of the TELEMAC AOI templates: stage, solve, read, surface.

The reach family (``helpers/reach.py`` + ``authoring/assembler.py`` +
``solving/solve.py``) meshes a corridor along a flowline. The other TELEMAC
domains - a lake fetch, a harbour basin - are the same shape instead: a regular
grid over an AOI, real topobathy at the nodes, one worker section in the
manifest, one result SELAFIN, one peak field.

This module is the ONE copy of that. What varies between the domains - which
manifest section, which solver, which result file, which outputs the supervisor
uploads - is DATA the author returns, not code paths here: a run says what
solves it.

Everything past the primary layer is best-effort by contract, exactly as in the
reach family: a missing bed COG or an unpublished mesh never voids a solve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.workflows.runtime import DeclarativeError, Step

from ..solving.solve import read_run_metrics

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.open_water")

__all__ = [
    "mesh_resolution_label",
    "OpenWaterError",
    "STAGED_BED_DEST",
    "dispatch_and_wait",
    "fetch_domain_bed",
    "GREAT_LAKES",
    "great_lake_for",
    "real_lake_bathy_label",
    "solves_on_real_bed",
    "staged_bed_inputs",
    "mesh_sizing_provenance",
    "SolveOpenWater",
    "download_open_water_result",
    "solve_open_water",
    "solved_domain_bbox",
    "case_section",
    "stage_telemac_manifest",
]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

#: Wall-clock ceiling on one open-water solve. These domains are a few thousand
#: grid nodes over hours of simulated time - the reach family's node-budget
#: estimator has no corridor geometry to work from here, so the bound is a flat
#: one rather than a fake calculation.
_SOLVE_TIMEOUT_S = 3600.0


class OpenWaterError(DeclarativeError):
    """An open-water TELEMAC domain could not be staged, solved or read."""

    error_code = "TELEMAC_OPEN_WATER_FAILED"


#: Where a staged bed raster lands inside the worker's run directory. The worker
#: READS this name; nothing in the image knows where the bytes came from.
STAGED_BED_DEST: str = "bed_source.tif"

#: Rough lon/lat extents of the five Great Lakes' open water. The gate on the
#: REAL-bathymetry path: the NOAA lake-datum grids cover these and nothing else,
#: so an AOI outside them has no real bed to sample and says so.
GREAT_LAKES: dict[str, tuple[float, float, float, float]] = {
    "superior": (-92.2, 46.4, -84.3, 49.1),
    "michigan": (-88.1, 41.6, -84.7, 46.1),
    "huron": (-84.8, 43.0, -79.7, 46.3),
    "erie": (-83.5, 41.3, -78.8, 42.9),
    "ontario": (-79.9, 43.2, -76.0, 44.3),
}


def great_lake_for(lon: float, lat: float) -> str | None:
    """Which Great Lake this point sits in, or ``None`` for anywhere else."""
    for name, (x0, y0, x1, y1) in GREAT_LAKES.items():
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return name
    return None


def real_lake_bathy_label(lake: str | None) -> str:
    """What the REAL Great Lakes bed IS, said once.

    Both lake-capable open-water templates sample the same NOAA lake-datum grid,
    so one sentence serves both. The IDEALIZED half of each label stays with its
    module: a Berkhoff shoal and a lock-exchange channel are different physics and
    deserve different sentences.
    """
    return f"real NOAA Great Lakes lake-datum bathymetry ({lake or 'AOI'})"


def solves_on_real_bed(bathy_source: Any, *,
                       lon: float | None = None,
                       lat: float | None = None,
                       mode: Any = None,
                       real_bed_modes: tuple[str, ...] | None = None) -> bool:
    """Whether this domain is solved on FETCHED bed data rather than an authored one.

    The producer and the author both have to answer this, and they have to
    agree: a producer that fetched where the author went idealized stages a raster
    nothing reads, and the reverse builds a real domain with no bed. One
    definition, two readers.

    Two gates, in order. ``real_bed_modes`` is the set of question modes that HAVE
    real geography at all - a Berkhoff shoal and a lock-exchange channel are
    verification domains whose bed is authored by the physics, so no bathymetry
    request makes them real. Then the source gate: the domain is real when the
    lake bed was asked for, or when auto-selection finds the AOI inside one of the
    Great Lakes the grids cover.
    """
    if real_bed_modes is not None and str(mode) not in real_bed_modes:
        return False
    asked = str(bathy_source or "auto").strip().lower()
    if asked == "noaa_greatlakes":
        return True
    if asked != "auto" or lon is None or lat is None:
        return False
    return great_lake_for(float(lon), float(lat)) is not None


async def fetch_domain_bed(*, bathy_source: Any = "auto",
                           mode: Any = None,
                           real_bed_modes: tuple[str, ...] | None = None,
                           px_per_deg: float = 1800.0,
                           max_px_per_side: int = 3000) -> dict[str, Any]:
    """The BED an open-water domain is solved on, fetched over the acquired AOI.

    This is the declared producer that replaced four copies of an in-container
    ``requests.get`` against the NOAA NCEI mosaic. Routing it agent-side is what
    gives the bed everything a container fetch could never have: the emit-on-fetch
    input layer (the bathymetry NATE asked to SEE, continuous rather than a lattice
    of sampled nodes), the read-through cache, the provenance record and the
    router's retry doctrine.

    ``px_per_deg`` is the SAMPLE LATTICE the builder's nodes are read against, so
    it is the builder's fact and travels from the template, not a default here.
    The bbox is the BOUND DOMAIN's, rounded exactly as the author rounds it: the
    raster a node is sampled from has to be the one the run describes, and a bbox
    that disagreed by a rounding step would sample a grid offset from the mesh.
    The lake gate reads the domain's CENTRE, while the author reads the AOI's own
    point; for a drawn or passed extent those are the same point, and for a
    geocoded place they differ by the AOI's 4-decimal rounding. A disagreement is
    therefore possible only within metres of a lake's edge, and it surfaces as the
    staging refusal in :func:`staged_bed_inputs`, never as a solve on a bed that
    is not there.

    An IDEALIZED domain - a Berkhoff shoal, a lock-exchange channel - has no
    geography to sample and fetches NOTHING: the returned record says so and the
    manifest stages no input.
    """
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime import current_domain

    domain = current_domain()
    extent = None if domain is None else domain.bbox
    if not extent or len(tuple(extent)) != 4:
        raise OpenWaterError(
            "the domain bed cannot be fetched: no domain with an extent is bound.",
            error_code="TELEMAC_PARAMS_INVALID")
    bbox = [round(float(v), 4) for v in extent]
    if not solves_on_real_bed(bathy_source,
                              lon=0.5 * (bbox[0] + bbox[2]),
                              lat=0.5 * (bbox[1] + bbox[3]),
                              mode=mode, real_bed_modes=real_bed_modes):
        return {"uri": None, "bbox": bbox, "source": "authored"}
    entry = TOOL_REGISTRY.get("fetch_ncei_dem_mosaic")
    if entry is None:
        raise OpenWaterError("fetch_ncei_dem_mosaic is not registered.",
                             error_code="TELEMAC_STAGING_FAILED")
    layer = await asyncio.to_thread(
        lambda: entry.fn(bbox=bbox, px_per_deg=float(px_per_deg),
                         max_px_per_side=int(max_px_per_side),
                         purpose="TELEMAC open-water bed elevation at mesh nodes"))
    uri = getattr(layer, "uri", None)
    if not uri:
        raise OpenWaterError(
            f"the domain bed fetch returned no raster for bbox={bbox}.",
            error_code="TELEMAC_STAGING_FAILED")
    return {"uri": str(uri), "bbox": bbox, "px_per_deg": float(px_per_deg),
            "source": "noaa_ncei_dem_all",
            "name": getattr(layer, "name", None)}


def staged_bed_inputs(bed: Mapping[str, Any] | None, *, real: bool,
                      section: str) -> list[dict[str, str]]:
    """The manifest ``inputs`` row that puts the fetched bed in the run directory.

    An idealized domain stages nothing, because it samples nothing. A REAL domain
    with no bed raster is a refusal rather than a silent fall-through: the worker
    holds no fetcher any more, so a missing staged bed would surface as a solve on
    whatever the builder does with an absent file rather than as the staging
    failure it is.
    """
    if not real:
        return []
    uri = (bed or {}).get("uri")
    if not uri:
        raise OpenWaterError(
            f"the TELEMAC {section} domain is solved on real bathymetry but no bed "
            "raster was staged; the worker fetches nothing of its own, so there is "
            "no bed to sample.", error_code="TELEMAC_STAGING_FAILED")
    return [{"gs_uri": str(uri), "dest": STAGED_BED_DEST}]


def case_section(*, module: str, steering: str, results: list[str],
                 server_facts: Mapping[str, Any], user_fortran: str | None = None,
                 coupling: str | None = None,
                 continue_from: str | None = None) -> dict[str, Any]:
    """The CASE a worker runs: which engine, which file, what it must produce.

    ``module`` names the engine binary, ``steering`` the authored file it reads,
    and ``results`` every file that must exist for the run to have succeeded.
    ``coupling`` names the module the steering file couples the solve with, because
    which
    runner can drive a coupled case is not the same question for every module and
    the worker decides on this word. ``continue_from`` is the staged name of the
    previous run's results the steering file restarts from, present only on a
    continued
    run.

    ``server_facts`` is what the SERVER already knows and the worker cannot learn
    from the files it is handed - the UTM zone, the bbox, the node and element
    counts, the edge the mesh was measured at, which dataset the bed came from.
    The worker copies it into its metrics VERBATIM: a fact re-derived in the
    container is a second answer that can disagree with the first.
    """
    return {"module": module, "steering": steering,
            **({"user_fortran": user_fortran} if user_fortran else {}),
            **({"coupling": coupling} if coupling else {}),
            **({"continue_from": continue_from} if continue_from else {}),
            "results": list(results), "server_facts": dict(server_facts)}


def stage_telemac_manifest(*, section: str, config: Mapping[str, Any],
                           run_tag: str, outputs: list[str],
                           inputs: list[dict[str, str]] | None = None,
                           prefix: str | None = None,
                           extra: Mapping[str, Any] | None = None) -> str:
    """Write the worker manifest to the cache bucket and return its ``s3://`` URI.

    THE manifest writer for the whole family. ``section`` is the key the worker's
    ENTRYPOINT dispatches on. ``prefix`` is where the manifest is STAGED, and it
    is not always the same word - the harbour module answers to ``agitation``
    inside the document while its manifests live under ``artemis/``. Collapsing
    the two into one name is how a manifest lands somewhere the worker looks and
    carries a key it does not read, which is a silent fall-through rather than an
    error.

    ``inputs`` is what the launcher stages into the run directory before the
    container starts, ``{gs_uri, dest}`` per entry. It carries everything these
    domains used to fetch for themselves, which is why the worker needs no
    network. An authored run's section is ``case`` - see :func:`case_section`.
    """
    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise OpenWaterError(
            "TRID3NT_CACHE_BUCKET must be set to stage the TELEMAC manifest.",
            error_code="TELEMAC_STAGING_FAILED")
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    manifest = {section: dict(config), "run_id": run_tag,
                "inputs": list(inputs or []), "telemac_args": [],
                "outputs": list(outputs), **dict(extra or {})}
    key = f"{prefix or section}/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


async def dispatch_and_wait(*, solver: str, manifest_uri: str, compute_class: str,
                           label: str, timeout_s: float,
                           grid_resolution_m: float | None = None,
                           active_cell_count: int | None = None) -> tuple[Any, str]:
    """Dispatch a staged manifest, drive the cards, wait, and hand back the result.

    The supervision dance every TELEMAC front performs identically: mint the
    dispatch and sim cards, bind the emitter so the worker's own progress reaches
    them, poll to completion, and route the terminal card whichever way the run
    ends - CANCELLED included, which is the clause a hand-copied version drops.
    Returns ``(run_result, batch_run_id)`` and judges nothing: what a non-complete
    status MEANS is the caller's typed error to raise, because the code it carries
    is the caller's contract.
    """
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
    )
    from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
    from trid3nt_server.workflows.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    emitter = current_emitter()
    handle = run_solver(solver=solver, model_setup_uri=manifest_uri,
                        compute_class=compute_class)
    run_id = handle.run_id
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=solver, handle=handle, compute_class=compute_class)
    if emitter is not None and sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=sim_step_id))
    progress = asyncio.ensure_future(drive_live_solve_progress(
        emitter=emitter, run_id=run_id, solver=solver,
        grid_resolution_m=grid_resolution_m, active_cell_count=active_cell_count,
        vcpus=None, eta_seconds=None))

    run_result = None
    try:
        run_result = await wait_for_completion(handle, timeout_s=timeout_s)
    except asyncio.CancelledError:
        logger.info("telemac %s solve cancelled awaiting solver", label)
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    finally:
        progress.cancel()
        try:
            await progress
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        set_emitter_binding(None)
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)
    return run_result, (getattr(run_result, "run_id", None) or run_id)


async def solve_open_water(*, run: dict[str, Any],
                           compute_class: str = "medium") -> dict[str, Any]:
    """Stage the run's manifest, dispatch it, wait, and return the run handle.

    The run carries its own solver, section, outputs and result file, so this is
    the ONE dispatch for every open-water TELEMAC domain. The returned ``uri`` is
    the result SELAFIN under the run prefix - what a ledger replay probes, so a
    resumed rerun can only skip the solve while the solved artifact is still there.

    ``run["requires_utm"]`` says whether a missing ``utm_epsg`` is a FAILURE.
    A domain built over real geography is ungeoreferenceable without the worker's
    zone, so its absence is a typed refusal. An IDEALIZED domain - the Berkhoff
    shoal, the lock-exchange channel - has no geographic footprint at all and
    legitimately reports no zone; its reader rasterizes the local metres in a
    placeholder frame instead. Refusing there refuses a run that is working
    exactly as designed.
    """
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket

    solver, section = run["solver"], run["section"]
    run_tag = run["run_tag"]
    manifest_uri = await asyncio.to_thread(
        stage_telemac_manifest, section=section, config=run["config"],
        run_tag=run_tag, outputs=run["outputs"], inputs=run.get("inputs"),
        prefix=run.get("prefix"))
    logger.info("telemac %s staged manifest run_tag=%s name=%s -> %s",
                section, run_tag, run["config"].get("name"), manifest_uri)

    run_result, batch_run_id = await dispatch_and_wait(
        solver=solver, manifest_uri=manifest_uri, compute_class=compute_class,
        label=section, timeout_s=_SOLVE_TIMEOUT_S,
        grid_resolution_m=run.get("mesh_size_m"))
    if run_result is None or run_result.status != "complete":
        raise OpenWaterError(
            f"the TELEMAC {section} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code=run.get("run_failed_code") or "TELEMAC_OPEN_WATER_FAILED")

    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    utm_epsg = metrics.get("utm_epsg")
    if utm_epsg is None and run.get("requires_utm", True):
        # A SELAFIN carries no CRS of its own, so a domain built over real
        # geography cannot be georeferenced at all without the worker's UTM zone -
        # a typed refusal, never a guessed zone.
        raise OpenWaterError(
            f"TELEMAC {section} run {batch_run_id} produced no utm_epsg; "
            "the result cannot be georeferenced.",
            error_code=run.get("output_missing_code") or "TELEMAC_OUTPUT_MISSING")
    return {
        "run_id": batch_run_id,
        "uri": f"s3://{_get_runs_bucket()}/{batch_run_id}/{run['result_basename']}",
        "utm_epsg": int(utm_epsg) if utm_epsg is not None else None,
        "metrics": metrics,
    }


def solved_domain_bbox(run: Mapping[str, Any],
                       metrics: Mapping[str, Any]) -> tuple[float, ...] | None:
    """The 4326 bbox the WORKER laid its local mesh frame over. ``None`` if none.

    The open-water builds put node 0 at the AOI's SW corner, so the reader has to
    add that exact corner back before reprojecting. "That exact corner" is the
    point: the author rounds the AOI to 4 decimals on its way into the manifest, so
    the ORIGINAL AOI is a few metres away from the one the worker meshed and
    offsets the whole field by that much. The worker's own report in
    ``telemac_metrics.json`` is the ground truth; the manifest's rounded bbox is
    what it was handed and is the fallback. The unrounded AOI is neither.

    An IDEALIZED domain has no geographic footprint and reports no bbox at all.
    """
    reported = (metrics or {}).get("bbox")
    staged = (run.get("config") or {}).get("bbox")
    for candidate in (reported, staged):
        if candidate is None:
            continue
        values = tuple(candidate)
        if len(values) != 4:
            raise OpenWaterError(
                f"the TELEMAC domain bbox {candidate!r} is not 4 values "
                "(min_lon, min_lat, max_lon, max_lat); the local mesh frame "
                "cannot be placed.", error_code="TELEMAC_PARAMS_INVALID")
        try:
            return tuple(float(v) for v in values)
        except (TypeError, ValueError) as exc:
            raise OpenWaterError(
                f"the TELEMAC domain bbox {candidate!r} carries non-numeric "
                "corners; the local mesh frame cannot be placed.",
                error_code="TELEMAC_PARAMS_INVALID") from exc
    return None


def download_open_water_result(run_id: str, basename: str,
                               *, error_code: str = "TELEMAC_OUTPUT_MISSING") -> str:
    """Download the run's result SELAFIN to a local path the postprocess can read."""
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    runs_bucket = _get_runs_bucket()
    local = str(Path(tempfile.mkdtemp(prefix=f"telemac-{run_id}-")) / basename)
    try:
        body = _get_s3_client().get_object(
            Bucket=runs_bucket, Key=f"{run_id}/{basename}")["Body"].read()
        with open(local, "wb") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        raise OpenWaterError(
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{run_id}/"
            f"{basename} was not downloadable: {exc}", error_code=error_code) from exc
    return local


#: The workers report ``dx_m`` rounded to 0.1 m, so any disagreement at or below
#: half of that last place is the REPORT's precision, not a move the builder made.
#: Without this an ask of 33.33 m built at 33.3 m raised a 3 cm "override" note.
_DX_REPORT_TOL_M = 0.05


def mesh_resolution_label(bed: str, run: Mapping[str, Any],
                         metrics: Mapping[str, Any], *, suffix: str = "") -> str:
    """What grid the run was SOLVED on, in one sentence, for the layer to carry.

    ONE f-string for every publisher, so the coarsening clause cannot drift
    between them. ``bed`` names what the bed IS (the one thing that genuinely
    differs per template); ``suffix`` carries an extra fact a domain has and the
    others do not, such as TELEMAC-3D's sigma-plane count.

    The spacing is the one the WORKER reports, falling back to the one the run
    asked for - never the other way round, or a run the node budget coarsened
    would advertise the spacing it did not use.
    """
    return (f"{bed} grid {metrics.get('dx_m', run['mesh_size_m']):g} m{suffix}"
            + (" (coarsened under node budget)" if metrics.get("coarsened") else ""))


def mesh_sizing_provenance(asked_m: Any, metrics: Mapping[str, Any]) -> list[Any]:
    """The user's grid-spacing ask, as a row, WHEN the worker MOVED it.

    A user lever the run quietly overrode is the silent-override class: the mesh
    was right and the label was a lie. The open-water builds move it two ways -
    the grid FLOOR raises an ask that is finer than the builder authors, and the
    node BUDGET raises one that is finer than the domain can afford - and neither
    said so, while the reach family has narrated exactly this since wave 2b.

    The row appears only when the ask and the built spacing differ by more than
    the report's own precision, so an honoured lever adds no noise, and it names
    the DIRECTION the value actually moved rather than assuming it was raised.
    ``basis="user"`` - the value came from the caller; the note says what happened.
    """
    from trid3nt_contracts.common import SyntheticInput

    built = metrics.get("dx_m")
    if asked_m is None or built is None:
        return []
    asked_f, built_f = float(asked_m), float(built)
    if abs(built_f - asked_f) <= _DX_REPORT_TOL_M:
        return []
    if built_f > asked_f:
        # Both coarsening paths RAISE the spacing. The node budget records itself;
        # anything else that raised a finer ask is the builder's own grid floor.
        direction = "RAISED"
        reason = ("the node budget for this domain" if metrics.get("coarsened")
                  else "the grid floor this builder authors")
    else:
        # Nothing in the open-water builds coarsens DOWNWARD, so a lowered spacing
        # came from the builder fitting the ask onto its own grid. Say that rather
        # than blaming a floor or a budget that did the opposite.
        direction = "LOWERED"
        reason = "the builder fitting the ask to this domain's grid"
    return [SyntheticInput(
        param="target_resolution_m", value=round(asked_f, 3), units="m",
        basis="user", consequence="numerical",
        note=(f"target_resolution_m {asked_f:g} {direction} to {built_f:g} m "
              f"by {reason}; the field was solved at {built_f:g} m"))]


class SolveOpenWater:
    """The open-water solve step. The plan's consequential node."""

    @staticmethod
    def telemac(*, run: Any, compute_class: Any) -> Step:
        """Dispatch a staged open-water run to its own TELEMAC worker."""
        return Step(runner=f"{_AUTHORING}.open_water.solve_open_water", stage="solve",
                    kwargs={"run": run, "compute_class": compute_class},
                    consequential=True)
