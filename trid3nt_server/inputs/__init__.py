"""The typed INPUTS: what a user hands a template, ingested once and read typed after.

An ingestion resolves what it was handed and never fetches, so a place name refuses
and names the geocoder.
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
