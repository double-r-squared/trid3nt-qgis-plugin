# 0029 - rendering and camera do not depend on the LLM remembering

Context: a renderable raster (a raw `s3://` LayerURI a workflow produced) only
ever rendered if the LLM separately remembered to call `publish_layer`; and the
web derives the Case camera by scanning chat history for a zoom-to on the last
`role="agent"` row. Both broke when a turn ended in tool calls with no trailing
narration (the common flood/publish shape): no publish call, or no closing row to
carry the layer/zoom accumulator.

Decision: rendering and camera-snap are server-side invariants, not LLM
responsibilities.
- Auto-publish: when a tool returns a renderable LayerURI, the server calls
  `publish_layer` deterministically (off the event loop), reusing the same
  `emit_layer_uri -> add_loaded_layer -> persist` machinery the manual wrap-site
  uses. The LLM is never the gate for a layer becoming visible.
- Terminal accumulator row: every turn persists a closing chat row carrying the
  layer handles + zoom-to accumulator, even when the final round produced only
  tool calls (an empty marker row if there is no narration). A Case reopen can
  then always recover layer attribution and snap the camera to the AOI.

Consequence: layer visibility and camera state are reproducible from persisted
Case state regardless of how the turn ended or whether the LLM cooperated.
Related: 0014 (the LLM passes handles, never URIs).
