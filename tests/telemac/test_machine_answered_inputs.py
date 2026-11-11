"""An input the run's own machinery answers when unstated is stated-only, and a
physics value nothing supplies stays required."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.modules.gaia import _bed
from trid3nt_server.workflows.telemac.templates.bed_scour import bed_scour
from trid3nt_server.workflows.telemac.templates.rain_on_grid import rain_on_grid


@pytest.mark.parametrize("module, names", [
    (rain_on_grid, ("curve_number", "rain_series_mm", "design_storm_mm_per_day")),
    (bed_scour, ("sediment_gradation",)),
])
def test_an_input_the_machinery_answers_is_stated_only(module, names):
    for name in names:
        row = getattr(module.INPUTS, name)
        assert row.stated_only and not row.required, name


def test_a_release_point_the_machinery_cannot_place_stays_required():
    assert bed_scour.INPUTS.release.required


def test_unstated_gradation_leaves_the_decks_single_class_and_stated_wins():
    unstated, _ = _bed({"gradation": None, "presets": bed_scour.GRADATION_PRESETS})
    assert unstated == {"CLASSES_TYPE_OF_SEDIMENT": ["NCO"]}
    stated, _ = _bed({"gradation": "sand_gravel_bimodal",
                      "presets": bed_scour.GRADATION_PRESETS})
    assert stated["CLASSES_INITIAL_FRACTION"] == [0.5, 0.5]
    assert len(stated["CLASSES_SEDIMENT_DIAMETERS"]) == 2


def test_the_rain_record_is_asked_without_refusing_where_none_covers():
    assert rain_on_grid.INPUTS.rain.optional
