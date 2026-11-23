"""The review reads the Form's display values without changing its wire rows."""

from types import SimpleNamespace

from trid3nt.sdk.model import Form
from trid3nt.workflows.runtime.fill import ACCEPTED, Verdict
from trid3nt.workflows.runtime.journal import bind_choices, drain_choices
from trid3nt.workflows.runtime.review import card_rows
from trid3nt_contracts.common import Origin
from trid3nt_contracts.coverage import SourceChoice, SourceOption


def test_form_review_choices_keep_the_wire_rows_and_order():
    source = SourceChoice(slot="bed", need="terrain", picked="fetch_dem", sentence="bed: measured",
                          rows=[SourceOption(fetcher="fetch_dem")])
    slot = SimpleNamespace(desc="Depth", unit="m", whole_bounds=None, rubrique=("Depth",))
    row = SimpleNamespace(value=4, slot=slot, provenance=SimpleNamespace(origin=Origin.USER))
    sheet = SimpleNamespace(body=SimpleNamespace(MODULE_INPUT={}), filled={"DEPTH": row},
                            required=lambda: (), engine_rows=lambda: ([], set()))
    token = bind_choices([source])
    try:
        previous = card_rows(sheet)
    finally:
        drain_choices(token)
    form = Form({"DEPTH": Verdict(ACCEPTED, 4, origin=Origin.USER)}, True, review_choices=(source,))
    assert card_rows(sheet, form) == previous


async def _edited_card(monkeypatch):
    from trid3nt.inputs import review as gate
    from trid3nt.workflows.runtime import script
    from trid3nt.workflows.runtime.review import _review

    sheet = SimpleNamespace(body=SimpleNamespace(MODULE_INPUT={}), filled={},
                            required=lambda: (), engine_rows=lambda: ([], set()))

    async def fill(values, edits):
        return sheet

    async def answer(**review):
        await review["apply_revision"]({"spill_duration_s": 360})
        card = await review["present"]()
        assert card.param_sheet.rows[0].value == 360
        return gate.ReviewOutcome(proceed=True, entries=review["entries"], params=review["params"])

    monkeypatch.setattr(gate, "gate_input_review", answer)
    monkeypatch.setattr(script, "_HOST", lambda: lambda *args: {})
    with script.capture({"spill_duration_s": 300}) as captured:
        await _review(fill, {"spill_duration_s": 300}, steering=SimpleNamespace(ASSERTED={"duration": "spill_duration_s"}),
                      workflow="telemac_dye_release", title="", spent=())
        assert captured.params == {"spill_duration_s": 360}


def test_revised_value_is_captured_after_the_card(monkeypatch):
    import asyncio

    asyncio.run(_edited_card(monkeypatch))
