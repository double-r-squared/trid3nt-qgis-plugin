"""What a template accepts when something is SUPPLIED to it, role by role.

A row names the kinds that role's supplied path was tested against and membership
is the whole of the door's test; an absent row is the refusal.
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
    The mesh role is typed to the closed :data:`MeshKind` vocabulary, so a typo is
    refused where it is written rather than at the door."""

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
