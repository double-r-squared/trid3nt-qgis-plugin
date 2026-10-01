"""Every declared canary's args against the tool it names: nothing lands nowhere.

A generated template signature absorbs unknown keywords into ``**_extra_ignored``,
so a declaration that names a row the template no longer has is not an error - it
is a canary whose stated value silently became a fetch. The bind is checked here
because the DECLARATION is where that drift appears.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

from dev.testing import accepted_args  # noqa: E402
from dev.testing.canaries import CANARIES  # noqa: E402


@pytest.mark.parametrize("name", sorted(CANARIES))
def test_every_arg_a_canary_states_is_one_its_tool_declares(name):
    declared = CANARIES[name]
    unread = sorted(set(declared.args) - accepted_args(declared.tool))
    assert not unread, (
        f"{name} states {unread}, which {declared.tool} does not declare: the "
        "values are swallowed and whatever row they were meant to fill fetches "
        "instead")


@pytest.mark.parametrize("name", sorted(n for n in CANARIES if CANARIES[n].before))
def test_every_arg_a_call_before_a_canary_states_is_one_its_tool_declares(name):
    for call in CANARIES[name].before:
        unread = sorted(set(call.args) - accepted_args(call.tool))
        assert not unread, (
            f"{name} calls {call.tool} as {call.name!r} with {unread}, which it "
            "does not declare: the values are swallowed")


def _named(args):
    """Every ``"$" + name`` placeholder anywhere in ``args``."""
    from dev.testing.live_run import PLACEHOLDER

    if isinstance(args, str):
        return {args[len(PLACEHOLDER):]} if args.startswith(PLACEHOLDER) else set()
    if isinstance(args, dict):
        args = list(args.values())
    if isinstance(args, (list, tuple)):
        return set().union(*(_named(value) for value in args)) if args else set()
    return set()


@pytest.mark.parametrize("name", sorted(n for n in CANARIES if CANARIES[n].before))
def test_every_layer_a_call_before_makes_is_read_by_a_call_after_it(name):
    """A placeholder names only a call before it, and a call whose layer nothing
    after it reads is a layer the run does not stand on."""
    calls = list(CANARIES[name].before)
    for rank, call in enumerate(calls):
        assert _named(call.args) <= {c.name for c in calls[:rank]}, call.name
        after = [c.args for c in calls[rank + 1:]] + [CANARIES[name].args]
        assert call.name in _named(after), (
            f"{name}: nothing after {call.name!r} reads the layer it makes")


def test_port_huron_composes_its_bed_in_the_order_the_last_passing_run_laid_it():
    """The survey gridded first by QGIS's IDW, the chart, the relief, the
    terrain last, each off its own fetch, the two offsets passed in, then the
    fill."""
    run = CANARIES["telemac_ice_cover_port_huron"]
    calls = {call.name: call for call in run.before}
    assert [call.tool for call in run.before] == [
        "fetch_ehydro_surveys", "run_qgis_algorithm", "run_qgis_algorithm",
        "fetch_chs_nonna", "fetch_etopo", "fetch_dem",
        "fetch_vertical_datum_offset", "fetch_vertical_datum_offset",
        "fetch_nhd_water_surface", "merge_rasters", "fill_nodata"]
    assert calls["projected"].args["params"]["INPUT"] == "$soundings"
    grid = calls["survey"].args
    assert grid["algorithm"] == "gdal:gridinversedistancenearestneighbor"
    assert grid["params"]["INPUT"] == "$projected"
    assert (grid["params"]["Z_FIELD"], grid["params"]["RADIUS"]) == (
        "depth_below_datum_m", 9.14)
    assert "-tr 40 40" in grid["params"]["EXTRA"]
    assert calls["merged"].args["layers"] == [
        "$survey", "$chart", "$relief", "$terrain"]
    assert calls["merged"].args["offsets"] == ["$chart_offset", "$relief_offset"]
    assert calls["bed"].args["layer"] == "$merged"
    assert run.args["bed"] == {"layer": "$bed"}
