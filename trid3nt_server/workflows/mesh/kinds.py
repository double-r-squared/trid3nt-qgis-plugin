"""The mesh KIND vocabulary: the shapes a mesher in this tree builds.

Two facts about a template's mesh, and they are never one declaration. The MESH
block (``tool.build_mesh``) states what the DEFAULT BUILD produces. The ``mesh``
row of the template's ``Accepts`` states which kinds it accepts when one is
SUPPLIED or discovered in the case.

The names are a closed ``Literal`` so an author writing either one is autocompleted
to the legal members and a typo is flagged where it is written rather than at the
door it would refuse at. A kind is a member here because a registered mesher builds
it; a word no mesher backs is not vocabulary.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["MeshKind"]


#: The shapes a mesher in this tree builds: a uniform lattice and a triangulation.
MeshKind = Literal["structured_grid", "unstructured_tri"]
