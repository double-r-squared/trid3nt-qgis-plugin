"""TELEMAC-2D rain-on-grid (rainfall-runoff) template package.

``rain_on_grid`` is the recipe, ``declarations`` its contract, and
``cn_infiltration`` the SCS curve-number surface only this question needs - the
rainfall-excess transform, the steep-slope correction, the antecedent-moisture
conversions and the land-cover CN/Manning table. The catchment MESHING lives in
the shared mesh front (``workflows/mesh/watershed.py``) and the TELEMAC mechanism
in the family steps (``workflows/telemac/steps/rain_on_grid.py``), because neither
is a fact about this question.
"""
