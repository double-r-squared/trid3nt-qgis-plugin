"""Engine template ``telemac_do_sag`` - TELEMAC-2D WAQTEL dissolved-oxygen sag.

The US TMDL / discharge-permit question: a point discharge (a permitted outfall,
a WWTP) enters a river reach - WHERE does dissolved oxygen bottom out downstream,
and does it VIOLATE the water-quality standard? Solves TELEMAC-2D with the WAQTEL
O2 module (WATER QUALITY PROCESS = 2) over a real NHDPlus reach: the fully-mixed
carbonaceous BOD + DO ride in at the top of the reach, CBOD decays downstream (k1)
consuming oxygen, and surface reaeration (k2) recovers it - the classic
Streeter-Phelps oxygen sag. Reuses the ``telemac_river_dye`` reach-seeding + mesh
+ two-pass solve pipeline via ``model_telemac_river_dye(do_sag_config=...)`` and
postprocesses to a DISSOLVED-O2 field COG + the along-reach DO-sag curve.

V&V: the WAQTEL O2 kinetics reduce EXACTLY to the Streeter-Phelps 1925 closed
form (P=R=BEN=k44=0, constant k2/Cs, T=20C); the in-image gate reproduces it to
0.011 mg/L at the sag minimum. A registered engine TEMPLATE (engine="telemac",
tier="template") surfaced by the ``run_telemac`` door - like ``telemac_river_dye``
it is ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` and runs local-docker only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import TelemacDoLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.data import register_tool
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.telemac._template_card import TemplateCard
from trid3nt_server.workflows.telemac.postprocess_telemac import (
    PostprocessTelemacError,
)
from trid3nt_server.workflows.telemac.river_dye.river_dye import (
    RunTelemacError,
    TelemacBanksUnavailableError,
    TelemacDyeScenarioError,
    TelemacReachDegenerateError,
    model_telemac_river_dye,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.do_sag.do_sag")

__all__ = ["telemac_do_sag"]


TEMPLATE_CARD = TemplateCard(
    question=(
        "the DISSOLVED-OXYGEN SAG below a permitted discharge / WWTP outfall in a "
        "river reach (US TMDL / Clean Water Act permit): where does DO bottom out "
        "downstream and does it VIOLATE the water-quality standard? (TELEMAC-2D "
        "WAQTEL O2 / Streeter-Phelps oxygen sag over a real reach)"
    ),
    required_inputs=["location OR bbox"],
    knobs=(
        "discharge_bod_mgl, upstream_do_mgl, water_temp_c, do_standard_mgl, "
        "k1_per_day, k2_per_day, reach_length_km, discharge_m3s, mesh_resolution, "
        "bank_source"
    ),
)


#: DECLARED mesh_resolution_m range. Same TELEMAC mesh-builder machinery
#: as telemac_river_dye: SOLVER floor 3 m (MESH_H_FLOOR_M), a long reach coarsened
#: under the node budget (self-labeled), no fixed coarse ceiling. Out-of-range
#: (sub-3 m) explicit ask quoted back, never silently snapped.
_TELEMAC_DO_SAG_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="NHD channel geometry + 3DEP terrain; edge sized from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the TELEMAC mesh "
        "builder authors, a long reach is coarsened under the node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

_TELEMAC_DO_SAG_METADATA = AtomicToolMetadata(
    name="telemac_do_sag",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC_DO_SAG_RES_SPEC,),
)


def _do_saturation_mgl(temp_c: float) -> float:
    """Freshwater DO saturation Cs (mg/L) vs temperature (Elmore-Hayes, 1 atm).

    A narrated literature relation (NOT a fabricated site value); the caller can
    override with an explicit ``do_saturation_mgl``. ~9.0 mg/L at 20C."""
    t = max(0.0, min(40.0, float(temp_c)))
    return round(14.652 - 0.41022 * t + 0.0079910 * t * t - 0.000077774 * t ** 3, 3)


@register_tool(
    _TELEMAC_DO_SAG_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def telemac_do_sag(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    discharge_bod_mgl: float = 20.0,
    upstream_do_mgl: float | None = None,
    water_temp_c: float = 20.0,
    do_saturation_mgl: float | None = None,
    do_standard_mgl: float = 5.0,
    k1_per_day: float = 0.3,
    k2_per_day: float = 0.9,
    reach_length_km: float = 12.0,
    channel_width_m: float = 60.0,
    sim_duration_s: float = 10800.0,
    discharge_m3s: float | None = None,
    mesh_resolution: str = "auto",
    mesh_resolution_m: float | None = None,
    bank_source: str = "nhd_area",
    compute_class: str = "medium",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> TelemacDoLayerURI | dict[str, Any]:
    """DISSOLVED-OXYGEN SAG below a discharge in a river (US TMDL / permit question).

    THE tool for "where does dissolved oxygen bottom out below this discharge",
    "will the DO sag violate the standard", "Streeter-Phelps oxygen sag", "BOD
    loading / oxygen demand downstream of a WWTP / outfall", "DO TMDL for this
    reach". Solves TELEMAC-2D + WAQTEL O2 over a REAL NHDPlus reach modeled
    STARTING at the fully-mixed discharge: the mixed carbonaceous BOD + DO enter
    at the top of the reach, CBOD decays downstream (deoxygenation k1) consuming
    oxygen, and surface reaeration (k2) recovers it. Produces a DISSOLVED-O2 field
    map + the along-reach DO-sag curve (DO vs downstream distance, the sag minimum
    + the standard line) + the sag-minimum location/value.

    Do NOT use this for: a conservative dye/tracer/contaminant plume that only
    dilutes (``telemac_river_dye``); groundwater plumes (``modflow_*``); flood
    depth (``sfincs_flood`` / ``hecras_riverine_flood``).

    Params:
        location: place name near the discharge (geocoded). Supply this OR ``bbox``.
        bbox: OPTIONAL explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326.
        discharge_bod_mgl: the FULLY-MIXED ultimate carbonaceous BOD at the top of
            the reach (effluent BOD already blended with the river), mg/L.
            Default 20 (a typical secondary-effluent-influenced mixed load, a
            labeled demo default, not a site measurement).
        upstream_do_mgl: DO carried in at the top of the reach, mg/L. Unset ->
            the saturation value (a stream at saturation upstream of the sag).
        water_temp_c: water temperature (C) - sets the DO saturation Cs the deficit
            is measured against. Default 20.
        do_saturation_mgl: OPTIONAL explicit saturation Cs (mg/L); overrides the
            temperature-derived value.
        do_standard_mgl: the DO water-quality standard the sag is judged against,
            mg/L. Default 5 (a common warm-water aquatic-life criterion).
        k1_per_day: CBOD deoxygenation rate, per day. Default 0.3 (typical).
        k2_per_day: surface reaeration rate, per day. Default 0.9.
        reach_length_km: modeled reach length downstream of the discharge, km.
            Default 12 (the sag critical point is often several km downstream).
        channel_width_m: modeled channel width, m. Default 60.
        sim_duration_s: simulated time to reach the steady-state sag, s.
            Default 10800 (3 h).
        discharge_m3s: OPTIONAL steady carrier discharge, m3/s. Unset -> resolved
            from the NOAA National Water Model at the reach (a typed gate if no
            coverage).
        mesh_resolution: "auto" | "fine" | "coarse".
        bank_source: "nhd_area" (default, real banks or a typed gate) |
            "constant_ribbon" (assumed width).
        input_mode: "user_gated" reviews the resolved discharge + banks before the
            solve; "auto" (default) proceeds with them labeled.

    Returns:
        On success: ``TelemacDoLayerURI`` (a ``LayerURI`` subtype) - the emitter
        loads the DISSOLVED-O2 field map + animates the SELAFIN sibling. Carries
        ``do_min_mgl`` / ``do_min_distance_m`` / ``do_violates_standard`` +
        ``sag_curve_*`` (narrate these typed numbers - Invariant 1).
        On failure: dict with ``status="error"`` + ``error_code``.
    """
    coerced_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        cb = coerce_bbox_value(bbox)
        if cb is None:
            if isinstance(bbox, str) and any(c.isalpha() for c in bbox) \
                    and not (location and str(location).strip()):
                location, bbox = bbox, None
            else:
                return {"status": "error", "error_code": "TELEMAC_PARAMS_INVALID",
                        "error_message": f"invalid bbox: {bbox!r}"}
        else:
            coerced_bbox = tuple(cb)  # type: ignore[assignment]

    has_loc = bool(location and str(location).strip())
    if not has_loc and coerced_bbox is None:
        return {"status": "error", "error_code": "TELEMAC_PARAMS_INCOMPLETE",
                "error_message": ("telemac_do_sag needs a place `location` "
                                  "(geocoded) or an explicit `bbox` AOI.")}
    if has_loc and coerced_bbox is not None:
        coerced_bbox = None  # location wins

    # coerce + clamp the WQ knobs (a bogus arg never crashes the call)
    def _clamp(v, lo, hi, dflt):
        try:
            return min(max(float(v), lo), hi)
        except (TypeError, ValueError):
            return dflt
    discharge_bod_mgl = _clamp(discharge_bod_mgl, 0.1, 5000.0, 20.0)
    water_temp_c = _clamp(water_temp_c, 0.0, 40.0, 20.0)
    do_standard_mgl = _clamp(do_standard_mgl, 0.0, 15.0, 5.0)
    k1_per_day = _clamp(k1_per_day, 0.01, 20.0, 0.3)
    k2_per_day = _clamp(k2_per_day, 0.01, 50.0, 0.9)
    reach_length_km = _clamp(reach_length_km, 0.5, 15.0, 12.0)
    sat = (float(do_saturation_mgl) if do_saturation_mgl is not None
           else _do_saturation_mgl(water_temp_c))
    up_do = float(upstream_do_mgl) if upstream_do_mgl is not None else sat
    up_do = min(max(up_do, 0.0), sat)

    do_sag_config = {
        "bod_mgl": float(discharge_bod_mgl),
        "upstream_do_mgl": float(up_do),
        "saturation_mgl": float(sat),
        "water_temp_c": float(water_temp_c),
        "k1_per_day": float(k1_per_day),
        "k2_per_day": float(k2_per_day),
        "k2_formula": 0,      # constant k2 (the S-P idealization; user-set k2)
        "standard_mgl": float(do_standard_mgl),
    }
    logger.info(
        "telemac_do_sag location=%r bbox=%s bod=%.4g up_do=%.3g sat=%.3g "
        "k1=%.3g k2=%.3g std=%.3g reach_km=%.3g",
        location, coerced_bbox, discharge_bod_mgl, up_do, sat, k1_per_day,
        k2_per_day, do_standard_mgl, reach_length_km,
    )

    try:
        layer = await model_telemac_river_dye(
            location=location if has_loc else None,
            bbox=coerced_bbox,
            reach_length_km=float(reach_length_km),
            channel_width_m=float(channel_width_m),
            sim_duration_s=float(sim_duration_s),
            mesh_resolution=str(mesh_resolution or "auto"),
            mesh_resolution_m=(float(mesh_resolution_m)
                               if mesh_resolution_m is not None else None),
            compute_class=compute_class,
            bank_source=bank_source,
            discharge_m3s=(float(discharge_m3s) if discharge_m3s is not None else None),
            input_mode=input_mode,
            do_sag_config=do_sag_config,
        )
        # water-quality provenance (law 9, audit row 32): the WQ terms rode SILENT.
        # Surface them without refusing (P8 label-only + the canonical closed-form
        # Streeter-Phelps runs at standard conditions): BOD load = the scenario
        # question; water temp = documented standard condition (20 C S-P benchmark;
        # fetch_usgs_water_quality queued for a site value); k1/k2 = documented
        # rate coefficients (O'Connor-Dobbins reaeration queued).
        _wq = [
            SyntheticInput(
                param="discharge_bod_mgl", value=round(float(discharge_bod_mgl), 2),
                units="mg/L",
                basis="default_demo" if float(discharge_bod_mgl) == 20.0 else "user",
                consequence="scenario",
                note="fully-mixed ultimate CBOD at the reach top -- the pollutant "
                     "source-term question (scenario lever)",
            ),
            SyntheticInput(
                param="water_temp_c", value=round(float(water_temp_c), 2), units="C",
                basis="default_demo" if float(water_temp_c) == 20.0 else "user",
                consequence="scenario",
                note="sets DO saturation Cs; 20 C = the standard Streeter-Phelps "
                     "condition (fetch_usgs_water_quality queued for a site temperature)",
            ),
            SyntheticInput(
                param="k1_per_day", value=round(float(k1_per_day), 3), units="1/day",
                basis="default_demo" if float(k1_per_day) == 0.3 else "user",
                consequence="numerical",
                note="CBOD deoxygenation rate -- documented rate coefficient",
            ),
            SyntheticInput(
                param="k2_per_day", value=round(float(k2_per_day), 3), units="1/day",
                basis="default_demo" if float(k2_per_day) == 0.9 else "user",
                consequence="numerical",
                note="surface reaeration rate -- documented coefficient "
                     "(O'Connor-Dobbins velocity/depth derivation queued)",
            ),
        ]
        layer = layer.model_copy(update={
            "synthetic_inputs": list(layer.synthetic_inputs or []) + _wq,
        })
        logger.info(
            "telemac_do_sag complete layer_id=%s do_min=%.3g mg/L at %sm violates=%s",
            layer.layer_id, layer.do_min_mgl, layer.do_min_distance_m,
            layer.do_violates_standard,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (TelemacBanksUnavailableError, TelemacReachDegenerateError):
        raise
    except (TelemacDyeScenarioError, PostprocessTelemacError, RunTelemacError) as exc:
        logger.warning("telemac_do_sag failed: %s (%s)",
                       getattr(exc, "error_code", "?"), exc)
        return {"status": "error",
                "error_code": getattr(exc, "error_code", "TELEMAC_RUN_FAILED"),
                "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("telemac_do_sag unexpected failure")
        return {"status": "error", "error_code": "TELEMAC_INTERNAL_ERROR",
                "error_message": str(exc)}
