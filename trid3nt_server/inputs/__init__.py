"""The typed INPUTS: what a user hands a template, ingested once and read typed after.

A Point, an Extent and a Shape each have ONE ingestion function, named for the
kind in its own module, that takes every form the value arrives in - a canvas
pick, a typed value, a selected layer, a place name - and one home for what is
done with them afterwards. The AOI
acquisition, the geometry-source reader and the layer-field reader live beside
them because a template reads the world through the same door.
"""

from .extent import Extent
from .point import Point, PointOutsideDomainError, point_arg
from .shape import Shape

__all__ = ["Extent", "Point", "PointOutsideDomainError", "Shape", "point_arg"]
