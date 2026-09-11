"""The rows every river template declares, and the two steps that establish the
modelled world and measure it: the reach's acquire and its settle.

The reach's own keywords, chain and recipe are each template's, restated."""

from __future__ import annotations

from typing import Any

from trid3nt_server.workflows.runtime import (
    Param,
    ParamRef,
    Ref,
    Step,
    doors,
    param_rows,
)
from trid3nt_server.workflows.telemac.helpers.forcing import CarrierDischarge
from trid3nt_server.workflows.telemac.helpers.reach import Geocode, ReachSeed

__all__ = ["PARAMS", "PARAM_ROWS", "RELEASE", "RELEASE_ROWS", "acquire", "settle"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

#: The friction the reach is solved at when the ask states none. It is named on
#: the declaration because the outflow stage is a normal depth AT this roughness:
#: a stage derived at one number under a deck written at another is a level the
#: run never sits at. The assembler reads the same two numbers.
_STRICKLER = 33.0


class PARAMS:
    """The rows the reach's acquire and settle read, on every river run.

    What is here is what those two steps read, never what a question adds."""

    river_geometry_uri = Param(
        door=doors.USER, optional=True, consequence="aoi",
        derived_when_absent="the reach flowline is fetched fresh for the AOI",
        desc="Reuse an already-fetched river flowline for this reach instead of "
             "re-fetching it")
    friction_coefficient = Param(
        door=doors.USER, optional=True, bounds=(10.0, 90.0),
        user_lever=True, consequence="numerical",
        derived_when_absent=(
            f"the reach is solved at Strickler {_STRICKLER}, which is also the "
            "roughness its outflow stage is derived as a normal depth at"),
        desc="Bed roughness under friction_law")
    friction_law = Param(
        door=doors.USER, optional=True, consequence="numerical",
        type=int,
        derived_when_absent="the coefficient is read as a Strickler one",
        desc="Law interpreting friction_coefficient: 2=Chezy, 3=Strickler, "
             "4=Manning")


    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Place name on the river, geocoded to the reach")

    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place")

    discharge_m3s = Param(
        door=doors.USER, optional=True, units="m^3/s",
        bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
        derived_when_absent=(
            "the steady carrier discharge is resolved from the NOAA National "
            "Water Model at the reach; no NWM coverage refuses typed rather "
            "than falling back to a constant"),
        desc="Steady upstream CARRIER discharge - the river flow that dilutes "
             "and transports the release")

    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the carrier discharge is read at the MOST RECENT published NWM "
            "cycle"),
        desc="The storm/event moment to read the carrier discharge cycle at - "
             "from phrasing like 'during last Tuesday's storm'; an ISO date "
             "or datetime (e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). "
             "Unset reads the most recent published NWM cycle. The NWM PDS "
             "bucket retains only the last ~30 days of history; a deeper "
             "request refuses typed rather than silently reading a "
             "different cycle.")

    output_interval_min = Param(
        door=doors.USER, optional=True, bounds=(0.1, 1440.0),
        units="min", consequence="numerical",
        desc="Result-writing cadence; unset keeps the steering file's own period")

    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")


class RELEASE:
    """The rows a run that RELEASES something at a point in the reach declares.

    Identical whatever is released; the substance's own rows are its template's.
    The release POINT is the template's own row: a Point slot under its role name."""

    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-reach release position, 0=upstream..1=downstream; the source "
             "must sit strictly INSIDE the reach, never on a boundary")

    spill_duration_s = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite pulse injection window")

    source_q_m3s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(0.5, 30.0),
        units="m^3/s", consequence="scenario",
        desc="Point-source discharge of the release itself, small against the "
             "river's carrier flow")

    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 60.0),
        units="m/s", consequence="scenario",
        desc="Sustained wind driving a surface wind-stress term; 0 = no wind")

    wind_direction_deg = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 360.0),
        units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E); only read when "
             "wind_speed_mps > 0")

    rainfall_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.0, 2000.0),
        units="mm/day", consequence="scenario",
        desc="Distributed ON-MESH rainfall applied at every wet node, independent "
             "of the inflow hydrograph")

    evaporation_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.0, 50.0),
        units="mm/day", consequence="scenario",
        desc="Distributed evaporation, subtracted from the net rain flux")

    rainfall_gridmet_window = Param(
        door=doors.USER, optional=True,
        consequence="scenario",
        desc="Real-storm source: an ISO window 'YYYY-MM-DD:YYYY-MM-DD' whose "
             "gridMET domain-mean daily precipitation supersedes rainfall_mm_per_day")


#: The shared rows, as a template composes them with its own.
PARAM_ROWS = param_rows(PARAMS)
RELEASE_ROWS = param_rows(RELEASE)


def acquire(*, rivers: Any, seed: Any) -> tuple[Step, ...]:
    """The steps that establish the modelled world and the flow that carries it.

    ``rivers`` is the template's own flowline row, ``seed`` the Point the one
    centerline is navigated from when it is set."""
    return (
        Geocode.reach(ParamRef("location"), ParamRef("bbox")).named("reach"),
        ReachSeed(reach=Ref("reach"), rivers=rivers, supplied=seed).named("seed"),
        CarrierDischarge(seed=Ref("seed"), explicit=ParamRef("discharge_m3s"),
                         event_time=ParamRef("event_time")
                         ).named("carrier_discharge"),
    )


def settle(*, centerline: Any, reach_polygon: Any, release: Any,
           spill_fraction: Any, marker_label: str = "Release point",
           rain: Any = None, continue_from: Any = None,
           dredge: Any = None) -> Step:
    """Measure the reach the accepted mesh holds -> what the sheet is filled from.

    The bed at the declared roles, the outflow section, the depth, the release."""
    return Step(runner=f"{_AUTHORING}.assembler.settle_reach", stage="author",
                kwargs={
                    "reach": Ref("reach"), "seed": Ref("seed"),
                    "mesh": Ref("mesh"), "centerline": centerline,
                    "reach_polygon": reach_polygon,
                    "carrier_discharge": Ref("carrier_discharge"),
                    "sim_duration_s": ParamRef("sim_duration_s"),
                    "mesh_resolution_m": ParamRef("mesh_resolution_m"),
                    "output_interval_min": ParamRef("output_interval_min"),
                    "friction_law": ParamRef("friction_law"),
                    "friction_coefficient": ParamRef("friction_coefficient"),
                    "release": release,
                    "spill_fraction": spill_fraction,
                    "marker_label": marker_label, "rain": rain,
                    "continue_from": continue_from, "dredge": dredge})
