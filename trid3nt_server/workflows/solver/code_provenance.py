"""WHICH CODE produced a run, and whether that code has moved since.

A run artifact outlives the tree that made it. Re-reading a packet from last
week and reasoning about today's code is the quiet failure this module exists to
prevent: the numbers are real, the code that produced them is gone, and nothing
on the page says so.

Two halves. The DISPATCH stamps the working tree's identity onto the run
(:func:`code_identity`), which the completion record carries. A READER of that
run asks what has changed in the engine's own paths since (:func:`staleness`),
and gets back a named warning rather than a diff to interpret.

The engine -> paths table is the one fact neither half can derive. It is written
here, once, rather than in each reader.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.solver.code_provenance")

__all__ = ["ENGINE_PATHS", "code_identity", "engine_paths", "resolve_engine",
           "staleness"]

#: Repository root: this file is ``<root>/trid3nt_server/workflows/solver/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: What each engine's ANSWER depends on, as repo-relative paths. The worker holds
#: the solver and its glue; the workflow package holds the deck, the dispatch and
#: the products. A commit outside both can still change a run - the fetcher
#: router is under everything - but "the engine moved" is the question a reader of
#: an engine's packet is actually asking, and widening it to the whole tree would
#: make every packet stale on every commit and so tell them nothing.
ENGINE_PATHS: dict[str, tuple[str, ...]] = {
    "telemac": ("workers/telemac/", "trid3nt_server/workflows/telemac/"),
}

#: Solver identifiers that are not their engine's name. Every other solver name
#: either IS the engine or starts with it.
_SOLVER_ENGINE_OVERRIDES: dict[str, str] = {
    "artemis_agitation": "telemac",
}

#: A commit list is a warning, not a changelog. Past this many, the warning says
#: how many and shows the newest.
_MAX_LISTED_COMMITS = 12


def _git(*args: str, empty_ok: bool = False) -> str | None:
    """Run git in the repo root; ``None`` on any failure (git missing, no repo).

    ``empty_ok`` separates "the command succeeded and said nothing" from "the
    command failed", which for a log query are opposite answers: an empty log is
    the CLEAN case - no commit touched these paths - and reading it as a failure
    reports a run whose engine never moved as one whose drift is unknown.
    """
    try:
        out = subprocess.run(  # noqa: S603 -- argv list, no shell
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or ("" if empty_ok else None)


def resolve_engine(engine_or_solver: str) -> str | None:
    """The ENGINE a solver identifier belongs to, or ``None`` when none is declared."""
    key = str(engine_or_solver or "").strip().lower()
    if not key:
        return None
    if key in ENGINE_PATHS:
        return key
    override = _SOLVER_ENGINE_OVERRIDES.get(key)
    if override:
        return override
    for engine in ENGINE_PATHS:
        if key.startswith(engine):
            return engine
    return None


def engine_paths(engine_or_solver: str) -> tuple[str, ...]:
    """The repo paths whose commits can change ``engine_or_solver``'s answers.

    Accepts a solver identifier as well as an engine name, because that is what a
    run record carries: ``artemis_agitation`` is the TELEMAC engine.
    """
    engine = resolve_engine(engine_or_solver)
    return ENGINE_PATHS[engine] if engine else ()


def code_identity() -> dict[str, Any]:
    """The working tree's identity, for stamping onto a dispatch.

    ``dirty`` is the honest half: a sha alone claims a run came from a commit,
    and an uncommitted edit makes that claim false. Never raises - a checkout
    that is not a git repo stamps ``None``, which a reader reports as unknown
    rather than as unchanged.
    """
    sha = _git("rev-parse", "HEAD")
    if sha is None:
        return {"code_sha": None, "code_dirty": None}
    status = _git("status", "--porcelain")
    return {"code_sha": sha, "code_dirty": bool(status)}


def staleness(*, code_sha: str | None, engine: str | None,
              code_dirty: bool | None = None) -> dict[str, Any] | None:
    """What has changed in ``engine``'s paths since ``code_sha``, as a warning.

    ``None`` means there is nothing to say: the run came from this exact commit
    with a clean tree, or the question cannot be answered at all (no sha
    recorded, no git, an engine with no declared paths) - and an unanswerable
    question must not be dressed up as a clean bill of health, so the unknown
    cases return a warning that SAYS they are unknown.
    """
    engine_key = resolve_engine(engine) or str(engine or "").strip().lower()
    paths = engine_paths(engine_key)
    if not code_sha:
        return {"kind": "code_identity_unknown",
                "message": ("this run recorded no code identity, so whether the "
                            "engine has changed since it ran cannot be answered")}
    if not paths:
        return {"kind": "engine_paths_unknown", "code_sha": code_sha,
                "message": (f"no code paths are declared for engine {engine!r}, so "
                            "whether it has changed since this run cannot be "
                            "answered")}
    log = _git("log", "--oneline", f"{code_sha}..HEAD", "--", *paths,
               empty_ok=True)
    if log is None:
        head = _git("rev-parse", "HEAD")
        if head == code_sha:
            return None if not code_dirty else _dirty_only(code_sha)
        return {"kind": "code_history_unreadable", "code_sha": code_sha,
                "message": (f"the commits between {code_sha[:9]} and this tree "
                            "could not be read, so engine drift is unknown")}
    commits = [line for line in log.splitlines() if line.strip()]
    if not commits:
        return None if not code_dirty else _dirty_only(code_sha)
    shown = commits[:_MAX_LISTED_COMMITS]
    more = len(commits) - len(shown)
    return {
        "kind": "engine_code_moved",
        "code_sha": code_sha,
        "engine": engine_key,
        "paths": list(paths),
        "commit_count": len(commits),
        "commits": shown,
        "message": (
            f"STALE vs CODE: {len(commits)} commit(s) have touched the "
            f"{engine_key} engine's paths since this run was dispatched at "
            f"{code_sha[:9]}"
            + (f" (newest {len(shown)} listed, {more} more)" if more else "")
            + "; the numbers below came from the older code."
        ),
    }


def _dirty_only(code_sha: str) -> dict[str, Any]:
    """The tree is at the run's commit but carries uncommitted edits."""
    return {
        "kind": "working_tree_dirty",
        "code_sha": code_sha,
        "message": ("the tree is at this run's commit but carries uncommitted "
                    "changes, so what produced these numbers is not fully "
                    "recorded anywhere"),
    }
