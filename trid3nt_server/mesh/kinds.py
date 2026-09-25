"""The mesh KIND vocabulary: the shapes a mesher in this tree builds.

A name is a member only where a registered mesher builds it. What a template
BUILDS by default and what it ACCEPTS when one is supplied are two separate
declarations, never one."""

from __future__ import annotations

from typing import Literal

__all__ = ["MeshKind"]


#: The shapes a mesher in this tree builds: a uniform lattice and a triangulation.
MeshKind = Literal["structured_grid", "unstructured_tri"]
