# `workflows/telemac/products/` - what a solved run is answered with

A solve leaves files. What turns those files into the answer - the rasterized
map layers, the scalars a narration quotes, the chart spec - lives here, and
only here: the worker is the engine room and derives nothing.

Everything past the primary layer is best-effort by contract. A missing
deposition COG, an unparsed slick or an unpublished results mesh retracts
nothing.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `postprocess_telemac.py` | A solved result's fields to the map products: the peak field, the rasterized grids, the animated frames. |
| `products.py` | A solved reach to its map layers, its scalars and its chart spec, each substance class leading with its own product. |
| `run_reads.py` | What a solved run's own files say, read on the server: GAIA's closure out of the listing, the slick out of the drogues track. |
