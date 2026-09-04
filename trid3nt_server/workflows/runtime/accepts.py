"""What a template accepts when something is SUPPLIED to it, role by role.

A template's PARAMS say what it can be TOLD. This says what it can be HANDED, and
it says it one ROLE at a time: a mesh, a release point, and whatever the
geometry-by-name seam brings next. Each row names the kinds that role's pipeline
was built and TESTED against, and membership in it is the whole of the test the
supply door runs.

ABSENCE IS THE REFUSAL, and it is per role. A template with no ``mesh`` row has no
tested supplied-mesh path, so the door refuses rather than admitting whatever it
was handed - while the same template may still accept a release, because two rows
are two claims about two pipelines and neither one licenses the other. A row is
written when the path it describes is tested, never in advance of it.

An accept-set that names NOTHING is not a stricter version of that absence: it is
authored nonsense, and it refuses where it is written rather than at a door nobody
would reach.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from trid3nt_server.workflows.runtime.errors import DeclarativeError
from trid3nt_server.workflows.mesh.kinds import MeshKind

__all__ = ["Accepts", "AcceptsDeclarationError"]


class AcceptsDeclarationError(DeclarativeError):
    """An accept-set no template could have meant, refused where it is authored."""

    error_code = "ACCEPTS_INVALID"


@dataclass(frozen=True, init=False)
class Accepts:
    """The kinds a template accepts for each SUPPLY ROLE - frozen rows, and membership.

    The mesh role is typed to the closed :data:`MeshKind` vocabulary, so an author
    writing one is autocompleted to the legal members and a typo is flagged where
    it is written rather than at the door it would refuse at.
    """

    roles: Mapping[str, tuple[str, ...]]

    def __init__(self, *, mesh: tuple[MeshKind, ...] | None = None,
                 release: tuple[str, ...] | None = None) -> None:
        declared = {"mesh": mesh, "release": release}
        blank = sorted(role for role, kinds in declared.items()
                       if kinds is not None and not kinds)
        if blank:
            named = ", ".join(f"{role}=()" for role in blank)
            raise AcceptsDeclarationError(
                f"Accepts({named}) names a role and then no kind for it. An empty "
                "row is not a stricter refusal than no row at all - leaving the "
                "role out is the one way to say a template accepts nothing "
                "supplied for it.")
        rows = {role: tuple(kinds) for role, kinds in declared.items() if kinds}
        if not rows:
            raise AcceptsDeclarationError(
                "Accepts() declares no role at all, which is not something a "
                "template means: one that accepts nothing supplied declares no "
                "accept-set, and that absence is already the refusal. Name the "
                "roles whose supplied path this template was tested against.")
        object.__setattr__(self, "roles", MappingProxyType(rows))

    def kinds(self, role: str) -> tuple[str, ...] | None:
        """The kinds accepted for ``role``; ``None`` when the role has no row."""
        return self.roles.get(role)

    def accepts(self, role: str, kind: object) -> bool:
        """Is ``kind`` a member of ``role``'s row? An absent row is a refusal."""
        return kind in (self.roles.get(role) or ())
