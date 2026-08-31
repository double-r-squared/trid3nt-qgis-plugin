"""Engine template ``coastal_tidal_surge`` - TELEMAC-2D coastal tidal/surge
inundation.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/lib/workflow.py``); the coastal mechanism is the TELEMAC facade's
open-water front (``workflows/telemac/steps/open_water.py`` +
``steps/coastal.py``). See ``docs/design/declarative-workflows.md``.

THE QUESTION: how far does an OBSERVED or PREDICTED coastal water-level series
FLOOD a stretch of coast. A regular UTM grid over a coastal AOI with real NOAA
DEM_all topobathy at the nodes, ONE seaward liquid boundary driven in time by a
NOAA CO-OPS series through the LIQUID BOUNDARIES FILE (SL(1)); SAINT-VENANT +
TIDAL FLATS wetting/drying floods the low coast as the boundary stage rises. The
discriminant: a storm-surge series (``series_type="observed"``) floods far more
land than the calm astronomical tide (``"prediction"``) over the SAME domain.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    Forcing,
    FormGate,
    Physics,
    Ref,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.coastal_tidal_surge.declarations import (
    DOC,
    PARAMS,
    PARAMS as P,
    VALIDITY,
)
from trid3nt_server.workflows.telemac.coastal_tidal_surge.series_type import (
    series_type,
)
from trid3nt_server.workflows.telemac.steps import compute_class
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_stage_chart", "coastal_tidal_surge",
           "plan"]

_SHARED = "trid3nt_server.workflows.shared"

#: The AOI half-width (deg) a geocoded coastal place is squared off to. ~0.06 deg
#: (~6 km) spans a shoreline with open water on one side and low land on the other,
#: which is the domain shape this question needs.
_COAST_HALF_DEG = 0.06

_TSTEPS = "trid3nt_server.workflows.telemac.steps"


#: The boundary FORCING - the gauge record, fetched fresh over the domain the AOI
#: step binds. Reference data: a water-level record is the world's, never supplied.
#: It reads the DOMAIN for where to look and the params for which series, which
#: station and which window.
class DATA:
    tides = tool(f"{_SHARED}.tide_series.resolve_tide_series",
                 series_type=P.series_type,
                 station=P.station,
                 start_date=P.start_date,
                 end_date=P.end_date)
    #: The BED, as declared reference data. Sampling it inside the solver
    #: container would bypass the emit, cache, provenance and retry the router
    #: gives every other fetch. Declaring it here puts the bathymetry on the
    #: canvas as a continuous surface and lets the worker run with no network:
    #: the producer fetches, the manifest stages the raster into the run
    #: directory, and the builder reads a file.
    #: ``px_per_deg`` is THIS builder's sample lattice - the grid its nodes are read
    #: against - so it travels from the template rather than being a router default.
    bed = tool(f"{_TSTEPS}.open_water.fetch_domain_bed",
               bathy_source=P.bathy_source,
               domain_kind="coast", px_per_deg=1800.0,
               max_px_per_side=3000)


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / DATA.<row> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

PHYSICS = Physics("coastal_surge",
                  datum_offset_m=P.datum_offset_m, ocean_edge=P.ocean_edge,
                  duration_hours=P.duration_hours, time_step_s=P.time_step_s,
                  bathy_source=P.bathy_source,
                  friction_law=P.friction_law,
                  friction_coefficient=P.friction_coefficient,
                  wind_speed_mps=P.wind_speed_mps,
                  wind_direction_from_deg=P.wind_direction_from_deg,
                  output_interval_min=P.output_interval_min,
                  bed=DATA.bed)

FORCING = Forcing(water_level=DATA.tides)

#: The MESH ASK, frozen at declaration and building nothing at import. An
#: open-water deck runs on a uniform lattice over the acquired AOI, and the router
#: checks every field against what the ``reg_grid`` mesher declares.
MESH = tool.build_mesh(
    mesher="reg_grid",
    kind="structured_grid",
    extent=Ref("aoi"),
    resolution_m=P.target_resolution_m,
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The coastal tidal/surge recipe. Pure and STATIC: it reads no value, it names them.

    The form gate comes FIRST so the fetch and the solve both run on the approved
    sheet: the window and the datum offset are exactly the values a reviewer would
    want to change, and a series fetched before the review would have been fetched
    for the window the review replaced.
    """
    return [
        FormGate(title="Review the coastal tide/surge scenario"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, shape="open_water",
                            aoi_half_deg=_COAST_HALF_DEG, aoi_name="coast",
                            code_prefix="COASTAL"),
        ops.author(mesh=MESH, physics=PHYSICS,
                   forcing=FORCING),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=FORCING)
           .chart("coastal_stage_vs_inundation", builder=build_stage_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("peak_depth_m", "inundation_peak_depth_m", "inundation_basis",
          "flooded_land_km2", "wet_area_km2", "peak_wl_m",
          "sl_peak_m", "series_type", "series_datum", "datum_offset_m",
          "station_id", "station_name", "ocean_edge", "mesh_size_m")


def build_stage_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The storm-tide chart SPEC: the boundary crest against what it flooded.

    Three measured bars, all off the published layer - the peak boundary stage the
    run was DRIVEN with, the peak free-surface level it REACHED, and the deepest
    inundation it produced - so the reader can see the forcing and the response in
    the same frame. ``None`` when the run measured no boundary stage, which is the
    honest "there is no chart to draw".
    """
    sl_peak = getattr(result, "sl_peak_m", None)
    peak_depth = getattr(result, "peak_depth_m", None)
    if sl_peak is None or peak_depth is None:
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    peak_wl = getattr(result, "peak_wl_m", None)
    bars = [{"quantity": "Boundary stage (peak)", "m": float(sl_peak)}]
    if peak_wl is not None:
        bars.append({"quantity": "Water level (peak)", "m": float(peak_wl)})
    bars.append({"quantity": "Water depth (peak, all water)", "m": float(peak_depth)})
    # The deepest INUNDATION and the deepest water are two different numbers. A
    # chart about flooding must name both: naming only the deeper one lets the
    # permanently submerged bay read as the answer.
    inundation = getattr(result, "inundation_peak_depth_m", None)
    if inundation is not None:
        bars.append({"quantity": "Inundation depth (peak, initially-dry land)",
                     "m": float(inundation)})

    kind = str(getattr(result, "series_type", None) or "observed")
    datum = getattr(result, "series_datum", None) or "MLLW"
    flooded = getattr(result, "flooded_land_km2", None)
    # With no location words the LAYER's own name is the title: it already reads
    # "Peak inundation depth (<coast>)", so prefixing it would say it twice.
    where = params.get("location")
    title = (f"Coastal {kind} tide - {where}" if where
             else (getattr(result, "name", None) or f"Coastal {kind} tide"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "bar"},
            "data": {"values": bars},
            "encoding": {
                "y": {"field": "quantity", "type": "nominal", "title": None,
                      "sort": None},
                "x": {"field": "m", "type": "quantitative", "title": "Metres"},
            },
        },
        title=title,
        caption=(
            f"Driven by the {kind} CO-OPS series ({datum} datum): peak boundary "
            f"stage {float(sl_peak):.3g} m, deepest water "
            f"{float(peak_depth):.3g} m"
            + (f", deepest INUNDATION over initially-dry land "
               f"{float(inundation):.3g} m" if inundation is not None else "")
            + (f", {float(flooded):.3g} km2 of land newly flooded"
               if flooded is not None else "")
            + ". Planning-grade screening, not a calibrated hindcast."
        ),
    )


_COASTAL_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=20.0,
    native_hint="NOAA DEM_all topobathy (~30-90 m coastal) / grid node spacing",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; the coastal grid floor is 20 m, a wide bbox is "
        "coarsened under the node budget (self-labeled); a planning-grade "
        "inundation screening field gains nothing finer than the topobathy"
    ),
)

_COASTAL_METADATA = AtomicToolMetadata(
    name="coastal_tidal_surge",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_COASTAL_RES_SPEC,),
)


coastal_tidal_surge = register_workflow(
    TelemacWorkflow, _COASTAL_METADATA, PARAMS, plan,
    data=DATA,
    # The coastal-surge pipeline this dispatched to was an in-worker builder,
    # retired with the worker unification; the case authoring that replaces it is
    # rung 4. The declaration stays readable and invoking refuses typed.
    parked="awaiting the rung-4 rebuild of the coastal-surge case authoring",
    answer=ANSWER,
    # The mesh row is present only when the WORKER moved the user's explicit
    # spacing (a grid floor or the node budget); on an honoured ask it reads null.
    provenance=(("datum_offset_m", "datum_offset_note"),
                ("series_type", "series_type_note"),
                ("target_resolution_m", "target_resolution_note")),
    # Flooded LAND is an area bounded by a wet/dry front, which lands between
    # nodes: measured 4x low on the coarse mesh. The inundation peak is a
    # magnitude maximum over that same front.
    sensitivity=(("flooded_land_km2", "extent"),
                 ("inundation_peak_depth_m", "peak")),
    # The friction coefficient's MEANING is set by the law beside it, which no
    # per-param bound can see. See declarations.VALIDITY.
    validity=VALIDITY,
    coerce=(
        location_or_bbox("coastal_tidal_surge", code_prefix="COASTAL",
                         hint="For a natural prompt like 'storm surge flooding near "
                              "<place>', pass location='<place>'."),
        series_type(),
        compute_class(),
    ),
    doc=DOC,
)
