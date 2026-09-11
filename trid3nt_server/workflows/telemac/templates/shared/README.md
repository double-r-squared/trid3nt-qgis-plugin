# `templates/shared/` - the rows and steps every river template declares

Shared things are engine-agnostic by construction only: the rows a reach run
and a point release declare, and the two steps that establish the modelled
world and measure it. A template restates its own keywords, chain and recipe;
nothing here is a body a template lists or a parent it extends.

## Files

| file | what it is |
| --- | --- |
| `river.py` | `PARAMS` and `RELEASE` - the rows every river run and every point release declare - and `acquire` and `settle`, the steps that geocode the reach, seed and navigate its one centerline, resolve the carrier discharge, and measure the accepted mesh into what the sheet is filled from. |
| `__init__.py` | The package door. It re-exports nothing: a consumer imports what it means. |
