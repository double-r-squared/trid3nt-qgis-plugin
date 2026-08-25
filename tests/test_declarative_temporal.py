"""Temporal transforms: quantity-class methods, gap refusal, the unit table.

Synthetic series only - this file exercises MECHANISM, and the doctrine it
encodes has to hold for a series nobody fetched as much as for one somebody did.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trid3nt_server.workflows.lib import (
    CATEGORICAL,
    Data,
    Fetch,
    Param,
    PlanValidationError,
    RATE,
    Ref,
    STATE,
    ResampleSpec,
    Step,
    TemporalGapError,
    TemporalShapeError,
    TemporalSpec,
    TemporalUnitsError,
    UnitsSpec,
    Plan,
    convert_units,
    doors,
    interpret,
    resolve_params,
    transform_series,
    transform_value,
)

_HERE = "tests.test_declarative_temporal"
_SEEN: list[dict] = []


async def stub_producer(**kwargs):
    _SEEN.append(kwargs)
    return "s3://b/produced.tif"


async def stub_step(**kwargs):
    return {"uri": "s3://b/k.tif", "seen": kwargs}


def _series(values, freq="6h", start="2026-08-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq=freq))


def _resample(to, method=None, max_gap="native*3"):
    return TemporalSpec(resample=ResampleSpec(to=to, method=method, max_gap=max_gap))


# --- quantity-class method defaults ---------------------------------------- #


def test_a_state_upsamples_by_linear_interpolation():
    out = transform_series(_series([1.0, 2.0]), _resample("1h"), quantity=STATE)
    assert out.note.startswith("resampled 6h->1h linear")
    # Six hours of straight line between 1 and 2: the third hour sits at 1.5.
    assert out.values.loc["2026-08-01T03:00"] == pytest.approx(1.5)


def test_a_rate_upsamples_conservatively_by_holding_its_interval():
    out = transform_series(_series([1.0, 2.0]), _resample("1h"), quantity=RATE)
    assert out.note.startswith("resampled 6h->1h conservative")
    # A rate reported FOR an interval is constant across it; interpolating one
    # would move mass the source never reported moving.
    assert out.values.loc["2026-08-01T03:00"] == pytest.approx(1.0)
    assert out.values.loc["2026-08-01T06:00"] == pytest.approx(2.0)


def test_a_rate_downsamples_to_the_interval_mean_which_preserves_the_total():
    out = transform_series(_series([1.0, 3.0, 5.0, 7.0]), _resample("1D"),
                           quantity=RATE)
    assert out.note.startswith("resampled 6h->1D conservative")
    assert out.values.tolist() == pytest.approx([4.0])


def test_a_categorical_moves_by_nearest_and_never_averages():
    out = transform_series(_series([1, 1, 2, 2]), _resample("3h"),
                           quantity=CATEGORICAL)
    assert out.note.startswith("resampled 6h->3h nearest")
    assert set(out.values.tolist()) <= {1, 2}


def test_a_categorical_refuses_a_declared_interpolating_method():
    with pytest.raises(Exception) as caught:
        transform_series(_series([1, 2]), _resample("1h", method="linear"),
                         quantity=CATEGORICAL)
    assert "CATEGORICAL" in str(caught.value)


def test_a_declared_method_overrides_the_class_default():
    out = transform_series(_series([1.0, 2.0]), _resample("1h", method="linear"),
                           quantity=RATE)
    assert out.values.loc["2026-08-01T03:00"] == pytest.approx(1.5)


# --- interpolation is DECLARED --------------------------------------------- #


def test_no_declaration_means_no_resample_and_the_note_says_so():
    s = _series([1.0, 2.0, 3.0])
    out = transform_series(s, None, quantity=STATE)
    assert out.values.equals(s)
    assert out.note == "native 6h state, no resample declared"


def test_a_target_at_the_native_cadence_states_that_it_moved_nothing():
    out = transform_series(_series([1.0, 2.0, 3.0]), _resample("6h"), quantity=STATE)
    assert "no resample" in out.note
    assert out.values.tolist() == [1.0, 2.0, 3.0]


# --- gaps refuse, never bridge --------------------------------------------- #


def test_a_hole_wider_than_max_gap_refuses_rather_than_interpolating_across_it():
    s = pd.Series([1.0, 2.0, 3.0, 4.0], index=pd.DatetimeIndex(
        ["2026-08-01T00:00", "2026-08-01T06:00", "2026-08-03T00:00",
         "2026-08-03T06:00"]))
    with pytest.raises(TemporalGapError) as caught:
        transform_series(s, _resample("1h"), quantity=STATE)
    message = str(caught.value)
    assert "42h hole" in message and "18h" in message


def test_a_hole_inside_max_gap_is_refinement_and_passes():
    s = pd.Series([1.0, 2.0, 3.0], index=pd.DatetimeIndex(
        ["2026-08-01T00:00", "2026-08-01T06:00", "2026-08-01T18:00"]))
    out = transform_series(s, _resample("1h"), quantity=STATE)
    assert out.note.startswith("resampled 6h->1h linear")


def test_an_explicit_max_gap_interval_overrides_the_native_multiple():
    s = pd.Series([1.0, 2.0, 3.0], index=pd.DatetimeIndex(
        ["2026-08-01T00:00", "2026-08-01T06:00", "2026-08-02T00:00"]))
    with pytest.raises(TemporalGapError):
        transform_series(s, _resample("1h", max_gap="12h"), quantity=STATE)
    assert transform_series(s, _resample("1h", max_gap="native*4"), quantity=STATE)


# --- the unit table -------------------------------------------------------- #


@pytest.mark.parametrize("value,source,target,expected", [
    (1.0, "in/day", "mm/day", 25.4),
    (1.0, "mm/h", "mm/day", 24.0),
    (1.0, "cfs", "m3/s", 0.028316846592),
    (32.0, "degF", "degC", 0.0),
    (273.15, "K", "degC", 0.0),
    (1.0, "ft", "m", 0.3048),
    (1000.0, "ug/L", "mg/L", 1.0),
    (5.0, "mm/day", "mm/day", 5.0),
])
def test_the_unit_table_converts_within_a_dimension(value, source, target, expected):
    assert convert_units(value, source, target) == pytest.approx(expected)


def test_a_cross_dimension_conversion_refuses_rather_than_guessing():
    with pytest.raises(TemporalUnitsError) as caught:
        convert_units(1.0, "m", "m3/s")
    assert "different quantities" in str(caught.value)


def test_an_unlisted_unit_refuses_and_names_the_table():
    with pytest.raises(TemporalUnitsError) as caught:
        convert_units(1.0, "furlongs/fortnight", "m")
    assert "declared unit table" in str(caught.value)


def test_units_normalization_stamps_what_it_converted():
    out = transform_series(_series([1.0, 2.0]),
                           TemporalSpec(units=UnitsSpec("mm/day")),
                           quantity=RATE, units="in/day")
    assert "converted in/day->mm/day" in out.note
    assert out.values.tolist() == pytest.approx([25.4, 50.8])


def test_an_unchanged_unit_still_says_it_was_declared():
    out = transform_series(_series([1.0, 2.0]),
                           TemporalSpec(units=UnitsSpec("mm/day")),
                           quantity=RATE, units="mm/day")
    assert "units mm/day (declared, unchanged)" in out.note


# --- the single-value path ------------------------------------------------- #


def test_a_scalar_normalizes_and_reports_its_native_cadence():
    out = transform_value(2.0, TemporalSpec(resample=ResampleSpec(to="1D"),
                                            units=UnitsSpec("mm/day")),
                          quantity=RATE, units="in/day", native="1D")
    assert out.values == pytest.approx(50.8)
    assert out.note == ("native 1D matches the declared 1D rate, no resample; "
                        "converted in/day->mm/day")


def test_a_scalar_refuses_a_resample_to_a_cadence_it_cannot_carry():
    with pytest.raises(TemporalShapeError) as caught:
        transform_value(2.0, _resample("1h"), quantity=RATE, native="1D")
    assert "fabricate the series" in str(caught.value)


def test_a_one_point_series_has_no_cadence_to_resample_from():
    with pytest.raises(TemporalShapeError):
        transform_series([("2026-08-01T00:00", 1.0)], _resample("1h"), quantity=STATE)


def test_pairs_are_accepted_as_well_as_a_pandas_series():
    out = transform_series([("2026-08-01T06:00", 2.0), ("2026-08-01T00:00", 1.0)],
                           _resample("3h"), quantity=STATE)
    # Out of order in, sorted before anything is measured.
    assert out.values.iloc[0] == pytest.approx(1.0)


# --- the DECLARATION surface ----------------------------------------------- #


def test_the_modifiers_ride_the_data_declaration_and_compose():
    decl = Data("rain", Fetch.tool("pkg.mod.fn")
                .resample(to="1D", max_gap="native*3")
                .normalize(units="mm/day"))
    spec = decl.producer.temporal
    assert spec.resample.to == "1D" and spec.resample.max_gap == "native*3"
    assert spec.units.units == "mm/day"


def test_the_modifiers_leave_the_ladder_alone():
    producer = Fetch.tool("pkg.mod.fn").ladder("a", "b").resample(to="1h")
    assert producer.ladder_rungs == ("a", "b")


def test_a_declaration_with_no_modifier_carries_no_spec():
    assert Data("rain", Fetch.tool("pkg.mod.fn")).producer.temporal is None


@pytest.mark.parametrize("kwargs", [
    {"to": "not-an-interval"},
    {"to": "1h", "method": "spline"},
    {"to": "1h", "max_gap": "nativeX3"},
    {"to": "1h", "max_gap": "native*banana"},
])
def test_a_malformed_resample_declaration_refuses_at_declaration_time(kwargs):
    with pytest.raises(PlanValidationError):
        Fetch.tool("pkg.mod.fn").resample(**kwargs)


def test_a_normalize_to_an_unlisted_unit_refuses_at_declaration_time():
    with pytest.raises(TemporalUnitsError):
        Fetch.tool("pkg.mod.fn").normalize(units="smoots")


# --- the interpreter hands the declaration to the producer ------------------ #


async def _run(data):
    _SEEN.clear()
    declared = (Param("x", door=doors.CONSTANT, default=1.0, type=float, desc="a constant"),)
    p = await resolve_params(declared, {})
    plan = Plan("w", None, (Step(runner=f"{_HERE}.stub_step",
                              kwargs={"m": Ref("rain")}),))
    await interpret(plan, p, declared, data, resume=False)
    return _SEEN[0]


@pytest.mark.asyncio
async def test_the_declared_transform_reaches_the_producer_that_owns_the_payload():
    seen = await _run([Data("rain", Fetch.tool(f"{_HERE}.stub_producer")
                            .resample(to="1D").normalize(units="mm/day"))])
    assert seen["temporal"].resample.to == "1D"
    assert seen["temporal"].units.units == "mm/day"


@pytest.mark.asyncio
async def test_a_producer_with_no_declared_transform_is_called_without_the_kwarg():
    seen = await _run([Data("rain", Fetch.tool(f"{_HERE}.stub_producer"))])
    assert "temporal" not in seen
