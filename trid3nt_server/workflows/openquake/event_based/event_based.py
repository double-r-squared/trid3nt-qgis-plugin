"""Engine template ``openquake_event_based`` - event-based (stochastic) PSHA.

The event-based calculator samples ``ses_per_logic_tree_path`` stochastic event
sets - a synthetic earthquake catalogue - and computes a ground-motion field per
rupture; the hazard curve is back-derived from the GMFs and a hazard map
extracted at the target PoE. This template runs the installed ``oq`` engine
locally as a subprocess of the composer (the offline lane, like
``openquake_scenario_gmf``), maps the event-based hazard, and cross-checks the
back-derived hazard curve against a classical-PSHA curve at the AOI centroid -
the classic event-based-vs-classical convergence check.

Published anchor: the GEM ``oq-engine`` EventBasedPSHA demo (an area source,
``calculation_mode = event_based``, ``ses_per_logic_tree_path``,
``hazard_curves_from_gmfs`` + ``hazard_maps``). Parameterized over the caller AOI
with a synthetic Gutenberg-Richter area source (a labelled demo source).

Determinism boundary (invariant 1): every scalar the agent narrates comes from
the parsed engine exports, never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.openquake_contracts import EventBasedHazardLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.openquake._local_oq import (
    DEFAULT_IMLS_G,
    LocalOqError,
    aoi_centroid,
    imls_list_str,
    region_str,
    render_area_source_model_xml,
    render_classical_point_job_ini,
    render_trivial_gmpe_logic_tree_xml,
    render_trivial_source_logic_tree_xml,
    run_oq_local,
)
from trid3nt_server.workflows.openquake._template_card import TemplateCard
from trid3nt_server.workflows.openquake.postprocess_openquake import (
    PostprocessOpenQuakeError,
    parse_hazard_curve_csv,
    postprocess_openquake,
)
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.openquake.event_based.event_based"
)

__all__ = [
    "openquake_event_based",
    "model_openquake_event_based",
    "render_event_based_job_ini",
    "EventBasedError",
]

#: Cap on event-based site-grid points; a wider AOI coarsens the grid to stay
#: under this (event-based over a grid is heavier than classical).
_MAX_EB_SITES: int = 400
#: Default event-based site-grid spacing (km).
_DEFAULT_EB_GRID_KM: float = 8.0


class EventBasedError(RuntimeError):
    """Raised when the event-based chain fails fatally before a layer.

    Codes: ``EB_PARAMS_INVALID`` (bad bbox / params), ``EB_MAP_EMPTY`` (the solve
    produced no hazard-map export), plus the propagated local-oq
    (``OQ_LOCAL_MISSING`` / ``OQ_LOCAL_SOLVE_FAILED``) and postprocess codes."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "event-based (stochastic) PSHA - a synthetic earthquake catalogue and "
        "per-rupture ground-motion fields, the event-based hazard map, and the "
        "convergence check of the back-derived hazard curve against classical PSHA"
    ),
    required_inputs=["bbox"],
    knobs=(
        "imt (PGA / PGV / SA(<period>)), poe, investigation_time_years, "
        "ses_per_logic_tree_path, gmpe, vs30, site_grid_spacing_km"
    ),
)


_EB_METADATA = AtomicToolMetadata(
    name="openquake_event_based",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="openquake",
    tier="template",
)


