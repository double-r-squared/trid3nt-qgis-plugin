"""The review card: the sheet as the rows a person reads and edits, and the hold that proceeds on it.

``card_rows`` renders a filled sheet; ``_review`` holds on the card, re-filling on an
edit and refusing a param the card cannot re-fill; ``_seat_on`` seats a person's edits
in the run's mapping. The card's engine sections come off the sheet's ``engine_rows``
hook, so this module names no engine."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts.common import Origin, SyntheticInput
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.errors import DeclarativeError

from .module import SlotRefused, accept
from .sheet import Sheet, fill_coupled
from .sheet import fill as fill_slots

__all__ = ["card_rows", "fill_sheet"]

logger = logging.getLogger(__name__)


def card_rows(sheet: Sheet) -> list[ParamSheetRow]:
    """The sheet as the card renders it: what is SET, what the run WRITES, what
    is OPEN, then the rest.

    The rest is the whole module, folded under advanced with its engine default."""
    rows = _source_rows() + _coverage_rows() + [_slot_row(name, row)
                                               for name, row
                                               in sheet.filled.items()]
    engine, generated = sheet.engine_rows()
    rows += engine
    rows += [_open_row(slot) for slot in sheet.required()]
    # The advanced fold reads down the dictionary's RUBRIQUES and, inside one, its own order. A keyword the sheet GENERATES
    # is already a written row above; the fold is engine DEFAULTS only.
    rest = [slot for name, slot in sheet.body.MODULE_INPUT.items()
            if name not in sheet.filled and name not in generated
            and not slot.is_required]
    return rows + [_default_row(slot) for slot in
                   sorted(rest, key=lambda slot: _group(slot))]


def _source_rows() -> list[ParamSheetRow]:
    """One row per input the match filled: the ranked list, pick highlighted.
    The card renders the list the model was given. Not editable: a source is superseded by supplying the slot."""
    from trid3nt_server.workflows.runtime.journal import run_choices

    return [ParamSheetRow(
        name=f"{choice.slot} source", value=choice.picked or "nothing matched",
        desc=f"Which source filled the {choice.slot} slot, matched on "
             f"{choice.need}.",
        origin=Origin.PRODUCER, editable=False,
        source_badge=(f"{len(choice.rows)} sources weighed"
                      if choice.tie else "matched on the rows"),
        note=choice.sentence, choices=choice, group="Sources")
        for choice in run_choices()]


def _coverage_rows() -> list[ParamSheetRow]:
    """WHAT THE CUT COVERS, over the water and over the land, one row each.
    On the card so the unmeasured share is weighed BEFORE the solve. Not editable: an op or supplying the slot changes it."""
    from trid3nt_server.inputs.run_notes import run_coverage

    return [ParamSheetRow(
        name=f"coverage {index}", value=line[:200], desc=line[:512],
        origin=Origin.DERIVED, editable=False,
        source_badge="measured on the grid the rows were merged at",
        group="Sources")
        for index, line in enumerate(run_coverage(), start=1)]


async def _review(fill: Callable[[Mapping[str, Any], Mapping[str, Any]], Any],
                  params: Mapping[str, Any], *, steering: type, workflow: str,
                  title: str, input_mode: str | None,
                  spent: Sequence[str],
                  seat: Callable[[Mapping[str, Any]], None] | None = None,
                  user: Sequence[str] = ()) -> Sheet:
    """Show the filled sheet and HOLD -> the sheet the person proceeded on.

    An edit re-fills its input and redraws the card, or refuses by name."""
    from trid3nt_server.inputs.gate.input_review import (
        GateCard,
        gate_input_review,
        render_input_review_lines,
    )

    values, edits, user = dict(params), {}, set(user)
    own = {name for name in _strings(steering.ASSERTED)
           if name in values} - set(spent)
    sheet = await fill(values, edits)

    def rows() -> list[ParamSheetRow]:
        return [_param_row(name, values[name], name in user)
                for name in sorted(own)] \
            + card_rows(sheet)

    async def card() -> GateCard:
        drawn = rows()
        return GateCard(lines=render_input_review_lines(_entries(drawn)),
                        param_sheet=ParamSheet(
                            workflow=workflow, rows=drawn,
                            title=title or f"Review the {workflow} sheet"))

    async def revise(revised: Mapping[str, Any]) -> None:
        nonlocal sheet
        for name in revised:
            if name in params and name not in own:
                raise SlotRefused(
                    f"{name} is a param of {workflow} the card cannot re-fill: "
                    + ("another stage of this run reads it off the run itself"
                       if name in spent else "no keyword of this sheet reads it")
                    + f". State {name} on the run instead.")
            if name not in params:
                try:
                    accept(steering, name, revised[name])
                except SlotRefused as exc:
                    raise SlotRefused(
                        f"{name} is not an input of {workflow}: no param of it "
                        f"is named so, and {exc}") from exc
        for name, value in revised.items():
            (values if name in own else edits)[name] = value
        user.update(own & set(revised))
        sheet = await fill(values, edits)

    outcome = await gate_input_review(
        tool_name=workflow, mode=input_mode, entries=_entries(rows()),
        params={}, present=card, apply_revision=revise)
    if not outcome.proceed:
        # A refusal keeps its reason's code; only a person's own cancel is the decline code every gate cancels under.
        raise DeclarativeError(
            outcome.cancel_reason or f"{workflow} was cancelled at the review.",
            error_code="NO_SESSION" if outcome.cancel_code == "no_session"
            else "USER_INPUT_CANCELLED")
    if seat is not None:
        seat(values)
    return sheet


def _strings(value: Any) -> set[str]:
    """Every plain string a template's assertions carry, the params a keyword reads by name among them."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {s for item in value.values() for s in _strings(item)}
    if isinstance(value, (list, tuple)):
        return {s for item in value for s in _strings(item)}
    return set()


