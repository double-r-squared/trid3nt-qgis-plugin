"""The seam surfaces the SAME context inputs the deleted per-family helpers did.

No network. Two halves reproduce the old coverage between them: each composer
declares a ``purpose`` word on the router fetch that feeds a formerly
hand-surfaced input; and the seam maps that word to the SAME context row the
helper emitted. Input by input, none lost."""

from __future__ import annotations

from types import SimpleNamespace

from trid3nt_server.tools.fetchers._router.emit_on_fetch import (
    input_layer_name,
)


def test_a_matched_row_is_asked_under_its_own_name():
    """The purpose word a template used to state on its fetch call is the ROW'S
    NAME now: no template names a fetcher, so the name the seam surfaces the
    input under is the only word the question spells."""
    from trid3nt_contracts.coverage import SourceChoice
    from trid3nt_server.tools.search.match import base_ask

    choice = SourceChoice(slot="bed", need="bathymetry", picked="fetch_dem")
    assert base_ask(choice, "bed", None, None, None, None,
                    None)["purpose"] == "bed"


def _fake_spec(source_class: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"fetch_{source_class}",
        source_class=source_class,
        output=SimpleNamespace(layer_type="raster"),
        vertical_datum=None,
    )


def test_purpose_words_map_to_input_names_the_helpers_used():
    """``input_layer_name`` turns each family's purpose word into the same
    'Input: <word> (...)' provenance the deleted helper emitted -- so the seam's
    surfaced row is name-equivalent, role forced to context by the seam."""
    words = {
        "terrain", "mesh bed", "river geometry", "land cover", "topo-bathymetry",
    }
    for word in words:
        name = input_layer_name(_fake_spec("usgs_3dep"), {}, word)
        assert name.startswith(f"Input: {word} ("), name
        # the composer word wins over the resolved variable/product param.
        name2 = input_layer_name(_fake_spec("nlcd"), {"variable": "dem"}, word)
        assert name2.startswith(f"Input: {word} ("), name2
