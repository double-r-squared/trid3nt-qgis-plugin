"""Engine template ``openquake_disaggregation`` - seismic-hazard disaggregation.

The disaggregation calculator decomposes the hazard at a single site (the AOI
centroid) at a target probability of exceedance into contributions by magnitude,
distance, and epsilon (the GMPE residual in standard deviations) - the "which
earthquake scenario dominates my site's hazard" answer that a classical hazard
map cannot give. It runs the installed ``oq`` engine locally as a subprocess of
the composer (the offline lane, like ``openquake_scenario_gmf``), parses the
magnitude-distance-epsilon contribution matrix, surfaces the disaggregation site
as a point marker, and emits the magnitude-distance contribution heatmap.

Published anchor: the GEM ``oq-engine`` Disaggregation demo (a source model with
an area source, ``calculation_mode = disaggregation``, ``poes_disagg`` +
mag/distance/epsilon bin widths). This template parameterizes that deck over the
caller AOI, using a synthetic Gutenberg-Richter area source (a labelled demo
source, narrated as such).

Determinism boundary (invariant 1): every scalar the agent narrates
(``dominant_magnitude`` / ``dominant_distance_km`` / ``iml_at_poe`` ...) comes
from the parsed engine contribution matrix, never free-generated.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI, LegendClass, LegendKey
from trid3nt_contracts.openquake_contracts import DisaggregationLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.openquake._local_oq import (
    DEFAULT_IMLS_G,
    LocalOqError,
    aoi_centroid,
    imls_list_str,
    render_area_source_model_xml,
    render_trivial_gmpe_logic_tree_xml,
    render_trivial_source_logic_tree_xml,
    run_oq_local,
)
from trid3nt_server.workflows.openquake._template_card import TemplateCard
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.openquake.disaggregation.disaggregation"
)

__all__ = [
    "openquake_disaggregation",
    "model_openquake_disaggregation",
    "render_disaggregation_job_ini",
    "parse_mag_dist_eps_csv",
    "DisaggregationError",
    "DISAGG_SITE_STYLE_PRESET",
]

#: Style-preset label for the surfaced disaggregation-site point.
DISAGG_SITE_STYLE_PRESET = "disagg_site"


class DisaggregationError(RuntimeError):
    """Raised when the disaggregation chain fails fatally before a layer.

    Codes: ``DISAGG_PARAMS_INVALID`` (bad bbox / params), ``DISAGG_EMPTY`` (the
    solve produced no contribution matrix), plus the propagated local-oq codes
    (``OQ_LOCAL_MISSING`` / ``OQ_LOCAL_SOLVE_FAILED``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "seismic-hazard disaggregation - which earthquake magnitude / distance / "
        "epsilon scenario dominates a site's hazard at a return period (e.g. 10% "
        "in 50 years); the magnitude-distance contribution matrix"
    ),
    required_inputs=["bbox"],
    knobs=(
        "imt (PGA / PGV / SA(<period>)), poe, investigation_time_years, gmpe, "
        "vs30, mag_bin_width, distance_bin_width, num_epsilon_bins"
    ),
)


_DISAGG_METADATA = AtomicToolMetadata(
    name="openquake_disaggregation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="openquake",
    tier="template",
)


