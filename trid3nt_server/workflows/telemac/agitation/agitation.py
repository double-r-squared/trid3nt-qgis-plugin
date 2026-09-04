"""Engine template ``artemis_harbor_agitation`` - ARTEMIS harbour wave agitation.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/runtime/workflow.py``); the agitation mechanism is the TELEMAC facade's
open-water front (``authoring/open_water.py`` + ``authoring/agitation.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: how much does swell amplify inside a harbour, and does the
breakwater shelter the berths. ARTEMIS is the phase-RESOLVING elliptic mild-slope
(Berkhoff) solver - the complement to TOMAWAC's phase-AVERAGED spectral tier -
so the answer is a steady-state agitation coefficient Kd = Hs/H0 in which
diffraction fringes, standing waves and resonance are visible rather than
averaged away. Three question classes:

  * ``diffraction`` - a breakwater shelters a berthing area. WHICH breakwater is
                      the caller's to supply: hand the ``structure`` slot a layer
                      (``fetch_osm_breakwaters`` finds the surveyed one) or a
                      drawn line. Nothing supplied = open water, labeled.
  * ``resonance``   - a narrow-mouth basin amplifies swell at its seiche periods.
  * ``shoal``       - a nearshore reef refracts and FOCUSES waves down-wave.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    Forcing,
    FormGate,
    Physics,
    Ref,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.agitation.agitation_mode import agitation_mode
from trid3nt_server.workflows.telemac.agitation.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.solving.solve import compute_class
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "artemis_harbor_agitation",
           "build_agitation_chart", "plan"]

#: A harbour approach is small: ~0.06 deg (~6 km) around a geocoded quay is the
#: open-water box the sheltering question lives in.
_HARBOR_HALF_DEG = 0.06

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"


#: The thing that SHELTERS, as a CONTEXT SLOT - the exemplar of the shape.
#:
#: There is no producer, and that is the declaration: this template will not name
#: a default source for somebody's breakwater. It says what SHAPE it accepts
#: (a polyline) and stops, because "which structure" is the caller's question,
#: not the engine's. Fill it with a layer (``fetch_osm_breakwaters`` for the
#: surveyed one, or any line layer), or with a line drawn on the canvas, or with
#: a barrier that does not exist yet - the last is the design question this tool
#: is actually for, and a baked "go fetch the real one" could never have answered
#: it. ``.optional()`` makes absence legal and LABELLED: the domain solves as
#: open water and the run says so on the layer and in provenance.
#:
#: The harbour bed is still sampled INSIDE the solver container - that one is on
#: the in-worker-fetch migration queue.
class DATA:
    structure = Data.supplied(geometry="polyline").optional()
    #: The DOMAIN, as a slot of the same shape. A phase-resolving solve is the
    #: most mesh-dependent question this fleet asks, so the mesh it runs on is the
    #: caller's to author: hand this slot a mesh ``build_mesh`` built - adaptive
    #: sizing, the breakwater cut in conformally, a seaward boundary designated -
    #: and it IS the domain. Unfilled, the run asks for the uniform grid the
    #: worker lays over the AOI, which is a labeled fallback and not a stance.
    mesh = Data.supplied(geometry="mesh").optional()
    #: The BED, as declared reference data. Sampling it inside the solver
    #: container would bypass the emit, cache, provenance and retry the router
    #: gives every other fetch. Declaring it here puts the bathymetry on the
    #: canvas as a continuous surface and lets the worker run with no network:
    #: the producer fetches, the manifest stages the raster into the run
    #: directory, and the builder reads a file.
    #: ``px_per_deg`` is THIS builder's sample lattice - the grid its nodes are read
    #: against - so it travels from the template rather than being a router default.
    bed = tool(f"{_AUTHORING}.open_water.fetch_domain_bed",
               bathy_source=P.bathy_source,
               mode=P.wave_mode,
               real_bed_modes=("diffraction",),
               px_per_deg=3000.0, max_px_per_side=2500)


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / DATA.<row> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

PHYSICS = Physics("harbor_agitation",
                  wave_mode=P.wave_mode,
                  wave_period_s=P.wave_period_s,
                  wave_direction_deg=P.wave_direction_deg,
                  wave_height_m=P.wave_height_m,
                  reflection_coef=P.reflection_coef,
                  # The slot, read late. Because producers are demand-pulled, an
                  # unfilled slot costs no fetch and binds to None - so the
                  # barrier is meshed WHEN the slot is filled and never otherwise.
                  structure=DATA.structure,
                  bathy_source=P.bathy_source,
                  bed=DATA.bed)

#: The MESH RECIPE, frozen at declaration and building nothing at import. An
#: open-water run solves on a uniform lattice over the acquired AOI, so the recipe
#: is the three agnostic params and the mesher's own near-empty default program:
#: a lattice at one size word, with no bed of its own (the solver stages that).
MESH = tool.build_mesh(
    mesher="reg_grid",
    kind="structured_grid",
    extent=Ref("aoi"),
    resolution_m=P.target_resolution_m,
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The harbour-agitation recipe. Pure and STATIC: it reads no value, it names them.

    The form gate comes FIRST because the incident wave is PRESCRIBED: Kd is a
    ratio against H0, so the height and period on that card scale every number the
    run goes on to produce.
    """
    return [
        FormGate(title="Review the incident wave and the structure"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, shape="open_water",
                            aoi_half_deg=_HARBOR_HALF_DEG, aoi_name="aoi",
                            code_prefix="ARTEMIS"),
        ops.author(mesh=MESH, physics=PHYSICS,
                   forcing=Forcing()),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=Forcing())
           .chart("harbor_agitation", builder=build_agitation_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("kd_max", "hs_max_m", "kd_sheltered", "kd_exposed", "resonant_period_s",
          "response_at_resonance", "response_off_resonance", "wave_mode",
          "wave_period_s", "mesh_size_m", "agitation_curve_m",
          "agitation_curve_kd", "agitation_curve_kind")


#: What the curve's axes ARE, per question class. Each mode sweeps a different
#: independent variable, so a single axis label would be wrong for two of the
#: three: a resonance run plots amplification against incident PERIOD, not Kd
#: against distance.
_CURVE_AXIS: dict[str, tuple[str, str]] = {
    "diffraction_transect": ("Distance along the transect (m)",
                             "Agitation coefficient Kd = Hs/H0"),
    "resonance_sweep": ("Incident wave period (s)",
                        "Basin response (amplification of H0)"),
    "shoal_axis_transect": ("Distance along the shoal axis (m)",
                            "Agitation coefficient Kd = Hs/H0"),
}
_CURVE_AXIS_DEFAULT = ("Along the measured curve",
                       "Agitation coefficient Kd = Hs/H0")


def build_agitation_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The agitation chart SPEC: Kd across the field, as the worker measured it.

    The curve is the RUN's own, carried on the layer, so the chart and the narrated
    sheltered/exposed pair are one measurement rather than two resamplings that
    nearly agree. ``None`` when the run measured no curve - which is the honest
    "there is nothing to plot".
    """
    xs = getattr(result, "agitation_curve_m", None)
    kd = getattr(result, "agitation_curve_kd", None)
    if not xs or not kd or len(xs) != len(kd):
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    kind = str(getattr(result, "agitation_curve_kind", None) or "transect")
    x_title, y_title = _CURVE_AXIS.get(kind, _CURVE_AXIS_DEFAULT)
    sheltered = getattr(result, "kd_sheltered", None)
    exposed = getattr(result, "kd_exposed", None)
    period = getattr(result, "wave_period_s", None)
    where = params.get("location")
    title = (f"Harbour agitation Kd - {where}" if where
             else (getattr(result, "name", None) or "Harbour agitation Kd"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": False},
            "data": {"values": [{"x": float(xs[i]), "kd": float(kd[i])}
                                for i in range(len(xs))]},
            "encoding": {
                "x": {"field": "x", "type": "quantitative", "title": x_title},
                "y": {"field": "kd", "type": "quantitative", "title": y_title},
            },
        },
        title=title,
        caption=(
            (f"Forced by a prescribed {float(period):.3g} s incident wave: "
             if period is not None else "")
            + (f"Kd {float(exposed):.3g} on the exposed approach against "
               f"{float(sheltered):.3g} in the lee - the structure cut agitation "
               f"in the strip it shadows by a factor of "
               f"{float(exposed) / float(sheltered):.3g}. "
               if sheltered and exposed else "")
            # WHAT WAS AND WAS NOT MEASURED. Both numbers are means over the
            # structure's own shadow strip inside the MESHED domain; water the
            # bathymetry left out of that domain - a dredged inner basin the
            # lake-datum grid does not cover - is not in either of them.
            + "Both are means over the meshed domain only. Phase-resolving "
              "screening, not a calibrated hindcast."
        ),
    )


_ARTEMIS_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=20.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / analytic domain",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; a phase-resolving elliptic solve needs several "
        "nodes per wavelength, so 20 m is the floor the harbour grid authors and a "
        "wide AOI is coarsened under the node budget (self-labeled)"
    ),
)

_ARTEMIS_METADATA = AtomicToolMetadata(
    name="artemis_harbor_agitation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_ARTEMIS_RES_SPEC,),
)


artemis_harbor_agitation = register_workflow(
    TelemacWorkflow, _ARTEMIS_METADATA, PARAMS, plan,
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("wave_period_s", "wave_period_note"),
                ("structure", "structure_note"),
                ("target_resolution_m", "target_resolution_note")),
    # A phase-RESOLVING solve is the most mesh-dependent of the family: Kd peaks
    # inside a diffraction fringe, and the sheltered/exposed pair is read inside
    # the shadow gradient. The measured coarse-vs-refined move on both is -30 to
    # -50%. The sheltering RATIO between them converges and is not labeled.
    sensitivity=(("kd_max", "peak"),
                 ("kd_sheltered", "gradient"),
                 ("kd_exposed", "gradient")),
    coerce=(
        location_or_bbox("artemis_harbor_agitation", code_prefix="ARTEMIS",
                         hint="For a natural prompt like 'is the marina at <place> "
                              "sheltered', pass location='<place>'."),
        agitation_mode(),
        compute_class(),
    ),
    doc=DOC,
)