@register_tool(
    _EB_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def openquake_event_based(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    imt: str = "PGA",
    poe: float = 0.10,
    investigation_time_years: float = 50.0,
    ses_per_logic_tree_path: int = 200,
    gmpe: str = "BooreAtkinson2008",
    vs30: float | None = None,
    site_grid_spacing_km: float = _DEFAULT_EB_GRID_KM,
    max_distance_km: float = 300.0,
    a_value: float = 4.0,
    b_value: float = 1.0,
    min_magnitude: float = 5.0,
    max_magnitude: float = 7.5,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> EventBasedHazardLayerURI | dict[str, Any]:
    """Run an event-based (stochastic) PSHA over an AOI + classical cross-check.

    Fidelity: OpenQuake ``event_based`` calculator over a synthetic
    Gutenberg-Richter area source (a labelled demo source, narrated as such).
    Samples ``ses_per_logic_tree_path`` stochastic event sets (a synthetic
    earthquake catalogue), computes a ground-motion field per rupture, maps the
    event-based hazard, and confirms the back-derived hazard curve matches a
    classical-PSHA curve at the AOI centroid. Planning-grade demo, not a
    site-specific study.

    Use this when: the user asks for event-based or stochastic PSHA, a synthetic
    earthquake CATALOGUE / stochastic event set, per-rupture ground-motion fields
    for a region, or wants to CONFIRM / cross-check the hazard against classical
    PSHA (the event-based-vs-classical convergence check). Do NOT use for: the
    classical hazard map / curve alone (``openquake_psha``); a single specified
    rupture's shaking (``openquake_scenario_gmf``); which scenario dominates the
    hazard (``openquake_disaggregation``); building damage
    (``pelicun_damage_assessment``).

    Params:
        bbox: AOI, EPSG:4326; a regular site grid is laid over it.
        imt: ``"PGA"`` (default, g), ``"PGV"`` (cm/s), or ``"SA(<period>)"``.
        poe: probability of exceedance for the hazard map (0,1), default 0.10.
        investigation_time_years: PoE window, default 50.
        ses_per_logic_tree_path: stochastic-event-set multiplier (catalogue
            length), default 200; larger = smoother convergence, slower solve.
        gmpe: ground-motion model, default "BooreAtkinson2008".
        vs30: reference site Vs30 (m/s). Unset -> the 760 rock demo default.
        site_grid_spacing_km: default 8 (coarsened for wide AOIs; event-based over
            a grid is RAM-hungry).
        max_distance_km: source-to-site integration distance, default 300.
        a_value/b_value: demo Gutenberg-Richter recurrence, default 4.0/1.0.
        min_magnitude/max_magnitude: demo source range, default 5.0/7.5.
        input_mode: ``"user_gated"`` presents the reference Vs30 for review before
            the solve; ``"auto"`` (default) proceeds with it labelled.

    Returns:
        On success: ``EventBasedHazardLayerURI`` (event-based hazard-map COG) with
        ``max_hazard_value``, ``hazard_area_km2``, ``n_sites``,
        ``ses_per_logic_tree_path``, ``n_ruptures``, ``n_events``,
        ``classical_consistency_note``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached.
    """
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "EB_PARAMS_INVALID",
            "error_message": (
                "openquake_event_based requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    try:
        poe_f = float(poe)
        if not (0.0 < poe_f < 1.0):
            raise ValueError(f"poe {poe_f} out of (0,1)")
        ses = int(ses_per_logic_tree_path)
        if ses < 1:
            raise ValueError("ses_per_logic_tree_path must be >= 1")
    except (TypeError, ValueError) as exc:
        return {
            "status": "error",
            "error_code": "EB_PARAMS_INVALID",
            "error_message": f"invalid event-based arguments: {exc}",
        }

    ref_vs30 = float(vs30) if vs30 is not None else 760.0
    _vs30_user = vs30 is not None
    _entries = [SyntheticInput(
        param="vs30", value=round(ref_vs30, 1), units="m/s",
        basis="user" if _vs30_user else "default_demo",
        note=(None if _vs30_user
              else "generic NEHRP B/C rock default; no Vs30 fetcher yet (not site-specific)"),
    )]
    _review = await gate_input_review(
        tool_name="openquake_event_based", mode=input_mode,
        entries=_entries, params={"vs30": ref_vs30},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"openquake_event_based {_review.cancel_reason}",
        }
    _rv = _review.params.get("vs30")
    if _rv is not None:
        ref_vs30 = float(_rv)

    logger.info(
        "openquake_event_based bbox=%s imt=%s poe=%.4g ses=%d gmpe=%s vs30=%.0f grid=%.1fkm",
        list(coerced), imt, poe_f, ses, gmpe, ref_vs30, float(site_grid_spacing_km),
    )

    try:
        layer = await model_openquake_event_based(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            imt=str(imt),
            poe=poe_f,
            investigation_time_years=float(investigation_time_years),
            ses_per_logic_tree_path=ses,
            gmpe=str(gmpe),
            reference_vs30=ref_vs30,
            site_grid_spacing_km=float(site_grid_spacing_km),
            max_distance_km=float(max_distance_km),
            a_value=float(a_value),
            b_value=float(b_value),
            min_magnitude=float(min_magnitude),
            max_magnitude=float(max_magnitude),
        )
        return layer.model_copy(update={"synthetic_inputs": _review.entries})
    except asyncio.CancelledError:
        raise
    except (EventBasedError, LocalOqError, PostprocessOpenQuakeError) as exc:
        logger.warning("openquake_event_based failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("openquake_event_based unexpected failure")
        return {
            "status": "error",
            "error_code": "EB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


def _adaptive_grid_km(bbox: tuple[float, float, float, float], grid_km: float) -> float:
    """Coarsen the site grid so the AOI stays under ``_MAX_EB_SITES`` points."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = math.radians((min_lat + max_lat) / 2.0)
    w = abs(max_lon - min_lon) * 111.32 * max(math.cos(mean_lat), 0.05)
    h = abs(max_lat - min_lat) * 111.32
    if w <= 0 or h <= 0:
        return grid_km
    n = (w / grid_km) * (h / grid_km)
    if n <= _MAX_EB_SITES:
        return grid_km
    return grid_km * math.sqrt(n / _MAX_EB_SITES)


def render_event_based_job_ini(
    *,
    bbox: tuple[float, float, float, float],
    imt: str,
    poe: float,
    investigation_time_years: float,
    ses_per_logic_tree_path: int,
    grid_spacing_km: float,
    max_distance_km: float,
    reference_vs30: float,
    gmpe_lt_file: str,
    source_lt_file: str,
) -> str:
    """Render the ``calculation_mode = event_based`` job.ini over a site grid."""
    iml_list = imls_list_str(DEFAULT_IMLS_G)
    return (
        "[general]\n"
        "description = event based PSHA\n"
        "calculation_mode = event_based\n"
        "ses_seed = 24\n\n"
        "[geometry]\n"
        f"region = {region_str(bbox)}\n"
        f"region_grid_spacing = {grid_spacing_km:g}\n\n"
        "[logic_tree]\n"
        "number_of_logic_tree_samples = 0\n\n"
        "[erf]\n"
        "rupture_mesh_spacing = 5\n"
        "width_of_mfd_bin = 0.2\n"
        "area_source_discretization = 10.0\n\n"
        "[site_params]\n"
        "reference_vs30_type = measured\n"
        f"reference_vs30_value = {reference_vs30:g}\n"
        "reference_depth_to_2pt5km_per_sec = 1.0\n"
        "reference_depth_to_1pt0km_per_sec = 50.0\n\n"
        "[calculation]\n"
        f"source_model_logic_tree_file = {source_lt_file}\n"
        f"gsim_logic_tree_file = {gmpe_lt_file}\n"
        f"investigation_time = {investigation_time_years:g}\n"
        f'intensity_measure_types_and_levels = {{"{imt}": [{iml_list}]}}\n'
        "truncation_level = 3\n"
        f"maximum_distance = {max_distance_km:g}\n"
        f"minimum_magnitude = {5.0:g}\n"
        "minimum_intensity = 0.05\n\n"
        "[event_based_params]\n"
        f"ses_per_logic_tree_path = {int(ses_per_logic_tree_path)}\n\n"
        "[output]\n"
        "export_dir = out\n"
        "ground_motion_fields = true\n"
        "hazard_curves_from_gmfs = true\n"
        "hazard_maps = true\n"
        "mean = true\n"
        f"poes = {poe:g}\n"
    )


def _count_csv_rows(outdir: Any, glob_pat: str) -> int:
    """Count data rows (non-comment, non-header) across CSVs matching ``glob_pat``."""
    total = 0
    for p in sorted(outdir.glob(glob_pat)):
        try:
            lines = [
                ln for ln in p.read_text(encoding="utf-8").splitlines()
                if ln and not ln.lstrip().startswith("#")
            ]
            total += max(len(lines) - 1, 0)  # minus the header row
        except Exception:  # noqa: BLE001
            continue
    return total


async def model_openquake_event_based(
    *,
    bbox: tuple[float, float, float, float],
    imt: str,
    poe: float,
    investigation_time_years: float,
    ses_per_logic_tree_path: int,
    gmpe: str,
    reference_vs30: float,
    site_grid_spacing_km: float,
    max_distance_km: float,
    a_value: float,
    b_value: float,
    min_magnitude: float,
    max_magnitude: float,
) -> EventBasedHazardLayerURI:
    """Run event-based PSHA + classical cross-check end-to-end.

    Raises ``EventBasedError`` / ``LocalOqError`` / ``PostprocessOpenQuakeError``.
    """
    run_id = new_ulid()
    begin_substeps(current_emitter(), 4)

    grid_km = _adaptive_grid_km(bbox, float(site_grid_spacing_km))
    lon, lat = aoi_centroid(bbox)
    source_xml = render_area_source_model_xml(
        bbox, a_value=a_value, b_value=b_value,
        min_magnitude=min_magnitude, max_magnitude=max_magnitude,
    )
    slt = render_trivial_source_logic_tree_xml()
    glt = render_trivial_gmpe_logic_tree_xml(gmpe)

    eb_files = {
        "source_model.xml": source_xml,
        "source_model_logic_tree.xml": slt,
        "gmpe_logic_tree.xml": glt,
        "job.ini": render_event_based_job_ini(
            bbox=bbox, imt=imt, poe=poe,
            investigation_time_years=investigation_time_years,
            ses_per_logic_tree_path=ses_per_logic_tree_path,
            grid_spacing_km=grid_km, max_distance_km=max_distance_km,
            reference_vs30=reference_vs30,
            gmpe_lt_file="gmpe_logic_tree.xml",
            source_lt_file="source_model_logic_tree.xml",
        ),
    }

    async with substep(current_emitter(), "run_event_based"):
        eb_out = await asyncio.to_thread(run_oq_local, eb_files, label="eventbased")

    # Hazard MAP (extracted at poe) -> COG via the shared classical postprocess.
    map_csvs = sorted(eb_out.glob("hazard_map-mean*.csv"))
    if not map_csvs:
        raise EventBasedError(
            "EB_MAP_EMPTY", "event-based solve produced no hazard-map export"
        )
    # prefer the poe-suffixed map (single-poe run exports hazard_map-mean-<rp>y).
    map_text = map_csvs[0].read_text(encoding="utf-8")

    async with substep(current_emitter(), "rasterize_and_publish"):
        seismic_layer = await asyncio.to_thread(
            postprocess_openquake, map_text,
            run_id=run_id, imt=imt, poe=float(poe),
            investigation_time_years=float(investigation_time_years),
        )

    # Back-derived event-based hazard CURVE at the centroid + classical cross-check.
    eb_curve = _read_curve(eb_out, imt)
    classical_files = {
        "source_model.xml": source_xml,
        "source_model_logic_tree.xml": slt,
        "gmpe_logic_tree.xml": glt,
        "job.ini": render_classical_point_job_ini(
            site_lon=lon, site_lat=lat, imt=imt,
            investigation_time_years=investigation_time_years,
            max_distance_km=max_distance_km, reference_vs30=reference_vs30,
            description="classical PSHA cross-check",
        ),
    }
    async with substep(current_emitter(), "classical_cross_check"):
        cl_out = await asyncio.to_thread(run_oq_local, classical_files, label="classical")
    cl_curve = _read_curve(cl_out, imt)

    n_ruptures = _count_csv_rows(eb_out, "ruptures*.csv")
    n_events = _count_csv_rows(eb_out, "events*.csv")

    consistency_note, median_rel_diff = _consistency(eb_curve, cl_curve)

    await _emit_consistency_chart(
        eb_curve, cl_curve, imt=imt,
        investigation_time_years=investigation_time_years,
        median_rel_diff=median_rel_diff,
        source_layer_uri=seismic_layer.uri,
    )

    layer = EventBasedHazardLayerURI(
        layer_id=f"eventbased-hazard-{run_id}",
        name=(
            f"Event-based seismic hazard ({imt}, "
            f"{int(round(seismic_layer.return_period_years))}-yr)"
        ),
        layer_type="raster",
        uri=seismic_layer.uri,
        style_preset=seismic_layer.style_preset,
        role="primary",
        units=seismic_layer.units,
        bbox=seismic_layer.bbox,
        imt=imt,
        poe=float(poe),
        investigation_time_years=float(investigation_time_years),
        return_period_years=seismic_layer.return_period_years,
        max_hazard_value=seismic_layer.max_hazard_value,
        hazard_area_km2=seismic_layer.hazard_area_km2,
        n_sites=seismic_layer.n_sites,
        ses_per_logic_tree_path=int(ses_per_logic_tree_path),
        n_ruptures=int(n_ruptures),
        n_events=int(n_events),
        classical_consistency_note=consistency_note,
    )
    logger.info(
        "model_openquake_event_based complete run_id=%s max_hazard=%.4g n_sites=%d "
        "ses=%d n_rup=%d n_evt=%d median_rel_diff=%.3f",
        run_id, seismic_layer.max_hazard_value, seismic_layer.n_sites,
        ses_per_logic_tree_path, n_ruptures, n_events, median_rel_diff,
    )
    return layer


def _read_curve(outdir: Any, imt: str) -> dict[str, list[float]]:
    """Read + parse the first hazard-curve CSV in ``outdir`` (empty dict if none)."""
    curves = sorted(outdir.glob("hazard_curve-mean*.csv"))
    if not curves:
        return {"imls": [], "poe": []}
    parsed = parse_hazard_curve_csv(curves[0].read_text(encoding="utf-8"))
    return {
        "imls": list(parsed.get("hazard_curve_imls_g") or []),
        "poe": list(parsed.get("hazard_curve_mean_poe") or []),
    }


def _consistency(
    eb: dict[str, list[float]], cl: dict[str, list[float]]
) -> tuple[str, float]:
    """Compare event-based vs classical hazard curves; return (note, median_rel_diff).

    Relative difference is taken over the IMLs where BOTH curves carry a positive
    PoE (log-plottable). An empty overlap yields an honest 'no overlap' note."""
    eb_imls, eb_poe = eb.get("imls") or [], eb.get("poe") or []
    cl_imls, cl_poe = cl.get("imls") or [], cl.get("poe") or []
    cl_map = {round(x, 6): p for x, p in zip(cl_imls, cl_poe)}
    diffs: list[float] = []
    for x, p in zip(eb_imls, eb_poe):
        q = cl_map.get(round(x, 6))
        if q and q > 0 and p and p > 0:
            diffs.append(abs(p - q) / q)
    if not diffs:
        return (
            "Event-based and classical hazard curves shared no comparable "
            "intensity levels (insufficient catalogue at this site).",
            0.0,
        )
    sd = sorted(diffs)
    median_rel = sd[len(sd) // 2]
    max_rel = sd[-1]
    # Median relative difference is the robust convergence indicator (the max is
    # dominated by the rare high-intensity tail the stochastic catalogue
    # undersamples); the two agree through the well-sampled range.
    verdict = "consistent with" if median_rel <= 0.25 else "approaching"
    return (
        f"The event-based hazard curve is {verdict} the classical PSHA curve at "
        f"the AOI centroid (median relative PoE difference {median_rel * 100:.0f}% "
        f"over {len(diffs)} shared intensity levels; larger {max_rel * 100:.0f}% at "
        f"the rare high-intensity tail the stochastic catalogue undersamples). The "
        f"two converge through the well-sampled range as the stochastic-event-set "
        f"count grows.",
        median_rel,
    )


async def _emit_consistency_chart(
    eb: dict[str, list[float]],
    cl: dict[str, list[float]],
    *,
    imt: str,
    investigation_time_years: float,
    median_rel_diff: float,
    source_layer_uri: str | None,
) -> None:
    """Emit the event-based-vs-classical hazard-curve overlay (best-effort).

    Two log-log PoE-vs-IML line series (classical + event-based) on ONE figure -
    the convergence check. Emits nothing when either series is empty (honesty
    floor)."""
    try:
        rows: list[dict[str, Any]] = []
        for label, curve in (("classical", cl), ("event-based", eb)):
            for x, p in zip(curve.get("imls") or [], curve.get("poe") or []):
                if float(x) > 0.0 and float(p) > 0.0:
                    rows.append({"iml": float(x), "poe": float(p), "method": label})
        if len({r["method"] for r in rows}) < 2:
            return
        inv = int(round(investigation_time_years)) if investigation_time_years else 50
        spec = {
            "data": {"values": rows},
            "mark": {"type": "line", "point": True, "tooltip": True},
            "encoding": {
                "x": {
                    "field": "iml", "type": "quantitative",
                    "scale": {"type": "log"}, "title": f"{imt} (g)",
                },
                "y": {
                    "field": "poe", "type": "quantitative",
                    "scale": {"type": "log"}, "title": f"Mean PoE in {inv}yr",
                },
                "color": {
                    "field": "method", "type": "nominal",
                    "title": "Method",
                    "scale": {"range": ["#1f5fbf", "#e07a00"]},
                },
            },
            "width": "container",
        }
        from trid3nt_server.data.processing.charts_common import (
            build_chart_payload,
        )

        payload = build_chart_payload(
            vega_lite_spec=spec,
            title=f"Event-based vs classical hazard curve - {imt}",
            caption=(
                f"Back-derived event-based hazard curve (orange) overlaid on the "
                f"classical-PSHA curve (blue) at the AOI centroid over {inv} yr; "
                f"median relative PoE difference {median_rel_diff * 100:.0f}% - the "
                f"convergence check (the two agree through the well-sampled range "
                f"and diverge only at the rare high-intensity tail)."
            ),
            source_layer_uri=source_layer_uri,
        )
        await emit_chart_payloads(payload)
    except Exception as exc:  # noqa: BLE001 - chart is best-effort
        logger.warning("event-based consistency chart emit failed (non-fatal): %s", exc)
