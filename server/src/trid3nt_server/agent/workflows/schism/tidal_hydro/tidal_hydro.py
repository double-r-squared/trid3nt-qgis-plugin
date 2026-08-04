"""Engine template ``schism_tidal_hydro`` -- SCHISM barotropic tidal hydrodynamics
(cross-scale coastal core; engine #12 landing, ADR 0118).

The LLM-facing exposure of SCHISM's refinement-grade cross-scale coastal solver,
scoped to the ONE archetype that needs NO external forcing legs: a BAROTROPIC
tidal circulation driven by an analytical/constituent open boundary. Two mesh
sources:

  * ``bundled_quarterannulus`` -- SCHISM's own Test_QuarterAnnulus verification
    case (Lynch & Gray analytical M2 tidal channel). The staged deck is the
    spike's proven-green fixture; the deliverable is the analytical RMSE/amplitude
    VERIFICATION at the station point (ADR 0115 re-proven through the product
    path). An idealized, NON-GEOGRAPHIC mesh.
  * ``coastal_tin`` -- an oceanmesh ``coastal_tin`` TIN for a real US coastal AOI,
    bathymetry sampled from fetch_topobathy/fetch_dem onto the TIN nodes (the
    ``tin_to_hgrid`` bridge), a constituent tidal boundary. The deliverable is a
    max water-surface elevation surface CLIPPED to the AOI + COG (ADR 0116) + the
    mesh preview + a station elevation-timeseries chart.

Determinism boundary (invariant 1): every elevation/RMSE number the agent narrates
comes from the typed ``SchismElevationLayerURI`` fields the postprocess computed --
never free-generated. SCHISM is LOCAL-DOCKER ONLY (the solver lives in the worker
image), so the composer dispatches through the generic run_solver seam.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.schism_contracts import (
    SCHISM_INPUT_INVALID,
    SCHISM_SOLVE_FAILED,
    SchismElevationLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.workflows.schism._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.schism.tidal_hydro.tidal_hydro"
)

__all__ = [
    "schism_tidal_hydro",
    "model_schism_tidal_hydro",
    "SchismScenarioError",
    "TEMPLATE_CARD",
]

#: The LOUD verification-geometry honesty floor stamped on QuarterAnnulus results.
_QA_NOTE: str = (
    "VERIFICATION GEOMETRY: this is SCHISM's own Test_QuarterAnnulus (Lynch-Gray "
    "analytical M2 tidal channel) -- an IDEALIZED, NON-GEOGRAPHIC mesh with a "
    "bundled analytical solution. It PROVES the barotropic solver reproduces the "
    "published tidal solution (the RMSE/amplitude gate), NOT tides at a real AOI. "
    "For a real US coastal AOI use mesh_source='coastal_tin'."
)

#: The bathymetry-source honesty floor for a coastal_tin run.
_COASTAL_NOTE_TMPL: str = (
    "BAROTROPIC TIDAL SCREENING on an oceanmesh coastal TIN: bathymetry sampled "
    "from {bathy_source} onto the mesh nodes; a spatially-uniform {constituents} "
    "tidal boundary (amplitude {amp} m) -- a screening tidal forcing, NOT a "
    "FES2014/TPXO per-node field. Surge/waves/compound flooding are off-scope "
    "(sfincs_flood for fast screening)."
)


class SchismScenarioError(RuntimeError):
    """Raised when the SCHISM chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "barotropic TIDAL circulation on an unstructured coastal mesh (SCHISM, the "
        "refinement-grade cross-scale coastal-hydrodynamics core): a max "
        "water-surface elevation surface + tidal range, forced by an "
        "analytical/constituent tide -- either SCHISM's QuarterAnnulus verification "
        "case (analytical RMSE gate) or an oceanmesh TIN for a real US coastal AOI"
    ),
    required_inputs=[],  # bundled verification mesh is self-contained
    knobs="mesh_source, location_query/bbox, constituents, tidal_amplitude_m, sim_days, input_mode",
)


