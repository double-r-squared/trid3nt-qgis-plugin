"""TELEMAC-2D rain-on-grid (rainfall-runoff) template package.

Homes the rain-on-grid substrate the registered ``telemac_rain_on_grid``
template is built from:

  * ``cn_infiltration`` -- SCS curve-number infiltration: the native
    per-node CN2 field, the steep-slope correction, the AMC conversions, the
    rainfall-excess preprocessing transform, AND the automatic native-vs-
    preprocessing runoff-path selector (``select_runoff_path``).
  * ``mesh_acquisition`` -- the watershed-first meshing STEP promoted
    from the sandbox: delineate the catchment at a pour point, mesh its
    interior refined by distance-to-river, project + write the solve SELAFIN.
    Precondition-gate shape (``acquire_watershed_mesh`` builds our own;
    ``use_supplied_mesh`` adopts a user mesh) so a user-supplied mesh slots in
    behind the same interface.

The registered ``telemac_rain_on_grid`` template body (worker RoG deck + parser
bump + image rebuild + live Coweeta proof) lands on top of this offline-tested
substrate; see for the build plan.
"""
