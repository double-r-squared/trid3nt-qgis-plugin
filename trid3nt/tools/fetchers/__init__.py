"""Data fetchers, one package per source, grouped by the phenomenon measured.

Shared cross-domain helpers live at this level; everything else is under a
domain subpackage."""

# A declaration cannot say what a named hook beside it says. A hand-rolled reader is a SECOND
# fetcher with its own retry, error vocabulary and provenance, out of reach of the guards that make a fetch honest.