_SCHISM_METADATA = AtomicToolMetadata(
    name="schism_tidal_hydro",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="schism",
    tier="template",
)


@register_tool(
    _SCHISM_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def schism_tidal_hydro(
    mesh_source: str = "bundled_quarterannulus",
    location_query: str | None = None,
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    constituents: list[str] | None = None,
    tidal_amplitude_m: float = 0.5,
    sim_days: float = 5.0,
    open_boundary_side: str = "south",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SchismElevationLayerURI | dict[str, Any]:
    """A REFINEMENT-GRADE BAROTROPIC TIDAL circulation on an unstructured coastal mesh (SCHISM).

    Fidelity: SCHISM (the semi-implicit cross-scale unstructured-grid hydrodynamic
    model behind NOAA STOFS-3D) barotropic tidal core -- refinement-grade coastal
    hydrodynamics. Forced by an ANALYTICAL/CONSTITUENT tide (no external forcing
    legs). Returns a MAX WATER-SURFACE ELEVATION surface + tidal range.

    THE tool for "run a SCHISM tidal simulation", "tidal circulation on an
    unstructured coastal mesh", "barotropic tide with SCHISM", "coastal ocean model
    SCHISM", "cross-scale coastal hydrodynamics". Two mesh sources:
      - ``bundled_quarterannulus`` (default): SCHISM's OWN QuarterAnnulus
        verification case -- reproduces the published analytical M2 solution
        (RMSE/amplitude gate). An idealized, non-geographic verification mesh.
      - ``coastal_tin``: an oceanmesh TIN for a REAL US coastal AOI (name it via
        ``location_query`` or ``bbox``), bathymetry sampled from our terrain tools,
        a constituent tidal boundary -> a clipped max-elevation COG + mesh + chart.

    Do NOT use this for:
        - FAST arbitrary-AOI flood screening -- use ``sfincs_flood`` (SFINCS).
        - Storm SURGE, wind WAVES, or COMPOUND coastal flooding -- those need the
          forcing legs (atmosphere/ocean/waves) and are the coming SCHISM
          candidates, NOT this barotropic-tidal archetype.
        - Riverine flood (``hecras_muncie_flood`` / ``sfincs_flood``), urban
          drainage (``swmm_urban_flood``), or tsunami (``geoclaw_inundation``).

    Params:
        mesh_source: ``"bundled_quarterannulus"`` (verification) or
            ``"coastal_tin"`` (real US coastal AOI).
        location_query / bbox: the coastal AOI for ``coastal_tin`` (a place name
            geocoded to a bbox, or an explicit EPSG:4326
            ``[min_lon, min_lat, max_lon, max_lat]``). Ignored for the bundled mesh.
        constituents: tidal constituents the boundary drives (default ``["M2"]``;
            allowed M2 S2 N2 K2 K1 O1 P1 Q1).
        tidal_amplitude_m: open-boundary tidal elevation amplitude (metres) for a
            ``coastal_tin`` run (a plausible US-coast value is ~0.15-0.7 m).
        sim_days: run length in days (default 5; the verification is 5 d).
        open_boundary_side: which TIN side is the open (seaward) tidal boundary
            (``south|north|east|west``; default ``south``).
        input_mode: run-mode lever (ADR 0107). ``"user_gated"`` reviews the tidal
            forcing + mesh basis (and previews the TIN mesh) before solving.

    Returns:
        On success: ``SchismElevationLayerURI`` (``LayerURI`` subtype) -- the
        emitter loads the max-elevation COG beside the SCHISM mesh preview. Carries
        ``elev_max_m`` / ``elev_min_m`` / ``tidal_range_m`` / ``n_nodes`` /
        ``sim_days`` / ``mesh_source`` and (verification only) ``analytical_rmse_m``
        / ``analytical_amp_err_m`` / ``analytical_correlation`` (narrate these typed
        numbers only -- invariant 1).
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``.

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"``.
    """
    from trid3nt_contracts.schism_contracts import SCHISM_CONSTITUENTS, SCHISM_MESH_SOURCES

    if mesh_source not in SCHISM_MESH_SOURCES:
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": f"mesh_source must be one of {SCHISM_MESH_SOURCES}, got {mesh_source!r}"}
    cons = list(constituents) if constituents else ["M2"]
    bad = [c for c in cons if c not in SCHISM_CONSTITUENTS]
    if bad:
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": f"unknown tidal constituent(s) {bad}; allowed {SCHISM_CONSTITUENTS}"}
    try:
        tidal_amplitude_m = float(tidal_amplitude_m)
        sim_days = float(sim_days)
    except (TypeError, ValueError):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "tidal_amplitude_m and sim_days must be numbers"}
    if not (0.0 < tidal_amplitude_m <= 5.0) or not (1.0 <= sim_days <= 15.0):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "tidal_amplitude_m in (0,5] and sim_days in [1,15]"}

    bbox_t = tuple(float(v) for v in bbox) if bbox and len(bbox) == 4 else None
    logger.info(
        "schism_tidal_hydro mesh_source=%s location=%s constituents=%s amp=%.3g sim_days=%.3g mode=%s",
        mesh_source, location_query, cons, tidal_amplitude_m, sim_days, input_mode,
    )
    try:
        result = await model_schism_tidal_hydro(
            mesh_source=mesh_source, location_query=location_query, bbox=bbox_t,
            constituents=cons, tidal_amplitude_m=tidal_amplitude_m, sim_days=sim_days,
            open_boundary_side=open_boundary_side, input_mode=input_mode,
        )
        if isinstance(result, dict):
            return result
        logger.info(
            "schism_tidal_hydro complete layer_id=%s elev_max=%.3g tidal_range=%.3g rmse=%s uri=%s",
            result.layer_id, result.elev_max_m, result.tidal_range_m,
            result.analytical_rmse_m, result.uri,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SchismScenarioError as exc:
        logger.warning("schism_tidal_hydro failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("schism_tidal_hydro unexpected failure")
        return {"status": "error", "error_code": "SCHISM_INTERNAL_ERROR", "error_message": str(exc)}


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.agent.tools.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.agent.workflows.schism import deck_authoring
from trid3nt_server.agent.workflows.schism import postprocess_schism as pp
from trid3nt_server.agent.workflows.schism.run_schism import SCHISM_SOLVER_NAME


def _cache_bucket() -> str:
    b = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not b:
        raise SchismScenarioError(
            SCHISM_SOLVE_FAILED, "TRID3NT_CACHE_BUCKET must be set to stage the SCHISM manifest."
        )
    return b


def _stage_manifest(deck_files: list[Path], run_tag: str, *, ncompute: int, nscribe: int) -> str:
    """Upload the generated case files as manifest inputs[]; return the manifest s3 uri.

    Mirrors the SFINCS-builder upload pattern: each deck file -> a cache-bucket key
    -> an ``{"gs_uri": ..., "dest": ...}`` input entry (dest is the basename so the
    worker sees it at ``/data/<name>``). The manifest also carries the entrypoint's
    variant/ncompute/nscribe knobs + the outputs glob."""
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    cache_bucket = _cache_bucket()
    s3 = _get_s3_client()
    inputs = []
    for f in deck_files:
        key = f"schism/{run_tag}/{f.name}"
        with open(f, "rb") as fh:
            s3.put_object(Bucket=cache_bucket, Key=key, Body=fh.read())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": f.name})
    # the worker writes scribed output to outputs/; staout_* is the station file.
    manifest = {
        "variant": "hydro",
        "ncompute": int(ncompute),
        "nscribe": int(nscribe),
        "run_id": run_tag,
        "inputs": inputs,
        "schism_args": [],
        "outputs": ["outputs/*.nc", "outputs/staout_*", "schism_metrics.json"],
    }
    key = f"schism/{run_tag}/manifest.json"
    s3.put_object(Bucket=cache_bucket, Key=key,
                  Body=json.dumps(manifest, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _download_run_output(run_id: str, rel_key: str) -> str | None:
    """Download ``<run_id>/<rel_key>`` from the runs bucket to a temp file; None on miss."""
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )
    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=_get_runs_bucket(), Key=f"{run_id}/{rel_key}")
        tmp = tempfile.NamedTemporaryFile(suffix="_" + Path(rel_key).name, delete=False)
        tmp.write(obj["Body"].read())
        tmp.close()
        return tmp.name
    except Exception as exc:  # noqa: BLE001
        logger.info("schism: run output miss %s/%s: %s", run_id, rel_key, exc)
        return None


def _runs_uri(run_id: str, rel_key: str) -> str:
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_runs_bucket
    return f"s3://{_get_runs_bucket()}/{run_id}/{rel_key}"


async def model_schism_tidal_hydro(
    *,
    mesh_source: str,
    location_query: str | None,
    bbox: tuple[float, float, float, float] | None,
    constituents: list[str],
    tidal_amplitude_m: float,
    sim_days: float,
    open_boundary_side: str,
    input_mode: str | None,
) -> SchismElevationLayerURI | dict[str, Any]:
    """Author/stage deck -> input+mesh gate -> solve -> postprocess -> publish."""
    emitter = current_emitter()
    begin_substeps(emitter, 3)  # run_solver + postprocess + publish

    workdir = Path(tempfile.mkdtemp(prefix="schism-deck-"))
    is_qa = mesh_source == "bundled_quarterannulus"

    # --- Stage 1: author/stage the deck + resolve the review provenance ------- #
    if is_qa:
        deck_files = deck_authoring.stage_quarterannulus_deck(workdir)
        fallback_note = _QA_NOTE
        review_entries = [
            SyntheticInput(param="mesh_source", value="bundled_quarterannulus",
                           basis="default_demo",
                           note="SCHISM Test_QuarterAnnulus verification mesh (idealized, non-geographic)"),
            SyntheticInput(param="tidal_boundary", value="M2 analytical (baked)",
                           basis="default_demo", note="the bundled bctides.in analytical M2 boundary"),
            SyntheticInput(param="sim_days", value=5.0, units="d", basis="default_demo",
                           note="the verification run length (past the 1-day ramp)"),
        ]
        n_nodes_grid = None
        n_elements_grid = None
        ncompute, nscribe = 2, 2
    else:
        deck_info = await _build_coastal_tin_deck(
            workdir, location_query=location_query, bbox=bbox, constituents=constituents,
            tidal_amplitude_m=tidal_amplitude_m, sim_days=sim_days,
            open_boundary_side=open_boundary_side, input_mode=input_mode, emitter=emitter,
        )
        deck_files = deck_info["deck_files"]
        fallback_note = deck_info["fallback_note"]
        review_entries = deck_info["review_entries"]
        n_nodes_grid = deck_info["n_nodes"]
        n_elements_grid = deck_info["n_elements"]
        ncompute, nscribe = 3, 2

    # --- Stage 2: the input-review gate (ADR 0107) ---------------------------- #
    review = await gate_input_review(
        tool_name="schism_tidal_hydro", mode=input_mode, entries=review_entries,
        params={"mesh_source": mesh_source, "tidal_amplitude_m": tidal_amplitude_m,
                "sim_days": sim_days},
    )
    if not review.proceed:
        return {"status": "error", "error_code": "SCHISM_INPUT_REVIEW_CANCELLED",
                "error_message": review.cancel_reason or "input review not approved; the solver did not run"}

    # --- Stage 3: stage manifest + dispatch ----------------------------------- #
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(
        _stage_manifest, deck_files, run_tag, ncompute=ncompute, nscribe=nscribe
    )
    logger.info("model_schism_tidal_hydro staged manifest run_tag=%s files=%d uri=%s",
                run_tag, len(deck_files), manifest_uri)

    from trid3nt_server.agent.tools.simulation.solver.solver import (
        run_solver, wait_for_completion,
    )
    handle = run_solver(solver=SCHISM_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class="medium")
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=SCHISM_SOLVER_NAME, handle=handle, compute_class="medium",
    )
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise SchismScenarioError(
            SCHISM_SOLVE_FAILED,
            "SCHISM solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )
    batch_run_id = getattr(run_result, "run_id", None) or run_tag

    # --- Stage 4: download out2d + station output, postprocess ---------------- #
    out2d_local = await asyncio.to_thread(_download_run_output, batch_run_id, "outputs/out2d_1.nc")
    if out2d_local is None:
        raise SchismScenarioError(SCHISM_SOLVE_FAILED,
                                  "SCHISM completed but outputs/out2d_1.nc was not downloadable")
    out2d_uri = _runs_uri(batch_run_id, "outputs/out2d_1.nc")
    try:
        async with substep(emitter, "postprocess_schism"):
            layers, metrics = await asyncio.to_thread(
                pp.postprocess_schism, out2d_local, out2d_uri, run_id=batch_run_id,
                mesh_source=mesh_source, sim_days=sim_days, constituents=constituents,
                n_nodes_grid=n_nodes_grid, n_elements_grid=n_elements_grid,
                fallback_note=fallback_note,
            )
    except pp.PostprocessSchismError as exc:
        raise SchismScenarioError(exc.error_code, str(exc)) from exc
    finally:
        try:
            Path(out2d_local).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    elev = layers[0]
    assert isinstance(elev, SchismElevationLayerURI)
    mesh_layer = layers[1] if len(layers) > 1 else None

    # --- Stage 4b: QuarterAnnulus analytical verification --------------------- #
    staout_local = await asyncio.to_thread(_download_run_output, batch_run_id, "outputs/staout_1")
    station_series = pp.read_station_series(staout_local) if staout_local else []
    if is_qa and staout_local:
        ana_path = deck_authoring.quarterannulus_fixture_dir() / "ForPlot_ana_elev.dat"
        verification = pp.verify_against_analytical(staout_local, ana_path)
        if verification:
            elev = elev.model_copy(update={
                "analytical_rmse_m": verification["rmse_m"],
                "analytical_amp_err_m": verification["amp_err_m"],
                "analytical_correlation": verification["correlation"],
                "station_elev_amplitude_m": verification["amp_modeled_m"],
            })
            logger.info(
                "QuarterAnnulus verification: RMSE=%.4f m amp_err=%.4f m amp=%.4f/%.4f corr=%.5f",
                verification["rmse_m"], verification["amp_err_m"],
                verification["amp_modeled_m"], verification["amp_analytical_m"],
                verification["correlation"],
            )
    if staout_local:
        try:
            Path(staout_local).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # --- Stage 5: publish the elevation COG (render chokepoint) --------------- #
    async with substep(emitter, "publish_layer"):
        elev = await asyncio.to_thread(_publish_elev_layer, elev, review.entries)

    # --- Best-effort: the SCHISM mesh preview (layer_type="mesh") ------------- #
    if mesh_layer is not None:
        try:
            await publish_input_layer(emitter, mesh_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism mesh preview emit skipped: %s", exc)

    # --- Best-effort: the station elevation-timeseries chart ------------------ #
    if emitter is not None and station_series:
        try:
            await _maybe_emit_station_chart(emitter, station_series, mesh_source, is_qa)
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism station chart skipped: %s", exc)

    # --- AUTHORITATIVE LAST zoom-to (geographic only) ------------------------- #
    if emitter is not None and elev.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(elev.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism zoom-to failed: %s", exc)

    return elev


async def _build_coastal_tin_deck(
    workdir: Path, *, location_query, bbox, constituents, tidal_amplitude_m, sim_days,
    open_boundary_side, input_mode, emitter,
) -> dict[str, Any]:
    """Generate a coastal TIN, sample bathymetry, and author the SCHISM deck.

    Raises SchismScenarioError with a typed code when a live dependency (an AOI, a
    shoreline shapefile, or a bathymetry raster) is unavailable -- an honest
    typed error, never a silent dead-end.
    """
    # 1. Resolve the AOI bbox.
    if bbox is None:
        if not location_query:
            raise SchismScenarioError(
                SCHISM_INPUT_INVALID,
                "coastal_tin needs a coastal AOI: pass location_query (a place name) or bbox",
            )
        from trid3nt_server.agent.tools.fetchers.socioeconomic.geocode_location.geocode_location import (
            geocode_location,
        )
        geo = geocode_location(location_query)
        bb = geo.get("bbox")
        if not bb or len(bb) != 4:
            raise SchismScenarioError(
                SCHISM_INPUT_INVALID, f"geocode_location({location_query!r}) returned no bbox")
        bbox = tuple(float(v) for v in bb)

    # 2. Generate the TIN via the mesh worker (shoreline from env/GSHHG).
    shoreline = os.environ.get("TRID3NT_GSHHG_SHP")
    if not shoreline or not Path(shoreline).exists():
        raise SchismScenarioError(
            SCHISM_INPUT_INVALID,
            "coastal_tin needs a shoreline shapefile: set TRID3NT_GSHHG_SHP to a "
            "worker-visible GSHHG L1 polygon shapefile (the oceanmesh coastal_tin input)",
        )
    from trid3nt_server.agent.mesh.coastal_tin import CoastalTinSpec, run_coastal_tin_worker

    tin_rundir = workdir / "tin"
    spec = CoastalTinSpec(
        bbox=bbox, shoreline_shp=f"/shoreline/{Path(shoreline).name}",
        min_edge_length_m=200.0, max_edge_length_m=2000.0, feature_size=True,
    )
    stats, _geojson = await asyncio.to_thread(
        run_coastal_tin_worker, spec, tin_rundir,
        mounts={str(Path(shoreline).parent): "/shoreline"},
    )
    mesh_npz = tin_rundir / "coastal_tin_mesh.npz"
    if not mesh_npz.exists():
        raise SchismScenarioError(
            SCHISM_SOLVE_FAILED,
            "the mesh worker did not emit coastal_tin_mesh.npz (raw nodes/cells) -- "
            "rebuild trid3nt-local/mesh:latest with the ADR 0118 additive change",
        )
    import numpy as np
    mesh = np.load(mesh_npz)
    points, cells = mesh["points"], mesh["cells"]

    # 3. Sample bathymetry onto the nodes. TRID3NT_SCHISM_BATHY_PATH is an
    # offline/test seam (a worker-visible local topobathy COG); else fetch it.
    bathy_override = os.environ.get("TRID3NT_SCHISM_BATHY_PATH")
    if bathy_override and Path(bathy_override).exists():
        dem_path, bathy_source = bathy_override, "local topobathy COG"
    else:
        dem_path, bathy_source = await _fetch_bathymetry_cog(bbox)
    depths = deck_authoring.sample_bathymetry_on_nodes(points, dem_path)

    # 4. Author the deck.
    deck = await asyncio.to_thread(
        deck_authoring.author_coastal_tin_deck, workdir / "case",
        points=points, cells=cells, depths=depths, constituents=constituents,
        tidal_amplitude_m=tidal_amplitude_m, sim_days=sim_days,
        open_boundary_side=open_boundary_side,
    )
    note = _COASTAL_NOTE_TMPL.format(
        bathy_source=bathy_source, constituents="+".join(constituents),
        amp=f"{tidal_amplitude_m:g}",
    )
    review_entries = [
        SyntheticInput(param="mesh_source", value="coastal_tin", basis="derived",
                       note=f"oceanmesh TIN, {deck['n_nodes']} nodes / {deck['n_elements']} elements"),
        SyntheticInput(param="bathymetry", value=bathy_source, basis="fetched",
                       real_source_if_any=bathy_source, note="sampled onto the TIN nodes (positive-down)"),
        SyntheticInput(param="tidal_amplitude_m", value=round(tidal_amplitude_m, 4), units="m",
                       basis="user" if tidal_amplitude_m != 0.5 else "default_demo",
                       note=f"uniform {'+'.join(constituents)} open-boundary amplitude (screening)"),
        SyntheticInput(param="sim_days", value=round(sim_days, 3), units="d",
                       basis="user" if sim_days != 5.0 else "default_demo"),
    ]
    return {
        "deck_files": deck["files"], "fallback_note": note, "review_entries": review_entries,
        "n_nodes": deck["n_nodes"], "n_elements": deck["n_elements"],
    }


async def _fetch_bathymetry_cog(bbox) -> tuple[str, str]:
    """Fetch a topobathy (else DEM) COG for the AOI; return (local_path, source_label)."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    for tool_name, label in (("fetch_topobathy", "topobathy"), ("fetch_dem", "DEM")):
        entry = TOOL_REGISTRY.get(tool_name)
        if entry is None:
            continue
        try:
            res = entry.fn(bbox=list(bbox))
            if asyncio.iscoroutine(res):
                res = await res
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism bathymetry %s failed: %s", tool_name, exc)
            continue
        uri = getattr(res, "uri", None) or (res.get("uri") if isinstance(res, dict) else None)
        if not uri:
            continue
        local = await asyncio.to_thread(_download_uri_to_tmp, uri)
        if local:
            return local, label
    raise SchismScenarioError(
        SCHISM_INPUT_INVALID,
        "could not fetch bathymetry (topobathy/DEM) for the coastal AOI -- the tidal "
        "mesh needs bathymetry sampled onto its nodes",
    )


def _download_uri_to_tmp(uri: str) -> str | None:
    """Download an s3:// COG uri to a temp file for rasterio sampling."""
    if not uri.startswith("s3://"):
        return uri if Path(uri).exists() else None
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client
    try:
        bucket, _, key = uri[len("s3://"):].partition("/")
        obj = _get_s3_client().get_object(Bucket=bucket, Key=key)
        tmp = tempfile.NamedTemporaryFile(suffix="_bathy.tif", delete=False)
        tmp.write(obj["Body"].read())
        tmp.close()
        return tmp.name
    except Exception as exc:  # noqa: BLE001
        logger.warning("schism bathymetry download failed %s: %s", uri, exc)
        return None


def _publish_elev_layer(
    elev: SchismElevationLayerURI, synthetic_inputs: list[SyntheticInput]
) -> SchismElevationLayerURI:
    """Publish the max-elevation COG through publish_layer + stamp provenance."""
    out = elev
    if synthetic_inputs:
        try:
            out = out.model_copy(update={"synthetic_inputs": list(synthetic_inputs)})
        except Exception:  # noqa: BLE001
            pass
    try:
        published_uri = publish_layer(
            layer_uri=out.uri, layer_id=out.layer_id, style_preset=out.style_preset,
        )
        return out.model_copy(update={"uri": published_uri})
    except PublishLayerError as exc:
        logger.warning("schism publish_layer FAILED layer_id=%s (%s) - returning raw COG",
                       out.layer_id, exc)
        return out


async def _maybe_emit_station_chart(
    emitter: Any, series: list[dict[str, float]], mesh_source: str, is_qa: bool
) -> None:
    """Station elevation-timeseries chart (the QuarterAnnulus station output is the spec)."""
    if not hasattr(emitter, "emit_chart"):
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    spec = {
        "data": {"values": series},
        "mark": {"type": "line", "color": "#1f5fbf"},
        "encoding": {
            "x": {"field": "t_hr", "type": "quantitative", "title": "time (hours)"},
            "y": {"field": "elev_m", "type": "quantitative", "title": "surface elevation (m)"},
        },
    }
    title = (
        "QuarterAnnulus station elevation (SCHISM vs analytical M2)"
        if is_qa else f"SCHISM station tidal elevation ({mesh_source})"
    )
    caption = (
        "The modeled water-surface elevation at the station point -- the barotropic "
        "tidal signal the SCHISM solve produced."
    )
    payload = build_chart_payload(vega_lite_spec=spec, title=title, caption=caption)
    await emitter.emit_chart(payload)