def _entries(rows: Sequence[ParamSheetRow]) -> list[SyntheticInput]:
    """The card's rows as provenance lines; a list value is narrated whole.
    A row stating what the deck writes holds no filled value and reads as derived."""
    return [SyntheticInput(
                param=row.name, basis=row.origin or Origin.DERIVED,
                note=row.source_badge,
                value=(row.value if isinstance(row.value, (int, float, str, bool))
                       else "; ".join(str(v) for v in row.value)))
            for row in rows if row.value is not None and not row.advanced]


def _seat_on(env: Any) -> Callable[[Mapping[str, Any]], None]:
    """Seat the values a person proceeded on in the run's mapping, so every read
    of a param after the sheet - a chart's reference among them - reads the edit."""
    from trid3nt_server.workflows.runtime.fill import ACCEPTED, Verdict

    def seat(values: Mapping[str, Any]) -> None:
        edited = {name: value for name, value in values.items()
                  if value != env.run.get(name)}
        env.inputs.update({name: Verdict(ACCEPTED, value, origin=Origin.USER)
                           for name, value in edited.items()})
        env.run.update(edited)
    return seat


def _param_row(name: str, value: Any, user: bool) -> ParamSheetRow:
    """One template param a keyword of this sheet reads, editable by its name."""
    return ParamSheetRow(
        name=name, value=(value if isinstance(value, (int, float, str, bool, list))
                          or value is None else str(value)),
        desc=f"The template param {name}; an edit re-fills every keyword that "
             "reads it.",
        origin=(Origin.USER if user else
                None if value is None else Origin.TEMPLATE),
        source_badge="this run's template param", group="Template")


