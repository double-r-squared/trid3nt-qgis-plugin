"""Layer URI emission and pipeline event plumbing.

Everything a computed layer passes through on its way to the map, in one
package:

  * ``layer_uri_emit`` - THE seam. ``publish_for_emission`` publishes a raster
    (ADR 0313: emission is automatic, so nothing asks), then ``emit_layer_uri``
    guards what is deliverable.
  * ``publish`` - the raster publish MECHANISM the seam calls: COG overviews,
    the style-resolver ladder, the data-driven legend, layer registration.
  * ``pipeline_emitter`` - the step cards + the ``loaded_layers`` accumulator
    that emits ``session-state``.
  * ``uri_registry`` - the session-scoped handle -> exact-URI indirection.
  * ``outputs_seam`` - the solver ``outputs.json`` manifest -> published layers.
  * ``quantity_styles`` - the per-quantity style presets solver outputs carry.
"""
