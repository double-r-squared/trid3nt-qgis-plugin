# `workflows/telemac/catalog/` - the engine's own dictionaries

One JSON per exposed module, extracted IN-IMAGE from the module's dico by
`scripts/extract_telemac_catalog.py` and committed. Never hand edited: a
transcribed keyword table is a second answer to a question the engine already
answers, and `tests/test_telemac_catalog_drift.py` re-extracts from the image
when one is present and fails on any difference.

A row carries what the dictionary says about one keyword - its raw name, the
identifier the image's own spaces-to-underscores map spells it by, its type and
size, its help de-LaTeXed to plain text, its rubrique, its level, its engine
default, whether SUBMIT marks it a file, and whether APPARENCE marks its list
unbounded. `modules/module.py` reads a file here into the wrapper's slots.

## Files

| file | keywords | what it is |
| --- | --- | --- |
| `telemac2d.json` | 376 | TELEMAC-2D, the shallow-water carrier. |
| `telemac3d.json` | 355 | TELEMAC-3D, the layered carrier. |
| `artemis.json` | 118 | ARTEMIS, the phase-resolving elliptic wave module. |
| `waqtel.json` | 91 | WAQTEL, the water-quality module a carrier couples with. |
| `gaia.json` | 148 | GAIA, the sediment module a carrier couples with. |
| `tomawac.json` | 223 | TOMAWAC, the spectral wave module. Extracted, and NOT wrapped - the reason is stated in the engine's own map. |
