# Template proof renders

One render set per landed workflow template, named after the workflow file
stem (`<stem>.png`, plus `_chart` / `_mesh` / variant suffixes). These are
the AS-SEEN-IN-QGIS proofs: layers composited over the Esri World Imagery
basemap approximating the QGIS canvas, charts rendered exactly as the
plugin chart dock draws them. When a template is corrected, its proofs are
regenerated and OVERWRITTEN in place under the same names.

Debug renders (gradient relief spot-checks and similar) do NOT live here -
they are on-demand-only, generated when NATE asks, and go to a tmp folder
(/tmp/trid3nt_debug_renders/).

Audit folder: NEVER cleaned or pruned without NATE's explicit say-so.
