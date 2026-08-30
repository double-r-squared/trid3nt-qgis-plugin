"""The mesh KIND vocabulary, and the accept-set a template declares over it.

Two facts about a template's mesh, and they are never one declaration. The MESH
block (``tool.build_mesh``) states what the DEFAULT BUILD produces. ``Compatible``
states which kinds of mesh the template accepts when one is SUPPLIED or discovered
in the case - the set its pipeline was built and tested against.

The kind names are a closed ``Literal`` so an author writing an accept-set is
autocompleted to the legal members and a typo is flagged where it is written
rather than at the door it would refuse at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Compatible", "MeshKind"]


#: The shapes a mesher in this tree builds: a uniform lattice, a triangulation,
#: and the graded cell mesh an engine realizes from an authoring bundle.
MeshKind = Literal["structured_grid", "unstructured_tri", "graded_cells"]


@dataclass(frozen=True, init=False)
class Compatible:
    """The kinds of supplied mesh a template accepts - a frozen set, and membership.

    ABSENCE IS A REFUSAL, not a permission: a template that declares no accept-set
    has no tested supplied-mesh path, so the door refuses rather than admitting
    whatever it was handed.
    """

    kinds: tuple[MeshKind, ...] = ()

    def __init__(self, *kinds: MeshKind) -> None:
        object.__setattr__(self, "kinds", tuple(kinds))

    def __contains__(self, kind: object) -> bool:
        return kind in self.kinds