def _slot_row(name: str, row: Any) -> ParamSheetRow:
    """One SET slot, as the card renders it: the value, the unit it is read in,
    the range it is taken inside, and where it came from."""
    value = row.value if isinstance(row.value, (int, float, str, bool, list)) \
        else str(row.value)
    return ParamSheetRow(
        name=name, value=value, desc=row.slot.desc[:512],
        units=row.slot.unit or None, bounds=row.slot.whole_bounds,
        origin=row.provenance.origin, source_badge=str(row.provenance),
        group=_group(row.slot))


def _open_row(slot: Any) -> ParamSheetRow:
    """One OPEN MANDATORY slot: an empty the run cannot begin without."""
    return ParamSheetRow(
        name=slot.identifier, value=None, desc=slot.desc[:512],
        editable=True, group=_group(slot),
        source_badge="required: the dictionary marks this file OBLIG")


def _default_row(slot: Any) -> ParamSheetRow:
    """One slot this run leaves to the engine, under the advanced fold.

    Carries the value the engine will use rather than an empty."""
    return ParamSheetRow(
        name=slot.identifier,
        value=None if slot.is_open else slot.engine_default,
        desc=slot.desc[:512], editable=True, advanced=True, group=_group(slot),
        source_badge=("open: the dictionary gives it no default"
                      if slot.is_open else "engine default"))


def _group(slot: Any) -> str:
    """The dictionary's own top-level rubrique - what the advanced fold sorts by."""
    return slot.rubrique[0] if slot.rubrique else ""


async def fill_sheet(*, engine: str, floor: Callable[[type, Mapping[str, Any]], tuple[dict[str, Any], dict[str, dict[str, Any]]]],
                     steering: type, produced: Mapping[str, Any], params: Mapping[str, Any],
                     settled: Mapping[str, Any] | None = None, workflow: str, title: str,
                     keywords: Mapping[str, Any], input_mode: str | None, spent: Sequence[str] = (),
                     compose: Callable[[Sheet, Mapping[str, Mapping[str, Any]], Mapping[str, Any]], Sheet] | None = None,
                     seat: Callable[[Mapping[str, Any]], None] | None = None,
                     write: Callable[[Sheet], Any] | None = None, user: Sequence[str] = ()) -> Sheet:
    """Set the body's slots against what the run holds -> the sheet, HELD on its one review.

    ``floor`` is the engine's routing of the raw ``keywords`` onto its bodies: the carrier's
    values are filled last and therefore beat a template value, and each coupled module's
    are merged onto that body's deck by ``compose`` (the engine's own composition, given the
    sheet, the coupled floor and the values held) or, where the engine states none, as written.
    ``seat`` takes the param values the person proceeded on; ``write`` writes and checks the deck
    of every sheet a card is drawn from; ``user`` names the params the call stated."""
    own, coupled = floor(steering, keywords)

    async def fill(values: Mapping[str, Any], edits: Mapping[str, Any]) -> Sheet:
        # A composite may read fetched data at the fill, so the fill runs off the loop.
        sheet = await asyncio.to_thread(
            fill_slots, steering, template=workflow, produced=dict(produced),
            params=dict(values), settled=settled, **{**own, **edits})
        if coupled and compose is not None:
            held = {**produced, **values,
                    **{name: row.value for name, row in sheet.filled.items()}}
            sheet = await asyncio.to_thread(compose, sheet, coupled, held)
        elif coupled:
            # After the fill: the coupled decks are what the carrier's coupling composite wrote into the sheet's files.
            sheet = fill_coupled(sheet, coupled)
        if write is not None:
            # The deck is written and its staged directory checked before the card is drawn, so a proceed is followed by the solve alone.
            await write(sheet)
        return sheet

    sheet = await _review(fill, params, steering=steering, workflow=workflow,
                          title=title, input_mode=input_mode, spent=spent,
                          seat=seat, user=user)
    logger.info("%s sheet filled: %s states %d keywords, %d open (%d required)",
                engine, sheet.body.__name__, len(sheet.filled),
                len(sheet.open()), len(sheet.required()))
    return sheet
