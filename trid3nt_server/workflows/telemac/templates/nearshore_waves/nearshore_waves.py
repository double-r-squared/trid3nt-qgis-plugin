"""Engine template ``tomawac_nearshore_waves`` - what the swell becomes inshore."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors, lever,
)
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.modules import WAC, series, spectrum
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DEFAULT_OPEN_DEPTH_M", "DOC",
           "MESH", "OUTPUTS", "PARAMS", "STEERING", "tomawac_nearshore_waves"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))

# every boundary stretch at least this deep opens
DEFAULT_OPEN_DEPTH_M: float = -12.0


class PARAMS:
    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "the buoy nearest the centre of the water the window was cut to "
            "is the one the open edge is forced at"),
        desc="Where OFFSHORE the incoming sea state is measured, as a Point: "
             "the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The nearest "
             "buoy to it is the record the open boundary is forced at, so put "
             "it on the water the swell arrives across")
    station = Param(
        door=doors.USER, user_lever=True, consequence="scenario", type=Point,
        desc="Where INSHORE the waves are read over time, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The height, "
             "the period and the direction are charted at the node of the mesh "
             "it settles onto, so put it on the water you are asking about")

    mesh_resolution_m = lever(
        "mesh_resolution_m",
        desc="Target element edge length the water is triangulated at; the "
             "waves shoal and then break where the DEPTH falls, so what this "
             "has to resolve is the slope of the bed across the surf zone")

    open_depth_threshold_m = Param(
        door=doors.SCENARIO, default=DEFAULT_OPEN_DEPTH_M,
        bounds=(-200.0, -1.0), units="m", consequence="physics",
        desc="How deep a boundary stretch must reach for it to be designated "
             "the OPEN edge the measured sea state enters through; every "
             "stretch that reaches it opens, and a window where none does has "
             "no edge for the spectrum and refuses")


_GEOMETRY = "coast.slf"
_BOUNDARY = "coast.cli"
_STEERING_FILE = "tom_nearshore.cas"
_SPECTRA = "tom_nearshore.spe"

_STATION = Placed("station", point="station", label="Wave station")


class DATA:
    extent = Data.supplied(geometry="rectangle")
    domain = Data.need("hydrography", kind="coastline", geometry="polyline")
    bed = Data.need("bathymetry")
    wave = Data.need("wave series", at="seed")
    level = Data.need("water level series").optional()
    mesh = Data.supplied(geometry="mesh").optional()


class STEERING(WAC):
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    ED_RESULTS_FILE = RESULT_FILENAME

    # the run covers TIME_STEP x NUMBER_OF_TIME_STEP: one measured hour
    TIME_STEP = 10.0
    NUMBER_OF_TIME_STEP = 360
    # in solver steps, not seconds
    PERIOD_FOR_GRAPHIC_PRINTOUTS = 10
    PERIOD_FOR_LISTING_PRINTOUTS = 60

    # a sector every fifteen degrees
    NUMBER_OF_DIRECTIONS = 24
    NUMBER_OF_FREQUENCIES = 25
    MINIMAL_FREQUENCY = 0.04

    # JONSWAP; the wave slot fills its height, period and direction
    TYPE_OF_BOUNDARY_DIRECTIONAL_SPECTRUM = 6

    PUNCTUAL_RESULTS_FILE = _SPECTRA

    # Battjes-Janssen
    DEPTH_INDUCED_BREAKING_DISSIPATION = 1
    BOTTOM_FRICTION_DISSIPATION = 1


MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent="domain",
    resolution_m="mesh_resolution_m",
    ops=[
        mesh_op("feature_sizing_function"),
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation"),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source="bed"),
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold="open_depth_threshold_m"),
    ],
)

OUTPUTS = [
    series("HM0", at="station").chart(),
    series("TPD", at="station").chart(),
    series("DMOY", at="station").chart(),
    spectrum(at="station").chart(),
]
CAPTIONS = {"HM0": "significant wave height", "TPD": "peak wave period",
            "DMOY": "mean wave direction", "BETA": "breaking rate",
            "DBR": "breaker dissipation", "wave": "a sea state",
            "level": "a water-surface elevation",
            "spectrum": "wave energy by frequency at the station"}

_METADATA = AtomicToolMetadata(
    name="tomawac_nearshore_waves",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

MESH_ON = "domain"
SUPPLIED_MESH = "mesh"

RESULTS = (RESULT_FILENAME,)

STEERING_FILE = _STEERING_FILE
PREFIX = "tomawac"

REVIEW_TITLE = "Review the sea state, the tide and the water it crosses"

DOC = dict(
    summary="NEARSHORE WAVES: what the offshore swell becomes at the shore - "
            "how high, how long, which way, and where it breaks.",
    routing=(
        "THE tool for \"how big are the waves at the beach\", \"what does this "
        "swell do as it comes in\", \"where does the surf break here\", \"wave "
        "height and period along this coast\". TOMAWAC, the spectral wave "
        "model, over the water the window is cut to at the coastline: the open "
        "edge forced at a sea state a buoy MEASURED, shoaling and refraction "
        "over the charted bed, breaking and bottom friction taking them down. "
        "Produces the height, the periods, the directions and the breaking "
        "band, animated, three of them charted where the ask points. Deck "
        "opinions, by keyword: TIME STEP, NUMBER OF TIME STEP, the spectral "
        "grid, DEPTH-INDUCED BREAKING DISSIPATION. Supply the window, "
        "`station`, and `event_time`."
    ),
    not_for=(
        "agitation behind a breakwater, which is phase-resolving "
        "(`artemis_harbor_agitation`); storm surge or the water level itself; "
        "the buoy record alone (`fetch_ndbc_buoys`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved sea state, the tide and the mesh '
         'for review/edit before the solve and WAITS; "auto" (session default) '
         "proceeds with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable TOMAWAC "
        "wrote, styled on one mesh layer and animated, plus the height, the "
        "period and the direction charted at the station. A coast the swell "
        "reaches calmly charts a small height and no breaking, which is "
        "what that place did in that hour. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)

tomawac_nearshore_waves = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    sensitivity=(("significant_wave_height", "peak"),),
    coerce=(
        point_arg("seed", tool="tomawac_nearshore_waves",
                  prompt="Click offshore, where the incoming sea state is "
                         "measured",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="tomawac_nearshore_waves",
                  prompt="Click on the water where the waves should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
