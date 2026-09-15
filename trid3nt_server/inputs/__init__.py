"""The typed INPUTS: what a user hands a template, ingested once and read typed after.

A Point, an Extent and a Shape each have ONE ingestion function, named for the
kind in its own module, that takes every form the value arrives in - a canvas
pick, a typed value, a selected layer, a place name - and one home for what is
done with them afterwards. The AOI
acquisition, the geometry-source reader and the layer-field reader live beside
them because a template reads the world through the same door.
"""

from . import user_input
from .bed import Bed, bed
from .boundary import BoundaryRun, boundary_runs, roles_from_runs
from .domain import Domain, domain
from .extent import Extent
from .point import Point, PointOutsideDomainError, point_arg
from .shape import Shape

__all__ = ["Bed", "BoundaryRun", "Domain", "Extent", "Point",
           "PointOutsideDomainError", "Shape", "bed", "boundary_runs", "domain",
           "point_arg", "roles_from_runs", "user_input"]
