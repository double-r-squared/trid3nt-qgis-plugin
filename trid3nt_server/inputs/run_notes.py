"""The NOTE and COVERAGE channels of the run in progress: what an ingestion says.

One contextvar list each, opened and drained by whatever runs the fill; outside a
run a note only logs and a coverage line is only a note.
"""

from __future__ import annotations

import contextvars
import logging

logger = logging.getLogger("trid3nt_server.inputs.run_notes")

__all__ = ["bind_coverage", "bind_notes", "cut_coverage", "drain_coverage",
           "drain_notes", "journal_note", "run_coverage"]

#: Notes from the step now running for THIS run's record; a note that only reached a log line dies with the process.
_NOTES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "trid3nt_run_notes", default=None)


def journal_note(text: str) -> None:
    """Record ``text`` on the run in progress. Outside a run it only logs.
    For what a run has to SAY rather than return; the note travels to the journal
    and onto the published layer beside the run's own fallback notes."""
    logger.info("run note: %s", text)
    notes = _NOTES.get()
    if notes is not None:
        notes.append(str(text))



def bind_notes() -> contextvars.Token:
    """Open a note channel for one run -> the token that closes it."""
    return _NOTES.set([])


def drain_notes(token: contextvars.Token) -> list[str]:
    """Close the channel and return what was written into it."""
    notes = _NOTES.get() or []
    _NOTES.reset(token)
    return list(notes)


#: What the cut covers (share of water and land painted, what nothing measured); the card reads it before the solve, the journal after.
_COVERAGE: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "trid3nt_run_coverage", default=None)


def cut_coverage(text: str) -> None:
    """State what the cut covers, on the journal AND on the run's own card."""
    journal_note(text)
    covered = _COVERAGE.get()
    if covered is not None:
        covered.append(str(text))


def run_coverage() -> list[str]:
    """What the run in progress has said its cut covers, in the order it said it."""
    return list(_COVERAGE.get() or ())


def bind_coverage() -> contextvars.Token:
    """Open a coverage channel for one run -> the token that closes it."""
    return _COVERAGE.set([])


def drain_coverage(token: contextvars.Token) -> list[str]:
    """Close the channel and return what was written into it."""
    covered = _COVERAGE.get() or []
    _COVERAGE.reset(token)
    return list(covered)
