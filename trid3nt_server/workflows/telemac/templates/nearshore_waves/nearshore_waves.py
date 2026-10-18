"""Engine template ``tomawac_nearshore_waves`` - what the swell becomes inshore."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.slots import BED, DOMAIN, EXTENT, LEVEL, WAVE
from trid3nt_server.tools.mesh.recipe import RESOLUTION
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.modules import WAC, series, spectrum
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["CAPTIONS", "MESH", "OUTPUTS", "INPUTS", "STEERING",
           "tomawac_nearshore_waves"]


_GEOMETRY = "coast.slf"
_BOUNDARY = "coast.cli"
_STEERING_FILE = "tom_nearshore.cas"
_SPECTRA = "tom_nearshore.spe"

_STATION = Placed("station", point="station", label="Wave station")


class INPUTS:
    seed = Input().stated()
    station = Input().stated()

    mesh_resolution_m = RESOLUTION

    open_depth_threshold_m = Input()

    extent = Input(EXTENT).need(geometry="rectangle")
    domain = Input(DOMAIN).need("hydrography", kind="coastline", geometry="polyline")
    bed = Input(BED).need("bathymetry")
    wave = Input(WAVE).need("wave series", at="seed")
    level = Input(LEVEL).need("water level series")


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

RESULTS = (RESULT_FILENAME,)

STEERING_FILE = _STEERING_FILE
PREFIX = "tomawac"

REVIEW_TITLE = "Review the sea state, the tide and the water it crosses"

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
    ),
)
