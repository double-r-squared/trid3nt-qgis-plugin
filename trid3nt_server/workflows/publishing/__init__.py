"""Publishing: a field becomes a layer, a series a chart, a field over time an
animation - written once, for every engine.

The reads an engine hands over carry no engine word; ``publish.publish`` takes
them, and the COG writer, the styling seam and the outputs manifest beside it
are the mechanism every published output goes through."""

from .publish import Published, quantity_of
from .reads import Deliverable, Field, Frames, Read, Series

__all__ = ["Deliverable", "Field", "Frames", "Published", "Read", "Series",
           "quantity_of"]
