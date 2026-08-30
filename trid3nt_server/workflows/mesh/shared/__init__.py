"""What every mesher needs and no mesher owns.

A mesher wraps one mesh library; the per-solver geometry writers and the node
primitives every mesher's output goes through are not part of any of them. They
live here so the numbering agreement between a geometry and the boundary file
written against it is made ONCE, and so a second mesher reads the same writer
rather than a copy of it.
"""
