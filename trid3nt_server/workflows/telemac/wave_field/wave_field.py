"""Engine template ``tomawac_wave_field`` - TOMAWAC spectral (phase-averaged) waves.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/lib/workflow.py``); the wave mechanism is the TELEMAC facade's
open-water front (``steps/open_water.py`` + ``steps/wave.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: how big do the waves get. TOMAWAC's third-generation wave-action
solver - the refinement-grade complement to SFINCS/SnapWave coastal screening -
over four question classes:

  * ``fetch_growth``    - fetch-limited wind-wave growth; Hs grows downwind, and
                          the upwind/downwind shore pair under the SAME storm is
                          what makes the answer checkable.
  * ``shoaling``        - an offshore swell steepens then depth-breaks up a beach.
  * ``bottom_friction`` - a shallow shelf dissipates wave energy.
  * ``wave_current``    - an opposing current amplifies Hs, a following one damps.

Two bed paths, chosen by where the AOI IS: a Great Lakes AOI samples the real
NOAA lake-datum bathymetry; anywhere else runs the geography-free idealized basin
that reproduces the official TOMAWAC verification physics, labeled as such.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    D,
    Data,
    Fetch,
    Forcing,
    FormGate,
    P,
    Physics,
    Ref,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.steps import compute_class
from trid3nt_server.workflows.telemac.wave_field.declarations import DOC, PARAMS
from trid3nt_server.workflows.telemac.wave_field.wave_mode import wave_mode
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_fetch_chart", "plan",
           "tomawac_wave_field"]

#: A lake fetch runs ALONG the wind and is wider than it is tall, so a geocoded
#: place is squared off asymmetrically (~0.7 deg of longitude, ~0.4 of latitude).
#: A square box here would model a different fetch than the question asked about.
_LAKE_HALF_DEG = (0.7, 0.4)

_TSTEPS = "trid3nt_server.workflows.telemac.steps"


#: The BED, as declared reference data. Sampling it inside the solver
#: container would bypass the emit, cache, provenance and retry the router
#: gives every other fetch. Declaring it here puts the bathymetry on the
#: canvas as a continuous surface and lets the worker run with no network:
#: the producer fetches, the manifest stages the raster into the run
#: directory, and the builder reads a file.
#: ``px_per_deg`` is THIS builder's sample lattice - the grid its nodes are read
#: against - so it travels from the template rather than being a router default.
DATA = (
    Data("bed", Fetch.tool(f"{_TSTEPS}.open_water.fetch_domain_bed",
                           bathy_source=P.bathy_source,
                           domain_kind="lake", px_per_deg=1200.0,
                           max_px_per_side=2000)),
)


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / D.<data> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

PHYSICS = Physics("wave_spectrum",
                  wave_mode=P.wave_mode,
                  wind_speed_mps=P.wind_speed_mps,
                  wind_direction_deg=P.wind_direction_deg,
                  boundary_hs_m=P.boundary_hs_m,
                  boundary_period_s=P.boundary_period_s,
                  current_speed_mps=P.current_speed_mps,
                  bottom_friction=P.bottom_friction,
                  sim_duration_hours=P.sim_duration_hours,
                  bathy_source=P.bathy_source,
                  bed=D.bed)

#: The MESH ASK, frozen at declaration and building nothing at import. An
#: open-water deck runs on a uniform lattice over the acquired AOI, and the router
#: checks every field against what the ``reg_grid`` mesher declares.
MESH = tool.build_mesh(
    mesher="reg_grid",
    kind="structured_grid",
    aoi=Ref("aoi"),
    resolution_m=P.target_resolution_m,
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The spectral-wave recipe. Pure and STATIC: it reads no value, it names them.

    The form gate comes FIRST because the storm is a PRESCRIBED value: the wind
    speed that sets the whole answer is a labeled default, and reviewing it after
    the solve would be reviewing a number that had already decided everything.
    """
    return [
        FormGate(title="Review the wave-field storm forcing"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, shape="open_water",
                            aoi_half_deg=_LAKE_HALF_DEG, aoi_name="aoi",
                            code_prefix="TOMAWAC"),
        ops.author(mesh=MESH, physics=PHYSICS,
                   forcing=Forcing()),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=Forcing())
           .chart("wave_fetch_growth", builder=build_fetch_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("hs_max_m", "hs_mean_m", "hs_upwind_m", "hs_downwind_m",
          "peak_period_max_s", "wave_mode", "wind_speed_mps", "mesh_size_m",
          "fetch_curve_km", "fetch_curve_hs_m")


def build_fetch_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The along-fetch growth chart SPEC: Hs against downwind distance.

    The curve is the WORKER's own measurement, carried out on the layer, so the
    chart and the narrated ``hs_downwind_m`` are the same numbers rather than two
    resamplings that nearly agree. ``None`` when the run measured no curve, which
    is the honest "there is nothing to plot" - a shoaling or wave-current run has
    no fetch axis to grow along.
    """
    xs = getattr(result, "fetch_curve_km", None)
    hs = getattr(result, "fetch_curve_hs_m", None)
    if not xs or not hs or len(xs) != len(hs):
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    upwind = getattr(result, "hs_upwind_m", None)
    downwind = getattr(result, "hs_downwind_m", None)
    wind = getattr(result, "wind_speed_mps", None)
    where = params.get("location")
    title = (f"Wave growth along the fetch - {where}" if where
             else (getattr(result, "name", None) or "Wave growth along the fetch"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": False},
            "data": {"values": [{"x_km": float(xs[i]), "hs_m": float(hs[i])}
                                for i in range(len(xs))]},
            "encoding": {
                "x": {"field": "x_km", "type": "quantitative",
                      "title": "Downwind distance (km)"},
                "y": {"field": "hs_m", "type": "quantitative",
                      "title": "Significant wave height Hs (m)"},
            },
        },
        title=title,
        caption=(
            f"Fetch-limited growth under a prescribed {float(wind):.3g} m/s wind: "
            if wind is not None else "Fetch-limited growth: ")
        + (f"Hs {float(upwind):.3g} m at the upwind shore rising to "
           f"{float(downwind):.3g} m downwind. "
           if upwind is not None and downwind is not None else "")
        + "Spectral screening, not a calibrated hindcast.",
    )


_TOMAWAC_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=150.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / idealized grid",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; GRID_H_FLOOR_M=150 m is the finest the wave "
        "grid authors, a large lake is coarsened under the GRID_NODE_CAP budget "
        "(self-labeled); a spectral screening field gains nothing finer"
    ),
)

_TOMAWAC_METADATA = AtomicToolMetadata(
    name="tomawac_wave_field",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TOMAWAC_RES_SPEC,),
)


tomawac_wave_field = register_workflow(
    TelemacWorkflow, _TOMAWAC_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    provenance=(("wind_speed_mps", "wind_note"),
                ("bathy_source", "bathy_note"),
                ("target_resolution_m", "target_resolution_note")),
    # Hs at the UPWIND end sits in the steep fetch-growth gradient, where a coarse
    # grid flattens it: measured -62%. Hs MAX over the lake is a saturated
    # maximum - a converged class - and carries no label.
    sensitivity=(("hs_upwind_m", "gradient"),),
    coerce=(
        location_or_bbox("tomawac_wave_field", code_prefix="TOMAWAC",
                         hint="For a natural prompt like 'how big do the waves get "
                              "on <lake>', pass location='<lake>'."),
        wave_mode(),
        compute_class(),
    ),
    doc=DOC,
)