@register_tool(
    _DISAGG_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def openquake_disaggregation(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    imt: str = "PGA",
    poe: float = 0.10,
    investigation_time_years: float = 50.0,
    gmpe: str = "BooreAtkinson2008",
    vs30: float | None = None,
    mag_bin_width: float = 0.5,
    distance_bin_width: float = 20.0,
    num_epsilon_bins: int = 3,
    max_distance_km: float = 300.0,
    a_value: float = 4.0,
    b_value: float = 1.0,
    min_magnitude: float = 5.0,
    max_magnitude: float = 7.5,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> DisaggregationLayerURI | dict[str, Any]:
    """Disaggregate a site's seismic hazard by magnitude, distance, and epsilon.

    Fidelity: OpenQuake ``disaggregation`` calculator over a synthetic
    Gutenberg-Richter area source (a labelled demo source, narrated as such) at
    the AOI centroid. Answers "which earthquake scenario (magnitude-distance-
    epsilon) contributes most to the hazard at this return period" - the
    decomposition a classical hazard map cannot give. Planning-grade demo, not a
    site-specific seismic-source study.

    Use this when: the user asks which magnitude / distance / scenario DOMINATES
    or DRIVES the seismic hazard at a site, for a hazard disaggregation, a
    magnitude-distance (M-R) deaggregation, or "what earthquake should I design
    for" at a return period (10% in 50 years / 475-yr). Do NOT use for: the
    hazard MAP / curve itself (``openquake_psha``); a specific scenario rupture's
    shaking (``openquake_scenario_gmf``); building damage
    (``pelicun_damage_assessment``).

    Params:
        bbox: AOI, EPSG:4326; the disaggregation runs at its centroid.
        imt: ``"PGA"`` (default, g), ``"PGV"`` (cm/s), or ``"SA(<period>)"``.
        poe: probability of exceedance to disaggregate at (0,1), default 0.10.
        investigation_time_years: PoE window, default 50.
        gmpe: ground-motion model, default "BooreAtkinson2008".
        vs30: reference site Vs30 (m/s). Unset -> the 760 rock demo default.
        mag_bin_width: magnitude bin width, default 0.5.
        distance_bin_width: distance bin width (km), default 20.
        num_epsilon_bins: number of epsilon (GMPE-residual) bins, default 3.
        max_distance_km: source-to-site integration distance, default 300.
        a_value/b_value: demo Gutenberg-Richter recurrence, default 4.0/1.0.
        min_magnitude/max_magnitude: demo source range, default 5.0/7.5.
        input_mode: ``"user_gated"`` presents the reference Vs30 for review before
            the solve; ``"auto"`` (default) proceeds with it labelled.

    Returns:
        On success: ``DisaggregationLayerURI`` (disaggregation-site point marker)
        with ``iml_at_poe``, ``dominant_magnitude``, ``dominant_distance_km``,
        ``dominant_epsilon``, ``mean_magnitude``, ``mean_distance_km``, ``n_bins``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached.
    """
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "DISAGG_PARAMS_INVALID",
            "error_message": (
                "openquake_disaggregation requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    try:
        poe_f = float(poe)
        if not (0.0 < poe_f < 1.0):
            raise ValueError(f"poe {poe_f} out of (0,1)")
        n_eps = int(num_epsilon_bins)
        if n_eps < 1:
            raise ValueError("num_epsilon_bins must be >= 1")
    except (TypeError, ValueError) as exc:
        return {
            "status": "error",
            "error_code": "DISAGG_PARAMS_INVALID",
            "error_message": f"invalid disaggregation arguments: {exc}",
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
        tool_name="openquake_disaggregation", mode=input_mode,
        entries=_entries, params={"vs30": ref_vs30},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"openquake_disaggregation {_review.cancel_reason}",
        }
    _rv = _review.params.get("vs30")
    if _rv is not None:
        ref_vs30 = float(_rv)

    logger.info(
        "openquake_disaggregation bbox=%s imt=%s poe=%.4g inv=%.0fyr gmpe=%s vs30=%.0f",
        list(coerced), imt, poe_f, float(investigation_time_years), gmpe, ref_vs30,
    )

    try:
        layer = await model_openquake_disaggregation(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            imt=str(imt),
            poe=poe_f,
            investigation_time_years=float(investigation_time_years),
            gmpe=str(gmpe),
            reference_vs30=ref_vs30,
            mag_bin_width=float(mag_bin_width),
            distance_bin_width=float(distance_bin_width),
            num_epsilon_bins=n_eps,
            max_distance_km=float(max_distance_km),
            a_value=float(a_value),
            b_value=float(b_value),
            min_magnitude=float(min_magnitude),
            max_magnitude=float(max_magnitude),
        )
        return layer.model_copy(update={"synthetic_inputs": _review.entries})
    except asyncio.CancelledError:
        raise
    except (DisaggregationError, LocalOqError) as exc:
        logger.warning("openquake_disaggregation failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("openquake_disaggregation unexpected failure")
        return {
            "status": "error",
            "error_code": "DISAGG_INTERNAL_ERROR",
            "error_message": str(exc),
        }


def render_disaggregation_job_ini(
    *,
    site_lon: float,
    site_lat: float,
    imt: str,
    poe: float,
    investigation_time_years: float,
    gmpe_lt_file: str,
    source_lt_file: str,
    max_distance_km: float,
    reference_vs30: float,
    mag_bin_width: float,
    distance_bin_width: float,
    num_epsilon_bins: int,
) -> str:
    """Render the ``calculation_mode = disaggregation`` job.ini for one site."""
    iml_list = imls_list_str(DEFAULT_IMLS_G)
    return (
        "[general]\n"
        "description = seismic hazard disaggregation\n"
        "calculation_mode = disaggregation\n"
        "random_seed = 23\n\n"
        "[geometry]\n"
        f"sites = {site_lon:.6f} {site_lat:.6f}\n\n"
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
        f"maximum_distance = {max_distance_km:g}\n\n"
        "[disaggregation]\n"
        f"poes_disagg = {poe:g}\n"
        f"mag_bin_width = {mag_bin_width:g}\n"
        f"distance_bin_width = {distance_bin_width:g}\n"
        "coordinate_bin_width = 1.0\n"
        f"num_epsilon_bins = {int(num_epsilon_bins)}\n"
        "disagg_outputs = Mag_Dist_Eps Mag_Dist\n\n"
        "[output]\n"
        "export_dir = out\n"
        "individual_rlzs = true\n"
    )


def parse_mag_dist_eps_csv(text: str) -> dict[str, Any]:
    """Parse an OpenQuake ``Mag_Dist_Eps`` disaggregation CSV.

    Header (after the ``#`` provenance line): ``imt,iml,poe,mag,dist,eps,rlz0``
    where ``rlz0`` is the per-bin hazard contribution (a probability). Returns a
    dict with the ``iml`` being disaggregated, the flat list of ``(mag, dist,
    eps, contribution)`` bins, and the derived dominant / mean scalars. Bins with
    zero contribution are kept out of the mean/dominant reductions.
    """
    lines = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
    if not lines:
        return {"iml": 0.0, "bins": [], "n_bins": 0}
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    bins: list[tuple[float, float, float, float]] = []
    iml_val = 0.0
    for row in reader:
        try:
            iml_val = float(row["iml"])
            mag = float(row["mag"])
            dist = float(row["dist"])
            eps = float(row["eps"])
            contrib = float(row["rlz0"])
        except (KeyError, TypeError, ValueError):
            continue
        if contrib > 0.0:
            bins.append((mag, dist, eps, contrib))
    total = sum(c for *_r, c in bins)
    if not bins or total <= 0.0:
        return {"iml": iml_val, "bins": [], "n_bins": 0}
    # Dominant = the modal magnitude-distance CELL (contribution summed over
    # epsilon) - the standard "controlling earthquake" a disaggregation reports,
    # consistent with the M-R contribution chart. The dominant epsilon is the
    # modal epsilon bin WITHIN that cell.
    cell: dict[tuple[float, float], float] = {}
    for m, d, _e, c in bins:
        cell[(m, d)] = cell.get((m, d), 0.0) + c
    dom_md = max(cell.items(), key=lambda kv: kv[1])[0]
    dom_eps_bin = max(
        (b for b in bins if (b[0], b[1]) == dom_md), key=lambda b: b[3]
    )
    mean_mag = sum(m * c for m, _d, _e, c in bins) / total
    mean_dist = sum(d * c for _m, d, _e, c in bins) / total
    mean_eps = sum(e * c for _m, _d, e, c in bins) / total
    return {
        "iml": iml_val,
        "bins": bins,
        "n_bins": len(bins),
        "total_contribution": total,
        "dominant_magnitude": dom_md[0],
        "dominant_distance_km": dom_md[1],
        "dominant_epsilon": dom_eps_bin[2],
        "mean_magnitude": mean_mag,
        "mean_distance_km": mean_dist,
        "mean_epsilon": mean_eps,
    }


async def model_openquake_disaggregation(
    *,
    bbox: tuple[float, float, float, float],
    imt: str,
    poe: float,
    investigation_time_years: float,
    gmpe: str,
    reference_vs30: float,
    mag_bin_width: float,
    distance_bin_width: float,
    num_epsilon_bins: int,
    max_distance_km: float,
    a_value: float,
    b_value: float,
    min_magnitude: float,
    max_magnitude: float,
) -> DisaggregationLayerURI:
    """Run the disaggregation end-to-end: local solve -> parse -> site + chart.

    Raises ``DisaggregationError`` / ``LocalOqError`` on any solve / parse failure.
    """
    run_id = new_ulid()
    begin_substeps(current_emitter(), 3)

    lon, lat = aoi_centroid(bbox)
    files = {
        "source_model.xml": render_area_source_model_xml(
            bbox, a_value=a_value, b_value=b_value,
            min_magnitude=min_magnitude, max_magnitude=max_magnitude,
        ),
        "source_model_logic_tree.xml": render_trivial_source_logic_tree_xml(),
        "gmpe_logic_tree.xml": render_trivial_gmpe_logic_tree_xml(gmpe),
        "job.ini": render_disaggregation_job_ini(
            site_lon=lon, site_lat=lat, imt=imt, poe=poe,
            investigation_time_years=investigation_time_years,
            gmpe_lt_file="gmpe_logic_tree.xml",
            source_lt_file="source_model_logic_tree.xml",
            max_distance_km=max_distance_km, reference_vs30=reference_vs30,
            mag_bin_width=mag_bin_width, distance_bin_width=distance_bin_width,
            num_epsilon_bins=num_epsilon_bins,
        ),
    }

    async with substep(current_emitter(), "run_disaggregation"):
        outdir = await asyncio.to_thread(run_oq_local, files, label="disagg")

    mde = sorted(outdir.glob("Mag_Dist_Eps*.csv"))
    if not mde:
        raise DisaggregationError(
            "DISAGG_EMPTY",
            "disaggregation solve produced no Mag_Dist_Eps export",
        )
    parsed = parse_mag_dist_eps_csv(mde[0].read_text(encoding="utf-8"))
    if parsed["n_bins"] <= 0:
        raise DisaggregationError(
            "DISAGG_EMPTY", "the disaggregation matrix carried no non-zero bins"
        )

    try:
        rp = -float(investigation_time_years) / math.log(1.0 - float(poe))
    except (ValueError, ZeroDivisionError):
        rp = 0.0

    source_note = (
        "Disaggregation ran against a synthetic Gutenberg-Richter area source over "
        f"the AOI (labelled demo source; G-R a={a_value:g} b={b_value:g}, "
        f"M{min_magnitude:g}-{max_magnitude:g}), not a site-specific seismic model."
    )

    async with substep(current_emitter(), "surface_site_and_chart"):
        site_uri = await asyncio.to_thread(
            _upload_disagg_site_geojson, lon, lat, parsed, imt, run_id,
        )
        await _emit_disagg_heatmap_chart(
            parsed, imt=imt, poe=poe,
            investigation_time_years=investigation_time_years,
            source_layer_uri=site_uri,
        )

    layer = DisaggregationLayerURI(
        layer_id=f"disagg-site-{run_id}",
        name=(
            f"Hazard disaggregation site - dominant M{parsed['dominant_magnitude']:.2g} "
            f"@ {parsed['dominant_distance_km']:.0f} km"
        ),
        layer_type="vector",
        uri=site_uri or f"disagg://{run_id}",
        style_preset=DISAGG_SITE_STYLE_PRESET,
        role="primary",
        bbox=tuple(bbox),
        legend=LegendKey(
            kind="categorical",
            classes=[LegendClass(value="site", color="#E63946", label="Disaggregation site")],
            label="Hazard disaggregation",
        ),
        imt=imt,
        poe=float(poe),
        investigation_time_years=float(investigation_time_years),
        return_period_years=rp,
        iml_at_poe=float(parsed["iml"]),
        dominant_magnitude=float(parsed["dominant_magnitude"]),
        dominant_distance_km=float(parsed["dominant_distance_km"]),
        dominant_epsilon=float(parsed["dominant_epsilon"]),
        mean_magnitude=float(parsed["mean_magnitude"]),
        mean_distance_km=float(parsed["mean_distance_km"]),
        n_bins=int(parsed["n_bins"]),
        source_model_note=source_note,
    )
    logger.info(
        "model_openquake_disaggregation complete run_id=%s iml=%.4g "
        "dom_M=%.2f dom_R=%.0fkm dom_eps=%.2f mean_M=%.2f mean_R=%.0fkm n_bins=%d",
        run_id, parsed["iml"], parsed["dominant_magnitude"],
        parsed["dominant_distance_km"], parsed["dominant_epsilon"],
        parsed["mean_magnitude"], parsed["mean_distance_km"], parsed["n_bins"],
    )
    return layer


def _upload_disagg_site_geojson(
    lon: float, lat: float, parsed: dict[str, Any], imt: str, run_id: str,
) -> str | None:
    """Upload a single-point GeoJSON marker for the disaggregation site (S3).

    Best-effort (returns None on any failure): the point is context; the chart
    carries the disaggregation content. SYNC boto3 - callers offload it.
    """
    fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": {
                "dominant_magnitude": round(float(parsed["dominant_magnitude"]), 2),
                "dominant_distance_km": round(float(parsed["dominant_distance_km"]), 1),
                "dominant_epsilon": round(float(parsed["dominant_epsilon"]), 2),
                "iml_at_poe": round(float(parsed["iml"]), 5),
                "imt": imt,
            },
        }],
    }
    try:
        from trid3nt_server.data.simulation.solver.solver import (
            _get_runs_bucket, _get_s3_client,
        )

        bucket = _get_runs_bucket()
        key = f"{run_id}/disagg_site.geojson"
        _get_s3_client().put_object(
            Bucket=bucket, Key=key,
            Body=json.dumps(fc).encode("utf-8"),
            ContentType="application/geo+json",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning("disagg site geojson upload failed (non-fatal): %s", exc)
        return None


async def _emit_disagg_heatmap_chart(
    parsed: dict[str, Any],
    *,
    imt: str,
    poe: float,
    investigation_time_years: float,
    source_layer_uri: str | None,
) -> None:
    """Emit the magnitude-distance contribution matrix (best-effort, no-op safe).

    A legended grouped-series line chart: x = distance bin (km, quantitative),
    y = hazard contribution (% of total, summed over epsilon), one series per
    magnitude bin (colour = magnitude, the legend). This reads the M-R
    contribution matrix as "how much each magnitude contributes at each distance"
    with a real legend (the dock interpreter renders multi-series lines + legend;
    a rect heatmap would degrade to a legend-less scatter). The dominant scenario
    is in the caption strip, not annotated over the plot (readability laws)."""
    try:
        bins: list[tuple[float, float, float, float]] = parsed.get("bins") or []
        total = float(parsed.get("total_contribution") or 0.0)
        if not bins or total <= 0.0:
            return
        # collapse epsilon: sum contribution per (mag, dist), as % of total.
        agg: dict[tuple[float, float], float] = {}
        for mag, dist, _eps, contrib in bins:
            agg[(mag, dist)] = agg.get((mag, dist), 0.0) + contrib
        rows = [
            {
                "distance_km": round(d, 2),
                "magnitude_bin": f"M{m:.2f}",
                "contribution_pct": round(100.0 * c / total, 4),
            }
            for (m, d), c in sorted(agg.items())
        ]
        if len({r["magnitude_bin"] for r in rows}) < 2:
            return
        inv = int(round(investigation_time_years)) if investigation_time_years else 50
        spec = {
            "data": {"values": rows},
            "mark": {"type": "line", "point": True, "tooltip": True},
            "encoding": {
                "x": {
                    "field": "distance_km", "type": "quantitative",
                    "title": "Distance R (km)",
                },
                "y": {
                    "field": "contribution_pct", "type": "quantitative",
                    "title": "Hazard contribution (%)",
                },
                "color": {
                    "field": "magnitude_bin", "type": "nominal",
                    "title": "Magnitude M",
                },
            },
            "width": "container",
        }
        from trid3nt_server.data.processing.charts_common import (
            build_chart_payload,
        )

        payload = build_chart_payload(
            vega_lite_spec=spec,
            title=f"Hazard disaggregation (M-R) - {imt} at {poe * 100:g}% in {inv}yr",
            caption=(
                f"Contribution to {imt} hazard at {poe * 100:g}% in {inv} yr by "
                f"distance, one line per magnitude bin (% of total, summed over "
                f"epsilon). Dominant scenario: M{parsed['dominant_magnitude']:.2f} at "
                f"{parsed['dominant_distance_km']:.0f} km, epsilon "
                f"{parsed['dominant_epsilon']:+.2f}; contribution-weighted mean "
                f"M{parsed['mean_magnitude']:.2f} at {parsed['mean_distance_km']:.0f} km."
            ),
            source_layer_uri=source_layer_uri,
        )
        await emit_chart_payloads(payload)
    except Exception as exc:  # noqa: BLE001 - chart is best-effort
        logger.warning("disagg contribution chart emit failed (non-fatal): %s", exc)
