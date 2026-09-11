# `workflows/publishing/` - a field becomes a layer, a series a chart, a field over time an animation

Publishing is written ONCE, for every engine. What arrives is a READ the engine
already made - node coordinates in lon/lat, values, a time axis, the result file
an animation plays from, and the measures its reader took while it held the
field - with a caption in the reader's own words. What leaves is the layer the
map loads, the chart the dock renders, the animation the seam plays. Nothing
here names an engine, a module, a template or a question: the quantity a
deliverable is held to one scale by is the caption's own words, and the style is
the row the caller declared beside the data.

The first layer a run publishes is the run's primary and the step's own return;
an animation adopts the range of the published layer of the same quantity, so
the frames and the still read on one ramp; a chart is persisted under the run so
the product outlives the turn that emitted it. Failure never retracts: a layer
that cannot be styled returns unstyled with its COG still in the store.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: the read value types, `Published`, and `quantity_of`. |
| `reads.py` | What a read IS - a `Field`, a `Series`, a `Frames`, a bare `Read` of measures - and the `Deliverable` that pairs one with how it is published. |
| `publish.py` | The one publisher: `publish` takes the deliverables in order and returns what the run leads with; a field is rasterized, written, uploaded and styled here, a series becomes the chart spec. |
| `raster.py` | A nodal field on an unstructured mesh onto a regular EPSG:4326 grid: the element fill (the solver's own P1 representation) and the node halo. |
| `cog.py` | Cloud-Optimized-GeoTIFF write, reproject, CRS-guard and upload; every failure a staged `CogIoError`. |
| `style.py` | `publish_product_layer` - the styling seam a typed product layer goes through before it is returned. |
| `manifest.py` | The `outputs.json` writer: one PUT under the run prefix, the exact key the outputs seam reads back. |
| `animation.py` | A field over time as an animation: the results-mesh entry beside the layer of the same quantity, written to the manifest and published through the seam. |
