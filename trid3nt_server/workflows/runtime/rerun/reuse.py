"""What a derived run INHERITS: where reuse stops, and which artifacts survive it.

An override moves a value; the plan says which work that value reaches, read off
the declaration by the same walk the validator and the interpreter use.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..data import DataDecl
from ..interpreter import expand_plan
from ..plan import Continued, ParamRef, Plan, Ref, declared_reads

__all__ = ["reuse_plan"]


def reuse_plan(plan: Plan, data: Sequence[DataDecl], changed: Sequence[str],
               *, continued: bool = False) -> tuple[int | None, frozenset[str]]:
    """``(cut, reusable_data)`` for a child whose sheet moved on ``changed``.
    ``cut`` is the first node an override reaches, ``None`` when the plan reads none
    of them; ``reusable_data`` is the ``Data`` no dirty producer or re-decided step feeds.
    ``continued`` dirties the step that opens the water on the run this one
    picks up from: a hot start moves no value and still re-runs the solve."""
    # The answer is a PREFIX, not a scatter: a step reads more than its declared
    # kwargs - it reads the DOMAIN the steps before it bound, which no declaration
    # names - so the first node an override reaches is a CUT, and everything from
    # it on is the child's to do.
    nodes = expand_plan(plan)
    decls = {decl.name: decl for decl in data}
    moved = set(changed)
    dirty: set[str] = set()

    # Data producers may read steps and steps may read Data, so the two walks
    # feed each other and neither can be run once.
    for _ in range(len(nodes) + len(decls) + 1):
        grew = False
        for name, decl in decls.items():
            if name not in dirty and _reads(dict(decl.producer_kwargs), moved, dirty):
                dirty.add(name)
                grew = True
        for node in nodes:
            name = _node_key(node)
            if name in dirty:
                continue
            read = node.spec if node.kind == "when" else dict(node.step.kwargs)
            if (continued and _reads_the_continuation(read)) or \
                    _reads(read, moved, dirty):
                dirty.add(name)
                grew = True
        if not grew:
            break

    cut = min((node.index for node in nodes if _node_key(node) in dirty),
              default=None)
    if cut is None:
        return None, frozenset(decls)
    redecided = {_node_key(node) for node in nodes if node.index >= cut}
    keep = {name for name, decl in decls.items()
            if name not in dirty
            and not _reads(dict(decl.producer_kwargs), set(), redecided)}
    return cut, frozenset(keep)


def _node_key(node: Any) -> str:
    """What a Ref to this node's result would name it.
    A branch marker is not Ref-able and every one shares a placeholder step, so
    branches are keyed by INDEX and one dirty branch cannot dirty the rest."""
    if node.kind == "when":
        return f"when:{node.index}"
    return node.step.name or node.step.label


def _reads_the_continuation(value: Any) -> bool:
    """Does this declared value read the run's CONTINUATION?"""
    if value is Continued:
        return True
    if isinstance(value, Mapping):
        return any(_reads_the_continuation(v) for v in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_reads_the_continuation(v) for v in value)
    return False


def _reads(value: Any, params: set[str], roots: set[str]) -> bool:
    """Does this declared value read any of these params, or anything dropped?"""
    if any(ref.name in params for ref in declared_reads(value, ParamRef)):
        return True
    return any(ref.root in params or ref.root in roots
               for ref in declared_reads(value, Ref))
